import argparse
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from customer_migration.mapping import prepare_salesforce_account
from customer_migration.salesforce import (
    connect_to_salesforce,
    save_and_verify_account,
)
from customer_migration.source import connect_to_databricks, fetch_customer
from customer_migration.validation import validate_customer


def main() -> None:
    parser = argparse.ArgumentParser(description="Save one customer to Salesforce and check it.")
    parser.add_argument("--customer-id", required=True, help="The source customer UUID")
    arguments = parser.parse_args()
    load_dotenv(Path(__file__).resolve().parent / ".env")

    with connect_to_databricks() as connection:
        customer = fetch_customer(
            connection, os.environ["DATABRICKS_CUSTOMERS_TABLE"], arguments.customer_id
        ).asDict()

    errors = validate_customer(customer)
    if errors:
        raise ValueError("Customer rejected: " + "; ".join(errors))
    account = prepare_salesforce_account(customer)

    with requests.Session() as session:
        instance_url = connect_to_salesforce(session)
        outcome, received_account = save_and_verify_account(session, instance_url, account)

    print(f"Salesforce account {outcome}.")
    print(f"Verified: all {len(account)} mapped fields match.")
    print(f"Salesforce account ID: {received_account['Id']}")


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException:
        raise SystemExit(
            "A Salesforce request failed. A write may have completed; "
            "rerun with the same customer ID to save and verify it again."
        ) from None
    except (ValueError, RuntimeError, KeyError) as error:
        raise SystemExit(str(error)) from None
