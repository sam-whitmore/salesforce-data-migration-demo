import json
import os
import unittest
from contextlib import ExitStack, nullcontext, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import requests
from databricks.sql.types import Row

from main import main
from p2_migration.mapping import prepare_salesforce_account
from p2_migration.migrate import MigrationError
from p2_migration.reconcile import read_recorded_run
from p2_migration.reporting import lock_report

SNAPSHOT = "test.migration.customers_snapshot_resume"


def customer(customer_id):
    return Row(
        customer_id=customer_id, legal_name="Example Company", billing_city="Whangārei",
        billing_postal_code="0110", billing_country_code="NZ",
    )


class ResumeTests(unittest.TestCase):
    def setUp(self):
        resources = ExitStack()
        self.addCleanup(resources.close)
        self.directory = Path(resources.enter_context(TemporaryDirectory()))
        self.report_path = self.directory / "original.jsonl"
        self.output = StringIO()
        resources.enter_context(redirect_stdout(self.output))
        resources.enter_context(patch("main.load_dotenv"))
        resources.enter_context(patch.dict(os.environ, {
            "PYTHON_DOTENV_DISABLED": "1",
            "DATABRICKS_SERVER_HOSTNAME": "example.databricks.com",
            "SALESFORCE_MY_DOMAIN_URL": "https://example.my.salesforce.com",
            "DATABRICKS_CUSTOMERS_TABLE": "test.raw.newer_source",
        }, clear=True))
        self.prepare = resources.enter_context(patch("main.prepare_customers"))
        self.database = object()
        self.connect = resources.enter_context(patch(
            "p2_migration.migrate.connect_to_databricks", return_value=nullcontext(self.database),
        ))
        self.customers = [customer(name) for name in ("a", "b", "c", "d")]
        resources.enter_context(patch(
            "p2_migration.migrate.count_customers",
            side_effect=lambda *_: (len(self.customers), len({row.customer_id for row in self.customers})),
        ))
        self.fetch_batches = resources.enter_context(patch(
            "p2_migration.migrate.fetch_customer_batches",
            side_effect=lambda *_args, **_kwargs: iter([self.customers[:2], self.customers[2:]]),
        ))
        resources.enter_context(patch(
            "p2_migration.reconcile.fetch_customer", side_effect=self.fetch_customer,
        ))
        self.session = MagicMock(spec=requests.Session)
        self.session.__enter__.return_value = self.session
        resources.enter_context(patch("p2_migration.migrate.requests.Session", return_value=self.session))
        self.authenticate = resources.enter_context(patch(
            "p2_migration.migrate.connect_to_salesforce", return_value="https://example.my.salesforce.com",
        ))
        resources.enter_context(patch("p2_migration.retries.sleep"))
        self.saved_accounts = {
            row.customer_id: {**prepare_salesforce_account(row.asDict()), "Id": f"account-{row.customer_id}"}
            for row in self.customers[:2]
        }
        self.session.get.side_effect = self.read_account
        self.save = resources.enter_context(patch(
            "p2_migration.migrate.save_and_verify_account", side_effect=self.save_account,
        ))
        self.write_interrupted_report()

    def fetch_customer(self, connection, table, customer_id):
        self.assertEqual(table, SNAPSHOT)
        matches = [row for row in self.customers if row.customer_id == customer_id]
        if len(matches) != 1:
            raise ValueError("Snapshot customer missing or ambiguous")
        return matches[0].asDict()

    def read_account(self, url, **kwargs):
        customer_id = url.rsplit("/", 1)[-1]
        response = MagicMock()
        response.status_code = 200 if customer_id in self.saved_accounts else 404
        response.headers = {}
        response.json.return_value = self.saved_accounts.get(customer_id)
        return response

    def save_account(self, session, instance_url, account):
        customer_id = account["Source_Customer_ID__c"]
        received = {**account, "Id": f"account-{customer_id}"}
        self.saved_accounts[customer_id] = received
        return "created", received

    def write_interrupted_report(self):
        events = [
            {"event": "run_started", "customers_table": SNAPSHOT,
             "databricks_server_hostname": "example.databricks.com",
             "salesforce_my_domain_url": "https://example.my.salesforce.com"},
            {"event": "account_load_started", "customer_id": "a"},
            {"event": "account_verified", "customer_id": "a"},
            {"event": "account_load_started", "customer_id": "b"},
            {"event": "run_finished", "status": "failed", "unverified_customer_id": "b"},
        ]
        self.report_path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")

    def events(self):
        return [json.loads(line) for line in self.report_path.read_text(encoding="utf-8").splitlines()]

    # Continue the same snapshot, preserving history and saving only the unattempted customers.
    def test_resume_skips_verified_and_reconciles_uncertain_accounts(self):
        # Arrange
        original = self.report_path.read_bytes()

        # Act
        main(["--resume", str(self.report_path)])

        # Assert
        self.prepare.assert_not_called()
        self.assertEqual([call.args[2]["Source_Customer_ID__c"] for call in self.save.call_args_list], ["c", "d"])
        self.session.get.assert_called_once()
        self.assertTrue(self.session.get.call_args.args[0].endswith("/b"))
        self.assertTrue(self.report_path.read_bytes().startswith(original))
        self.assertEqual(list(self.directory.iterdir()), [self.report_path])
        self.assertTrue(all(call.args[1] == SNAPSHOT for call in self.fetch_batches.call_args_list))
        summary = self.events()[-1]
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["accounts_previously_verified"], 1)
        self.assertEqual(summary["accounts_reconciled"], 1)
        self.assertEqual(summary["accounts_created"], 2)
        self.assertEqual(summary["accounts_verified"], 4)
        self.assertEqual(read_recorded_run(self.report_path).verified_customer_ids, frozenset("abcd"))

    # A second lost save response is reconciled on the next resume, never blindly repeated.
    def test_resume_can_itself_be_interrupted_and_resumed(self):
        # Arrange
        def save_then_lose_response(session, instance_url, account):
            self.save_account(session, instance_url, account)
            raise requests.ReadTimeout("private response details")
        self.save.side_effect = save_then_lose_response

        # Act
        with self.assertRaises(MigrationError):
            main(["--resume", str(self.report_path)])
        interrupted = read_recorded_run(self.report_path)
        self.save.side_effect = self.save_account
        main(["--resume", str(self.report_path)])

        # Assert
        self.assertEqual(interrupted.uncertain_customer_id, "c")
        self.assertEqual(interrupted.verified_customer_ids, frozenset("ab"))
        self.assertEqual([call.args[2]["Source_Customer_ID__c"] for call in self.save.call_args_list], ["c", "d"])
        self.assertEqual([call.args[0].rsplit("/", 1)[-1] for call in self.session.get.call_args_list], ["b", "c"])
        self.assertEqual(self.events()[-1]["accounts_verified"], 4)
        self.assertEqual(self.events()[-1]["status"], "complete")
        self.assertNotIn("private response details", self.report_path.read_text())

    # A stop before any attempt, or between verified accounts, needs no uncertain-account readback.
    def test_resume_without_an_uncertain_attempt(self):
        # Arrange
        for event_count, expected_ids in ((1, list("abcd")), (3, list("bcd"))):
            with self.subTest(event_count=event_count):
                self.write_interrupted_report()
                events = self.events()[:event_count]
                self.report_path.write_text("".join(json.dumps(event) + "\n" for event in events))
                self.save.reset_mock()

                # Act
                main(["--resume", str(self.report_path)])

                # Assert
                self.session.get.assert_not_called()
                self.assertEqual([call.args[2]["Source_Customer_ID__c"] for call in self.save.call_args_list], expected_ids)
                self.assertEqual(self.events()[-1]["accounts_verified"], 4)
                self.assertEqual(self.events()[-1]["status"], "complete")

    # A differing, unavailable or unreadable uncertain account must block every later save.
    def test_unresolved_account_blocks_resume(self):
        # Arrange
        for case in ("different", "unavailable", "timeout"):
            with self.subTest(case=case):
                self.write_interrupted_report()
                self.saved_accounts["b"] = {**prepare_salesforce_account(customer("b").asDict()), "Id": "account-b"}
                self.session.get.side_effect = self.read_account
                if case == "different":
                    self.saved_accounts["b"]["Name"] = "Changed name"
                elif case == "unavailable":
                    del self.saved_accounts["b"]
                else:
                    self.session.get.side_effect = requests.ReadTimeout()

                # Act and Assert
                with self.assertRaises(MigrationError):
                    main(["--resume", str(self.report_path)])
                self.save.assert_not_called()
                self.assertEqual(read_recorded_run(self.report_path).uncertain_customer_id, "b")
                self.assertEqual(self.events()[-1]["status"], "failed")

    # Rejected rows remain incomplete on repeated resumes, while successful rows are skipped.
    def test_rejected_rows_remain_incomplete_without_reloading_successes(self):
        # Arrange
        self.customers[-1] = Row(**{**self.customers[-1].asDict(), "billing_postal_code": "110"})

        # Act
        for _ in range(2):
            with self.assertRaisesRegex(MigrationError, "Migration incomplete"):
                main(["--resume", str(self.report_path)])

        # Assert
        self.save.assert_called_once()
        self.assertEqual(self.save.call_args.args[2]["Source_Customer_ID__c"], "c")
        self.assertEqual(self.events()[-1]["customers_rejected"], 1)
        self.assertEqual(self.events()[-1]["accounts_verified"], 3)
        self.assertEqual(self.events()[-1]["status"], "incomplete")

    # Missing recorded rows or duplicate IDs must not allow a partial snapshot to be resumed.
    def test_invalid_snapshot_blocks_saves(self):
        # Arrange
        for rows in ([customer("b"), customer("c")], [customer("a"), customer("a"), customer("b")]):
            with self.subTest(rows=rows):
                self.write_interrupted_report()
                self.customers = rows

                # Act and Assert
                with self.assertRaises(MigrationError):
                    main(["--resume", str(self.report_path)])
                self.authenticate.assert_not_called()
                self.save.assert_not_called()

    # A completed run is a no-op, leaving its report and both services untouched.
    def test_completed_run_is_not_resumed(self):
        # Arrange
        events = self.events()[:3] + [{"event": "run_finished", "status": "complete"}]
        self.report_path.write_text("".join(json.dumps(event) + "\n" for event in events))
        original = self.report_path.read_bytes()

        # Act
        main(["--resume", str(self.report_path)])

        # Assert
        self.connect.assert_not_called()
        self.save.assert_not_called()
        self.assertEqual(self.report_path.read_bytes(), original)

    # Destination mismatch or broken history cannot modify a report or start a connection.
    def test_unusable_report_or_changed_destination_stops_before_appending(self):
        # Arrange
        for case in ("destination", "truncated", "missing"):
            with self.subTest(case=case):
                self.write_interrupted_report()
                if case == "destination":
                    self.report_path.write_text(self.report_path.read_text().replace("example.databricks.com", "other.databricks.com"))
                elif case == "truncated":
                    with self.report_path.open("a") as file:
                        file.write('{"event":')
                else:
                    self.report_path.unlink()
                original = self.report_path.read_bytes() if self.report_path.exists() else None

                # Act and Assert
                with self.assertRaises(MigrationError):
                    main(["--resume", str(self.report_path)])
                self.connect.assert_not_called()
                if original is None:
                    self.assertFalse(self.report_path.exists())
                else:
                    self.assertEqual(self.report_path.read_bytes(), original)

    # The operating-system lock prevents another writer and is released when its owner exits.
    def test_locked_run_cannot_be_resumed_until_released(self):
        # Arrange
        original = self.report_path.read_bytes()

        # Act and Assert
        with self.report_path.open("r+") as owner, lock_report(owner):
            with self.assertRaisesRegex(MigrationError, "already active"):
                main(["--resume", str(self.report_path)])
            self.connect.assert_not_called()
            self.assertEqual(self.report_path.read_bytes(), original)
        main(["--resume", str(self.report_path)])
        self.assertEqual(self.events()[-1]["status"], "complete")

    # Failure to persist reconciliation must prevent every later Salesforce save.
    def test_report_sync_failure_blocks_further_saves(self):
        # Arrange
        sync_results = [None, OSError("disk sync failed"), None]

        # Act
        with (
            patch("p2_migration.reporting.fsync", side_effect=sync_results),
            self.assertRaisesRegex(MigrationError, "Could not read or save the run report"),
        ):
            main(["--resume", str(self.report_path)])

        # Assert
        self.save.assert_not_called()
        self.assertEqual(self.events()[-1]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
