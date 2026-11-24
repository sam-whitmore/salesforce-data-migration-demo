import argparse
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from customer_migration.pipeline import load_accounts, prepare_accounts
from customer_migration.salesforce import connect_to_salesforce
from customer_migration.source import (
    connect_to_databricks,
    count_customers,
    fetch_customer_batches,
)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Preview customers or load them into Salesforce.")
    parser.add_argument("--load", action="store_true", help="Save and verify valid customers in Salesforce")
    arguments = parser.parse_args(argv)
    load_dotenv(Path(__file__).resolve().parent / ".env")

    table_name = os.environ["DATABRICKS_CUSTOMERS_TABLE"]
    batch_size = 6
    total_customers = 0
    rejected_customers = 0
    prepared_accounts = 0
    created_accounts = 0
    updated_accounts = 0

    with connect_to_databricks() as connection, requests.Session() as session:
        instance_url = ""
        if arguments.load:
            total, unique = count_customers(connection, table_name)
            if total != unique:
                raise ValueError("Customer IDs must be present and unique before loading")
            instance_url = connect_to_salesforce(session)

        batches = fetch_customer_batches(connection, table_name, batch_size)
        for batch_number, customers in enumerate(batches, start=1):
            total_customers += len(customers)
            print(f"Batch {batch_number}: {len(customers)} customers")
            accounts = prepare_accounts(customer.asDict() for customer in customers)
            rejected_customers += len(customers) - len(accounts)
            prepared_accounts += len(accounts)
            if arguments.load:
                created, updated = load_accounts(session, instance_url, accounts)
                created_accounts += created
                updated_accounts += updated

    print(f"Total customers retrieved: {total_customers}")
    print(f"Valid customers: {total_customers - rejected_customers}")
    print(f"Rejected customers: {rejected_customers}")
    print(f"Accounts prepared for Salesforce: {prepared_accounts}")
    if arguments.load:
        print(f"Accounts created: {created_accounts}")
        print(f"Accounts updated: {updated_accounts}")
        print(f"Accounts verified: {created_accounts + updated_accounts}")


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException:
        raise SystemExit(
            "A Salesforce request failed. Earlier writes may have completed; "
            "rerun with the same source IDs to save and verify them again."
        ) from None
    except (ValueError, RuntimeError, KeyError) as error:
        raise SystemExit(str(error)) from None
