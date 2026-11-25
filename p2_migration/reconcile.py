import json
import os
from dataclasses import dataclass
from pathlib import Path

import requests
from databricks.sql.exc import Error as DatabricksError

from p2_migration.databricks import connect_to_databricks, fetch_customer
from p2_migration.mapping import prepare_salesforce_account
from p2_migration.salesforce import (
    EXTERNAL_ID_FIELD,
    AccountUnavailableError,
    compare_account,
    connect_to_salesforce,
    read_account,
)
from p2_migration.validation import validate_customer


class ReconciliationError(RuntimeError):
    """The comparison could not confirm matching customer fields."""


@dataclass(frozen=True)
class RecordedRun:
    customers_table: str
    uncertain_customer_id: str | None
    databricks_server_hostname: str | None
    salesforce_my_domain_url: str | None
    verified_customer_ids: frozenset[str] = frozenset()
    last_status: str | None = None


def read_recorded_run(report_path: Path) -> RecordedRun:
    """Read sequential load events without treating a missing summary as success."""
    customers_table = None
    databricks_server_hostname = None
    salesforce_my_domain_url = None
    pending_customer_id = None
    finished = False
    resumed = False
    verified_customer_ids = set()
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
            if line_number == 1:
                customers_table = entry.get("customers_table")
                if event != "run_started" or not isinstance(customers_table, str) or not customers_table.strip():
                    raise ValueError("The report must start with a run_started entry naming its snapshot")
                table_parts = customers_table.split(".")
                if len(table_parts) != 3 or not table_parts[-1].strip("`").startswith("customers_snapshot_"):
                    raise ValueError("Reconciliation requires a retained customers_snapshot table, not the original source")
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

            if event in ("account_load_started", "account_verified", "account_reconciled"):
                customer_id = entry.get("customer_id")
                if not isinstance(customer_id, str) or not customer_id.strip():
                    raise ValueError(f"Missing customer ID on report line {line_number}")
                if customer_id in verified_customer_ids:
                    raise ValueError("The report repeats an already verified customer")
                if event == "account_load_started":
                    if pending_customer_id is not None:
                        raise ValueError("The report contains overlapping account attempts")
                    pending_customer_id = customer_id
                else:
                    if event == "account_reconciled" and not resumed:
                        raise ValueError("Reconciled progress requires a resume entry")
                    if customer_id != pending_customer_id:
                        raise ValueError("Verification has no matching attempt; older or inconsistent reports need manual inspection")
                    pending_customer_id = None
                    verified_customer_ids.add(customer_id)
            elif event == "customer_rejected":
                if pending_customer_id is not None:
                    raise ValueError("The report rejects a customer during an unfinished account attempt")
            elif event == "run_finished":
                unverified = entry.get("unverified_customer_id")
                if unverified is not None and unverified != pending_customer_id:
                    raise ValueError("The summary has no matching attempt; inspect the report manually")
                finished = True
                last_status = entry.get("status")
                if last_status not in (None, "complete", "incomplete", "failed"):
                    raise ValueError("The report contains an unknown run outcome")
                if last_status == "complete" and pending_customer_id is not None:
                    raise ValueError("The report claims completion with an unresolved attempt")
            else:
                raise ValueError(f"Unknown or misplaced event on report line {line_number}")

    if customers_table is None:
        raise ValueError("The report is empty")
    return RecordedRun(
        customers_table, pending_customer_id, databricks_server_hostname, salesforce_my_domain_url,
        frozenset(verified_customer_ids), last_status,
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


def reconcile_customers(report_path: Path) -> None:
    """Inspect one run's uncertain attempt without changing data or its report."""
    print("\nRead-only reconciliation\n" + "=" * 60, flush=True)
    try:
        recorded_run = read_recorded_run(report_path)
        print(f"Report: {report_path}")
        print(f"Saved snapshot: {recorded_run.customers_table}")
        if recorded_run.uncertain_customer_id is None:
            print("No recorded uncertain attempt to check. This does not establish a complete migration.")
            return
        check_destinations(recorded_run)
        compare_uncertain_account(recorded_run)
    except DatabricksError:
        raise ReconciliationError("Could not read the saved Databricks snapshot; check its availability and access") from None
    except requests.RequestException:
        raise ReconciliationError("A Salesforce request failed; the account remains unresolved") from None
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
    customer_id = str(expected[EXTERNAL_ID_FIELD])
    try:
        actual = read_account(session, instance_url, customer_id)
    except AccountUnavailableError:
        raise ReconciliationError(
            "Account unavailable (HTTP 404). It may be missing or inaccessible; no write was attempted."
        ) from None
    differences = compare_account(expected, actual)
    if differences:
        raise ReconciliationError("Account differs from the snapshot: " + ", ".join(differences))
    return actual
