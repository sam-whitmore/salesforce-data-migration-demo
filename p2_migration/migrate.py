from collections.abc import Iterable, Mapping

import requests
from databricks.sql.exc import Error as DatabricksError

from p1_preparation.prepare import MigrationTables
from p2_migration.databricks import (
    connect_to_databricks,
    count_customers,
    fetch_customer_batches,
)
from p2_migration.licences import compare_snapshot_licence, process_licence_batches
from p2_migration.mapping import prepare_salesforce_account
from p2_migration.products import compare_snapshot_product, process_product_batches
from p2_migration.reconcile import prepare_snapshot_account, read_matching_account
from p2_migration.reporting import MigrationReport, open_run_report
from p2_migration.salesforce import (
    CUSTOMER_ID_FIELD,
    connect_to_salesforce,
    save_and_verify_account,
)
from p2_migration.validation import validate_customer


class MigrationError(RuntimeError):
    """Migration could not complete."""


def migrate_records(tables: MigrationTables, *, load: bool) -> None:
    """Run the batches and explain expected database, request or validation failures."""
    phase = "Migration" if load else "Preview"
    print(f"\n2. {phase}\n" + "=" * 60, flush=True)
    if load:
        print("Load customers, products and licences; read each record back to verify its mapped fields.", flush=True)
    try:
        with open_run_report(
            tables.customers_table, load=load,
            products_table=tables.products_table, licences_table=tables.licences_table,
            load_products=load and tables.products_table is not None,
            load_licences=load and tables.licences_table is not None,
        ) as report:
            process_migration_batches(tables, load=load, report=report)
    except DatabricksError:
        message = "Could not read migration data from Databricks. Check the warehouse, table and access permissions."
        if load:
            message += " Earlier Salesforce writes may have completed; inspect the report before resuming."
        raise MigrationError(message) from None
    except requests.RequestException:
        raise MigrationError(
            "A Salesforce request failed. Earlier writes may have completed; "
            "inspect the run report and Salesforce record before resuming."
        ) from None
    except OSError:
        raise MigrationError(
            "Could not save the migration report. Check local storage and permissions. "
            "Earlier Salesforce writes may have completed."
        ) from None
    except (ValueError, RuntimeError) as error:
        raise MigrationError(str(error)) from None


def process_migration_batches(
    tables: MigrationTables, *, load: bool, report: MigrationReport,
    verified_customer_ids: frozenset[str] = frozenset(),
    verified_product_ids: frozenset[str] = frozenset(),
    verified_licence_ids: frozenset[str] = frozenset(),
) -> None:
    """Resolve uncertainty before further writes, then load parents before licences."""
    if load and tables.licences_table is not None and tables.products_table is None:
        raise ValueError("Licence loading requires the product stage")
    if report.current_licence_id is not None:
        if tables.licences_table is None:
            raise ValueError("Licence recovery requires its saved snapshot")
        licence_id = report.current_licence_id
        received = compare_snapshot_licence(tables.licences_table, licence_id)
        report.record_reconciled_licence(received["Id"])
        verified_licence_ids = verified_licence_ids | {licence_id}
        print(f"  Licence {licence_id}: reconciled, save not repeated", flush=True)

    if report.current_product_id is not None:
        if tables.products_table is None:
            raise ValueError("Product recovery requires its saved snapshot")
        product_id = report.current_product_id
        received = compare_snapshot_product(tables.products_table, product_id)
        report.record_reconciled_product(received["Id"])
        verified_product_ids = verified_product_ids | {product_id}
        print(f"  Product {product_id}: reconciled, save not repeated", flush=True)

    print("\n2.1 Customers\n" + "-" * 60, flush=True)
    process_customer_batches(
        tables.customers_table, load=load, report=report,
        verified_customer_ids=verified_customer_ids,
    )
    if load and tables.products_table is not None:
        process_product_batches(tables.products_table, report, verified_product_ids)
    if load and tables.licences_table is not None:
        if report.rejected_customers or report.rejected_products:
            print("\nLicences not loaded: correct the rejected customer or product records first.", flush=True)
        else:
            process_licence_batches(tables.licences_table, report, verified_licence_ids)
    finish_migration(
        report, load=load,
        products_included=load and tables.products_table is not None,
        licences_included=load and tables.licences_table is not None,
    )


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
        report.record_load_started(str(account[CUSTOMER_ID_FIELD]))
        outcome, received_account = save_and_verify_account(session, instance_url, account)
        report.record_verified_account(received_account["Id"], outcome)
        print(f"  {outcome.capitalize()}, verified | {account['Name']}", flush=True)


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
            print("Checking customer IDs and connecting to Salesforce...", flush=True)
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


def check_recorded_customers(connection, customers_table: str, customer_ids: set[str]) -> None:
    """Refuse to continue if a previously verified customer disappeared from the snapshot."""
    missing = customer_ids.copy()
    for customers in fetch_customer_batches(connection, customers_table, batch_size=6):
        missing.difference_update(customer.customer_id for customer in customers)
    if missing:
        raise ValueError("Previously verified customers are missing from the saved snapshot; inspect it before resuming")


def finish_migration(
    report: MigrationReport, *, load: bool, products_included: bool, licences_included: bool = False,
) -> None:
    """Summarise the full run only after all included stages finish."""
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
    if products_included:
        print(f"Products retrieved: {report.total_products}")
        print(f"Products rejected: {report.rejected_products}")
        print(f"Products created: {report.created_products}")
        print(f"Products updated: {report.updated_products}")
        if report.previously_verified_products or report.reconciled_products:
            print(f"Products previously verified: {report.previously_verified_products}")
            print(f"Products reconciled without saving: {report.reconciled_products}")
        print(f"Products verified: {report.verified_products}")
    if licences_included:
        print(f"Licences retrieved: {report.total_licences}")
        print(f"Licences rejected: {report.rejected_licences}")
        print(f"Licences created: {report.created_licences}")
        print(f"Licences updated: {report.updated_licences}")
        if report.previously_verified_licences or report.reconciled_licences:
            print(f"Licences previously verified: {report.previously_verified_licences}")
            print(f"Licences reconciled without saving: {report.reconciled_licences}")
        print(f"Licences verified: {report.verified_licences}")
    rejected = report.rejected_customers + report.rejected_products + report.rejected_licences
    report.status = "incomplete" if rejected else "complete"
    if load and rejected:
        raise MigrationError(
            f"Migration incomplete: {rejected} record(s) rejected. "
            "See the run report for their IDs and reasons."
        )
