import os
import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

from p2_migration.databricks import (
    connect_to_databricks,
    count_customers,
    fetch_customer_batches,
)
from tests.fake_database import FakeDatabase


class DatabricksConnectionTests(unittest.TestCase):
    # Incomplete settings fail before any connection is attempted.
    def test_missing_settings_are_reported_before_connecting(self):
        # Arrange
        with patch.dict(os.environ, {}, clear=True), patch("p2_migration.databricks.sql.connect") as connect:
            # Act and Assert
            with self.assertRaisesRegex(ValueError, "Missing Databricks settings: DATABRICKS_SERVER_HOSTNAME"):
                connect_to_databricks()
            connect.assert_not_called()


class FetchCustomerBatchesTests(unittest.TestCase):
    # Every customer appears once, including in the smaller final batch.
    def test_returns_every_customer_once_including_a_smaller_final_batch(self):
        # Arrange
        database = FakeDatabase(customers=["customer-1", "customer-2", "customer-3"])

        # Act
        batches = list(fetch_customer_batches(database, "test.customers", batch_size=2))

        # Assert
        self.assertEqual(batches, [["customer-1", "customer-2"], ["customer-3"]])
        self.assertEqual(database.query_count, 1)
        self.assertEqual(database.parameters, {"customers_table_to_read": "test.customers"})

    # Creating the generator does not fetch data; each request fetches one batch.
    def test_fetches_the_next_batch_only_when_requested(self):
        # Arrange
        database = FakeDatabase(customers=["customer-1", "customer-2", "customer-3"])

        # Act
        batches = fetch_customer_batches(database, "test.customers", batch_size=2)
        fetches_before_request = database.fetch_count

        first_batch = next(batches)
        fetches_after_first_request = database.fetch_count

        second_batch = next(batches)
        fetches_after_second_request = database.fetch_count

        remaining_batches = list(batches)

        # Assert
        self.assertEqual(fetches_before_request, 0)
        self.assertEqual(first_batch, ["customer-1", "customer-2"])
        self.assertEqual(fetches_after_first_request, 1)
        self.assertEqual(second_batch, ["customer-3"])
        self.assertEqual(fetches_after_second_request, 2)
        self.assertEqual(remaining_batches, [])

    # An empty table produces no batches.
    def test_empty_table_returns_no_batches(self):
        # Arrange
        database = FakeDatabase(customers=[])

        # Act
        batches = list(fetch_customer_batches(database, "test.customers", batch_size=2))

        # Assert
        self.assertEqual(batches, [])

    # Batch sizes must be positive; zero and negative values must not query the database.
    def test_non_positive_batch_sizes_are_rejected_before_querying(self):
        # Arrange
        invalid_sizes = (0, -1)

        for batch_size in invalid_sizes:
            with self.subTest(batch_size=batch_size):
                database = FakeDatabase(customers=[])

                # Act and Assert
                with self.assertRaises(ValueError):
                    list(fetch_customer_batches(database, "test.customers", batch_size=batch_size))
                self.assertEqual(database.query_count, 0)


class CountCustomersTests(unittest.TestCase):
    # Bind the chosen snapshot and keep total and distinct counts in the returned order.
    def test_binds_the_selected_table_and_preserves_count_result_order(self):
        # Arrange
        database = Mock()
        cursor = Mock()
        database.cursor.return_value = nullcontext(cursor)
        cursor.fetchone.return_value = (20, 19)
        table = "test.migration.customers_snapshot_example"

        # Act
        counts = count_customers(database, table)

        # Assert
        self.assertEqual(counts, (20, 19))
        cursor.execute.assert_called_once()
        self.assertEqual(cursor.execute.call_args.kwargs["parameters"], {"customers_table_to_read": table})
        cursor.fetchone.assert_called_once()


if __name__ == "__main__":
    unittest.main()
