import os
import unittest
from contextlib import ExitStack, nullcontext, redirect_stdout
from io import StringIO
from unittest.mock import Mock, patch

import requests
from databricks.sql.exc import OperationalError
from databricks.sql.types import Row
from dbt.artifacts.schemas.run import RunExecutionResult

from main import main
from p1_preparation.prepare import PreparationError
from p2_migration.migrate import MigrationError
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
        resources.enter_context(patch.dict(os.environ, {
            "DATABRICKS_CUSTOMERS_TABLE": "test.raw.customers",
            "DATABRICKS_TOKEN": "example-token",
        }, clear=True))

        runner = resources.enter_context(patch("p1_preparation.prepare.dbtRunner"))
        self.build = runner.return_value.invoke
        self.build.return_value.success = True
        model = Mock(resource_type="model", relation_name="test.migration.stage_customers")
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
        self.assertEqual(self.database.parameters["customers_table_to_read"], "test.migration.stage_customers")
        self.connect_to_salesforce.assert_called_once()
        self.assertEqual(self.save_and_verify.call_count, 7)
        loaded_ids = [call.args[2]["Source_Customer_ID__c"] for call in self.save_and_verify.call_args_list]
        self.assertEqual(loaded_ids, [str(number) for number in range(7)])
        self.assertIn("Accounts created: 6", self.output.getvalue())
        self.assertIn("Accounts updated: 1", self.output.getvalue())
        self.assertIn("Accounts verified: 7", self.output.getvalue())

    # Invalid records are reported and never sent to Salesforce.
    def test_skips_invalid_customers_and_continues(self):
        # Arrange
        self.database.customers = iter([
            customer_row("invalid-customer", postcode=110),
            customer_row("valid-customer"),
        ])
        self.count_customers.return_value = (2, 2)

        # Act
        main(["--load"])

        # Assert
        self.save_and_verify.assert_called_once()
        sent_account = self.save_and_verify.call_args.args[2]
        self.assertEqual(sent_account["Source_Customer_ID__c"], "valid-customer")
        self.assertIn("Rejected customers: 1", self.output.getvalue())
        self.assertIn("Accounts verified: 1", self.output.getvalue())

    # Duplicate source IDs stop the run before any Salesforce requests.
    def test_duplicate_ids_prevent_loading(self):
        # Arrange
        self.count_customers.return_value = (2, 1)

        # Act and Assert
        with self.assertRaisesRegex(MigrationError, "present and unique"):
            main(["--load"])
        self.connect_to_salesforce.assert_not_called()
        self.save_and_verify.assert_not_called()

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

    # Unexpected programming errors remain visible instead of being labelled network failures.
    def test_unexpected_errors_are_not_disguised(self):
        # Arrange
        self.connect_to_databricks.side_effect = TypeError("unexpected programming error")

        # Act and Assert
        with self.assertRaisesRegex(TypeError, "unexpected programming error"):
            main([])


if __name__ == "__main__":
    unittest.main()
