import argparse
from pathlib import Path

from dotenv import load_dotenv

from p1_preparation.prepare import PreparationError, prepare_customers
from p2_migration.migrate import MigrationError, migrate_customers
from p2_migration.reconcile import ReconciliationError, reconcile_customers
from p2_migration.resume import resume_customers


def main(argv: list[str] | None = None) -> None:
    arguments = parse_arguments(argv)
    load_dotenv(Path(__file__).resolve().parent / ".env")

    if arguments.reconcile:
        reconcile_customers(arguments.reconcile)
        return
    if arguments.resume:
        resume_customers(arguments.resume)
        return

    customers_table_to_read = prepare_customers(load=arguments.load)
    migrate_customers(customers_table_to_read, load=arguments.load)


def parse_arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview, load, reconcile or resume Salesforce customers.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--load", action="store_true",
        help="Build and test a customer snapshot, then save and verify Salesforce accounts",
    )
    mode.add_argument(
        "--reconcile", type=Path, metavar="REPORT",
        help="Compare an uncertain account with its saved snapshot without changing records",
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
