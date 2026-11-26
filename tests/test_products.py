import json
import os
import unittest
from contextlib import ExitStack, nullcontext, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import requests
from databricks.sql.types import Row

from p1_preparation.prepare import MigrationTables
from p2_migration.databricks import count_products, fetch_product, fetch_product_batches
from p2_migration.mapping import prepare_salesforce_product
from p2_migration.migrate import MigrationError, migrate_records
from p2_migration.reconcile import (
    ReconciliationError,
    read_recorded_run,
    reconcile_migration,
)
from p2_migration.reporting import MigrationReport
from p2_migration.resume import resume_migration
from p2_migration.validation import validate_product
from tests.fake_database import FakeDatabase
from tests.test_resume import customer
from tests.test_salesforce import response_with

PRODUCTS_TABLE = "test.migration.products_snapshot_example"
TABLES = MigrationTables("test.migration.customers_snapshot_example", PRODUCTS_TABLE)


def product(product_id="p1", active=True):
    return Row(product_id=product_id, product_code=product_id.upper(), product_name=f"Product {product_id}", is_active=active)


class ProductDataTests(unittest.TestCase):
    # Inactive products remain valid and map to false, rather than disappearing from the load.
    def test_maps_active_and_inactive_products_without_changing_source(self):
        for active in (True, False):
            with self.subTest(active=active):
                # Arrange
                source = product(active=active).asDict()
                original = source.copy()

                # Act
                errors = validate_product(source)
                fields = prepare_salesforce_product(source)

                # Assert
                self.assertEqual(errors, [])
                self.assertEqual(fields, {
                    "Source_Product_ID__c": "p1", "ProductCode": "P1", "Name": "Product p1", "IsActive": active,
                })
                self.assertEqual(source, original)

    # Source types and field lengths must fit Salesforce without silent coercion or truncation.
    def test_rejects_invalid_text_and_non_boolean_availability(self):
        cases = [("is_active", value) for value in (None, 0, 1, "false", "true")]
        for field, limit in (("product_id", 36), ("product_code", 255), ("product_name", 255)):
            cases.extend((field, value) for value in (None, "", " ", 123, "x" * (limit + 1)))
        for field, value in cases:
            with self.subTest(field=field, value=value):
                # Arrange
                source = {**product().asDict(), field: value}

                # Act
                errors = validate_product(source)

                # Assert
                self.assertEqual(len(errors), 1)
                self.assertTrue(errors[0].startswith(field))

    # All product reads bind identifiers; recovery requires one match and batching stays bounded.
    def test_reads_the_selected_snapshot_in_batches_and_by_id(self):
        # Arrange
        rows = [product("p1"), product("p2"), product("p3", False)]
        database = FakeDatabase(rows)
        batches = fetch_product_batches(database, PRODUCTS_TABLE, batch_size=2)

        # Act and Assert
        self.assertEqual(database.fetch_count, 0)
        self.assertEqual(next(batches), rows[:2])
        self.assertEqual(database.fetch_count, 1)
        self.assertEqual(list(batches), [rows[2:]])
        self.assertEqual(database.parameters, {"products_table_to_read": PRODUCTS_TABLE})
        for matches in ([], rows[:1], rows[:2]):
            with self.subTest(matches=len(matches)):
                database = FakeDatabase(matches)
                if len(matches) == 1:
                    self.assertEqual(fetch_product(database, PRODUCTS_TABLE, "p1"), rows[0].asDict())
                else:
                    with self.assertRaisesRegex(ValueError, "exactly one"):
                        fetch_product(database, PRODUCTS_TABLE, "p1")
                self.assertEqual(database.parameters, {"products_table_to_read": PRODUCTS_TABLE, "product_id": "p1"})
        cursor = Mock()
        cursor.fetchone.return_value = (3, 2)
        connection = Mock()
        connection.cursor.return_value = nullcontext(cursor)
        self.assertEqual(count_products(connection, PRODUCTS_TABLE), (3, 2))
        self.assertEqual(cursor.execute.call_args.kwargs["parameters"], {"products_table_to_read": PRODUCTS_TABLE})
        for batch_size in (0, -1):
            with self.subTest(batch_size=batch_size), self.assertRaises(ValueError):
                list(fetch_product_batches(connection, PRODUCTS_TABLE, batch_size))


