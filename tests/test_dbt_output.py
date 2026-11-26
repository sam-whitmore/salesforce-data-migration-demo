import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock

from dbt.artifacts.schemas.run import RunExecutionResult
from dbt.events.types import NodeFinished, NodeStart
from dbt_common.events.base_types import msg_from_base_event

from p1_preparation.dbt_output import DbtProgress, print_dbt_summary


class DbtOutputTests(unittest.TestCase):
    # Real dbt events produce occasional progress, without a start/finish pair for every test.
    def test_groups_finished_tests_and_reports_completed_snapshots(self):
        # Arrange
        output = StringIO()
        progress = DbtProgress()
        node = {"node_name": "example_check", "resource_type": "test"}

        # Act
        with redirect_stdout(output):
            for _ in range(7):
                progress.on_event(msg_from_base_event(NodeStart(node_info=node)))
                progress.on_event(msg_from_base_event(NodeFinished(node_info=node, run_result={"status": "pass"})))
            progress.on_event(msg_from_base_event(NodeFinished(
                node_info={"node_name": "stage_customers", "resource_type": "model"},
                run_result={"status": "success"},
            )))

        # Assert
        self.assertEqual(progress.checks_finished, 7)
        self.assertEqual(output.getvalue().splitlines(), [
            "  Data and transformation checks finished: 5",
            "  Snapshot customers: success",
        ])

    # A compact summary must not hide warnings, failures, skipped checks or unknown outcomes.
    def test_summary_keeps_outcomes_distinct(self):
        # Arrange
        output = StringIO()
        outcomes = (
            ("model", "success"), ("unit_test", "pass"), ("test", "pass"),
            ("test", "warn"), ("test", "fail"), ("test", "error"),
            ("test", "skipped"), ("test", "unexpected"),
        )
        result = Mock(spec=RunExecutionResult, results=[
            SimpleNamespace(node=SimpleNamespace(resource_type=kind), status=status)
            for kind, status in outcomes
        ])

        # Act
        with redirect_stdout(output):
            print_dbt_summary(result)

        # Assert
        table_rows = [
            [cell.strip() for cell in line.strip("|").split("|")]
            for line in output.getvalue().splitlines() if line.startswith("|")
        ]
        self.assertEqual(table_rows, [
            ["Work", "Passed", "Warn", "Failed", "Skipped", "Other"],
            ["Snapshots", "1", "0", "0", "0", "0"],
            ["Transformation tests", "1", "0", "0", "0", "0"],
            ["Data checks", "1", "1", "2", "1", "1"],
        ])


if __name__ == "__main__":
    unittest.main()
