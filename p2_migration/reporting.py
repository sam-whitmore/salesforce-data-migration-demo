import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from errno import EACCES, EAGAIN
from fcntl import LOCK_EX, LOCK_NB, LOCK_UN, flock
from os import fsync
from pathlib import Path
from typing import TextIO
from uuid import uuid4

REPORT_DIRECTORY = Path(__file__).resolve().parents[1] / "reports"


@dataclass
class MigrationReport:
    """Count progress without keeping every customer in memory."""

    file: TextIO | None = None
    status: str = "failed"
    total_customers: int = 0
    rejected_customers: int = 0
    prepared_accounts: int = 0
    created_accounts: int = 0
    updated_accounts: int = 0
    previously_verified_accounts: int = 0
    reconciled_accounts: int = 0
    current_customer_id: str | None = None

    @property
    def verified_accounts(self) -> int:
        return (
            self.created_accounts + self.updated_accounts
            + self.previously_verified_accounts + self.reconciled_accounts
        )

    def write_event(self, event: str, **details: object) -> None:
        if self.file is not None:
            entry = {
                "event": event,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                **details,
            }
            self.file.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self.file.flush()
            fsync(self.file.fileno())

    def record_load_started(self, customer_id: str) -> None:
        """Persist the intent before saving; this does not prove Salesforce received it."""
        self.current_customer_id = customer_id
        self.write_event("account_load_started", customer_id=customer_id)

    def reject_customer(self, customer_id: object, errors: list[str]) -> None:
        self.rejected_customers += 1
        self.write_event(
            "customer_rejected",
            customer_id=None if customer_id is None else str(customer_id),
            reasons=errors,
        )

    def record_verified_account(self, account_id: object, outcome: str) -> None:
        if outcome == "created":
            self.created_accounts += 1
        else:
            self.updated_accounts += 1
        customer_id = self.current_customer_id
        self.current_customer_id = None
        self.write_event(
            "account_verified",
            customer_id=customer_id,
            account_id=account_id,
            outcome=outcome,
        )

    def write_summary(self) -> None:
        self.write_event(
            "run_finished",
            status=self.status,
            customers_retrieved=self.total_customers,
            customers_rejected=self.rejected_customers,
            accounts_prepared=self.prepared_accounts,
            accounts_created=self.created_accounts,
            accounts_updated=self.updated_accounts,
            accounts_previously_verified=self.previously_verified_accounts,
            accounts_reconciled=self.reconciled_accounts,
            accounts_verified=self.verified_accounts,
            unverified_customer_id=self.current_customer_id,
        )

    def record_reconciled_account(self, account_id: object) -> None:
        """Record matching current fields without claiming another create or update."""
        self.write_event(
            "account_reconciled", customer_id=self.current_customer_id, account_id=account_id,
        )
        self.reconciled_accounts += 1
        self.current_customer_id = None


@contextmanager
def lock_report(file: TextIO) -> Iterator[None]:
    """Keep one local process in charge of this report until its load stops."""
    try:
        flock(file.fileno(), LOCK_EX | LOCK_NB)
    except OSError as error:
        if error.errno in (EACCES, EAGAIN):
            raise RuntimeError("This run is already active; wait for its load or resume to stop") from None
        raise
    try:
        yield
    finally:
        flock(file.fileno(), LOCK_UN)


@contextmanager
def open_run_report(
    customers_table_to_read: str, *, load: bool,
    products_table: str | None = None, licences_table: str | None = None,
) -> Iterator[MigrationReport]:
    """Save load outcomes locally; previews only keep counters in memory."""
    if not load:
        yield MigrationReport()
        return

    REPORT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    report_path = REPORT_DIRECTORY / f"{uuid4()}.jsonl"
    with report_path.open("x", encoding="utf-8") as file, lock_report(file):
        report = MigrationReport(file=file)
        report.write_event(
            "run_started",
            customers_table=customers_table_to_read,
            products_table=products_table,
            licences_table=licences_table,
            databricks_server_hostname=os.environ.get("DATABRICKS_SERVER_HOSTNAME"),
            salesforce_my_domain_url=os.environ.get("SALESFORCE_MY_DOMAIN_URL"),
        )
        print(f"Run report: {report_path}", flush=True)
        try:
            yield report
        finally:
            # A stopped run remains failed unless processing explicitly finishes.
            report.write_summary()
            print(f"\nRun outcome: {report.status}")
            if report.current_customer_id is not None:
                print(f"Customer needing verification: {report.current_customer_id}")
            print(f"Report saved: {report_path}")