class ProductMigrationTests(unittest.TestCase):
    def setUp(self):
        """Run the real workflow and HTTP adapter against simulated service boundaries."""
        resources = ExitStack()
        self.addCleanup(resources.close)
        self.directory = Path(resources.enter_context(TemporaryDirectory()))
        resources.enter_context(patch("p2_migration.reporting.REPORT_DIRECTORY", self.directory))
        resources.enter_context(redirect_stdout(StringIO()))
        resources.enter_context(patch.dict(os.environ, {
            "PYTHON_DOTENV_DISABLED": "1",
            "DATABRICKS_SERVER_HOSTNAME": "example.databricks.com",
            "SALESFORCE_MY_DOMAIN_URL": "https://example.my.salesforce.com",
        }, clear=True))
        self.rows = [product("p1"), product("p2"), product("p3", False)]
        self.saved_products = {}
        self.lose_response_for = None
        self.change_readback = False
        self.session = Mock()
        self.session.patch.side_effect = self.save_product
        self.session.get.side_effect = self.read_product
        resources.enter_context(patch("p2_migration.products.requests.Session", return_value=nullcontext(self.session)))
        for module in ("migrate", "products"):
            resources.enter_context(patch(f"p2_migration.{module}.connect_to_databricks", side_effect=lambda: nullcontext(object())))
            resources.enter_context(patch(f"p2_migration.{module}.connect_to_salesforce", return_value="https://example.my.salesforce.com"))
        resources.enter_context(patch("p2_migration.retries.sleep"))
        resources.enter_context(patch("p2_migration.migrate.count_customers", return_value=(1, 1)))
        self.customer_batches = resources.enter_context(patch("p2_migration.migrate.fetch_customer_batches", return_value=[[customer("c1")]]))
        self.save_account = resources.enter_context(patch(
            "p2_migration.migrate.save_and_verify_account",
            return_value=("updated", {"Id": "sf-c1"}),
        ))
        self.count_products = resources.enter_context(patch(
            "p2_migration.products.count_products",
            side_effect=lambda *_: (len(self.rows), len({row.product_id for row in self.rows})),
        ))
        resources.enter_context(patch("p2_migration.products.fetch_product_batches", side_effect=self.batches))
        resources.enter_context(patch("p2_migration.products.fetch_product", side_effect=self.fetch_one))

    def batches(self, connection, table, batch_size):
        self.assertEqual(table, PRODUCTS_TABLE)
        return iter([self.rows[start:start + batch_size] for start in range(0, len(self.rows), batch_size)])

    def fetch_one(self, connection, table, product_id):
        self.assertEqual(table, PRODUCTS_TABLE)
        matches = [row for row in self.rows if row.product_id == product_id]
        if len(matches) != 1:
            raise ValueError("Snapshot product missing or ambiguous")
        return matches[0].asDict()

    def report_path(self):
        return next(self.directory.glob("*.jsonl"))

    def events(self):
        return [json.loads(line) for line in self.report_path().read_text().splitlines()]

    def save_product(self, url, *, json, **kwargs):
        product_id = url.rsplit("/", 1)[-1]
        # The real disk report must contain the intent before every outgoing write.
        self.assertEqual(self.events()[-1]["event"], "product_load_started")
        self.assertEqual(self.events()[-1]["product_id"], product_id)
        self.assertTrue(url.endswith(f"/Product2/Source_Product_ID__c/{product_id}"))
        self.assertNotIn("Source_Product_ID__c", json)
        existed = product_id in self.saved_products
        self.saved_products[product_id] = {**json, "Id": f"sf-{product_id}", "Source_Product_ID__c": product_id}
        if product_id == self.lose_response_for:
            raise requests.ReadTimeout("private response details")
        return response_with(200 if existed else 201)

    def read_product(self, url, **kwargs):
        product_id = url.rsplit("/", 1)[-1]
        fields = self.saved_products.get(product_id)
        if self.change_readback and fields:
            fields = {**fields, "Name": "Changed by another process"}
        return response_with(200 if fields else 404, fields)

    # Creates and updates preserve the external ID and verify every field, including false.
    def test_loads_products_after_customers_and_distinguishes_create_from_update(self):
        # Arrange
        self.saved_products["p1"] = {**prepare_salesforce_product(self.rows[0].asDict()), "Id": "sf-p1"}

        # Act
        migrate_records(TABLES, load=True)

        # Assert
        self.customer_batches.assert_called_once()
        self.assertEqual(self.saved_products["p3"]["IsActive"], False)
        self.assertEqual([call[0] for call in self.session.method_calls], ["patch", "get"] * 3)
        events = self.events()
        self.assertEqual(events[2]["event"], "account_verified")
        self.assertEqual(events[3]["event"], "product_load_started")
        self.assertEqual(events[-1]["accounts_verified"], 1)
        self.assertTrue(events[0]["load_products"])
        self.assertEqual(events[-1]["status"], "complete")
        self.assertEqual(events[-1]["products_created"], 2)
        self.assertEqual(events[-1]["products_updated"], 1)
        self.assertEqual(events[-1]["products_verified"], 3)
        self.assertEqual(read_recorded_run(self.report_path()).verified_product_ids, frozenset(("p1", "p2", "p3")))

    # Rejected products keep the full run incomplete, while valid products can still load.
    def test_rejected_product_is_reported_without_being_sent(self):
        # Arrange
        self.rows[1] = Row(**{**self.rows[1].asDict(), "is_active": "false"})

        # Act
        with self.assertRaisesRegex(MigrationError, "Migration incomplete"):
            migrate_records(TABLES, load=True)

        # Assert
        self.assertEqual(set(self.saved_products), {"p1", "p3"})
        self.assertEqual(self.events()[-1]["products_rejected"], 1)
        self.assertEqual(self.events()[-1]["status"], "incomplete")

    # Ambiguous IDs and unwritable progress both block the first product save.
    def test_product_preconditions_prevent_writes(self):
        for failure in ("duplicate_ids", "report_storage"):
            with self.subTest(failure=failure):
                # Arrange
                self.session.reset_mock()
                original = MigrationReport.record_product_load_started
                if failure == "duplicate_ids":
                    self.rows = [product(), product()]
                else:
                    self.rows = [product()]

                def record_intent(report, product_id):
                    if failure == "report_storage":
                        raise OSError("disk unavailable")
                    original(report, product_id)

                # Act
                with patch.object(MigrationReport, "record_product_load_started", record_intent), self.assertRaises(MigrationError):
                    migrate_records(TABLES, load=True)

                # Assert
                self.session.patch.assert_not_called()

    # Repeated lost responses are reconciled from the same snapshot without replaying either save.
    def test_interrupted_product_load_reconciles_and_resumes_the_saved_snapshot(self):
        # Arrange
        self.lose_response_for = "p2"
        with self.assertRaises(MigrationError):
            migrate_records(TABLES, load=True)
        report_path = self.report_path()
        self.assertEqual(self.events()[-1]["status"], "failed")
        self.assertEqual(self.events()[-1]["unverified_product_id"], "p2")
        original = report_path.read_bytes()
        self.session.reset_mock()

        # Act
        reconcile_migration(report_path)
        after_reconciliation = report_path.read_bytes()
        self.lose_response_for = "p3"
        with self.assertRaises(MigrationError):
            resume_migration(report_path)
        self.assertEqual(read_recorded_run(report_path).uncertain_product_id, "p3")
        resume_migration(report_path)

        # Assert
        self.assertEqual(after_reconciliation, original)
        self.assertTrue(report_path.read_bytes().startswith(original))
        self.assertEqual([call.args[0].rsplit("/", 1)[-1] for call in self.session.patch.call_args_list], ["p3"])
        self.assertEqual(self.events()[-1]["products_previously_verified"], 2)
        self.assertEqual(self.events()[-1]["products_reconciled"], 1)
        self.save_account.assert_called_once()
        self.assertEqual(self.events()[-1]["products_verified"], 3)
        self.assertEqual(self.events()[-1]["status"], "complete")
        resume_migration(report_path)
        self.session.patch.assert_called_once()

    # A mismatch or unavailable uncertain product stops recovery before even revisiting customers.
    def test_unresolved_product_blocks_all_further_writes(self):
        # Arrange
        self.lose_response_for = "p1"
        with self.assertRaises(MigrationError):
            migrate_records(TABLES, load=True)
        for problem in ("mismatch", "missing"):
            with self.subTest(problem=problem):
                self.change_readback = problem == "mismatch"
                if problem == "missing":
                    self.saved_products.clear()
                self.session.reset_mock()
                self.customer_batches.reset_mock()

                # Act and Assert
                with self.assertRaises(ReconciliationError):
                    reconcile_migration(self.report_path())
                with self.assertRaises(MigrationError):
                    resume_migration(self.report_path())
                self.session.patch.assert_not_called()
                self.customer_batches.assert_not_called()
                self.assertEqual(self.events()[-1]["status"], "failed")

    # A successful HTTP save cannot complete the run if a mapped value changed before readback.
    def test_product_readback_mismatch_leaves_an_uncertain_attempt(self):
        # Arrange
        self.change_readback = True

        # Act
        with self.assertRaisesRegex(MigrationError, "was saved.*Name"):
            migrate_records(TABLES, load=True)

        # Assert
        self.session.patch.assert_called_once()
        self.assertEqual(self.events()[-1]["products_verified"], 0)
        self.assertEqual(read_recorded_run(self.report_path()).uncertain_product_id, "p1")

    # Old reports may name a product snapshot, but resuming must preserve their customer-only scope.
    def test_old_report_does_not_silently_start_loading_products(self):
        # Arrange
        path = self.directory / "legacy.jsonl"
        path.write_text(json.dumps({
            "event": "run_started", "customers_table": TABLES.customers_table,
            "products_table": PRODUCTS_TABLE,
            "databricks_server_hostname": "example.databricks.com",
            "salesforce_my_domain_url": "https://example.my.salesforce.com",
        }) + "\n")

        # Act
        resume_migration(path)

        # Assert
        self.count_products.assert_not_called()
        self.session.patch.assert_not_called()
        self.assertEqual(self.events()[-1]["status"], "complete")

    # Mixed histories must not hide overlapping attempts, duplicate verifications or invalid scope.
    def test_rejects_inconsistent_product_history(self):
        # Arrange
        start = {"event": "run_started", "customers_table": TABLES.customers_table,
                 "products_table": PRODUCTS_TABLE, "load_products": True}
        attempt = {"event": "product_load_started", "product_id": "p1"}
        verified = {"event": "product_verified", "product_id": "p1"}
        histories = [
            [{**start, "load_products": "yes"}],
            [{**start, "products_table": "test.raw.products"}],
            [{**start, "load_products": False}, attempt],
            [start, attempt, {"event": "account_load_started", "customer_id": "c1"}],
            [start, {"event": "account_load_started", "customer_id": "c1"}, attempt],
            [start, attempt, verified, attempt],
            [start, attempt, {"event": "product_verified", "product_id": "p2"}],
            [start, attempt, {"event": "run_finished", "status": "complete"}],
            [start, attempt, {"event": "run_finished", "unverified_customer_id": "p1"}],
        ]
        path = self.directory / "invalid.jsonl"
        for history in histories:
            with self.subTest(history=history):
                path.write_text("".join(json.dumps(event) + "\n" for event in history))

                # Act and Assert
                with self.assertRaises(ValueError):
                    read_recorded_run(path)
