import csv
import json
import os
import unittest
from contextlib import ExitStack, nullcontext, redirect_stdout
from datetime import date
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import requests
from databricks.sql.types import Row

from p1_preparation.prepare import MigrationTables
from p2_migration.migrate import MigrationError, migrate_records
from p2_migration.reconcile import (
    ReconciliationError,
    read_recorded_run,
    reconcile_migration,
)
from p2_migration.resume import resume_migration
from p2_migration.salesforce import RecordUnavailableError
from tests.test_salesforce import response_with

TABLES = MigrationTables(*(f"test.migration.{entity}_snapshot_example" for entity in ("customers", "products", "licences")))


def sample_rows(entity):
    """Give the committed CSV examples the same Python types as Databricks SQL."""
    path = Path(__file__).resolve().parents[1] / "p0_data" / "seed" / f"{entity}_v1.csv"
    rows = []
    with path.open(encoding="utf-8") as file:
        for source in csv.DictReader(file):
            record: dict[str, object] = dict(source)
            if entity == "products":
                record["is_active"] = source["is_active"] == "true"
            if entity == "licences":
                record["seats"] = int(source["seats"])
                for field in ("starts_on", "ends_before", "cancelled_on"):
                    record[field] = date.fromisoformat(source[field]) if source[field] else None
            rows.append(Row(**record))
    return rows


