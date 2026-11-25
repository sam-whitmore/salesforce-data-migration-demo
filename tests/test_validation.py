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

    # Required values must be non-empty text; validation reports rather than repairs them.
    def test_required_fields_reject_invalid_text_without_changing_it(self):
        # Arrange
        invalid_fields = (
            ("customer_id", 123),
            ("legal_name", "   "),
            ("billing_city", None),
            ("billing_postal_code", 110),
            ("billing_country_code", ""),
        )

        for field, value in invalid_fields:
            with self.subTest(field=field, value=value):
                customer = {**self.customer, field: value}
                original_customer = customer.copy()

                # Act
                errors = validate_customer(customer)

                # Assert
                self.assertEqual(errors, [f"{field} must be non-empty text"])
                self.assertEqual(customer, original_customer)

    # NZ postcodes require exactly four ASCII digits, not merely four characters.
    def test_nz_postcodes_require_exactly_four_ascii_digits(self):
        # Arrange
        invalid_postcodes = ("110", "01100", "01A0", "٠١١٠", "0110 ")

        for postcode in invalid_postcodes:
            with self.subTest(postcode=postcode):
                customer = {**self.customer, "billing_postal_code": postcode}

                # Act
                errors = validate_customer(customer)

                # Assert
                self.assertEqual(errors, ["NZ billing_postal_code must contain exactly four digits"])

    # Country codes require both the right length and uppercase ASCII letters.
    def test_country_codes_require_exactly_two_uppercase_letters(self):
        # Arrange
        invalid_codes = ("nz", "N", "NZZ", "N1", "NZ ", "ÑZ")

        for country_code in invalid_codes:
            with self.subTest(country_code=country_code):
                customer = {**self.customer, "billing_country_code": country_code}

                # Act
                errors = validate_customer(customer)

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
