import json
import os
from dataclasses import dataclass
from pathlib import Path

import requests
from databricks.sql.exc import Error as DatabricksError

from p2_migration.databricks import connect_to_databricks, fetch_customer
from p2_migration.licences import compare_snapshot_licence
from p2_migration.mapping import prepare_salesforce_account
from p2_migration.products import compare_snapshot_product
from p2_migration.salesforce import (
    CUSTOMER_ID_FIELD,
    RecordUnavailableError,
    compare_fields,
    connect_to_salesforce,
    read_account,
)
from p2_migration.validation import validate_customer


class ReconciliationError(RuntimeError):
    """The comparison could not confirm matching record fields."""


@dataclass(frozen=True)
class RecordedRun:
    customers_table: str
    uncertain_customer_id: str | None
    databricks_server_hostname: str | None
    salesforce_my_domain_url: str | None
    verified_customer_ids: frozenset[str] = frozenset()
    last_status: str | None = None
    products_table: str | None = None
    uncertain_product_id: str | None = None
    verified_product_ids: frozenset[str] = frozenset()
    licences_table: str | None = None
    uncertain_licence_id: str | None = None
    verified_licence_ids: frozenset[str] = frozenset()


def read_recorded_run(report_path: Path) -> RecordedRun:
    """Read sequential load events without treating a missing summary as success."""
    customers_table = None
    databricks_server_hostname = None
    salesforce_my_domain_url = None
    pending = None
    finished = False
    resumed = False
    verified = {"customer": set(), "product": set(), "licence": set()}
    licences_table = None
    products_table = None
    last_status = None

    with report_path.open(encoding="utf-8") as report:
        for line_number, line in enumerate(report, start=1):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                raise ValueError(f"Invalid JSON on report line {line_number}; inspect the report") from None
            if not line.endswith("\n"):
                raise ValueError("The report ends with an incomplete line; inspect it before continuing")
            if not isinstance(entry, dict):
                raise ValueError(f"Unexpected record on report line {line_number}")
            event = entry.get("event")
            if not isinstance(event, str):
                raise ValueError(f"Missing event name on report line {line_number}")
            if line_number == 1:
                customers_table = entry.get("customers_table")
                if event != "run_started" or not isinstance(customers_table, str) or not customers_table.strip():
                    raise ValueError("The report must start with a run_started entry naming its snapshot")
                table_parts = customers_table.split(".")
                if len(table_parts) != 3 or not table_parts[-1].strip("`").startswith("customers_snapshot_"):
                    raise ValueError("Reconciliation requires a retained customers_snapshot table, not the original source")
                # Older customer-only reports also named a product snapshot, but did not load it.
                load_products = entry.get("load_products", False)
                if not isinstance(load_products, bool):
                    raise ValueError("The product load scope must be true or false")
                if load_products:
                    products_table = entry.get("products_table")
                    if not isinstance(products_table, str):
                        raise ValueError("Product loading requires its saved snapshot")
                    parts = products_table.split(".")
                    if len(parts) != 3 or not parts[-1].strip("`").startswith("products_snapshot_"):
                        raise ValueError("Product recovery requires a retained products_snapshot table")
                load_licences = entry.get("load_licences", False)
                if not isinstance(load_licences, bool):
                    raise ValueError("The licence load scope must be true or false")
                if load_licences:
                    licences_table = entry.get("licences_table")
                    if products_table is None or not isinstance(licences_table, str):
                        raise ValueError("Licence recovery requires product loading and a saved licence snapshot")
                    parts = licences_table.split(".")
                    if len(parts) != 3 or not parts[-1].strip("`").startswith("licences_snapshot_"):
                        raise ValueError("Licence recovery requires a retained licences_snapshot table")
                databricks_server_hostname = entry.get("databricks_server_hostname")
                salesforce_my_domain_url = entry.get("salesforce_my_domain_url")
                for address in (databricks_server_hostname, salesforce_my_domain_url):
                    if address is not None and not isinstance(address, str):
                        raise ValueError("Recorded destination addresses must be text")
                continue

            if event == "resume_started":
                if last_status == "complete":
                    raise ValueError("A completed run must not contain a resume")
                finished = False
                resumed = True
                last_status = None
                continue
            if finished:
                raise ValueError(f"Unexpected record on report line {line_number}")

            progress_events = {
                "account_load_started": ("customer", "started"),
                "account_verified": ("customer", "verified"),
                "account_reconciled": ("customer", "reconciled"),
                "product_load_started": ("product", "started"),
                "product_verified": ("product", "verified"),
                "product_reconciled": ("product", "reconciled"),
                "licence_load_started": ("licence", "started"),
                "licence_verified": ("licence", "verified"),
                "licence_reconciled": ("licence", "reconciled"),
            }
            if event in progress_events:
                entity, action = progress_events[event]
                if entity == "product" and products_table is None:
                    raise ValueError("The report does not include product loading")
                if entity == "licence" and licences_table is None:
                    raise ValueError("The report does not include licence loading")
                source_id = entry.get(f"{entity}_id")
                if not isinstance(source_id, str) or not source_id.strip():
                    raise ValueError(f"Missing {entity} ID on report line {line_number}")
                if source_id in verified[entity]:
                    raise ValueError(f"The report repeats an already verified {entity}")
                if action == "started":
                    if pending is not None:
                        raise ValueError("The report contains overlapping load attempts")
                    pending = (entity, source_id)
                else:
                    if action == "reconciled" and not resumed:
                        raise ValueError("Reconciled progress requires a resume entry")
                    if pending != (entity, source_id):
                        raise ValueError("Verification has no matching attempt; older or inconsistent reports need manual inspection")
                    pending = None
                    verified[entity].add(source_id)
            elif event in ("customer_rejected", "product_rejected", "licence_rejected"):
                if event == "product_rejected" and products_table is None:
                    raise ValueError("The report does not include product loading")
                if event == "licence_rejected" and licences_table is None:
                    raise ValueError("The report does not include licence loading")
                if pending is not None:
                    raise ValueError("The report rejects a record during an unfinished load attempt")
            elif event == "run_finished":
                for entity in ("customer", "product", "licence"):
                    unverified = entry.get(f"unverified_{entity}_id")
                    if unverified is not None and pending != (entity, unverified):
                        raise ValueError("The summary has no matching attempt; inspect the report manually")
                finished = True
                last_status = entry.get("status")
                if last_status not in (None, "complete", "incomplete", "failed"):
                    raise ValueError("The report contains an unknown run outcome")
                if last_status == "complete" and pending is not None:
                    raise ValueError("The report claims completion with an unresolved attempt")
            else:
                raise ValueError(f"Unknown or misplaced event on report line {line_number}")

    if customers_table is None:
        raise ValueError("The report is empty")
    return RecordedRun(
        customers_table=customers_table,
        uncertain_customer_id=pending[1] if pending and pending[0] == "customer" else None,
        databricks_server_hostname=databricks_server_hostname,
        salesforce_my_domain_url=salesforce_my_domain_url,
        verified_customer_ids=frozenset(verified["customer"]), last_status=last_status,
        products_table=products_table,
        uncertain_product_id=pending[1] if pending and pending[0] == "product" else None,
        verified_product_ids=frozenset(verified["product"]),
        licences_table=licences_table,
        uncertain_licence_id=pending[1] if pending and pending[0] == "licence" else None,
        verified_licence_ids=frozenset(verified["licence"]),
    )