class LicenceMigrationTests(unittest.TestCase):
    def setUp(self):
        """Keep the real load, report and recovery logic; simulate only the external services."""
        resources = ExitStack()
        self.addCleanup(resources.close)
        self.directory = Path(resources.enter_context(TemporaryDirectory()))
        resources.enter_context(patch("p2_migration.reporting.REPORT_DIRECTORY", self.directory))
        self.output = StringIO()
        resources.enter_context(redirect_stdout(self.output))
        resources.enter_context(patch.dict(os.environ, {
            "PYTHON_DOTENV_DISABLED": "1", "DATABRICKS_SERVER_HOSTNAME": "example.databricks.com",
            "SALESFORCE_MY_DOMAIN_URL": "https://example.my.salesforce.com",
        }, clear=True))
        self.rows = {entity: sample_rows(entity) for entity in ("customers", "products", "licences")}
        self.saved = {entity: {} for entity in ("Account", "Product2", "Licence__c")}
        self.lose_response_for = None
        self.changed_field = None
        self.session = Mock()
        self.session.patch.side_effect = self.save_record
        self.session.get.side_effect = self.read_record
        resources.enter_context(patch("p2_migration.licences.requests.Session", side_effect=lambda: nullcontext(self.session)))
        resources.enter_context(patch("p2_migration.retries.sleep"))
        for module, entity, singular in (("migrate", "customers", "customer"), ("products", "products", "product"), ("licences", "licences", "licence")):
            resources.enter_context(patch(f"p2_migration.{module}.connect_to_databricks", side_effect=lambda: nullcontext(object())))
            resources.enter_context(patch(f"p2_migration.{module}.connect_to_salesforce", return_value="https://example.my.salesforce.com"))
            resources.enter_context(patch(
                f"p2_migration.{module}.count_{entity}",
                side_effect=lambda *_, entity=entity, singular=singular: (len(self.rows[entity]), len({getattr(row, f"{singular}_id") for row in self.rows[entity]})),
            ))
            resources.enter_context(patch(
                f"p2_migration.{module}.fetch_{singular}_batches",
                side_effect=lambda connection, table, batch_size, entity=entity: self.batches(entity, table, batch_size),
            ))
        resources.enter_context(patch("p2_migration.licences.fetch_licence", side_effect=self.fetch_licence))

    def batches(self, entity, table, batch_size):
        self.assertEqual(table, getattr(TABLES, f"{entity}_table"))
        rows = self.rows[entity]
        return iter([rows[start:start + batch_size] for start in range(0, len(rows), batch_size)])

    def fetch_licence(self, connection, table, licence_id):
        self.assertEqual(table, TABLES.licences_table)
        matches = [row for row in self.rows["licences"] if row.licence_id == licence_id]
        if len(matches) != 1:
            raise ValueError("Snapshot licence missing or ambiguous")
        return matches[0].asDict()

    def report_path(self):
        return max(self.directory.glob("*.jsonl"), key=lambda path: path.stat().st_mtime_ns)

    def events(self):
        return [json.loads(line) for line in self.report_path().read_text().splitlines()]

    def save_record(self, url, *, json, **kwargs):
        entity, source_field, source_id = url.rsplit("/", 3)[-3:]
        existing = self.saved[entity].get(source_id)
        record_id = existing["Id"] if existing else f"sf-{entity}-{len(self.saved[entity]) + 1}"
        if entity == "Licence__c":
            self.assertEqual(self.events()[-1]["event"], "licence_load_started")
            self.assertEqual(self.events()[-1]["licence_id"], source_id)
            self.assertNotIn("Source_Licence_ID__c", json)
        self.saved[entity][source_id] = {**json, source_field: source_id, "Id": record_id}
        if entity == "Licence__c":
            self.saved[entity][source_id]["Name"] = f"LIC-{len(self.saved[entity]):06}"
        if source_id == self.lose_response_for:
            raise requests.ReadTimeout("private request details")
        return response_with(200 if existing else 201)

    def read_record(self, url, **kwargs):
        entity, _, source_id = url.rsplit("/", 3)[-3:]
        record = self.saved[entity].get(source_id)
        if entity == "Licence__c" and record is not None and self.changed_field:
            record = {**record, self.changed_field: "unexpected-value"}
        return response_with(200 if record else 404, record)

    # The committed examples load in parent-first order, preserve all terms, then update the same records.
    def test_full_load_and_repeat_preserve_relationships_dates_and_record_identity(self):
        # Arrange and Act
        migrate_records(TABLES, load=True)
        first_ids = {key: record["Id"] for key, record in self.saved["Licence__c"].items()}
        first_summary = self.events()[-1]
        write_order = [call.args[0].rsplit("/", 3)[-3] for call in self.session.patch.call_args_list]
        migrate_records(TABLES, load=True)

        # Assert
        self.assertEqual(write_order, ["Account"] * 20 + ["Product2"] * 3 + ["Licence__c"] * 12)
        self.assertEqual(first_summary["licences_created"], 12)
        self.assertEqual(self.events()[-1]["licences_created"], 0)
        self.assertEqual(self.events()[-1]["licences_updated"], 12)
        self.assertEqual(self.events()[-1]["licences_verified"], 12)
        self.assertEqual(self.events()[-1]["status"], "complete")
        self.assertEqual({key: record["Id"] for key, record in self.saved["Licence__c"].items()}, first_ids)
        for row in self.rows["licences"]:
            received = self.saved["Licence__c"][row.licence_id]
            self.assertEqual(received["Customer__c"], self.saved["Account"][row.customer_id]["Id"])
            self.assertEqual(received["Product__c"], self.saved["Product2"][row.product_id]["Id"])
            self.assertEqual(received["Seats__c"], row.seats)
            self.assertEqual(received["Starts_On__c"], row.starts_on.isoformat())
            self.assertEqual(received["Ends_Before__c"], row.ends_before.isoformat())
            self.assertEqual(received["Cancelled_On__c"], row.cancelled_on.isoformat() if row.cancelled_on else None)

    # An absent or invalid parent response must never become a blank or guessed relationship.
    def test_unresolved_parents_prevent_licence_writes(self):
        cases = (
            ("read_account", RecordUnavailableError("missing")),
            ("read_product", RecordUnavailableError("missing")),
            ("read_product", {"Source_Product_ID__c": "wrong-source", "Id": "sf-product"}),
            ("read_account", {"Source_Customer_ID__c": self.rows["licences"][0].customer_id}),
        )
        for reader, result in cases:
            with self.subTest(reader=reader, result=result):
                # Arrange
                replacement = Mock(side_effect=result) if isinstance(result, Exception) else Mock(return_value=result)

                # Act
                with patch(f"p2_migration.licences.{reader}", replacement), self.assertRaises(MigrationError):
                    migrate_records(TABLES, load=True)

                # Assert
                self.assertEqual(self.saved["Licence__c"], {})
                self.assertEqual(self.events()[-1]["status"], "failed")
                self.assertNotIn("licence_load_started", [event["event"] for event in self.events()])

    # Rejected parents stop the licence stage; rejected licences allow other valid terms but not success.
    def test_rejections_keep_the_run_incomplete(self):
        for entity, field, value in (("customers", "billing_postal_code", 110), ("products", "is_active", "false"), ("licences", "seats", 0)):
            with self.subTest(entity=entity):
                # Arrange
                original = self.rows[entity][0]
                self.rows[entity][0] = Row(**{**original.asDict(), field: value})
                self.saved["Licence__c"].clear()

                # Act
                with self.assertRaisesRegex(MigrationError, "Migration incomplete"):
                    migrate_records(TABLES, load=True)

                # Assert
                self.assertEqual(self.events()[-1]["status"], "incomplete")
                self.assertEqual(len(self.saved["Licence__c"]), 11 if entity == "licences" else 0)
                self.rows[entity][0] = original

    # Any changed mapped field, including either parent link, prevents verified or complete outcomes.
    def test_readback_checks_all_seven_licence_fields(self):
        for field in ("Source_Licence_ID__c", "Customer__c", "Product__c", "Seats__c", "Starts_On__c", "Ends_Before__c", "Cancelled_On__c"):
            with self.subTest(field=field):
                # Arrange
                self.changed_field = field

                # Act
                with self.assertRaisesRegex(MigrationError, field):
                    migrate_records(TABLES, load=True)

                # Assert
                self.assertEqual(self.events()[-1]["licences_verified"], 0)
                self.assertEqual(self.events()[-1]["status"], "failed")
                self.assertEqual(read_recorded_run(self.report_path()).uncertain_licence_id, self.rows["licences"][0].licence_id)

    # A save followed by a lost response is checked without writing, then resumed without replaying parents or that licence.
    def test_reconciliation_and_repeated_resume_preserve_progress(self):
        # Arrange
        self.lose_response_for = self.rows["licences"][1].licence_id
        with self.assertRaises(MigrationError):
            migrate_records(TABLES, load=True)
        report_path = self.report_path()
        original = report_path.read_bytes()
        self.session.reset_mock()

        # Act
        reconcile_migration(report_path)
        after_check = report_path.read_bytes()
        self.lose_response_for = self.rows["licences"][2].licence_id
        with self.assertRaises(MigrationError):
            resume_migration(report_path)
        resume_migration(report_path)

        # Assert
        self.assertEqual(after_check, original)
        self.assertTrue(report_path.read_bytes().startswith(original))
        writes = [call.args[0].rsplit("/", 1)[-1] for call in self.session.patch.call_args_list]
        self.assertEqual(writes, [row.licence_id for row in self.rows["licences"][2:]])
        self.assertEqual(self.events()[-1]["licences_previously_verified"], 2)
        self.assertEqual(self.events()[-1]["licences_reconciled"], 1)
        self.assertEqual(self.events()[-1]["licences_verified"], 12)
        self.assertEqual(self.events()[-1]["status"], "complete")
        self.assertNotIn("private request details", report_path.read_text())
        self.session.reset_mock()
        resume_migration(report_path)
        self.session.patch.assert_not_called()

    # An unresolved licence or relationship blocks recovery before any further parent or licence writes.
    def test_unresolved_licence_blocks_all_recovery_writes(self):
        # Arrange
        self.lose_response_for = self.rows["licences"][0].licence_id
        with self.assertRaises(MigrationError):
            migrate_records(TABLES, load=True)
        for problem in ("Customer__c", "Product__c", "missing"):
            with self.subTest(problem=problem):
                self.changed_field = problem if problem != "missing" else None
                if problem == "missing":
                    self.saved["Licence__c"].clear()
                self.session.reset_mock()

                # Act and Assert
                with self.assertRaises(ReconciliationError):
                    reconcile_migration(self.report_path())
                with self.assertRaises(MigrationError):
                    resume_migration(self.report_path())
                self.session.patch.assert_not_called()
                self.assertEqual(self.events()[-1]["status"], "failed")

    # Repeated source IDs or missing previously verified IDs prevent further licence writes.
    def test_snapshot_identity_checks_block_unsafe_continuation(self):
        # Arrange: a duplicate ID makes the first load ambiguous.
        original = self.rows["licences"].copy()
        self.rows["licences"].append(original[0])

        # Act and Assert
        with self.assertRaisesRegex(MigrationError, "present and unique"):
            migrate_records(TABLES, load=True)
        self.assertEqual(self.saved["Licence__c"], {})

        # Arrange: a later interrupted run loses a previously verified row from its snapshot.
        self.rows["licences"] = original.copy()
        self.lose_response_for = original[1].licence_id
        with self.assertRaises(MigrationError):
            migrate_records(TABLES, load=True)
        self.rows["licences"].pop(0)
        self.session.reset_mock()

        # Act and Assert
        with self.assertRaisesRegex(MigrationError, "Previously verified licences are missing"):
            resume_migration(self.report_path())
        self.session.patch.assert_not_called()
        self.assertEqual(self.events()[-1]["status"], "failed")

    # The licence save must not happen if its write intent cannot be synced to the report.
    def test_report_storage_failure_prevents_the_licence_write(self):
        # Arrange
        def sync_report(file_descriptor):
            if self.events()[-1]["event"] == "licence_load_started":
                raise OSError("storage unavailable")

        # Act
        with patch("p2_migration.reporting.fsync", side_effect=sync_report), self.assertRaises(MigrationError):
            migrate_records(TABLES, load=True)

        # Assert
        self.assertEqual(self.saved["Licence__c"], {})
        self.assertEqual(self.events()[-1]["status"], "failed")
        self.assertEqual(read_recorded_run(self.report_path()).uncertain_licence_id, self.rows["licences"][0].licence_id)

    # Naming a licence snapshot in an older report does not authorise a new licence stage on resume.
    def test_old_product_report_keeps_its_original_scope(self):
        # Arrange
        path = self.directory / "old.jsonl"
        path.write_text(json.dumps({
            "event": "run_started", "customers_table": TABLES.customers_table,
            "products_table": TABLES.products_table, "licences_table": TABLES.licences_table, "load_products": True,
            "databricks_server_hostname": "example.databricks.com",
            "salesforce_my_domain_url": "https://example.my.salesforce.com",
        }) + "\n")

        # Act
        resume_migration(path)

        # Assert
        self.assertEqual(len(self.saved["Account"]), 20)
        self.assertEqual(len(self.saved["Product2"]), 3)
        self.assertEqual(self.saved["Licence__c"], {})
        self.assertEqual(self.events()[-1]["status"], "complete")

    # Invalid scope and mixed-entity histories must not erase or invent licence progress.
    def test_inconsistent_licence_history_is_rejected(self):
        # Arrange
        start = {"event": "run_started", "customers_table": TABLES.customers_table,
                 "products_table": TABLES.products_table, "licences_table": TABLES.licences_table,
                 "load_products": True, "load_licences": True}
        attempt = {"event": "licence_load_started", "licence_id": "licence-1"}
        verified = {"event": "licence_verified", "licence_id": "licence-1"}
        histories = [
            [{**start, "load_licences": "yes"}], [{**start, "load_products": False}],
            [{**start, "licences_table": "test.raw.licences"}],
            [{**start, "load_licences": False}, attempt], [start, attempt, verified, attempt],
            [start, {"event": "product_load_started", "product_id": "product-1"}, attempt],
            [start, attempt, {"event": "account_load_started", "customer_id": "customer-1"}],
            [start, attempt, {"event": "run_finished", "status": "complete"}],
        ]
        path = self.directory / "invalid.jsonl"
        for history in histories:
            with self.subTest(history=history):
                path.write_text("".join(json.dumps(event) + "\n" for event in history))

                # Act and Assert
                with self.assertRaises(ValueError):
                    read_recorded_run(path)
