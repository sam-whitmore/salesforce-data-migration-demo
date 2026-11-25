import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from textwrap import dedent


class ReportPersistenceTests(unittest.TestCase):
    # An abrupt process exit bypasses finally, but the pending attempt must still be visible.
    def test_write_intent_survives_exit_without_a_final_summary(self):
        # Arrange
        repository = Path(__file__).resolve().parents[1]
        child_program = dedent("""
            import os
            import sys
            from pathlib import Path
            from p2_migration import reporting

            reporting.REPORT_DIRECTORY = Path(sys.argv[1])
            with reporting.open_run_report("test.migration.customers_snapshot_example", load=True) as report:
                report.record_load_started("interrupted-customer")
                os._exit(23)  # Exit immediately, without closing files or running finally.
        """)

        with TemporaryDirectory() as directory:
            # Act: the child only writes a local report; it makes no service calls.
            process = subprocess.run(
                [sys.executable, "-c", child_program, directory],
                cwd=repository,
                env={**os.environ, "PYTHON_DOTENV_DISABLED": "1"},
                capture_output=True,
                timeout=10,
            )
            reports = list(Path(directory).glob("*.jsonl"))

            # Assert
            self.assertEqual(process.returncode, 23)
            self.assertEqual(len(reports), 1)
            events = [json.loads(line) for line in reports[0].read_text(encoding="utf-8").splitlines()]
            self.assertEqual([event["event"] for event in events], ["run_started", "account_load_started"])
            self.assertEqual(events[-1]["customer_id"], "interrupted-customer")


if __name__ == "__main__":
    unittest.main()
