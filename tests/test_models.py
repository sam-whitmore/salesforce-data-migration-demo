import os
import unittest
from contextlib import ExitStack, nullcontext, redirect_stdout
from copy import deepcopy
from io import StringIO
from typing import Any
from unittest.mock import Mock, patch

import requests
from databricks.sql.exc import OperationalError

from p1_preparation.check_models import (
    ModelCheckError,
    check_migration_models,
    check_object_setup,
)
from p1_preparation.prepare import MigrationTables


def field(name, field_type, **attributes) -> dict[str, Any]:
    """A visible, writable field as returned by Salesforce's Describe API."""
    return {"name": name, "type": field_type, "createable": True, "updateable": True, **attributes}


def example_descriptions() -> dict[str, Any]:
    """Keep this example independent of the production expectations."""
    source_id = {"length": 36, "externalId": True, "unique": True, "caseSensitive": False}
    fields = {
        "Account": [
            field("Source_Customer_ID__c", "string", **source_id),
            field("Name", "string"),
            field("BillingCity", "string"),
            field("BillingPostalCode", "string"),
            field("BillingCountryCode", "picklist"),
        ],
        "Product2": [
            field("Source_Product_ID__c", "string", **source_id),
            field("ProductCode", "string"),
            field("Name", "string"),
            field("IsActive", "boolean"),
        ],
        "Licence__c": [
            field("Name", "string", autoNumber=True, createable=False, updateable=False),
            field("Source_Licence_ID__c", "string", **source_id),
            field("Customer__c", "reference", referenceTo=["Account"], nillable=False, relationshipOrder=None),
            field("Product__c", "reference", referenceTo=["Product2"], nillable=True, relationshipOrder=None),
            field("Seats__c", "double", precision=9, scale=0, nillable=False),
            field("Starts_On__c", "date", nillable=False),
            field("Ends_Before__c", "date", nillable=False),
            field("Cancelled_On__c", "date", nillable=True),
        ],
    }
    descriptions = {
        name: {"fields": values, "queryable": True, "createable": True, "updateable": True}
        for name, values in fields.items()
    }
    for parent, lookup, restricted in (("Account", "Customer__c", True), ("Product2", "Product__c", False)):
        descriptions[parent]["childRelationships"] = [{
            "childSObject": "Licence__c", "field": lookup,
            "restrictedDelete": restricted, "cascadeDelete": False,
        }]
    return descriptions


