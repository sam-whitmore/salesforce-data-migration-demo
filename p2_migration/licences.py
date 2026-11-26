from collections.abc import Mapping

import requests

from p2_migration.databricks import (
    connect_to_databricks,
    count_licences,
    fetch_licence,
    fetch_licence_batches,
)
from p2_migration.mapping import prepare_salesforce_licence
from p2_migration.reporting import MigrationReport
from p2_migration.salesforce import (
    CUSTOMER_ID_FIELD,
    LICENCE_ID_FIELD,
    PRODUCT_ID_FIELD,
    RecordUnavailableError,
    compare_fields,
    connect_to_salesforce,
    read_account,
    read_licence,
    read_product,
    save_and_verify_licence,
)
from p2_migration.validation import validate_licence


def process_licence_batches(
    licences_table: str, report: MigrationReport,
    verified_licence_ids: frozenset[str] = frozenset(),
) -> None:
    """Load every valid term after its customer and product stages have completed."""
    print("\n2.3 Licences\n" + "-" * 60, flush=True)
    print("Resolve customer and product links, save each licence, then verify all seven fields.", flush=True)
    with connect_to_databricks() as connection, requests.Session() as session:
        total, unique = count_licences(connection, licences_table)
        if total != unique:
            raise ValueError("Licence IDs must be present and unique before loading")
        if verified_licence_ids:
            check_recorded_licences(connection, licences_table, verified_licence_ids)
        instance_url = connect_to_salesforce(session)
        batches = fetch_licence_batches(connection, licences_table, batch_size=6)
        for batch_number, licences in enumerate(batches, start=1):
            report.total_licences += len(licences)
            print(f"\nBatch {batch_number}: {len(licences)} licences", flush=True)
            for row in licences:
                if row.licence_id in verified_licence_ids:
                    continue
                licence = row.asDict()
                errors = validate_licence(licence)
                if errors:
                    report.reject_licence(licence.get("licence_id"), errors)
                    print(f"  Rejected licence {licence.get('licence_id')!r}: " + "; ".join(errors))
                    continue
                fields = prepare_linked_licence(session, instance_url, licence)
                report.prepared_licences += 1
                report.record_licence_load_started(str(fields[LICENCE_ID_FIELD]))
                outcome, received = save_and_verify_licence(session, instance_url, fields)
                report.record_verified_licence(received["Id"], outcome)
                print(f"  {outcome.capitalize()}, verified | {received.get('Name', row.licence_id)}", flush=True)


def prepare_linked_licence(
    session: requests.Session, instance_url: str, licence: Mapping[str, object],
) -> dict[str, object]:
    """Resolve existing parents by source ID; never create a placeholder relationship."""
    customer_id = str(licence["customer_id"])
    product_id = str(licence["product_id"])
    try:
        account = read_account(session, instance_url, customer_id)
        product = read_product(session, instance_url, product_id)
    except RecordUnavailableError:
        raise ValueError(
            f"Cannot link licence {licence['licence_id']}: its customer or product is missing or inaccessible. "
            "No licence save was attempted."
        ) from None
    return prepare_salesforce_licence(
        licence,
        customer_account_id=parent_record_id(account, CUSTOMER_ID_FIELD, customer_id),
        salesforce_product_id=parent_record_id(product, PRODUCT_ID_FIELD, product_id),
    )


def parent_record_id(record: Mapping[str, object], source_field: str, source_id: str) -> str:
    """Require the lookup to return the intended parent and its Salesforce ID."""
    if record.get(source_field) != source_id:
        raise ValueError(f"Parent lookup did not return the expected {source_field}")
    salesforce_id = record.get("Id")
    if not isinstance(salesforce_id, str) or not salesforce_id.strip():
        raise ValueError("Parent lookup did not return a Salesforce record ID")
    return salesforce_id


def check_recorded_licences(connection, licences_table: str, licence_ids: frozenset[str]) -> None:
    """Refuse to continue if verified licences disappeared from the saved snapshot."""
    missing = set(licence_ids)
    for licences in fetch_licence_batches(connection, licences_table, batch_size=6):
        missing.difference_update(licence.licence_id for licence in licences)
    if missing:
        raise ValueError("Previously verified licences are missing from the saved snapshot; inspect it before resuming")


def compare_snapshot_licence(licences_table: str, licence_id: str) -> dict[str, object]:
    """Check an uncertain licence and both links without repeating its save."""
    with connect_to_databricks() as connection, requests.Session() as session:
        licence = fetch_licence(connection, licences_table, licence_id)
        errors = validate_licence(licence)
        if errors:
            raise ValueError("Snapshot licence is invalid: " + "; ".join(errors))
        instance_url = connect_to_salesforce(session)
        expected = prepare_linked_licence(session, instance_url, licence)
        actual = read_licence(session, instance_url, licence_id)
        differences = compare_fields(expected, actual)
        if differences:
            raise ValueError("Licence differs from the snapshot: " + ", ".join(differences))
        return actual
