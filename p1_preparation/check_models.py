from typing import TYPE_CHECKING, Any

import requests
from databricks.sql.exc import Error as DatabricksError

from p2_migration.databricks import connect_to_databricks, describe_table
from p2_migration.salesforce import connect_to_salesforce, describe_object
from shared.console import print_table

if TYPE_CHECKING:
    from p1_preparation.prepare import MigrationTables

# Each source column has an expected Databricks type and a Salesforce destination.
FIELD_MAPPINGS = {
    "Account": {
        "customer_id": ("string", "Source_Customer_ID__c"),
        "legal_name": ("string", "Name"),
        "billing_city": ("string", "BillingCity"),
        "billing_postal_code": ("string", "BillingPostalCode"),
        "billing_country_code": ("string", "BillingCountryCode"),
    },
    "Product2": {
        "product_id": ("string", "Source_Product_ID__c"),
        "product_code": ("string", "ProductCode"),
        "product_name": ("string", "Name"),
        "is_active": ("boolean", "IsActive"),
    },
    "Licence__c": {
        "licence_id": ("string", "Source_Licence_ID__c"),
        "customer_id": ("string", "Customer__c"),
        "product_id": ("string", "Product__c"),
        "seats": ("bigint", "Seats__c"),
        "starts_on": ("date", "Starts_On__c"),
        "ends_before": ("date", "Ends_Before__c"),
        "cancelled_on": ("date", "Cancelled_On__c"),
    },
}

# Property names follow Salesforce's Describe API; nillable means a field may be empty.
SOURCE_ID = {
    "type": "string", "length": 36, "externalId": True,
    "unique": True, "caseSensitive": False,
}
EXPECTED_FIELDS: dict[str, dict[str, dict[str, Any]]] = {
    "Account": {
        "Source_Customer_ID__c": SOURCE_ID,
        "Name": {"type": "string"},
        "BillingCity": {"type": "string"},
        "BillingPostalCode": {"type": "string"},
        "BillingCountryCode": {"type": "picklist"},
    },
    "Product2": {
        "Source_Product_ID__c": SOURCE_ID,
        "ProductCode": {"type": "string"},
        "Name": {"type": "string"},
        "IsActive": {"type": "boolean"},
    },
    "Licence__c": {
        "Name": {"type": "string", "autoNumber": True, "createable": False, "updateable": False},
        "Source_Licence_ID__c": SOURCE_ID,
        "Customer__c": {
            "type": "reference", "referenceTo": ["Account"],
            "nillable": False, "relationshipOrder": None,
        },
        "Product__c": {
            "type": "reference", "referenceTo": ["Product2"],
            "nillable": True, "relationshipOrder": None,
        },
        "Seats__c": {"type": "double", "precision": 9, "scale": 0, "nillable": False},
        "Starts_On__c": {"type": "date", "nillable": False},
        "Ends_Before__c": {"type": "date", "nillable": False},
        "Cancelled_On__c": {"type": "date", "nillable": True},
    },
}

# Deletion behaviour is described on the parent, not on the licence's lookup field.
LICENCE_RELATIONSHIPS = {
    "Account": ("Customer__c", True),
    "Product2": ("Product__c", False),
}


class ModelCheckError(RuntimeError):
    """Source or destination definitions could not be checked or do not match our model."""