def check_destinations(recorded_run: RecordedRun) -> None:
    """Compare the recorded addresses with current settings before contacting either service."""
    destinations = (
        ("DATABRICKS_SERVER_HOSTNAME", recorded_run.databricks_server_hostname),
        ("SALESFORCE_MY_DOMAIN_URL", recorded_run.salesforce_my_domain_url),
    )
    for setting, recorded_address in destinations:
        if not recorded_address or not recorded_address.strip():
            raise ValueError(f"The report does not record {setting}; inspect this older or incomplete run manually")
        if os.environ.get(setting) != recorded_address:
            raise ValueError(f"{setting} does not match the run report; use the original connection settings")


def reconcile_migration(report_path: Path) -> None:
    """Inspect one run's uncertain attempt without changing data or its report."""
    print("\nRead-only reconciliation\n" + "=" * 60, flush=True)
    try:
        recorded_run = read_recorded_run(report_path)
        print(f"Report: {report_path}")
        print(f"Saved snapshot: {recorded_run.customers_table}")
        if all(source_id is None for source_id in (
            recorded_run.uncertain_customer_id, recorded_run.uncertain_product_id, recorded_run.uncertain_licence_id,
        )):
            print("No recorded uncertain attempt to check. This does not establish a complete migration.")
            return
        check_destinations(recorded_run)
        if recorded_run.uncertain_licence_id is not None:
            if recorded_run.licences_table is None:
                raise ValueError("Licence recovery requires its saved snapshot")
            print(f"Licence: {recorded_run.uncertain_licence_id}")
            print(f"Saved licence snapshot: {recorded_run.licences_table}")
            compare_snapshot_licence(recorded_run.licences_table, recorded_run.uncertain_licence_id)
            print("Result: all 7 mapped licence fields, including both links, currently match. No records were changed.")
        elif recorded_run.uncertain_product_id is not None:
            if recorded_run.products_table is None:
                raise ValueError("Product recovery requires its saved snapshot")
            print(f"Product: {recorded_run.uncertain_product_id}")
            print(f"Saved product snapshot: {recorded_run.products_table}")
            compare_snapshot_product(recorded_run.products_table, recorded_run.uncertain_product_id)
            print("Result: all 4 mapped product fields currently match. No records were changed.")
        else:
            compare_uncertain_account(recorded_run)
    except DatabricksError:
        raise ReconciliationError("Could not read the saved Databricks snapshot; check its availability and access") from None
    except requests.RequestException:
        raise ReconciliationError("A Salesforce request failed; the record remains unresolved") from None
    except OSError:
        raise ReconciliationError("Could not read the run report; check its path and permissions") from None
    except (ValueError, RuntimeError) as error:
        raise ReconciliationError(str(error)) from None


