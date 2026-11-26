from dataclasses import dataclass

from dbt.artifacts.schemas.run import RunExecutionResult
from dbt_common.events.base_types import EventMsg

from shared.console import print_table


@dataclass
class DbtProgress:
    """Show milestones while dbt runs; its warnings, errors and file logs stay enabled."""

    checks_finished: int = 0

    def on_event(self, event: EventMsg) -> None:
        if event.info.name != "NodeFinished":
            return
        node = getattr(event.data, "node_info")
        result = getattr(event.data, "run_result")
        if node.resource_type == "model":
            name = node.node_name.removeprefix("stage_")
            print(f"  Snapshot {name}: {result.status}", flush=True)
        elif node.resource_type in ("test", "unit_test"):
            self.checks_finished += 1
            if self.checks_finished % 5 == 0:
                print(f"  Data and transformation checks finished: {self.checks_finished}", flush=True)


def print_dbt_summary(result: RunExecutionResult) -> None:
    """Summarise actual outcomes, keeping warnings, failures and skipped checks distinct."""
    groups = {"model": "Snapshots", "unit_test": "Transformation tests", "test": "Data checks"}
    counts = {label: [0, 0, 0, 0, 0] for label in groups.values()}
    status_columns = {"success": 0, "pass": 0, "warn": 1, "fail": 2, "error": 2, "skipped": 3}
    for entry in result.results:
        label = groups.get(entry.node.resource_type, "Other tasks")
        totals = counts.setdefault(label, [0, 0, 0, 0, 0])
        totals[status_columns.get(entry.status, 4)] += 1

    print("\ndbt results", flush=True)
    print_table(
        ("Work", "Passed", "Warn", "Failed", "Skipped", "Other"),
        [(label, *(str(count) for count in totals)) for label, totals in counts.items()],
    )