def check_migration_models(tables: "MigrationTables") -> None:
    """Check the tested snapshots and Salesforce definitions before any records are saved."""
    print("\n1.2 Compare Databricks and Salesforce models\n" + "-" * 60, flush=True)
    print("Checking column types, destination fields and the integration user's permissions...", flush=True)
    snapshot_tables = {
        "Account": tables.customers_table,
        "Product2": tables.products_table,
        "Licence__c": tables.licences_table,
    }
    if not all(snapshot_tables.values()):
        raise ModelCheckError("Model checks require all three prepared snapshots.")
    problems = []
    try:
        with connect_to_databricks() as connection, requests.Session() as session:
            instance_url = connect_to_salesforce(session)
            for object_name, table_name in snapshot_tables.items():
                if table_name is None:
                    raise ModelCheckError(f"Missing snapshot for {object_name}.")
                columns = describe_table(connection, table_name)
                description = describe_object(session, instance_url, object_name)
                object_problems = check_source_columns(object_name, columns)
                object_problems.extend(check_object_setup(object_name, description))
                problems.extend(object_problems)
                print_model_alignment(table_name, object_name, columns, description)
                print(f"  Model check: {'needs attention' if object_problems else 'OK'}")
                for problem in object_problems:
                    print(f"  - {problem}")
    except DatabricksError:
        raise ModelCheckError("Could not inspect the Databricks snapshot columns. No Salesforce records were changed.") from None
    except requests.RequestException:
        raise ModelCheckError("A Salesforce setup request failed. No Salesforce records were changed.") from None
    except (ValueError, RuntimeError) as error:
        raise ModelCheckError(str(error)) from None

    if problems:
        raise ModelCheckError(f"Migration model checks failed: {len(problems)} issue(s). No Salesforce records were changed.")

    print("\nModel checks passed. Preparation is complete.", flush=True)
    print("Next: load customers, products and licences, verifying each record and both licence links.", flush=True)


def check_source_columns(object_name: str, columns: dict[str, str]) -> list[str]:
    """Require the source types our mappings expect, even when a snapshot has no rows."""
    problems = []
    for column, (expected_type, _) in FIELD_MAPPINGS[object_name].items():
        actual_type = columns.get(column)
        if actual_type != expected_type:
            problems.append(
                f"Source for {object_name}.{column}: expected {expected_type}, "
                f"found {actual_type or 'missing column'}."
            )
    return problems


def print_model_alignment(
    table_name: str, object_name: str, columns: dict[str, str], description: dict[str, Any]
) -> None:
    """Show actual types alongside the intended field mappings."""
    fields = {field["name"]: field for field in description["fields"]}
    entity = {"Account": "Customers", "Product2": "Products", "Licence__c": "Licences"}[object_name]
    print(f"\n{entity} → {object_name} (load in this run)", flush=True)
    print(f"Snapshot: {table_name}", flush=True)
    rows = []
    for column, (_, target) in FIELD_MAPPINGS[object_name].items():
        target_type = fields.get(target, {}).get("type", "missing or hidden")
        rows.append((column, columns.get(column, "missing"), target, target_type))
    print_table(("Databricks field", "Type", "Salesforce field", "Type"), rows)
    if object_name == "Licence__c":
        print("Licence loading resolves source parent IDs to Salesforce record IDs before saving.", flush=True)


def check_object_setup(object_name: str, description: dict[str, Any]) -> list[str]:
    """Compare visible fields and reported permissions with our destination model."""
    problems = []
    for permission in ("queryable", "createable", "updateable"):
        if description.get(permission) is not True:
            problems.append(f"{object_name}: {permission} must be enabled for the integration user.")

    fields = {field["name"]: field for field in description["fields"]}
    for field_name, expected in EXPECTED_FIELDS[object_name].items():
        field = fields.get(field_name)
        if field is None:
            problems.append(f"{object_name}.{field_name}: missing or not visible to the integration user.")
            continue

        # Mapped fields must be writable; the auto-number Name is generated by Salesforce.
        attributes = {"createable": True, "updateable": True, **expected}
        for attribute, value in attributes.items():
            if field.get(attribute) != value:
                problems.append(
                    f"{object_name}.{field_name}: {attribute} should be {value!r}, "
                    f"found {field.get(attribute)!r}."
                )

    if object_name in LICENCE_RELATIONSHIPS:
        lookup_field, restrict_deletion = LICENCE_RELATIONSHIPS[object_name]
        relationship = next((
            item for item in description.get("childRelationships", [])
            if item["childSObject"] == "Licence__c" and item["field"] == lookup_field
        ), None)
        if relationship is None:
            problems.append(f"{object_name}: missing Licence__c relationship through {lookup_field}.")
        elif (
            relationship.get("restrictedDelete") is not restrict_deletion
            or relationship.get("cascadeDelete") is not False
        ):
            problems.append(
                f"{object_name}: {lookup_field} must have restrictedDelete={restrict_deletion} "
                "and cascadeDelete=False."
            )

    return problems