def compare_uncertain_account(recorded_run: RecordedRun) -> None:
    """Compare current mapped fields, without inferring which write produced them."""
    customer_id = recorded_run.uncertain_customer_id
    if customer_id is None:
        raise ValueError("An uncertain customer ID is required")
    print(f"Customer: {customer_id}")
    print(f"Databricks workspace: {recorded_run.databricks_server_hostname}")
    print(f"Salesforce domain: {recorded_run.salesforce_my_domain_url}")
    print("Connection addresses match the run report.")

    with connect_to_databricks() as connection, requests.Session() as session:
        expected = prepare_snapshot_account(connection, recorded_run.customers_table, customer_id)
        instance_url = connect_to_salesforce(session)
        print(f"Salesforce instance: {instance_url}")
        read_matching_account(session, instance_url, expected)
    print("Result: all 5 mapped fields currently match the saved snapshot.")
    print("No records were changed. This does not prove which request saved the account.")


def prepare_snapshot_account(
    connection, customers_table: str, customer_id: str,
) -> dict[str, object]:
    """Prepare one valid snapshot customer using the normal migration rules."""
    customer = fetch_customer(connection, customers_table, customer_id)
    errors = validate_customer(customer)
    if errors:
        raise ValueError("Snapshot customer is invalid: " + "; ".join(errors))
    return prepare_salesforce_account(customer)


def read_matching_account(
    session: requests.Session, instance_url: str, expected: dict[str, object],
) -> dict[str, object]:
    """Return the Account only when its mapped fields match the snapshot."""
    customer_id = str(expected[CUSTOMER_ID_FIELD])
    try:
        actual = read_account(session, instance_url, customer_id)
    except RecordUnavailableError:
        raise ReconciliationError(
            "Account unavailable (HTTP 404). It may be missing or inaccessible; no write was attempted."
        ) from None
    differences = compare_fields(expected, actual)
    if differences:
        raise ReconciliationError("Account differs from the snapshot: " + ", ".join(differences))
    return actual
