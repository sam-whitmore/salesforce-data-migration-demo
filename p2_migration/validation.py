import re
from collections.abc import Mapping
from datetime import date


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


def validate_product(product: Mapping[str, object]) -> list[str]:
    """Check the destination's text limits and require an actual active/inactive value."""
    errors = []
    for field, limit in (("product_id", 36), ("product_code", 255), ("product_name", 255)):
        value = product.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{field} must be non-empty text")
        elif len(value) > limit:
            errors.append(f"{field} must be at most {limit} characters")
    if not isinstance(product.get("is_active"), bool):
        errors.append("is_active must be true or false")
    return errors


def validate_licence(licence: Mapping[str, object]) -> list[str]:
    """Check IDs, whole-number seats and business dates without discarding old terms."""
    errors = []
    for field in ("licence_id", "customer_id", "product_id"):
        value = licence.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{field} must be non-empty text")
        elif len(value) > 36:
            errors.append(f"{field} must be at most 36 characters")

    seats = licence.get("seats")
    if type(seats) is not int or not 1 <= seats <= 999999999:
        errors.append("seats must be a whole number between 1 and 999999999")

    start = licence.get("starts_on")
    end = licence.get("ends_before")
    cancellation = licence.get("cancelled_on")
    for field in ("starts_on", "ends_before", "cancelled_on"):
        value = licence.get(field)
        if field == "cancelled_on" and value is None:
            continue
        # Databricks DATE values must not silently become timestamps or parsed text.
        if type(value) is not date:
            errors.append(f"{field} must be a date")
    if type(start) is date and type(end) is date and start >= end:
        errors.append("starts_on must be before ends_before")
    if type(cancellation) is date:
        if type(start) is date and cancellation < start:
            errors.append("cancelled_on must be on or after starts_on")
        if type(end) is date and cancellation >= end:
            errors.append("cancelled_on must be before ends_before")
    return errors
