import os
from pathlib import Path

from dotenv import load_dotenv

from customer_migration.source import (
    connect_to_databricks,
    fetch_customer_batches,
)


def main() -> None:
    load_dotenv(Path(__file__).resolve().parent / ".env")

    table_name = os.environ["DATABRICKS_CUSTOMERS_TABLE"]
    batch_size = 6
    total_customers = 0

    with connect_to_databricks() as connection:
        batches = fetch_customer_batches(connection, table_name, batch_size)
        for batch_number, customers in enumerate(batches, start=1):
            total_customers += len(customers)
            print(f"Batch {batch_number}: {len(customers)} customers")

    print(f"Total customers retrieved: {total_customers}")


if __name__ == "__main__":
    main()
