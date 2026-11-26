from collections.abc import Mapping
from datetime import date


def prepare_salesforce_account(customer: Mapping[str, object]) -> dict[str, object]:
    """Map a customer that passed validation to Salesforce Account fields."""
    return {
        "Source_Customer_ID__c": customer["customer_id"],
        "Name": customer["legal_name"],
        "BillingCity": customer["billing_city"],
        "BillingPostalCode": customer["billing_postal_code"],
        "BillingCountryCode": customer["billing_country_code"],
    }


def prepare_salesforce_product(product: Mapping[str, object]) -> dict[str, object]:
    """Map a source product, including inactive products needed by existing licences."""
    return {
        "Source_Product_ID__c": product["product_id"],
        "ProductCode": product["product_code"],
        "Name": product["product_name"],
        "IsActive": product["is_active"],
    }


def prepare_salesforce_licence(
    licence: Mapping[str, object], *, customer_account_id: str, salesforce_product_id: str,
) -> dict[str, object]:
    """Map a validated term using resolved Salesforce IDs for its two relationships."""
    return {
        "Source_Licence_ID__c": licence["licence_id"],
        "Customer__c": customer_account_id,
        "Product__c": salesforce_product_id,
        "Seats__c": licence["seats"],
        "Starts_On__c": _date_text(licence["starts_on"]),
        "Ends_Before__c": _date_text(licence["ends_before"]),
        "Cancelled_On__c": _date_text(licence.get("cancelled_on")),
    }


def _date_text(value: object) -> str | None:
    """Salesforce accepts dates as YYYY-MM-DD and clears optional dates with null."""
    if value is None:
        return None
    if type(value) is not date:
        raise ValueError("Salesforce date fields require a date without a time")
    return value.isoformat()
