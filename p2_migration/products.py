import requests

from p2_migration.databricks import (
    connect_to_databricks,
    count_products,
    fetch_product,
    fetch_product_batches,
)
from p2_migration.mapping import prepare_salesforce_product
from p2_migration.reporting import MigrationReport
from p2_migration.salesforce import (
    PRODUCT_ID_FIELD,
    compare_fields,
    connect_to_salesforce,
    read_product,
    save_and_verify_product,
)
from p2_migration.validation import validate_product


def process_product_batches(
    products_table: str, report: MigrationReport,
    verified_product_ids: frozenset[str] = frozenset(),
) -> None:
    """Load valid products in batches, retaining inactive catalogue entries."""
    print("\n2.2 Products\n" + "-" * 60, flush=True)
    with connect_to_databricks() as connection, requests.Session() as session:
        total, unique = count_products(connection, products_table)
        if total != unique:
            raise ValueError("Product IDs must be present and unique before loading")
        if verified_product_ids:
            missing = set(verified_product_ids)
            for products in fetch_product_batches(connection, products_table, batch_size=6):
                missing.difference_update(product.product_id for product in products)
            if missing:
                raise ValueError("Previously verified products are missing from the saved snapshot; inspect it before resuming")
        instance_url = connect_to_salesforce(session)
        for batch_number, products in enumerate(fetch_product_batches(connection, products_table, batch_size=6), start=1):
            report.total_products += len(products)
            print(f"\nBatch {batch_number}: {len(products)} products", flush=True)
            for row in products:
                if row.product_id in verified_product_ids:
                    continue
                product = row.asDict()
                errors = validate_product(product)
                if errors:
                    report.reject_product(product.get("product_id"), errors)
                    print(f"  Rejected product {product.get('product_id')!r}: " + "; ".join(errors))
                    continue
                fields = prepare_salesforce_product(product)
                report.prepared_products += 1
                report.record_product_load_started(str(fields[PRODUCT_ID_FIELD]))
                outcome, received = save_and_verify_product(session, instance_url, fields)
                report.record_verified_product(received["Id"], outcome)
                print(f"  {outcome.capitalize()}, verified | {fields['Name']}", flush=True)


def compare_snapshot_product(products_table: str, product_id: str) -> dict[str, object]:
    """Read back an uncertain product without repeating its save."""
    with connect_to_databricks() as connection, requests.Session() as session:
        product = fetch_product(connection, products_table, product_id)
        errors = validate_product(product)
        if errors:
            raise ValueError("Snapshot product is invalid: " + "; ".join(errors))
        expected = prepare_salesforce_product(product)
        instance_url = connect_to_salesforce(session)
        actual = read_product(session, instance_url, product_id)
        differences = compare_fields(expected, actual)
        if differences:
            raise ValueError("Product differs from the snapshot: " + ", ".join(differences))
        return actual
