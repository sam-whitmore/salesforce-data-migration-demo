import argparse
from pathlib import Path

from dotenv import load_dotenv

from p1_preparation.prepare import PreparationError, prepare_customers
from p2_migration.migrate import MigrationError, migrate_customers


def main(argv: list[str] | None = None) -> None:
    arguments = parse_arguments(argv)
    load_dotenv(Path(__file__).resolve().parent / ".env")

    customers_table_to_read = prepare_customers(load=arguments.load)
    migrate_customers(customers_table_to_read, load=arguments.load)


def parse_arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview customers or load them into Salesforce.")
    parser.add_argument(
        "--load", action="store_true",
        help="Build and test the customer view, then save and verify Salesforce accounts",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    try:
        main()
    except (PreparationError, MigrationError) as error:
        raise SystemExit(str(error)) from None
