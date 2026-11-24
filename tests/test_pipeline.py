import os
import unittest
from contextlib import ExitStack, nullcontext, redirect_stdout
from io import StringIO
from unittest.mock import Mock, patch

from databricks.sql.types import Row

from main import main
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


class PipelineTests(unittest.TestCase):
    def setUp(self):
        """Replace local settings and external services with test substitutes."""
        resources = ExitStack()
        self.addCleanup(resources.close)
        self.output = StringIO()
        resources.enter_context(redirect_stdout(self.output))
        resources.enter_context(patch("main.load_dotenv"))
        resources.enter_context(patch.dict(os.environ, {
            "DATABRICKS_CUSTOMERS_TABLE": "test.customers",
        }, clear=True))

        self.database = FakeDatabase(customers=[])
        resources.enter_context(patch(
            "main.connect_to_databricks", return_value=nullcontext(self.database)
        ))
        resources.enter_context(patch(
            "main.requests.Session", return_value=nullcontext(Mock())
        ))
        self.count_customers = resources.enter_context(patch(
            "main.count_customers", return_value=(7, 7)
        ))
        self.connect_to_salesforce = resources.enter_context(patch(
            "main.connect_to_salesforce", return_value="https://example.my.salesforce.com"
        ))
        self.save_and_verify = resources.enter_context(patch(
            "customer_migration.pipeline.save_and_verify_account",
            return_value=("created", {"Id": "example-account"}),
        ))

    # The default command prepares multiple batches without contacting Salesforce.
    def test_preview_does_not_authenticate_or_save(self):
        # Arrange
        self.database.customers = iter(customer_row(str(number)) for number in range(7))

        # Act
        main([])

        # Assert
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
        with self.assertRaisesRegex(ValueError, "present and unique"):
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
        with self.assertRaisesRegex(ValueError, "BillingPostalCode differs"):
            main(["--load"])
        self.assertEqual(self.save_and_verify.call_count, 2)
        self.assertNotIn("Accounts verified:", self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
