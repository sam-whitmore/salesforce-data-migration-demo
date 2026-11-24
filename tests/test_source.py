import unittest

from customer_migration.source import fetch_customer_batches
from tests.fake_database import FakeDatabase


class FetchCustomerBatchesTests(unittest.TestCase):
    def test_returns_every_customer_once_including_a_smaller_final_batch(self):
        database = FakeDatabase(customers=["customer-1", "customer-2", "customer-3"])

        batches = list(fetch_customer_batches(database, "test.customers", batch_size=2))

        self.assertEqual(batches, [["customer-1", "customer-2"], ["customer-3"]])
        self.assertEqual(database.query_count, 1)

    def test_fetches_the_next_batch_only_when_requested(self):
        database = FakeDatabase(customers=["customer-1", "customer-2", "customer-3"])
        batches = fetch_customer_batches(database, "test.customers", batch_size=2)

        self.assertEqual(database.fetch_count, 0)

        self.assertEqual(next(batches), ["customer-1", "customer-2"])
        self.assertEqual(database.fetch_count, 1)

        self.assertEqual(next(batches), ["customer-3"])
        self.assertEqual(database.fetch_count, 2)

        self.assertEqual(list(batches), [])

    def test_empty_table_returns_no_batches(self):
        database = FakeDatabase(customers=[])

        batches = list(fetch_customer_batches(database, "test.customers", batch_size=2))

        self.assertEqual(batches, [])

    def test_zero_batch_size_is_rejected(self):
        database = FakeDatabase(customers=[])

        with self.assertRaises(ValueError):
            list(fetch_customer_batches(database, "test.customers", batch_size=0))

        self.assertEqual(database.query_count, 0)

    def test_negative_batch_size_is_rejected(self):
        database = FakeDatabase(customers=[])

        with self.assertRaises(ValueError):
            list(fetch_customer_batches(database, "test.customers", batch_size=-1))

        self.assertEqual(database.query_count, 0)


if __name__ == "__main__":
    unittest.main()
