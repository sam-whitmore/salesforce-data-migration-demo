import json
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from dbt.artifacts.schemas.run import RunExecutionResult
from dbt.cli.main import dbtRunner


class PreparationError(RuntimeError):
    """Migration preparation could not complete."""


@dataclass(frozen=True)
class MigrationTables:
    """The customer source for preview, or all three tested snapshots for a load."""

    customers_table: str
    products_table: str | None = None
    licences_table: str | None = None


def prepare_migration(*, load: bool) -> MigrationTables:
    """Select the preview source or build and test the three migration snapshots."""
    databricks_customers_table = os.environ.get("DATABRICKS_CUSTOMERS_TABLE")
    if not databricks_customers_table:
        raise PreparationError("Missing setting: DATABRICKS_CUSTOMERS_TABLE")
    print("\n1. Preparation\n" + "=" * 60, flush=True)
    print(f"Source: {databricks_customers_table}", flush=True)
    if not load:
        print("Preview only: read the source without creating a snapshot.")
        return MigrationTables(customers_table=databricks_customers_table)

    databricks_customers_table_parts = databricks_customers_table.split(".")
    if len(databricks_customers_table_parts) != 3 or not all(databricks_customers_table_parts):
        raise PreparationError("DATABRICKS_CUSTOMERS_TABLE must contain catalog.schema.table")
    (
        databricks_customers_catalog,
        databricks_customers_schema,
        databricks_customers_table_name,
    ) = databricks_customers_table_parts

    # dbt masks variables with this prefix in its logs.
    databricks_token = os.environ.get("DATABRICKS_TOKEN")
    if not databricks_token:
        raise PreparationError("Missing setting: DATABRICKS_TOKEN")
    os.environ["DBT_ENV_SECRET_DATABRICKS_TOKEN"] = databricks_token
    snapshot_id = uuid4().hex
    customers_snapshot_table_name = f"customers_snapshot_{snapshot_id}"
    dbt_project_directory = str(Path(__file__).resolve().parent / "dbt")
    print("Building and testing customer, product and licence snapshots...\n", flush=True)
    dbt_build = dbtRunner().invoke([
        "build", # Build the selected model and run its associated tests
        "--project-dir", dbt_project_directory, # Where the dbt project lives
        "--profiles-dir", dbt_project_directory, # Where its connection configuration lives
        "--select", "stage_customers", "stage_products", "stage_licences", # Which models to build
        "--indirect-selection", "cautious", # Only test relationships whose tables are selected.
        "--vars", json.dumps({ # Variables supplied to this run
            "databricks_customers_catalog": databricks_customers_catalog,
            "databricks_customers_schema": databricks_customers_schema,
            "databricks_customers_table_name": databricks_customers_table_name,
            "customers_snapshot_table_name": customers_snapshot_table_name,
            "products_snapshot_table_name": f"products_snapshot_{snapshot_id}",
            "licences_snapshot_table_name": f"licences_snapshot_{snapshot_id}",
        }),
    ])
    if not dbt_build.success:
        raise PreparationError("Migration preparation failed. See the dbt errors above.")
    if not isinstance(dbt_build.result, RunExecutionResult):
        raise PreparationError("dbt did not return build results")

    # Read each tested snapshot's full name from dbt so extraction uses the same tables.
    snapshot_tables = {}
    for entry in dbt_build.result.results:
        if entry.node.resource_type == "model" and entry.node.relation_name:
            snapshot_tables[entry.node.name] = entry.node.relation_name
            # `relation_name` is dbt's term for the full database name, including the catalog, schema and table.
    for model in ("stage_customers", "stage_products", "stage_licences"):
        if model not in snapshot_tables:
            raise PreparationError(f"dbt did not return the prepared snapshot for {model}")

    tables = MigrationTables(
        customers_table=snapshot_tables["stage_customers"],
        products_table=snapshot_tables["stage_products"],
        licences_table=snapshot_tables["stage_licences"],
    )
    print(f"\nCustomer snapshot: {tables.customers_table}", flush=True)
    print(f"Product snapshot: {tables.products_table}", flush=True)
    print(f"Licence snapshot: {tables.licences_table}", flush=True)
    return tables
