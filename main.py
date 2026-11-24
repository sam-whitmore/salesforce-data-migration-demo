import os
from pathlib import Path

from dotenv import load_dotenv

from customer_migration.source import (
    connect_to_databricks,
    count_customers,
)


def main() -> None:
    load_dotenv(Path(__file__).resolve().parent / ".env")

    table_name = os.environ["DATABRICKS_CUSTOMERS_TABLE"]

    with connect_to_databricks() as connection:
        total, unique = count_customers(connection, table_name)

    print(f"Total customers: {total}")
    print(f"Unique customer IDs: {unique}")


if __name__ == "__main__":
    main()
