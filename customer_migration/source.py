import os
from collections.abc import Iterator

from databricks import sql
from databricks.sql.types import Row


def connect_to_databricks():
    return sql.connect(
        server_hostname=os.environ["DATABRICKS_SERVER_HOSTNAME"],
        http_path=os.environ["DATABRICKS_HTTP_PATH"],
        access_token=os.environ["DATABRICKS_TOKEN"],
    )


def count_customers(connection, table_name: str) -> tuple[int, int]:
    query = """
        SELECT
            COUNT(*) AS total_customers,
            COUNT(DISTINCT customer_id) AS unique_customer_ids
        FROM IDENTIFIER(:table_name)
    """

    with connection.cursor() as cursor:
        cursor.execute(
            query,
            parameters={"table_name": table_name},
        )
        total_customers, unique_customer_ids = cursor.fetchone()

    return total_customers, unique_customer_ids


def fetch_customer_batches(
    connection, table_name: str, batch_size: int
) -> Iterator[list[Row]]:
    """Yield customer records in batches without collecting the full result."""
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")

    query = """
        SELECT
            customer_id,
            legal_name,
            billing_city,
            billing_postal_code,
            billing_country_code
        FROM IDENTIFIER(:table_name)
        ORDER BY customer_id
    """

    with connection.cursor() as cursor:
        cursor.execute(query, parameters={"table_name": table_name})

        while True:
            customers = cursor.fetchmany(batch_size)
            if not customers:
                break
            yield customers


def fetch_customer(connection, table_name: str, customer_id: str) -> Row:
    """Read one customer, rejecting missing or duplicate source IDs."""
    query = """
        SELECT
            customer_id,
            legal_name,
            billing_city,
            billing_postal_code,
            billing_country_code
        FROM IDENTIFIER(:table_name)
        WHERE customer_id = :customer_id
        LIMIT 2
    """

    with connection.cursor() as cursor:
        cursor.execute(query, parameters={
            "table_name": table_name,
            "customer_id": customer_id,
        })
        customers = cursor.fetchmany(2)

    if not customers:
        raise ValueError("No customer found with that source ID")
    if len(customers) > 1:
        raise ValueError("More than one customer has that source ID")
    return customers[0]
