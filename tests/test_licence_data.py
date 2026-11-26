import unittest
from contextlib import nullcontext
from datetime import date, datetime
from unittest.mock import Mock

from databricks.sql.types import Row

from p2_migration.databricks import count_licences, fetch_licence, fetch_licence_batches
from p2_migration.mapping import prepare_salesforce_licence
from p2_migration.validation import validate_licence
from tests.fake_database import FakeDatabase


def licence(**changes):
    return {
        "licence_id": "licence-1", "customer_id": "customer-1", "product_id": "product-1",
        "seats": 25, "starts_on": date(2026, 1, 1), "ends_before": date(2027, 1, 1),
        "cancelled_on": None, **changes,
    }


class LicenceDataTests(unittest.TestCase):
    # Business dates retain their meaning; the payload uses Salesforce parent IDs and explicit nulls.
    def test_maps_terms_and_relationships_without_changing_the_source(self):
        for cancellation in (None, date(2026, 1, 1), date(2026, 12, 31)):
            with self.subTest(cancellation=cancellation):
                # Arrange
                source = licence(cancelled_on=cancellation)
                original = source.copy()

                # Act
                errors = validate_licence(source)
                fields = prepare_salesforce_licence(source, customer_account_id="sf-account", salesforce_product_id="sf-product")

                # Assert
                self.assertEqual(errors, [])
                self.assertEqual(fields, {
                    "Source_Licence_ID__c": "licence-1", "Customer__c": "sf-account", "Product__c": "sf-product",
                    "Seats__c": 25, "Starts_On__c": "2026-01-01", "Ends_Before__c": "2027-01-01",
                    "Cancelled_On__c": cancellation.isoformat() if cancellation else None,
                })
                self.assertNotIn("Name", fields)
                self.assertEqual(source, original)

    # Reject invalid IDs, fractional seats, timestamp/text dates and terms outside the agreed bounds.
    def test_rejects_values_that_do_not_fit_the_licence_model(self):
        cases = []
        for field in ("licence_id", "customer_id", "product_id"):
            cases.extend({field: value} for value in (None, "", " ", 123, "x" * 37))
        cases.extend({"seats": value} for value in (None, True, 0, -1, 1.5, "25", 1000000000))
        for field in ("starts_on", "ends_before", "cancelled_on"):
            cases.extend({field: value} for value in ("2026-06-01", datetime(2026, 6, 1), 123))
        cases.extend([
            {"starts_on": None}, {"ends_before": None},
            {"ends_before": date(2026, 1, 1)}, {"ends_before": date(2025, 12, 31)},
            {"cancelled_on": date(2025, 12, 31)}, {"cancelled_on": date(2027, 1, 1)},
        ])
        for changes in cases:
            with self.subTest(changes=changes):
                # Arrange
                source = licence(**changes)

                # Act
                errors = validate_licence(source)

                # Assert
                self.assertTrue(errors)

    # Extraction yields bounded batches from the selected snapshot without fetching ahead.
    def test_fetches_licences_in_batches_and_binds_the_table_name(self):
        # Arrange
        rows = [Row(**licence(licence_id=f"licence-{number}")) for number in range(3)]
        database = FakeDatabase(rows)
        batches = fetch_licence_batches(database, "test.migration.licences_snapshot_example", 2)

        # Act and Assert
        self.assertEqual(database.fetch_count, 0)
        self.assertEqual(next(batches), rows[:2])
        self.assertEqual(database.fetch_count, 1)
        self.assertEqual(list(batches), [rows[2:]])
        self.assertEqual(database.parameters, {"licences_table_to_read": "test.migration.licences_snapshot_example"})
        for size in (0, -1):
            with self.subTest(size=size), self.assertRaises(ValueError):
                list(fetch_licence_batches(database, "unused", size))

    # Recovery must find exactly one original licence, while ID checks preserve total/distinct counts.
    def test_count_and_single_record_reads_bind_the_selected_snapshot(self):
        # Arrange
        table = "test.migration.licences_snapshot_example"
        row = Row(**licence())
        cursor = Mock()
        cursor.fetchone.return_value = (3, 2)
        connection = Mock()
        connection.cursor.return_value = nullcontext(cursor)

        # Act and Assert
        self.assertEqual(count_licences(connection, table), (3, 2))
        self.assertEqual(cursor.execute.call_args.kwargs["parameters"], {"licences_table_to_read": table})
        for rows in ([], [row], [row, row]):
            with self.subTest(matches=len(rows)):
                database = FakeDatabase(rows)
                if len(rows) == 1:
                    self.assertEqual(fetch_licence(database, table, "licence-1"), row.asDict())
                else:
                    with self.assertRaisesRegex(ValueError, "exactly one"):
                        fetch_licence(database, table, "licence-1")
                self.assertEqual(database.parameters, {"licences_table_to_read": table, "licence_id": "licence-1"})
