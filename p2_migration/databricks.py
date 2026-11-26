import json
import os
from collections.abc import Iterator

from databricks import sql
from databricks.sql.types import Row


def connect_to_databricks():
    required = ("DATABRICKS_SERVER_HOSTNAME", "DATABRICKS_HTTP_PATH", "DATABRICKS_TOKEN")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise ValueError("Missing Databricks settings: " + ", ".join(missing))
    return sql.connect(
        server_hostname=os.environ["DATABRICKS_SERVER_HOSTNAME"],
        http_path=os.environ["DATABRICKS_HTTP_PATH"],
        access_token=os.environ["DATABRICKS_TOKEN"],
    )


def describe_table(connection, table_name: str) -> dict[str, str]:
    """Read column names and types from a prepared snapshot, without reading its records."""
    with connection.cursor() as cursor:
        cursor.execute(
            "DESCRIBE TABLE EXTENDED IDENTIFIER(:table_name) AS JSON",
            parameters={"table_name": table_name},
        )
        description = json.loads(cursor.fetchone()[0])
    return {column["name"]: column["type"]["name"] for column in description["columns"]}


def count_customers(connection, customers_table_to_read: str) -> tuple[int, int]:
    query = """
        SELECT
            COUNT(*) AS total_customers,
            COUNT(DISTINCT customer_id) AS unique_customer_ids
        FROM IDENTIFIER(:customers_table_to_read)
    """

    with connection.cursor() as cursor:
        cursor.execute(
            query,
            parameters={"customers_table_to_read": customers_table_to_read},
        )
        total_customers, unique_customer_ids = cursor.fetchone()

    return total_customers, unique_customer_ids


def fetch_customer_batches(
    connection, customers_table_to_read: str, batch_size: int
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
        FROM IDENTIFIER(:customers_table_to_read)
        ORDER BY customer_id
    """

    with connection.cursor() as cursor:
        cursor.execute(query, parameters={"customers_table_to_read": customers_table_to_read})

        while True:
            customers = cursor.fetchmany(batch_size)
            if not customers:
                break
            yield customers


def fetch_customer(connection, customers_table_to_read: str, customer_id: str) -> dict[str, object]:
    """Read one snapshot customer, rejecting missing or ambiguous matches."""
    query = """
        SELECT customer_id, legal_name, billing_city, billing_postal_code, billing_country_code
        FROM IDENTIFIER(:customers_table_to_read)
        WHERE customer_id = :customer_id
        LIMIT 2
    """
    with connection.cursor() as cursor:
        cursor.execute(query, parameters={
            "customers_table_to_read": customers_table_to_read,
            "customer_id": customer_id,
        })
        customers = cursor.fetchmany(2)
    if len(customers) != 1:
        raise ValueError("The saved snapshot must contain exactly one matching customer")
    return customers[0].asDict()


def count_products(connection, products_table_to_read: str) -> tuple[int, int]:
    query = """
        SELECT
            COUNT(*) AS total_products,
            COUNT(DISTINCT product_id) AS unique_product_ids
        FROM IDENTIFIER(:products_table_to_read)
    """

    with connection.cursor() as cursor:
        cursor.execute(
            query,
            parameters={"products_table_to_read": products_table_to_read},
        )
        total_products, unique_product_ids = cursor.fetchone()

    return total_products, unique_product_ids


def fetch_product_batches(
    connection, products_table_to_read: str, batch_size: int
) -> Iterator[list[Row]]:
    """Yield product records in batches without collecting the full result."""
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")

    query = """
        SELECT
            product_id,
            product_code,
            product_name,
            is_active
        FROM IDENTIFIER(:products_table_to_read)
        ORDER BY product_id
    """

    with connection.cursor() as cursor:
        cursor.execute(query, parameters={"products_table_to_read": products_table_to_read})

        while True:
            products = cursor.fetchmany(batch_size)
            if not products:
                break
            yield products


def fetch_product(connection, products_table_to_read: str, product_id: str) -> dict[str, object]:
    """Read one snapshot product, rejecting missing or ambiguous matches."""
    query = """
        SELECT product_id, product_code, product_name, is_active
        FROM IDENTIFIER(:products_table_to_read)
        WHERE product_id = :product_id
        LIMIT 2
    """
    with connection.cursor() as cursor:
        cursor.execute(query, parameters={
            "products_table_to_read": products_table_to_read,
            "product_id": product_id,
        })
        products = cursor.fetchmany(2)
    if len(products) != 1:
        raise ValueError("The saved snapshot must contain exactly one matching product")
    return products[0].asDict()


def count_licences(connection, licences_table_to_read: str) -> tuple[int, int]:
    query = """
        SELECT
            COUNT(*) AS total_licences,
            COUNT(DISTINCT licence_id) AS unique_licence_ids
        FROM IDENTIFIER(:licences_table_to_read)
    """

    with connection.cursor() as cursor:
        cursor.execute(
            query,
            parameters={"licences_table_to_read": licences_table_to_read},
        )
        total_licences, unique_licence_ids = cursor.fetchone()

    return total_licences, unique_licence_ids


def fetch_licence_batches(
    connection, licences_table_to_read: str, batch_size: int
) -> Iterator[list[Row]]:
    """Yield licence records in batches without collecting the full result."""
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")

    query = """
        SELECT
            licence_id,
            customer_id,
            product_id,
            seats,
            starts_on,
            ends_before,
            cancelled_on
        FROM IDENTIFIER(:licences_table_to_read)
        ORDER BY licence_id
    """

    with connection.cursor() as cursor:
        cursor.execute(query, parameters={"licences_table_to_read": licences_table_to_read})

        while True:
            licences = cursor.fetchmany(batch_size)
            if not licences:
                break
            yield licences


def fetch_licence(connection, licences_table_to_read: str, licence_id: str) -> dict[str, object]:
    """Read one snapshot licence, rejecting missing or ambiguous matches."""
    query = """
        SELECT licence_id, customer_id, product_id, seats, starts_on, ends_before, cancelled_on
        FROM IDENTIFIER(:licences_table_to_read)
        WHERE licence_id = :licence_id
        LIMIT 2
    """
    with connection.cursor() as cursor:
        cursor.execute(query, parameters={
            "licences_table_to_read": licences_table_to_read,
            "licence_id": licence_id,
        })
        licences = cursor.fetchmany(2)
    if len(licences) != 1:
        raise ValueError("The saved snapshot must contain exactly one matching licence")
    return licences[0].asDict()
