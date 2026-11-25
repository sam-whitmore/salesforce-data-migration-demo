from os import SEEK_END
from pathlib import Path

import requests
from databricks.sql.exc import Error as DatabricksError

from p2_migration.migrate import MigrationError, process_customer_batches
from p2_migration.reconcile import check_destinations, read_recorded_run
from p2_migration.reporting import MigrationReport, lock_report


def resume_customers(report_path: Path) -> None:
    """Continue the original snapshot and append progress to its locked report."""
    print("\nResume migration\n" + "=" * 60, flush=True)
    try:
        with report_path.open("r+", encoding="utf-8") as file, lock_report(file):
            recorded_run = read_recorded_run(report_path)
            check_destinations(recorded_run)
            if recorded_run.last_status == "complete":
                print("This run is already complete; no accounts were loaded.")
                return
            file.seek(0, SEEK_END)
            report = MigrationReport(
                file=file,
                previously_verified_accounts=len(recorded_run.verified_customer_ids),
                current_customer_id=recorded_run.uncertain_customer_id,
            )
            report.write_event("resume_started")
            print(f"Run report: {report_path}", flush=True)
            print(f"Saved snapshot: {recorded_run.customers_table}")
            try:
                process_customer_batches(
                    recorded_run.customers_table, load=True, report=report,
                    verified_customer_ids=recorded_run.verified_customer_ids,
                )
            finally:
                report.write_summary()
                print(f"\nRun outcome: {report.status}")
                print(f"Report saved: {report_path}")
    except DatabricksError:
        raise MigrationError("Could not read the saved Databricks snapshot; the resume stopped") from None
    except requests.RequestException:
        raise MigrationError("A Salesforce request failed; inspect the report before resuming again") from None
    except OSError:
        raise MigrationError("Could not read or save the run report; check local storage and permissions") from None
    except (ValueError, RuntimeError) as error:
        raise MigrationError(str(error)) from None
