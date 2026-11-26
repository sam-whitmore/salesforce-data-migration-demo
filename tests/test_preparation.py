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

from p1_preparation.prepare import PreparationError, prepare_migration


class PrepareMigrationTests(unittest.TestCase):
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
        self.check_models = resources.enter_context(patch("p1_preparation.prepare.check_migration_models"))
        runner = resources.enter_context(patch("p1_preparation.prepare.dbtRunner"))
        self.invoke = runner.return_value.invoke
        self.invoke.return_value.success = True
        models = [
            SimpleNamespace(
                resource_type="model", name=f"stage_{entity}",
                relation_name=f"`example`.`prepared`.`{entity}_snapshot_00000000000000000000000000000001`",
            )
            for entity in ("customers", "products", "licences")
        ]
        self.invoke.return_value.result = Mock(
            spec=RunExecutionResult, results=[SimpleNamespace(node=model, status="success") for model in models],
        )

    # Preview selects the original Databricks table without invoking dbt or needing its token.
    def test_preview_returns_the_customers_table_without_building(self):
        # Arrange
        del os.environ["DATABRICKS_TOKEN"]

        # Act
        tables = prepare_migration(load=False)

        # Assert
        self.assertEqual(tables.customers_table, "example.raw.customers")
        self.assertIsNone(tables.products_table)
        self.assertIsNone(tables.licences_table)
        self.invoke.assert_not_called()
        self.check_models.assert_not_called()
        self.new_snapshot_id.assert_not_called()

    # All three tested snapshots are returned together, including the tables used by relationship checks.
    def test_build_passes_the_source_and_returns_all_tested_snapshots(self):
        # Arrange
        dbt_project_directory = str(Path(__file__).resolve().parents[1] / "p1_preparation" / "dbt")
        dbt_variables = {
            "databricks_customers_catalog": "example",
            "databricks_customers_schema": "raw",
            "databricks_customers_table_name": "customers",
            "customers_snapshot_table_name": "customers_snapshot_00000000000000000000000000000001",
            "products_snapshot_table_name": "products_snapshot_00000000000000000000000000000001",
            "licences_snapshot_table_name": "licences_snapshot_00000000000000000000000000000001",
        }

        # Act
        tables = prepare_migration(load=True)

        # Assert
        self.invoke.assert_called_once()
        arguments = self.invoke.call_args.args[0]
        self.assertEqual(arguments[:-1], [
            "build", "--log-level", "warn", "--log-level-file", "debug",
            "--log-path", str(Path(dbt_project_directory) / "logs"),
            "--project-dir", dbt_project_directory, "--profiles-dir", dbt_project_directory,
            "--select", "stage_customers", "stage_products", "stage_licences",
            "--indirect-selection", "cautious", "--vars",
        ])
        self.assertEqual(json.loads(arguments[-1]), dbt_variables)
        self.assertEqual(
            tables.customers_table,
            "`example`.`prepared`.`customers_snapshot_00000000000000000000000000000001`",
        )
        self.assertEqual(tables.products_table, "`example`.`prepared`.`products_snapshot_00000000000000000000000000000001`")
        self.assertEqual(tables.licences_table, "`example`.`prepared`.`licences_snapshot_00000000000000000000000000000001`")
        self.assertEqual(os.environ["DBT_ENV_SECRET_DATABRICKS_TOKEN"], "example-token")
        self.check_models.assert_called_once_with(tables)

    # Later loads must not rebuild a snapshot that an earlier load is still reading.
    def test_each_load_requests_a_different_snapshot_table(self):
        # Arrange
        self.new_snapshot_id.side_effect = [UUID(int=1), UUID(int=2)]

        # Act
        prepare_migration(load=True)
        prepare_migration(load=True)

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
        with self.assertRaisesRegex(PreparationError, "Migration preparation failed"):
            prepare_migration(load=True)
        self.check_models.assert_not_called()

    # An incomplete Databricks table name fails before dbt can write anything.
    def test_incomplete_customers_table_name_is_rejected(self):
        # Arrange
        os.environ["DATABRICKS_CUSTOMERS_TABLE"] = "customers"

        # Act and Assert
        with self.assertRaisesRegex(PreparationError, "catalog.schema.table"):
            prepare_migration(load=True)
        self.invoke.assert_not_called()

    # A successful command missing any one of the snapshots cannot start a migration.
    def test_missing_snapshot_is_reported(self):
        # Arrange
        results = self.invoke.return_value.result.results
        for missing in ("stage_customers", "stage_products", "stage_licences"):
            with self.subTest(missing=missing):
                self.invoke.return_value.result.results = [entry for entry in results if entry.node.name != missing]

                # Act and Assert
                with self.assertRaisesRegex(PreparationError, f"did not return.*{missing}"):
                    prepare_migration(load=True)

    # Missing settings are named without reading or displaying a credential file.
    def test_missing_customers_table_setting_is_reported(self):
        # Arrange
        del os.environ["DATABRICKS_CUSTOMERS_TABLE"]

        # Act and Assert
        with self.assertRaisesRegex(PreparationError, "Missing setting: DATABRICKS_CUSTOMERS_TABLE"):
            prepare_migration(load=False)
        self.invoke.assert_not_called()

    # Loading requires a token, while preview only selects the original Databricks table.
    def test_missing_token_prevents_building(self):
        # Arrange
        del os.environ["DATABRICKS_TOKEN"]

        # Act and Assert
        with self.assertRaisesRegex(PreparationError, "Missing setting: DATABRICKS_TOKEN"):
            prepare_migration(load=True)
        self.invoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()
