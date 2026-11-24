from collections.abc import Iterable, Mapping

import requests

from customer_migration.mapping import prepare_salesforce_account
from customer_migration.salesforce import EXTERNAL_ID_FIELD, save_and_verify_account
from customer_migration.validation import validate_customer


def prepare_accounts(customers: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    """Prepare valid customers in this batch and report rejected records."""
    accounts = []
    for customer in customers:
        errors = validate_customer(customer)
        if errors:
            print(f"  Rejected customer {customer.get('customer_id')!r}: {'; '.join(errors)}")
            continue
        accounts.append(prepare_salesforce_account(customer))
    return accounts


def load_accounts(
    session: requests.Session, instance_url: str, accounts: Iterable[Mapping[str, object]]
) -> tuple[int, int]:
    """Save and verify this batch, returning counts of created and updated accounts."""
    created = 0
    updated = 0
    for account in accounts:
        print(f"  Loading customer {account[EXTERNAL_ID_FIELD]}")
        outcome, received_account = save_and_verify_account(session, instance_url, account)
        if outcome == "created":
            created += 1
        else:
            updated += 1
        print(f"  Account {received_account['Id']}: {outcome}, verified")
    return created, updated
