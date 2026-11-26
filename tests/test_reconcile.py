import json
import os
import unittest
from contextlib import ExitStack, nullcontext, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import requests
from databricks.sql.exc import OperationalError
from databricks.sql.types import Row

from main import main
from p2_migration.reconcile import ReconciliationError, read_recorded_run
from p2_migration.reporting import open_run_report
from tests.fake_database import FakeDatabase

SNAPSHOT = "test.migration.customers_snapshot_example"
START = {
    "event": "run_started", "customers_table": SNAPSHOT,
    "databricks_server_hostname": "example.databricks.com",
    "salesforce_my_domain_url": "https://example.my.salesforce.com",
}
ATTEMPT = {"event": "account_load_started", "customer_id": "customer-1"}
VERIFIED = {"event": "account_verified", "customer_id": "customer-1"}


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        resources = ExitStack()
        self.addCleanup(resources.close)
        self.directory = Path(resources.enter_context(TemporaryDirectory()))
        self.report_path = self.directory / "run.jsonl"
        self.output = StringIO()
        resources.enter_context(redirect_stdout(self.output))
        resources.enter_context(patch("main.load_dotenv"))
        resources.enter_context(patch.dict(os.environ, {
            "PYTHON_DOTENV_DISABLED": "1",
            "DATABRICKS_SERVER_HOSTNAME": "example.databricks.com",
            "DATABRICKS_CUSTOMERS_TABLE": "test.raw.changed_source",
            "SALESFORCE_MY_DOMAIN_URL": "https://example.my.salesforce.com",
        }, clear=True))
        self.prepare = resources.enter_context(patch("main.prepare_migration"))
        self.migrate = resources.enter_context(patch("main.migrate_records"))
        self.customer = Row(
            customer_id="customer-1", legal_name="Example Company", billing_city="Whangārei",
            billing_postal_code="0110", billing_country_code="NZ",
        )
        self.database = FakeDatabase([self.customer])
        self.connect_database = resources.enter_context(patch(
            "p2_migration.reconcile.connect_to_databricks", return_value=nullcontext(self.database),
        ))
        self.session = MagicMock(spec=requests.Session)
        self.session.__enter__.return_value = self.session
        resources.enter_context(patch("p2_migration.reconcile.requests.Session", return_value=self.session))
        self.authenticate = resources.enter_context(patch(
            "p2_migration.reconcile.connect_to_salesforce", return_value="https://example.my.salesforce.com",
        ))
        self.session.get.return_value.status_code = 200
        self.session.get.return_value.json.return_value = {
            "Id": "account-1", "Source_Customer_ID__c": "customer-1", "Name": "Example Company",
            "BillingCity": "Whangārei", "BillingPostalCode": "0110", "BillingCountryCode": "NZ",
        }

    def write_report(self, *events):
        self.report_path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")

    # Reports retain the connection addresses needed for comparison, without copying credentials.
    def test_report_records_destination_addresses_without_credentials(self):
        # Arrange
        secrets = {
            "DATABRICKS_TOKEN": "private-databricks-token",
            "SALESFORCE_CLIENT_ID": "private-client-id",
            "SALESFORCE_CLIENT_SECRET": "private-client-secret",
        }

        # Act
        with (
            patch.dict(os.environ, secrets),
            patch("p2_migration.reporting.REPORT_DIRECTORY", self.directory),
            open_run_report(SNAPSHOT, load=True) as report,
        ):
            report.record_load_started("customer-1")
        report_path = next(self.directory.glob("*.jsonl"))
        recorded_run = read_recorded_run(report_path)
        contents = report_path.read_text(encoding="utf-8")

        # Assert
        self.assertEqual(recorded_run.databricks_server_hostname, START["databricks_server_hostname"])
        self.assertEqual(recorded_run.salesforce_my_domain_url, START["salesforce_my_domain_url"])
        self.assertEqual(recorded_run.uncertain_customer_id, "customer-1")
        for name, value in secrets.items():
            self.assertNotIn(name, contents)
            self.assertNotIn(value, contents)

    # Missing or different addresses must stop reconciliation before either service is contacted.
    def test_unconfirmed_destinations_prevent_connections(self):
        # Arrange
        cases = (
            ("databricks_server_hostname", "another.databricks.com"),
            ("salesforce_my_domain_url", "https://another.my.salesforce.com"),
            ("databricks_server_hostname", None),
            ("salesforce_my_domain_url", None),
            ("databricks_server_hostname", ""),
            ("salesforce_my_domain_url", 123),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                self.write_report({**START, field: value}, ATTEMPT)

                # Act and Assert
                with self.assertRaises(ReconciliationError):
                    main(["--reconcile", str(self.report_path)])
                self.connect_database.assert_not_called()
                self.authenticate.assert_not_called()

    # Match the retained snapshot, bypass both normal phases, and leave all data and reports unchanged.
    def test_matching_account_uses_saved_snapshot_without_writes(self):
        # Arrange
        self.write_report(START, ATTEMPT)
        original = self.report_path.read_bytes()

        # Act
        main(["--reconcile", str(self.report_path)])

        # Assert
        self.assertEqual(self.database.parameters, {
            "customers_table_to_read": SNAPSHOT, "customer_id": "customer-1",
        })
        self.assertEqual(self.database.query_count, 1)
        self.session.get.assert_called_once()
        self.assertEqual([call[0] for call in self.session.method_calls], ["get"])
        self.prepare.assert_not_called()
        self.migrate.assert_not_called()
        self.assertEqual(self.report_path.read_bytes(), original)
        self.assertEqual(list(self.directory.iterdir()), [self.report_path])
        self.assertIn("all 5 mapped fields currently match", self.output.getvalue())

    # A completed attempt or a run with no attempts needs no database or Salesforce access.
    def test_no_recorded_uncertainty_does_not_contact_services(self):
        # Arrange
        cases = ((START,), (START, ATTEMPT, VERIFIED))
        for events in cases:
            with self.subTest(events=events):
                self.write_report(*events)

                # Act
                main(["--reconcile", str(self.report_path)])

                # Assert
                self.connect_database.assert_not_called()
                self.authenticate.assert_not_called()
                self.assertIn("does not establish a complete migration", self.output.getvalue())

    # A logging failure after verification can leave a pending attempt even when the summary ID is empty.
    def test_summary_does_not_erase_an_unverified_attempt(self):
        # Arrange
        self.write_report(START, ATTEMPT, {"event": "run_finished", "unverified_customer_id": None})

        # Act
        run = read_recorded_run(self.report_path)

        # Assert
        self.assertEqual(run.uncertain_customer_id, "customer-1")

    # Prior verified accounts and rejected customers must not become reconciliation candidates.
    def test_selects_only_the_later_unverified_attempt(self):
        # Arrange
        self.write_report(
            START,
            {"event": "customer_rejected", "customer_id": "invalid"},
            ATTEMPT, VERIFIED,
            {**ATTEMPT, "customer_id": "customer-2"},
            {"event": "run_finished", "unverified_customer_id": "customer-2", "status": "failed"},
        )

        # Act
        run = read_recorded_run(self.report_path)

        # Assert
        self.assertEqual(run.customers_table, SNAPSHOT)
        self.assertEqual(run.uncertain_customer_id, "customer-2")

    # Broken, legacy or contradictory reports must stop before any external requests.
    def test_unusable_reports_stop_before_connections(self):
        # Arrange
        cases = {
            "empty": [],
            "missing start": [ATTEMPT],
            "original source": [{**START, "customers_table": "test.raw.customers"}, ATTEMPT],
            "old verification without attempt": [START, VERIFIED],
            "overlapping attempts": [START, ATTEMPT, ATTEMPT],
            "wrong verification": [START, ATTEMPT, {**VERIFIED, "customer_id": "different"}],
            "missing ID": [START, {"event": "account_load_started"}],
            "unknown event": [START, {"event": "surprise"}],
            "summary mismatch": [START, {"event": "run_finished", "unverified_customer_id": "unknown"}],
            "events after finish": [START, {"event": "run_finished"}, ATTEMPT],
            "non-object": [START, []],
            "repeated verified customer": [START, ATTEMPT, VERIFIED, ATTEMPT],
            "reconciled without resume": [START, ATTEMPT, {**VERIFIED, "event": "account_reconciled"}],
            "complete with uncertainty": [START, ATTEMPT, {"event": "run_finished", "status": "complete"}],
            "resume after completion": [START, {"event": "run_finished", "status": "complete"}, {"event": "resume_started"}],
            "unknown outcome": [START, {"event": "run_finished", "status": "unexpected"}],
        }
        for name, events in cases.items():
            with self.subTest(name=name):
                self.write_report(*events)

                # Act and Assert
                with self.assertRaises(ReconciliationError):
                    main(["--reconcile", str(self.report_path)])
                self.connect_database.assert_not_called()
                self.authenticate.assert_not_called()

    # A partially written final line is evidence to inspect, not something to silently discard.
    def test_truncated_report_is_preserved_and_rejected(self):
        # Arrange
        self.write_report(START)
        with self.report_path.open("a", encoding="utf-8") as report:
            report.write('{"event": "account_load_started"')
        original = self.report_path.read_bytes()

        # Act and Assert
        with self.assertRaisesRegex(ReconciliationError, "Invalid JSON on report line 2"):
            main(["--reconcile", str(self.report_path)])
        self.assertEqual(self.report_path.read_bytes(), original)
        self.connect_database.assert_not_called()

    # Only a unique, valid snapshot customer is suitable for comparison.
    def test_missing_duplicate_or_invalid_snapshot_customer_stops_before_salesforce(self):
        # Arrange
        cases = ([], [self.customer, self.customer], [Row(**{**self.customer.asDict(), "billing_postal_code": 110})])
        for customers in cases:
            with self.subTest(customers=customers):
                self.write_report(START, ATTEMPT)
                self.database.customers = iter(customers)

                # Act and Assert
                with self.assertRaises(ReconciliationError):
                    main(["--reconcile", str(self.report_path)])
                self.authenticate.assert_not_called()

    # A mismatch names the fields without printing customer values or trying to repair them.
    def test_differing_fields_remain_unresolved(self):
        # Arrange
        self.write_report(START, ATTEMPT)
        self.session.get.return_value.json.return_value["Name"] = "private changed name"

        # Act and Assert
        with self.assertRaisesRegex(ReconciliationError, "Account differs from the snapshot: Name") as raised:
            main(["--reconcile", str(self.report_path)])
        self.assertNotIn("private changed name", str(raised.exception))
        self.session.patch.assert_not_called()

    # A 404 is not proof that a record never existed or is safe to create again.
    def test_unavailable_account_does_not_trigger_a_save(self):
        # Arrange
        self.write_report(START, ATTEMPT)
        self.session.get.return_value.status_code = 404

        # Act and Assert
        with self.assertRaisesRegex(ReconciliationError, "missing or inaccessible"):
            main(["--reconcile", str(self.report_path)])
        self.session.patch.assert_not_called()

    # Connection and permission failures must not be reported as a missing or matching account.
    def test_service_errors_remain_unresolved_without_exposing_details(self):
        # Arrange
        cases = ("database", "network", "permissions")
        for case in cases:
            with self.subTest(case=case):
                self.write_report(START, ATTEMPT)
                self.database.customers = iter([self.customer])
                self.connect_database.side_effect = OperationalError("private details") if case == "database" else None
                self.session.get.side_effect = requests.exceptions.SSLError("private details") if case == "network" else None
                self.session.get.return_value.status_code = 403

                # Act and Assert
                with self.assertRaises(ReconciliationError) as raised:
                    main(["--reconcile", str(self.report_path)])
                self.assertNotIn("private details", str(raised.exception))
                self.assertNotIn("missing or inaccessible", str(raised.exception))
                self.session.patch.assert_not_called()

    # Conflicting modes fail at argument parsing, before preparation or reconciliation.
    def test_load_reconcile_and_resume_cannot_be_combined(self):
        # Arrange
        combinations = (
            ["--load", "--reconcile", str(self.report_path)],
            ["--load", "--resume", str(self.report_path)],
            ["--reconcile", str(self.report_path), "--resume", str(self.report_path)],
        )
        for arguments in combinations:
            with self.subTest(arguments=arguments):

                # Act
                with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as raised:
                    main(arguments)

                # Assert
                self.assertEqual(raised.exception.code, 2)
                self.prepare.assert_not_called()
                self.connect_database.assert_not_called()


if __name__ == "__main__":
    unittest.main()
