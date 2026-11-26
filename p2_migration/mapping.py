from collections.abc import Mapping


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
