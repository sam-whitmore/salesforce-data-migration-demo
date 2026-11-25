import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
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
    current_customer_id: str | None = None

    @property
    def verified_accounts(self) -> int:
        return self.created_accounts + self.updated_accounts

    def write_event(self, event: str, **details: object) -> None:
        if self.file is not None:
            entry = {
                "event": event,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                **details,
            }
            self.file.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self.file.flush()

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
            accounts_verified=self.verified_accounts,
            unverified_customer_id=self.current_customer_id,
        )


@contextmanager
def open_run_report(customers_table_to_read: str, *, load: bool) -> Iterator[MigrationReport]:
    """Save load outcomes locally; previews only keep counters in memory."""
    if not load:
        yield MigrationReport()
        return

    REPORT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    report_path = REPORT_DIRECTORY / f"{uuid4()}.jsonl"
    with report_path.open("x", encoding="utf-8") as file:
        report = MigrationReport(file=file)
        report.write_event("run_started", customers_table=customers_table_to_read)
        print(f"Run report: {report_path}", flush=True)
        try:
            yield report
        finally:
            # A stopped run remains failed unless processing explicitly finishes.
            report.write_summary()
            print(f"\nRun outcome: {report.status}")
            print(f"Report saved: {report_path}")
