from collections.abc import Iterable, Mapping

import requests
from databricks.sql.exc import Error as DatabricksError

from p2_migration.databricks import (
    connect_to_databricks,
    count_customers,
    fetch_customer_batches,
)
from p2_migration.mapping import prepare_salesforce_account
from p2_migration.reconcile import prepare_snapshot_account, read_matching_account
from p2_migration.reporting import MigrationReport, open_run_report
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
    phase = "Migration" if load else "Preview"
    print(f"\n2. {phase}\n" + "=" * 60, flush=True)
    try:
        with open_run_report(customers_table_to_read, load=load) as report:
            process_customer_batches(customers_table_to_read, load=load, report=report)
    except DatabricksError:
        message = "Could not read customers from Databricks. Check the warehouse, table and access permissions."
        if load:
            message += " Earlier Salesforce writes may have completed; inspect the report before resuming."
        raise MigrationError(message) from None
    except requests.RequestException:
        raise MigrationError(
            "A Salesforce request failed. Earlier writes may have completed; "
            "inspect the run report and Salesforce account before resuming."
        ) from None
    except OSError:
        raise MigrationError(
            "Could not save the migration report. Check local storage and permissions. "
            "Earlier Salesforce writes may have completed."
        ) from None
    except (ValueError, RuntimeError) as error:
        raise MigrationError(str(error)) from None


def prepare_accounts(
    customers: Iterable[Mapping[str, object]], report: MigrationReport
) -> list[dict[str, object]]:
    """Prepare valid customers in this batch and report rejected records."""
    accounts = []
    for customer in customers:
        errors = validate_customer(customer)
        if errors:
            report.reject_customer(customer.get("customer_id"), errors)
            print(f"  Rejected customer {customer.get('customer_id')!r}")
            for error in errors:
                print(f"    - {error}")
            continue
        accounts.append(prepare_salesforce_account(customer))
        report.prepared_accounts += 1
    return accounts


def load_accounts(
    session: requests.Session,
    instance_url: str,
    accounts: Iterable[Mapping[str, object]],
    report: MigrationReport,
) -> None:
    """Record each attempt before saving, then record successful verification."""
    for account in accounts:
        report.record_load_started(str(account[EXTERNAL_ID_FIELD]))
        print(f"  Loading customer {account[EXTERNAL_ID_FIELD]}", flush=True)
        outcome, received_account = save_and_verify_account(session, instance_url, account)
        report.record_verified_account(received_account["Id"], outcome)
        print(f"    {outcome.capitalize()}, verified | Salesforce Account: {received_account['Id']}")


def process_customer_batches(
    customers_table_to_read: str, *, load: bool, report: MigrationReport,
    verified_customer_ids: frozenset[str] = frozenset(),
) -> None:
    """Read customers in batches, then preview or save and verify their accounts."""
    batch_size = 6
    accounts_to_skip = set(verified_customer_ids)

    with connect_to_databricks() as connection, requests.Session() as session:
        instance_url = ""
        if load:
            total, unique = count_customers(connection, customers_table_to_read)
            if total != unique:
                raise ValueError("Customer IDs must be present and unique before loading")
            if accounts_to_skip:
                check_recorded_customers(connection, customers_table_to_read, accounts_to_skip)
            instance_url = connect_to_salesforce(session)
            if report.current_customer_id is not None:
                customer_id = report.current_customer_id
                expected = prepare_snapshot_account(connection, customers_table_to_read, customer_id)
                received = read_matching_account(session, instance_url, expected)
                report.record_reconciled_account(received["Id"])
                accounts_to_skip.add(customer_id)
                print(f"  Customer {customer_id}: reconciled, save not repeated")

        batches = fetch_customer_batches(connection, customers_table_to_read, batch_size)
        for batch_number, customers in enumerate(batches, start=1):
            report.total_customers += len(customers)
            print(f"\nBatch {batch_number}: {len(customers)} customers")
            accounts = prepare_accounts(
                (customer.asDict() for customer in customers if customer.customer_id not in accounts_to_skip),
                report,
            )
            if load:
                load_accounts(session, instance_url, accounts, report)

    print("\nSummary\n" + "-" * 60)
    print(f"Total customers retrieved: {report.total_customers}")
    print(f"Valid customers: {report.total_customers - report.rejected_customers}")
    print(f"Rejected customers: {report.rejected_customers}")
    print(f"Accounts prepared for Salesforce: {report.prepared_accounts}")
    if load:
        print(f"Accounts created: {report.created_accounts}")
        print(f"Accounts updated: {report.updated_accounts}")
        if report.previously_verified_accounts or report.reconciled_accounts:
            print(f"Accounts previously verified: {report.previously_verified_accounts}")
            print(f"Accounts reconciled without saving: {report.reconciled_accounts}")
        print(f"Accounts verified: {report.verified_accounts}")
    report.status = "incomplete" if report.rejected_customers else "complete"
    if load and report.rejected_customers:
        raise MigrationError(
            f"Migration incomplete: {report.rejected_customers} customer(s) rejected. "
            "See the run report for their IDs and reasons."
        )


def check_recorded_customers(connection, customers_table: str, customer_ids: set[str]) -> None:
    """Refuse to continue if a previously verified customer disappeared from the snapshot."""
    missing = customer_ids.copy()
    for customers in fetch_customer_batches(connection, customers_table, batch_size=6):
        missing.difference_update(customer.customer_id for customer in customers)
    if missing:
        raise ValueError("Previously verified customers are missing from the saved snapshot; inspect it before resuming")
