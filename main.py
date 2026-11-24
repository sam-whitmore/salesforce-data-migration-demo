import os
from pathlib import Path

from dotenv import load_dotenv

from customer_migration.mapping import prepare_salesforce_account
from customer_migration.source import (
    connect_to_databricks,
    fetch_customer_batches,
)
from customer_migration.validation import validate_customer


def main() -> None:
    load_dotenv(Path(__file__).resolve().parent / ".env")

    table_name = os.environ["DATABRICKS_CUSTOMERS_TABLE"]
    batch_size = 6
    total_customers = 0
    rejected_customers = 0
    prepared_accounts = 0

    with connect_to_databricks() as connection:
        batches = fetch_customer_batches(connection, table_name, batch_size)
        for batch_number, customers in enumerate(batches, start=1):
            total_customers += len(customers)
            print(f"Batch {batch_number}: {len(customers)} customers")
            accounts = []

            for customer in customers:
                customer_data = customer.asDict()
                errors = validate_customer(customer_data)
                if errors:
                    rejected_customers += 1
                    print(f"  Rejected customer {customer.customer_id!r}: {'; '.join(errors)}")
                    continue

                accounts.append(prepare_salesforce_account(customer_data))

            prepared_accounts += len(accounts)

    print(f"Total customers retrieved: {total_customers}")
    print(f"Valid customers: {total_customers - rejected_customers}")
    print(f"Rejected customers: {rejected_customers}")
    print(f"Accounts prepared for Salesforce: {prepared_accounts}")


if __name__ == "__main__":
    main()
