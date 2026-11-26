import os
from collections.abc import Mapping
from urllib.parse import quote, urlsplit

import requests

from p2_migration.retries import request_with_retries

API_VERSION = "68.0"
EXTERNAL_ID_FIELD = "Source_Customer_ID__c"


class AccountUnavailableError(RuntimeError):
    """The Account lookup returned 404; absence and lack of access can look alike."""


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


def _account_url(instance_url: str, customer_id: str) -> str:
    encoded_id = quote(customer_id, safe="")
    return (
        f"{_salesforce_url(instance_url)}/services/data/v{API_VERSION}"
        f"/sobjects/Account/{EXTERNAL_ID_FIELD}/{encoded_id}"
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
    customer_id = account[EXTERNAL_ID_FIELD]
    if not isinstance(customer_id, str) or not customer_id.strip():
        raise ValueError("The source customer ID must be non-empty text")

    # The external ID belongs in the URL, not in the fields being updated.
    fields = dict(account)
    del fields[EXTERNAL_ID_FIELD]
    response = request_with_retries(
        session.patch,
        _account_url(instance_url, customer_id),
        json=fields,
    )
    if response.status_code == 201:
        return "created"
    if response.status_code == 200:
        return "updated"
    raise RuntimeError(f"Salesforce account save failed (HTTP {response.status_code})")


def read_account(
    session: requests.Session, instance_url: str, customer_id: str
) -> dict[str, object]:
    """Read an Account using the same source ID used when saving it."""
    response = request_with_retries(
        session.get,
        _account_url(instance_url, customer_id),
        read_only=True,
    )
    if response.status_code == 404:
        raise AccountUnavailableError("Salesforce account unavailable (HTTP 404)")
    if response.status_code != 200:
        raise RuntimeError(f"Salesforce account read failed (HTTP {response.status_code})")
    return response.json()


def compare_account(
    expected: Mapping[str, object], actual: Mapping[str, object]
) -> list[str]:
    """Return mapped fields that are missing or differ from what was sent."""
    return [field for field, value in expected.items() if field not in actual or actual[field] != value]


def save_and_verify_account(
    session: requests.Session, instance_url: str, account: Mapping[str, object]
) -> tuple[str, dict[str, object]]:
    """Save one Account and confirm that all mapped values were retained."""
    customer_id = account[EXTERNAL_ID_FIELD]
    if not isinstance(customer_id, str):
        raise ValueError("The source customer ID must be text")

    outcome = save_account(session, instance_url, account)
    received_account = read_account(session, instance_url, customer_id)
    differences = compare_account(account, received_account)
    if differences:
        raise ValueError(
            f"Account {customer_id} was saved, but these fields differ: "
            + ", ".join(differences)
        )
    return outcome, received_account
