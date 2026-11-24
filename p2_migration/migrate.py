from collections.abc import Iterable, Mapping

import requests
from databricks.sql.exc import Error as DatabricksError

from p2_migration.databricks import (
    connect_to_databricks,
    count_customers,
    fetch_customer_batches,
)
from p2_migration.mapping import prepare_salesforce_account
from p2_migration.salesforce import (
    EXTERNAL_ID_FIELD,
    connect_to_salesforce,
    save_and_verify_account,
)
from p2_migration.validation import validate_customer


class MigrationError(RuntimeError):
    """Customer migration could not complete."""


def migrate_customers(customers_table_to_read: str, *, load: bool) -> None:
    """Run the batches and explain expected database, request or validation failures."""
    try:
        process_customer_batches(customers_table_to_read, load=load)
    except DatabricksError:
        message = "Could not read customers from Databricks. Check the warehouse, table and access permissions."
        if load:
            message += " Earlier Salesforce writes may have completed; rerun with the same source IDs."
        raise MigrationError(message) from None
    except requests.RequestException:
        raise MigrationError(
            "A Salesforce request failed. Earlier writes may have completed; "
            "rerun with the same source IDs to save and verify them again."
        ) from None
    except (ValueError, RuntimeError) as error:
        raise MigrationError(str(error)) from None


def prepare_accounts(customers: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    """Prepare valid customers in this batch and report rejected records."""
    accounts = []
    for customer in customers:
        errors = validate_customer(customer)
        if errors:
            print(f"  Rejected customer {customer.get('customer_id')!r}: {'; '.join(errors)}")
            continue
        accounts.append(prepare_salesforce_account(customer))
    return accounts


def load_accounts(
    session: requests.Session, instance_url: str, accounts: Iterable[Mapping[str, object]]
) -> tuple[int, int]:
    """Save and verify this batch, returning counts of created and updated accounts."""
    created = 0
    updated = 0
    for account in accounts:
        print(f"  Loading customer {account[EXTERNAL_ID_FIELD]}")
        outcome, received_account = save_and_verify_account(session, instance_url, account)
        if outcome == "created":
            created += 1
        else:
            updated += 1
        print(f"  Account {received_account['Id']}: {outcome}, verified")
    return created, updated


def process_customer_batches(customers_table_to_read: str, *, load: bool) -> None:
    """Read customers in batches, then preview or save and verify their accounts."""
    batch_size = 6
    total_customers = 0
    rejected_customers = 0
    prepared_accounts = 0
    created_accounts = 0
    updated_accounts = 0

    with connect_to_databricks() as connection, requests.Session() as session:
        instance_url = ""
        if load:
            total, unique = count_customers(connection, customers_table_to_read)
            if total != unique:
                raise ValueError("Customer IDs must be present and unique before loading")
            instance_url = connect_to_salesforce(session)

        batches = fetch_customer_batches(connection, customers_table_to_read, batch_size)
        for batch_number, customers in enumerate(batches, start=1):
            total_customers += len(customers)
            print(f"Batch {batch_number}: {len(customers)} customers")
            accounts = prepare_accounts(customer.asDict() for customer in customers)
            rejected_customers += len(customers) - len(accounts)
            prepared_accounts += len(accounts)
            if load:
                created, updated = load_accounts(session, instance_url, accounts)
                created_accounts += created
                updated_accounts += updated

    print(f"Total customers retrieved: {total_customers}")
    print(f"Valid customers: {total_customers - rejected_customers}")
    print(f"Rejected customers: {rejected_customers}")
    print(f"Accounts prepared for Salesforce: {prepared_accounts}")
    if load:
        print(f"Accounts created: {created_accounts}")
        print(f"Accounts updated: {updated_accounts}")
        print(f"Accounts verified: {created_accounts + updated_accounts}")
