import json
import os
import unittest
from contextlib import ExitStack, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID

from dbt.artifacts.schemas.run import RunExecutionResult

from p1_preparation.prepare import PreparationError, prepare_customers


class PrepareCustomersTests(unittest.TestCase):
    def setUp(self):
        """Provide example settings and replace dbt without reading local credentials."""
        resources = ExitStack()
        self.addCleanup(resources.close)
        resources.enter_context(redirect_stdout(StringIO()))
        resources.enter_context(patch.dict(os.environ, {
            "DATABRICKS_CUSTOMERS_TABLE": "example.raw.customers",
            "DATABRICKS_TOKEN": "example-token",
        }, clear=True))
        self.new_snapshot_id = resources.enter_context(patch(
            "p1_preparation.prepare.uuid4", return_value=UUID(int=1),
        ))
        runner = resources.enter_context(patch("p1_preparation.prepare.dbtRunner"))
        self.invoke = runner.return_value.invoke
        self.invoke.return_value.success = True
        model = SimpleNamespace(
            resource_type="model", name="stage_customers",
            relation_name="`example`.`prepared`.`customers_snapshot_00000000000000000000000000000001`",
        )
        self.invoke.return_value.result = Mock(
            spec=RunExecutionResult, results=[SimpleNamespace(node=model)],
        )

    # Preview selects the original Databricks table without invoking dbt or needing its token.
    def test_preview_returns_the_customers_table_without_building(self):
        # Arrange
        del os.environ["DATABRICKS_TOKEN"]

        # Act
        customers_table_to_read = prepare_customers(load=False)

        # Assert
        self.assertEqual(customers_table_to_read, "example.raw.customers")
        self.invoke.assert_not_called()
        self.new_snapshot_id.assert_not_called()

    # The build names a separate snapshot and returns the table dbt actually tested.
    def test_build_passes_the_customers_table_and_returns_the_tested_snapshot(self):
        # Arrange
        dbt_project_directory = str(Path(__file__).resolve().parents[1] / "p1_preparation" / "dbt")
        dbt_variables = {
            "databricks_customers_catalog": "example",
            "databricks_customers_schema": "raw",
            "databricks_customers_table_name": "customers",
            "customers_snapshot_table_name": "customers_snapshot_00000000000000000000000000000001",
        }

        # Act
        customers_table_to_read = prepare_customers(load=True)

        # Assert
        self.invoke.assert_called_once()
        arguments = self.invoke.call_args.args[0]
        self.assertEqual(arguments[:-1], [
            "build", "--project-dir", dbt_project_directory, "--profiles-dir", dbt_project_directory,
            "--select", "stage_customers", "--vars",
        ])
        self.assertEqual(json.loads(arguments[-1]), dbt_variables)
        self.assertEqual(
            customers_table_to_read,
            "`example`.`prepared`.`customers_snapshot_00000000000000000000000000000001`",
        )
        self.assertEqual(os.environ["DBT_ENV_SECRET_DATABRICKS_TOKEN"], "example-token")

    # Later loads must not rebuild a snapshot that an earlier load is still reading.
    def test_each_load_requests_a_different_snapshot_table(self):
        # Arrange
        self.new_snapshot_id.side_effect = [UUID(int=1), UUID(int=2)]

        # Act
        prepare_customers(load=True)
        prepare_customers(load=True)

        # Assert
        snapshot_names = []
        for call in self.invoke.call_args_list:
            arguments = call.args[0]
            variables = json.loads(arguments[arguments.index("--vars") + 1])
            snapshot_names.append(variables["customers_snapshot_table_name"])
        self.assertEqual(snapshot_names, [
            "customers_snapshot_00000000000000000000000000000001",
            "customers_snapshot_00000000000000000000000000000002",
        ])

    # Failed builds and data checks never supply a snapshot to the migration.
    def test_failed_build_raises_an_error(self):
        # Arrange
        self.invoke.return_value.success = False

        # Act and Assert
        with self.assertRaisesRegex(PreparationError, "Customer preparation failed"):
            prepare_customers(load=True)

    # An incomplete Databricks table name fails before dbt can write anything.
    def test_incomplete_customers_table_name_is_rejected(self):
        # Arrange
        os.environ["DATABRICKS_CUSTOMERS_TABLE"] = "customers"

        # Act and Assert
        with self.assertRaisesRegex(PreparationError, "catalog.schema.table"):
            prepare_customers(load=True)
        self.invoke.assert_not_called()

    # A successful command without the expected model cannot start a migration.
    def test_missing_snapshot_is_reported(self):
        # Arrange
        self.invoke.return_value.result.results = []

        # Act and Assert
        with self.assertRaisesRegex(PreparationError, "did not return"):
            prepare_customers(load=True)

    # Missing settings are named without reading or displaying a credential file.
    def test_missing_customers_table_setting_is_reported(self):
        # Arrange
        del os.environ["DATABRICKS_CUSTOMERS_TABLE"]

        # Act and Assert
        with self.assertRaisesRegex(PreparationError, "Missing setting: DATABRICKS_CUSTOMERS_TABLE"):
            prepare_customers(load=False)
        self.invoke.assert_not_called()

    # Loading requires a token, while preview only selects the original Databricks table.
    def test_missing_token_prevents_building(self):
        # Arrange
        del os.environ["DATABRICKS_TOKEN"]

        # Act and Assert
        with self.assertRaisesRegex(PreparationError, "Missing setting: DATABRICKS_TOKEN"):
            prepare_customers(load=True)
        self.invoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()
