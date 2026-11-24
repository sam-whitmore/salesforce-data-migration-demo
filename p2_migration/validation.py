import re
from collections.abc import Mapping


def validate_customer(customer: Mapping[str, object]) -> list[str]:
    """Return problems with a customer's values, without changing them."""
    errors = []
    required_fields = (
        "customer_id",
        "legal_name",
        "billing_city",
        "billing_postal_code",
        "billing_country_code",
    )

    for field in required_fields:
        value = customer.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{field} must be non-empty text")

    country_code = customer.get("billing_country_code")
    if isinstance(country_code, str) and country_code.strip():
        if not re.fullmatch(r"[A-Z]{2}", country_code):
            errors.append("billing_country_code must contain two uppercase letters")

    postcode = customer.get("billing_postal_code")
    if country_code == "NZ" and isinstance(postcode, str) and postcode.strip():
        if not re.fullmatch(r"[0-9]{4}", postcode):
            errors.append("NZ billing_postal_code must contain exactly four digits")

    return errors
