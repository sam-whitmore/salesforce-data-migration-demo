import json
import os
import runpy
import sys
import unittest
from contextlib import ExitStack, nullcontext, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import requests
from databricks.sql.exc import OperationalError
from databricks.sql.types import Row
from dbt.artifacts.schemas.run import RunExecutionResult

from main import main
from p1_preparation.prepare import PreparationError
from p2_migration.migrate import MigrationError
from p2_migration.reporting import MigrationReport
from tests.fake_database import FakeDatabase


def customer_row(customer_id, postcode="0110"):
    """Create a small source record for a workflow test."""
    return Row(
        customer_id=customer_id,
        legal_name="Example Company",
        billing_city="Whangārei",
        billing_postal_code=postcode,
        billing_country_code="NZ",
    )


class MainTests(unittest.TestCase):
    def setUp(self):
        """Replace local settings and external services with test substitutes."""
        resources = ExitStack()
        self.addCleanup(resources.close)
        self.output = StringIO()
        resources.enter_context(redirect_stdout(self.output))
        resources.enter_context(patch("main.load_dotenv"))
        temporary_directory = resources.enter_context(TemporaryDirectory())
        self.report_directory = Path(temporary_directory) / "reports"
        resources.enter_context(patch("p2_migration.reporting.REPORT_DIRECTORY", self.report_directory))
        resources.enter_context(patch.dict(os.environ, {
            "DATABRICKS_CUSTOMERS_TABLE": "test.raw.customers",
            "DATABRICKS_TOKEN": "example-token",
        }, clear=True))

        runner = resources.enter_context(patch("p1_preparation.prepare.dbtRunner"))
        self.build = runner.return_value.invoke
        self.build.return_value.success = True
        model = Mock(resource_type="model", relation_name="test.migration.customers_snapshot_example")
        model.name = "stage_customers"
        self.build.return_value.result = Mock(
            spec=RunExecutionResult, results=[Mock(node=model)],
        )

        self.database = FakeDatabase(customers=[])
        self.connect_to_databricks = resources.enter_context(patch(
            "p2_migration.migrate.connect_to_databricks", return_value=nullcontext(self.database)
        ))
        resources.enter_context(patch(
            "p2_migration.migrate.requests.Session", return_value=nullcontext(Mock())
        ))
        self.count_customers = resources.enter_context(patch(
            "p2_migration.migrate.count_customers", return_value=(7, 7)
        ))
        self.connect_to_salesforce = resources.enter_context(patch(
            "p2_migration.migrate.connect_to_salesforce", return_value="https://example.my.salesforce.com"
        ))
        self.save_and_verify = resources.enter_context(patch(
            "p2_migration.migrate.save_and_verify_account",
            return_value=("created", {"Id": "example-account"}),
        ))

    def read_report(self):
        """Read the one report produced by this test's load."""
        reports = list(self.report_directory.glob("*.jsonl"))
        self.assertEqual(len(reports), 1)
        return [json.loads(line) for line in reports[0].read_text(encoding="utf-8").splitlines()]

    # The default command prepares multiple batches without contacting Salesforce.
    def test_preview_does_not_authenticate_or_save(self):
        # Arrange
        self.database.customers = iter(customer_row(str(number)) for number in range(7))

        # Act
        main([])

        # Assert
        self.build.assert_not_called()
        self.assertEqual(self.database.parameters["customers_table_to_read"], "test.raw.customers")
        self.connect_to_salesforce.assert_not_called()
        self.save_and_verify.assert_not_called()
        self.assertIn("Batch 2: 1 customers", self.output.getvalue())
        self.assertIn("Accounts prepared for Salesforce: 7", self.output.getvalue())
        self.assertNotIn("Accounts verified:", self.output.getvalue())
        self.assertFalse(self.report_directory.exists())

    # All batches share one login and distinguish newly created accounts from updates.
    def test_loads_multiple_batches_and_reports_verified_totals(self):
        # Arrange
        self.database.customers = iter(customer_row(str(number)) for number in range(7))
        self.save_and_verify.side_effect = [
            ("updated", {"Id": "existing-account"}),
            *[("created", {"Id": f"new-account-{number}"}) for number in range(6)],
        ]

        # Act
        main(["--load"])

        # Assert
        self.build.assert_called_once()
        self.assertEqual(self.database.parameters["customers_table_to_read"], "test.migration.customers_snapshot_example")
        self.count_customers.assert_called_once_with(self.database, "test.migration.customers_snapshot_example")
        self.connect_to_salesforce.assert_called_once()
        self.assertEqual(self.save_and_verify.call_count, 7)
        loaded_ids = [call.args[2]["Source_Customer_ID__c"] for call in self.save_and_verify.call_args_list]
        self.assertEqual(loaded_ids, [str(number) for number in range(7)])
        self.assertIn("Accounts created: 6", self.output.getvalue())
        self.assertIn("Accounts updated: 1", self.output.getvalue())
        self.assertIn("Accounts verified: 7", self.output.getvalue())
        events = self.read_report()
        self.assertEqual(events[0]["customers_table"], "test.migration.customers_snapshot_example")
        self.assertEqual(events[-1]["status"], "complete")
        self.assertEqual(events[-1]["accounts_verified"], 7)
        self.assertEqual(events[-1]["accounts_created"], 6)
        self.assertEqual(events[-1]["accounts_updated"], 1)
        verified_ids = [event["customer_id"] for event in events if event["event"] == "account_verified"]
        self.assertEqual(verified_ids, loaded_ids)

    # A genuinely empty source completes without inventing rejected or verified records.
    def test_empty_source_completes_with_zero_counts(self):
        # Arrange
        self.count_customers.return_value = (0, 0)

        # Act
        main(["--load"])

        # Assert
        self.save_and_verify.assert_not_called()
        summary = self.read_report()[-1]
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["customers_retrieved"], 0)
        self.assertEqual(summary["accounts_verified"], 0)

    # Before another save begins, the report already contains prior progress and the new attempt.
    def test_progress_and_write_intent_are_saved_before_the_next_account(self):
        # Arrange
        self.database.customers = iter([customer_row("first"), customer_row("second")])
        events_during_load = []

        def save_account(session, instance_url, account):
            if account["Source_Customer_ID__c"] == "second":
                events_during_load.extend(self.read_report())
            return "created", {"Id": "example-account"}

        self.save_and_verify.side_effect = save_account

        # Act
        main(["--load"])

        # Assert
        self.assertEqual(
            [(event["event"], event.get("customer_id")) for event in events_during_load],
            [
                ("run_started", None),
                ("account_load_started", "first"),
                ("account_verified", "first"),
                ("account_load_started", "second"),
            ],
        )
        self.assertNotIn("run_finished", [event["event"] for event in events_during_load])

    # Valid records still load, but a rejected record makes the overall load incomplete.
    def test_rejected_customers_make_the_load_incomplete(self):
        # Arrange
        self.database.customers = iter([
            customer_row("invalid-customer", postcode=110),
            customer_row("valid-customer"),
        ])
        self.count_customers.return_value = (2, 2)

        # Act
        with self.assertRaisesRegex(MigrationError, "Migration incomplete"):
            main(["--load"])

        # Assert
        self.save_and_verify.assert_called_once()
        sent_account = self.save_and_verify.call_args.args[2]
        self.assertEqual(sent_account["Source_Customer_ID__c"], "valid-customer")
        self.assertIn("Rejected customers: 1", self.output.getvalue())
        self.assertIn("Accounts verified: 1", self.output.getvalue())
        events = self.read_report()
        rejection = next(event for event in events if event["event"] == "customer_rejected")
        self.assertEqual(rejection["customer_id"], "invalid-customer")
        self.assertEqual(rejection["reasons"], ["billing_postal_code must be non-empty text"])
        self.assertEqual(events[-1]["status"], "incomplete")
        self.assertEqual(events[-1]["customers_rejected"], 1)
        self.assertEqual(events[-1]["accounts_verified"], 1)

    # Preview remains a read-only inspection, even when records need correcting.
    def test_preview_reports_rejections_without_failing_or_writing(self):
        # Arrange
        self.database.customers = iter([customer_row("invalid-customer", postcode="110")])

        # Act
        main([])

        # Assert
        self.assertIn("Rejected customers: 1", self.output.getvalue())
        self.save_and_verify.assert_not_called()
        self.assertFalse(self.report_directory.exists())

    # Rejecting every customer cannot be mistaken for a successful empty load.
    def test_all_rejected_customers_produce_an_incomplete_report(self):
        # Arrange
        self.database.customers = iter([customer_row("invalid-customer", postcode="110")])
        self.count_customers.return_value = (1, 1)

        # Act
        with self.assertRaisesRegex(MigrationError, "Migration incomplete"):
            main(["--load"])

        # Assert
        self.save_and_verify.assert_not_called()
        summary = self.read_report()[-1]
        self.assertEqual(summary["status"], "incomplete")
        self.assertEqual(summary["customers_rejected"], 1)
        self.assertEqual(summary["accounts_verified"], 0)

    # The executable entry point turns an incomplete migration into a nonzero exit.
    def test_incomplete_load_exits_unsuccessfully(self):
        # Arrange
        self.database.customers = iter([customer_row("invalid-customer", postcode="110")])
        entry_point = Path(__file__).resolve().parents[1] / "main.py"

        # Act
        with (
            patch.object(sys, "argv", [str(entry_point), "--load"]),
            patch("dotenv.load_dotenv"),
            self.assertRaises(SystemExit) as raised,
        ):
            runpy.run_path(str(entry_point), run_name="__main__")

        # Assert
        self.assertIsInstance(raised.exception.code, str)
        self.assertIn("Migration incomplete", str(raised.exception.code))

    # Duplicate source IDs stop the run before any Salesforce requests.
    def test_duplicate_ids_prevent_loading(self):
        # Arrange
        self.count_customers.return_value = (2, 1)

        # Act and Assert
        with self.assertRaisesRegex(MigrationError, "present and unique"):
            main(["--load"])
        self.connect_to_salesforce.assert_not_called()
        self.save_and_verify.assert_not_called()
        self.assertEqual(self.read_report()[-1]["status"], "failed")

    # A verification failure stops later accounts and cannot produce a success total.
    def test_stops_loading_after_a_verification_failure(self):
        # Arrange
        self.database.customers = iter(customer_row(str(number)) for number in range(7))
        self.save_and_verify.side_effect = [
            ("created", {"Id": "first-account"}),
            ValueError("Account was saved, but BillingPostalCode differs"),
        ]

        # Act and Assert
        with self.assertRaisesRegex(MigrationError, "BillingPostalCode differs"):
            main(["--load"])
        self.assertEqual(self.save_and_verify.call_count, 2)
        self.assertNotIn("Accounts verified:", self.output.getvalue())
        summary = self.read_report()[-1]
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["accounts_verified"], 1)
        self.assertEqual(summary["unverified_customer_id"], "1")

    # Failed preparation stops the workflow before extraction or Salesforce access.
    def test_failed_dbt_build_prevents_migration(self):
        # Arrange
        self.build.return_value.success = False

        # Act and Assert
        with self.assertRaisesRegex(PreparationError, "Customer preparation failed"):
            main(["--load"])
        self.connect_to_databricks.assert_not_called()
        self.connect_to_salesforce.assert_not_called()
        self.save_and_verify.assert_not_called()
        self.assertFalse(self.report_directory.exists())

    # A database failure is identified as Databricks, without showing raw error details.
    def test_databricks_failure_is_reported_during_preview(self):
        # Arrange
        self.connect_to_databricks.side_effect = OperationalError("private connection details")

        # Act and Assert
        with self.assertRaisesRegex(MigrationError, "Could not read customers from Databricks") as raised:
            main([])
        self.assertNotIn("private connection details", str(raised.exception))
        self.assertNotIn("Salesforce", str(raised.exception))
        self.connect_to_salesforce.assert_not_called()

    # A later read failure preserves earlier writes and cannot report a complete run.
    def test_databricks_failure_after_a_batch_explains_partial_progress(self):
        # Arrange
        self.database.fetchmany = Mock(side_effect=[
            [customer_row("first-customer")],
            OperationalError("connection lost"),
        ])

        # Act and Assert
        with self.assertRaisesRegex(MigrationError, "Could not read customers from Databricks") as raised:
            main(["--load"])
        self.assertIn("Earlier Salesforce writes may have completed", str(raised.exception))
        self.assertEqual(self.save_and_verify.call_count, 1)
        self.assertNotIn("Accounts verified:", self.output.getvalue())
        summary = self.read_report()[-1]
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["accounts_verified"], 1)

    # A Salesforce timeout stops later records and explains that a write may have completed.
    def test_salesforce_timeout_stops_the_load(self):
        # Arrange
        self.database.customers = iter(customer_row(str(number)) for number in range(2))
        self.save_and_verify.side_effect = requests.Timeout("private request details")

        # Act and Assert
        with self.assertRaisesRegex(MigrationError, "A Salesforce request failed") as raised:
            main(["--load"])
        self.assertIn("writes may have completed", str(raised.exception))
        self.assertNotIn("private request details", str(raised.exception))
        self.assertEqual(self.save_and_verify.call_count, 1)
        self.assertNotIn("Accounts verified:", self.output.getvalue())
        summary = self.read_report()[-1]
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["accounts_verified"], 0)
        self.assertEqual(summary["unverified_customer_id"], "0")
        self.assertNotIn("private request details", json.dumps(self.read_report()))

    # A report must be writable before customers are read or sent to Salesforce.
    def test_unwritable_report_prevents_migration(self):
        # Arrange
        self.report_directory.write_text("This file prevents creating the reports directory.")

        # Act
        with self.assertRaisesRegex(MigrationError, "Could not save the migration report"):
            main(["--load"])

        # Assert
        self.connect_to_databricks.assert_not_called()
        self.connect_to_salesforce.assert_not_called()
        self.save_and_verify.assert_not_called()

    # If the attempt cannot be synced to disk, its Salesforce save must not begin.
    def test_failed_attempt_sync_prevents_the_salesforce_save(self):
        # Arrange
        self.database.customers = iter([customer_row("first")])

        # Act
        with (
            patch("p2_migration.reporting.fsync", side_effect=[None, OSError("disk sync failed"), None]),
            self.assertRaisesRegex(MigrationError, "Could not save the migration report"),
        ):
            main(["--load"])

        # Assert
        self.save_and_verify.assert_not_called()
        self.assertEqual(self.read_report()[-1]["status"], "failed")

    # Ctrl+C records partial progress but does not pretend the current write was undone.
    def test_keyboard_interrupt_records_an_unverified_customer(self):
        # Arrange
        self.database.customers = iter([customer_row("interrupted-customer")])
        self.save_and_verify.side_effect = KeyboardInterrupt

        # Act
        with self.assertRaises(KeyboardInterrupt):
            main(["--load"])

        # Assert
        summary = self.read_report()[-1]
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["unverified_customer_id"], "interrupted-customer")

    # Losing report storage stops further writes without forgetting a verified account.
    def test_report_write_failure_stops_later_accounts(self):
        # Arrange
        self.database.customers = iter([customer_row("first"), customer_row("second")])
        write_event = MigrationReport.write_event

        def fail_to_record_verification(report, event, **details):
            if event == "account_verified":
                raise OSError("report storage unavailable")
            write_event(report, event, **details)

        # Act
        with (
            patch.object(MigrationReport, "write_event", autospec=True, side_effect=fail_to_record_verification),
            self.assertRaisesRegex(MigrationError, "Could not save the migration report"),
        ):
            main(["--load"])

        # Assert
        self.save_and_verify.assert_called_once()
        summary = self.read_report()[-1]
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["accounts_verified"], 1)
        self.assertIsNone(summary["unverified_customer_id"])

    # Unexpected programming errors remain visible instead of being labelled network failures.
    def test_unexpected_errors_are_not_disguised(self):
        # Arrange
        self.connect_to_databricks.side_effect = TypeError("unexpected programming error")

        # Act and Assert
        with self.assertRaisesRegex(TypeError, "unexpected programming error"):
            main([])


if __name__ == "__main__":
    unittest.main()
