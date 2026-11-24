import unittest

from p2_migration.validation import validate_customer


class ValidateCustomerTests(unittest.TestCase):
    def setUp(self):
        """Prepare a fresh, valid customer before each test."""
        self.customer: dict[str, object] = {
            "customer_id": "customer-1",
            "legal_name": "Example Company",
            "billing_city": "Whangārei",
            "billing_postal_code": "0110",
            "billing_country_code": "NZ",
        }

    # A valid customer passes without losing the postcode's leading zero.
    def test_valid_customer_keeps_the_leading_zero(self):
        # Arrange: use the valid customer prepared by setUp.

        # Act
        errors = validate_customer(self.customer)

        # Assert
        self.assertEqual(errors, [])
        self.assertEqual(self.customer["billing_postal_code"], "0110")

    # A customer needs an ID.
    def test_missing_customer_id_is_reported(self):
        # Arrange
        del self.customer["customer_id"]

        # Act
        errors = validate_customer(self.customer)

        # Assert
        self.assertEqual(errors, ["customer_id must be non-empty text"])

    # Spaces alone do not count as a name, and validation leaves them unchanged.
    def test_blank_name_is_reported(self):
        # Arrange
        self.customer["legal_name"] = "   "

        # Act
        errors = validate_customer(self.customer)

        # Assert
        self.assertEqual(errors, ["legal_name must be non-empty text"])
        self.assertEqual(self.customer["legal_name"], "   ")

    # A null city is reported as missing text.
    def test_null_city_is_reported(self):
        # Arrange
        self.customer["billing_city"] = None

        # Act
        errors = validate_customer(self.customer)

        # Assert
        self.assertEqual(errors, ["billing_city must be non-empty text"])

    # A numeric postcode is rejected rather than converted to text.
    def test_numeric_postcode_is_rejected_without_converting_it(self):
        # Arrange
        self.customer["billing_postal_code"] = 110

        # Act
        errors = validate_customer(self.customer)

        # Assert
        self.assertEqual(errors, ["billing_postal_code must be non-empty text"])
        self.assertEqual(self.customer["billing_postal_code"], 110)

    # A three-digit NZ postcode is too short.
    def test_short_nz_postcode_is_reported(self):
        # Arrange
        self.customer["billing_postal_code"] = "110"

        # Act
        errors = validate_customer(self.customer)

        # Assert
        self.assertEqual(errors, ["NZ billing_postal_code must contain exactly four digits"])

    # A four-character NZ postcode must contain only digits.
    def test_nz_postcode_cannot_contain_letters(self):
        # Arrange
        self.customer["billing_postal_code"] = "01A0"

        # Act
        errors = validate_customer(self.customer)

        # Assert
        self.assertEqual(errors, ["NZ billing_postal_code must contain exactly four digits"])

    # Country codes must use uppercase letters.
    def test_lowercase_country_code_is_reported(self):
        # Arrange
        self.customer["billing_country_code"] = "nz"

        # Act
        errors = validate_customer(self.customer)

        # Assert
        self.assertEqual(errors, ["billing_country_code must contain two uppercase letters"])

    # A UK postcode is not subject to the NZ four-digit rule.
    def test_nz_postcode_rule_does_not_apply_to_other_countries(self):
        # Arrange
        self.customer["billing_country_code"] = "GB"
        self.customer["billing_city"] = "London"
        self.customer["billing_postal_code"] = "SW1A 1AA"

        # Act
        errors = validate_customer(self.customer)

        # Assert
        self.assertEqual(errors, [])

    # All problems are returned together, rather than stopping at the first.
    def test_reports_multiple_problems_together(self):
        # Arrange
        self.customer["legal_name"] = ""
        self.customer["billing_postal_code"] = "110"

        # Act
        errors = validate_customer(self.customer)

        # Assert
        self.assertEqual(errors, [
            "legal_name must be non-empty text",
            "NZ billing_postal_code must contain exactly four digits",
        ])


if __name__ == "__main__":
    unittest.main()
