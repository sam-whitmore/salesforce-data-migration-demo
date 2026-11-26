import os
from collections.abc import Mapping
from urllib.parse import quote, urlsplit

import requests

from p2_migration.retries import request_with_retries

API_VERSION = "68.0"
CUSTOMER_ID_FIELD = "Source_Customer_ID__c"
PRODUCT_ID_FIELD = "Source_Product_ID__c"


class RecordUnavailableError(RuntimeError):
    """A lookup returned 404; absence and lack of access can look alike."""


def _salesforce_url(value: str) -> str:
    """Check the destination before sending credentials or an access token."""
    url = urlsplit(value)
    if (
        url.scheme != "https"
        or not url.hostname
        or not url.hostname.endswith(".my.salesforce.com")
        or url.path not in ("", "/")
        or url.query
        or url.fragment
        or url.username
        or url.password
        or url.port not in (None, 443)
    ):
        raise ValueError("Use an HTTPS Salesforce My Domain URL without a page path")
    return value.rstrip("/")


def connect_to_salesforce(session: requests.Session) -> str:
    """Authenticate the session and return the instance URL."""
    required = ("SALESFORCE_MY_DOMAIN_URL", "SALESFORCE_CLIENT_ID", "SALESFORCE_CLIENT_SECRET")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise ValueError("Missing Salesforce settings: " + ", ".join(missing))
    domain = _salesforce_url(os.environ["SALESFORCE_MY_DOMAIN_URL"])
    response = request_with_retries(
        session.post,
        f"{domain}/services/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": os.environ["SALESFORCE_CLIENT_ID"],
            "client_secret": os.environ["SALESFORCE_CLIENT_SECRET"],
        },
    )
    if response.status_code != 200:
        raise RuntimeError(f"Salesforce authentication failed (HTTP {response.status_code})")

    authentication = response.json()
    instance_url = _salesforce_url(authentication["instance_url"])
    session.headers["Authorization"] = f"Bearer {authentication['access_token']}"
    return instance_url


def _record_url(instance_url: str, object_name: str, id_field: str, source_id: str) -> str:
    return (
        f"{_salesforce_url(instance_url)}/services/data/v{API_VERSION}"
        f"/sobjects/{object_name}/{id_field}/{quote(source_id, safe='')}"
    )


def describe_object(
    session: requests.Session, instance_url: str, object_name: str
) -> dict[str, object]:
    """Read the object's field definitions and permissions visible to this user."""
    response = request_with_retries(
        session.get,
        f"{_salesforce_url(instance_url)}/services/data/v{API_VERSION}"
        f"/sobjects/{quote(object_name, safe='')}/describe",
        read_only=True,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"Could not inspect Salesforce {object_name} (HTTP {response.status_code}). "
            "Check the object name and the integration user's access."
        )
    return response.json()


def save_account(
    session: requests.Session, instance_url: str, account: Mapping[str, object]
) -> str:
    """Create or update an Account using the source customer ID."""
    return _save_record(session, instance_url, "Account", CUSTOMER_ID_FIELD, account)


def _save_record(
    session: requests.Session, instance_url: str, object_name: str,
    id_field: str, record: Mapping[str, object],
) -> str:
    source_id = record[id_field]
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("The source ID must be non-empty text")

    # The external ID belongs in the URL, not in the fields being updated.
    fields = dict(record)
    del fields[id_field]
    response = request_with_retries(
        session.patch,
        _record_url(instance_url, object_name, id_field, source_id),
        json=fields,
    )
    if response.status_code == 201:
        return "created"
    if response.status_code == 200:
        return "updated"
    raise RuntimeError(f"Salesforce {object_name.lower()} save failed (HTTP {response.status_code})")


def read_account(
    session: requests.Session, instance_url: str, customer_id: str
) -> dict[str, object]:
    """Read an Account using the same source ID used when saving it."""
    return _read_record(session, instance_url, "Account", CUSTOMER_ID_FIELD, customer_id)


def _read_record(
    session: requests.Session, instance_url: str, object_name: str,
    id_field: str, source_id: str,
) -> dict[str, object]:
    response = request_with_retries(
        session.get,
        _record_url(instance_url, object_name, id_field, source_id),
        read_only=True,
    )
    if response.status_code == 404:
        raise RecordUnavailableError(f"Salesforce {object_name.lower()} unavailable (HTTP 404)")
    if response.status_code != 200:
        raise RuntimeError(f"Salesforce {object_name.lower()} read failed (HTTP {response.status_code})")
    return response.json()


def compare_fields(
    expected: Mapping[str, object], actual: Mapping[str, object]
) -> list[str]:
    """Return mapped fields that are missing or differ from what was sent."""
    return [field for field, value in expected.items() if field not in actual or actual[field] != value]


def save_and_verify_account(
    session: requests.Session, instance_url: str, account: Mapping[str, object]
) -> tuple[str, dict[str, object]]:
    """Save one Account and confirm that all mapped values were retained."""
    customer_id = account[CUSTOMER_ID_FIELD]
    if not isinstance(customer_id, str):
        raise ValueError("The source customer ID must be text")

    outcome = save_account(session, instance_url, account)
    received_account = read_account(session, instance_url, customer_id)
    differences = compare_fields(account, received_account)
    if differences:
        raise ValueError(
            f"Account {customer_id} was saved, but these fields differ: "
            + ", ".join(differences)
        )
    return outcome, received_account


def read_product(session: requests.Session, instance_url: str, product_id: str) -> dict[str, object]:
    """Find a Product2 record by its source product ID."""
    return _read_record(session, instance_url, "Product2", PRODUCT_ID_FIELD, product_id)


def save_and_verify_product(
    session: requests.Session, instance_url: str, product: Mapping[str, object],
) -> tuple[str, dict[str, object]]:
    """Create or update one product, then check all four mapped fields."""
    outcome = _save_record(session, instance_url, "Product2", PRODUCT_ID_FIELD, product)
    received = read_product(session, instance_url, str(product[PRODUCT_ID_FIELD]))
    differences = compare_fields(product, received)
    if differences:
        raise ValueError("Product was saved, but these fields differ: " + ", ".join(differences))
    return outcome, received
