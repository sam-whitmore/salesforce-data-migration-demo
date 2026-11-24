import unittest

from customer_migration.source import fetch_customer, fetch_customer_batches
from tests.fake_database import FakeDatabase


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

    # A zero batch size is rejected before any query runs.
    def test_zero_batch_size_is_rejected(self):
        # Arrange
        database = FakeDatabase(customers=[])

        # Act and Assert: the operation must raise ValueError.
        with self.assertRaises(ValueError):
            list(fetch_customer_batches(database, "test.customers", batch_size=0))

        # Assert
        self.assertEqual(database.query_count, 0)

    # A negative batch size is rejected before any query runs.
    def test_negative_batch_size_is_rejected(self):
        # Arrange
        database = FakeDatabase(customers=[])

        # Act and Assert: the operation must raise ValueError.
        with self.assertRaises(ValueError):
            list(fetch_customer_batches(database, "test.customers", batch_size=-1))

        # Assert
        self.assertEqual(database.query_count, 0)


class FetchCustomerTests(unittest.TestCase):
    # A single matching record can proceed to validation.
    def test_returns_one_matching_customer(self):
        # Arrange: the simulated query has returned one match.
        database = FakeDatabase(customers=["customer-1"])

        # Act
        customer = fetch_customer(database, "test.customers", "customer-1")

        # Assert
        self.assertEqual(customer, "customer-1")

    # A missing customer stops the load before Salesforce is contacted.
    def test_rejects_a_missing_customer(self):
        # Arrange
        database = FakeDatabase(customers=[])

        # Act and Assert
        with self.assertRaisesRegex(ValueError, "No customer found"):
            fetch_customer(database, "test.customers", "customer-1")

    # Duplicate source IDs must be resolved instead of choosing a record arbitrarily.
    def test_rejects_duplicate_source_customers(self):
        # Arrange
        database = FakeDatabase(customers=["customer-1", "customer-1"])

        # Act and Assert
        with self.assertRaisesRegex(ValueError, "More than one customer"):
            fetch_customer(database, "test.customers", "customer-1")


if __name__ == "__main__":
    unittest.main()
