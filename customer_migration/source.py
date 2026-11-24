import os

from databricks import sql


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