class MigrationModelTests(unittest.TestCase):
    def setUp(self):
        resources = ExitStack()
        self.addCleanup(resources.close)
        self.output = StringIO()
        resources.enter_context(redirect_stdout(self.output))
        resources.enter_context(patch.dict(os.environ, {
            "SALESFORCE_MY_DOMAIN_URL": "https://example.my.salesforce.com",
            "SALESFORCE_CLIENT_ID": "example-client",
            "SALESFORCE_CLIENT_SECRET": "example-secret",
        }, clear=True))
        self.tables = MigrationTables("test.prepared.customers", "test.prepared.products", "test.prepared.licences")
        self.database = Mock()
        resources.enter_context(patch(
            "p1_preparation.check_models.connect_to_databricks", return_value=nullcontext(self.database),
        ))
        self.source_columns = {
            "test.prepared.customers": {
                "customer_id": "string", "legal_name": "string", "billing_city": "string",
                "billing_postal_code": "string", "billing_country_code": "string",
            },
            "test.prepared.products": {
                "product_id": "string", "product_code": "string", "product_name": "string", "is_active": "boolean",
            },
            "test.prepared.licences": {
                "licence_id": "string", "customer_id": "string", "product_id": "string", "seats": "bigint",
                "starts_on": "date", "ends_before": "date", "cancelled_on": "date",
            },
        }
        self.describe_table = resources.enter_context(patch(
            "p1_preparation.check_models.describe_table",
            side_effect=lambda connection, table: self.source_columns[table],
        ))
        self.sleep = resources.enter_context(patch("p2_migration.retries.sleep"))
        session_factory = resources.enter_context(patch("p1_preparation.check_models.requests.Session"))
        self.session = session_factory.return_value.__enter__.return_value
        self.session.post.return_value = Mock(status_code=200)
        self.session.post.return_value.json.return_value = {
            "instance_url": "https://example.my.salesforce.com", "access_token": "example-token",
        }
        self.descriptions = example_descriptions()
        self.session.get.side_effect = [
            Mock(status_code=200, json=Mock(return_value=description))
            for description in self.descriptions.values()
        ]

    # Model checks use the supplied snapshots and only read Salesforce metadata.
    def test_check_reads_all_three_models_without_writing_records(self):
        # Arrange
        expected_urls = [
            f"https://example.my.salesforce.com/services/data/v68.0/sobjects/{name}/describe"
            for name in ("Account", "Product2", "Licence__c")
        ]

        # Act
        check_migration_models(self.tables)

        # Assert
        self.session.post.assert_called_once()
        self.assertTrue(self.session.post.call_args.args[0].endswith("/services/oauth2/token"))
        self.assertEqual([call.args[0] for call in self.session.get.call_args_list], expected_urls)
        for call in self.session.get.call_args_list:
            self.assertEqual(call.kwargs, {"timeout": 30, "allow_redirects": False})
        self.assertEqual([call[0] for call in self.session.method_calls], ["post", "get", "get", "get"])
        self.assertEqual([call.args[1] for call in self.describe_table.call_args_list], list(self.source_columns))
        self.assertIn("Model checks passed", self.output.getvalue())
        self.assertRegex(self.output.getvalue(), r"\| Databricks field\s+\| Type\s+\| Salesforce field\s+\| Type\s+\|")
        self.assertRegex(self.output.getvalue(), r"\| billing_postal_code\s+\| string\s+\| BillingPostalCode\s+\| string\s+\|")
        self.assertIn("customer Accounts only", self.output.getvalue())

    # Object permissions and field visibility/editability can fail independently.
    def test_reports_missing_fields_and_denied_permissions_together(self):
        # Arrange
        product = self.descriptions["Product2"]
        product["createable"] = False
        product["updateable"] = False
        product["queryable"] = False
        product["fields"] = [value for value in product["fields"] if value["name"] != "Source_Product_ID__c"]
        product["fields"][0]["updateable"] = False

        # Act
        problems = check_object_setup("Product2", product)

        # Assert
        self.assertEqual(len(problems), 5)
        self.assertTrue(any("Source_Product_ID__c: missing or not visible" in problem for problem in problems))
        self.assertTrue(any("ProductCode: updateable" in problem for problem in problems))

    # Each differing field setting is identified, including optional Product and cancellation fields.
    def test_reports_incompatible_field_definitions(self):
        # Arrange
        cases = (
            ("Source_Licence_ID__c", "externalId", False),
            ("Source_Licence_ID__c", "unique", False),
            ("Source_Licence_ID__c", "caseSensitive", True),
            ("Source_Licence_ID__c", "length", 18),
            ("Source_Licence_ID__c", "createable", False),
            ("Customer__c", "referenceTo", ["Contact"]),
            ("Customer__c", "relationshipOrder", 0),
            ("Customer__c", "nillable", True),
            ("Product__c", "nillable", False),
            ("Seats__c", "precision", 8),
            ("Seats__c", "scale", 2),
            ("Starts_On__c", "type", "datetime"),
            ("Ends_Before__c", "nillable", True),
            ("Cancelled_On__c", "nillable", False),
            ("Name", "autoNumber", False),
        )
        for name, attribute, value in cases:
            with self.subTest(field=name, attribute=attribute):
                description = deepcopy(self.descriptions["Licence__c"])
                changed_field = next(item for item in description["fields"] if item["name"] == name)
                changed_field[attribute] = value

                # Act
                problems = check_object_setup("Licence__c", description)

                # Assert
                self.assertEqual(len(problems), 1)
                self.assertIn(f"Licence__c.{name}: {attribute}", problems[0])

    # Parent deletion must neither erase licences nor contradict Product's lookup limitation.
    def test_checks_parent_deletion_behaviour_and_missing_relationships(self):
        # Arrange
        for parent in ("Account", "Product2"):
            for change in ("missing", "cascadeDelete", "restrictedDelete"):
                with self.subTest(parent=parent, change=change):
                    description = deepcopy(self.descriptions[parent])
                    if change == "missing":
                        description["childRelationships"] = []
                    else:
                        relationship = description["childRelationships"][0]
                        relationship[change] = not relationship[change]

                    # Act
                    problems = check_object_setup(parent, description)

                    # Assert
                    self.assertEqual(len(problems), 1)
                    self.assertTrue(problems[0].startswith(parent + ":"))

    # A wrong source type or missing column prevents success, including on empty snapshots.
    def test_source_schema_mismatches_stop_model_checks(self):
        # Arrange
        self.source_columns["test.prepared.licences"]["seats"] = "double"
        del self.source_columns["test.prepared.licences"]["ends_before"]

        # Act
        with self.assertRaisesRegex(ModelCheckError, "2 issue"):
            check_migration_models(self.tables)

        # Assert
        self.assertIn("seats: expected bigint, found double", self.output.getvalue())
        self.assertIn("ends_before: expected date, found missing column", self.output.getvalue())
        self.assertNotIn("Model checks passed", self.output.getvalue())

    # Failed requests stop the check without printing private HTTP details or claiming success.
    def test_request_failures_are_sanitised(self):
        # Arrange
        for failure in (403, requests.ReadTimeout("private request details")):
            with self.subTest(failure=type(failure).__name__):
                self.output.seek(0)
                self.output.truncate()
                if isinstance(failure, int):
                    self.session.get.side_effect = None
                    self.session.get.return_value = Mock(status_code=failure, text="private response details")
                else:
                    self.session.get.side_effect = failure

                # Act
                with self.assertRaises(ModelCheckError) as raised:
                    check_migration_models(self.tables)

                # Assert
                self.assertNotIn("private", str(raised.exception))
                self.assertNotIn("Model checks passed", self.output.getvalue())
                if isinstance(failure, int):
                    self.assertIn("Account (HTTP 403)", str(raised.exception))

    # A database failure cannot fall through to loading or expose private connection details.
    def test_databricks_failure_is_sanitised(self):
        # Arrange
        self.describe_table.side_effect = OperationalError("private connection details")

        # Act
        with self.assertRaisesRegex(ModelCheckError, "Could not inspect the Databricks snapshot") as raised:
            check_migration_models(self.tables)

        # Assert
        self.assertNotIn("private", str(raised.exception))
        self.session.get.assert_not_called()
        self.assertNotIn("Model checks passed", self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
