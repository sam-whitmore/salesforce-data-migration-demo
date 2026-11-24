import json
import os
from pathlib import Path

from dbt.artifacts.schemas.run import RunExecutionResult
from dbt.cli.main import dbtRunner


class PreparationError(RuntimeError):
    """Customer preparation could not complete."""


def prepare_customers(*, load: bool) -> str:
    """Select the original Databricks table for preview or build its prepared view for loading."""
    databricks_customers_table = os.environ.get("DATABRICKS_CUSTOMERS_TABLE")
    if not databricks_customers_table:
        raise PreparationError("Missing setting: DATABRICKS_CUSTOMERS_TABLE")
    if not load:
        return databricks_customers_table

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
    dbt_project_directory = str(Path(__file__).resolve().parent / "dbt")
    dbt_build = dbtRunner().invoke([
        "build",
        "--project-dir", dbt_project_directory,
        "--profiles-dir", dbt_project_directory,
        "--select", "stage_customers",
        "--vars", json.dumps({
            "databricks_customers_catalog": databricks_customers_catalog,
            "databricks_customers_schema": databricks_customers_schema,
            "databricks_customers_table_name": databricks_customers_table_name,
        }),
    ])
    if not dbt_build.success:
        raise PreparationError("Customer preparation failed. See the dbt errors above.")
    if not isinstance(dbt_build.result, RunExecutionResult):
        raise PreparationError("dbt did not return build results")

    # Read the actual view name from dbt so the loader follows its configuration.
    for entry in dbt_build.result.results:
        if entry.node.resource_type == "model" and entry.node.name == "stage_customers":
            if entry.node.relation_name:
                return entry.node.relation_name
    raise PreparationError("dbt did not return the prepared customer view")
