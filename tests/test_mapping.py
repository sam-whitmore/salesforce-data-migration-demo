import unittest

from customer_migration.mapping import prepare_salesforce_account


class PrepareSalesforceAccountTests(unittest.TestCase):
    def setUp(self):
        """Prepare a customer that has already passed validation."""
        self.customer = {
            "customer_id": "01a20ada-2c00-7305-8588-34a676a8372c",
            "legal_name": "Example Company",
            "billing_city": "Whangārei",
            "billing_postal_code": "0110",
            "billing_country_code": "NZ",
        }

    # Only the intended Account fields are prepared, with the original text intact.
    def test_maps_customer_fields_and_preserves_their_values(self):
        # Arrange
        self.customer["source_system"] = "synthetic_erp"
        expected_account = {
            "Source_Customer_ID__c": "01a20ada-2c00-7305-8588-34a676a8372c",
            "Name": "Example Company",
            "BillingCity": "Whangārei",
            "BillingPostalCode": "0110",
            "BillingCountryCode": "NZ",
        }

        # Act
        account = prepare_salesforce_account(self.customer)

        # Assert
        self.assertEqual(account, expected_account)

    # A name change keeps the same source ID for matching the customer later.
    def test_renaming_a_customer_keeps_the_same_external_id(self):
        # Arrange
        renamed_customer = self.customer.copy()
        renamed_customer["legal_name"] = "Renamed Company"

        # Act
        original_account = prepare_salesforce_account(self.customer)
        renamed_account = prepare_salesforce_account(renamed_customer)

        # Assert
        self.assertEqual(
            renamed_account["Source_Customer_ID__c"],
            original_account["Source_Customer_ID__c"],
        )
        self.assertEqual(renamed_account["Name"], "Renamed Company")

    # Preparing or editing the output does not alter the source customer.
    def test_keeps_the_source_customer_unchanged(self):
        # Arrange
        original_customer = self.customer.copy()

        # Act
        account = prepare_salesforce_account(self.customer)
        account["Name"] = "Changed Output"

        # Assert
        self.assertEqual(self.customer, original_customer)


if __name__ == "__main__":
    unittest.main()
