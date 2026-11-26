import argparse
from pathlib import Path

from dotenv import load_dotenv

from p1_preparation.prepare import PreparationError, prepare_migration
from p2_migration.migrate import MigrationError, migrate_records
from p2_migration.reconcile import ReconciliationError, reconcile_migration
from p2_migration.resume import resume_migration


def main(argv: list[str] | None = None) -> None:
    arguments = parse_arguments(argv)
    load_dotenv(Path(__file__).resolve().parent / ".env")

    if arguments.reconcile:
        reconcile_migration(arguments.reconcile)
        return
    if arguments.resume:
        resume_migration(arguments.resume)
        return

    tables = prepare_migration(load=arguments.load)
    migrate_records(tables, load=arguments.load)


def parse_arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview, load, reconcile or resume Salesforce records.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--load", action="store_true",
        help="Build and test snapshots, check destination models, then save and verify customers and products",
    )
    mode.add_argument(
        "--reconcile", type=Path, metavar="REPORT",
        help="Compare an uncertain record with its saved snapshot without changing records",
    )
    mode.add_argument(
        "--resume", type=Path, metavar="REPORT",
        help="Continue an interrupted load using its saved snapshot and report",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    try:
        main()
    except (PreparationError, MigrationError, ReconciliationError) as error:
        raise SystemExit(str(error)) from None
