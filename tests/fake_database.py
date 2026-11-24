from contextlib import contextmanager
from itertools import islice


class FakeDatabase:
    """Supply records from memory to test batching without a database connection.

    Records can be simple labels because batching does not inspect their fields.
    SQL execution, column types and network behaviour are not simulated.
    """

    def __init__(self, customers):
        self.customers = iter(customers)
        self.parameters = {}
        self.query_count = 0
        self.fetch_count = 0

    @contextmanager
    def cursor(self):
        yield self

    def execute(self, query, parameters):
        self.parameters = parameters
        self.query_count += 1

    def fetchmany(self, size):
        self.fetch_count += 1
        return list(islice(self.customers, size))
