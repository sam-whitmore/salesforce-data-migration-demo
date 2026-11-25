from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from time import sleep
from typing import Any

import requests

MAX_ATTEMPTS = 3
MAX_RETRY_WAIT_SECONDS = 30
RETRYABLE_READ_STATUSES = {429, 500, 502, 503, 504}


def request_with_retries(
    request: Callable[..., requests.Response],
    url: str,
    *,
    read_only: bool = False,
    **kwargs: Any,
) -> requests.Response:
    """Retry temporary read failures and connection timeouts, without replaying uncertain writes."""
    attempt = 1
    while True:
        wait_seconds: float = 2 ** (attempt - 1)
        try:
            response = request(url, timeout=30, allow_redirects=False, **kwargs)
        except requests.exceptions.SSLError:
            raise
        except requests.ConnectTimeout:
            # Requests documents this exception as safe to retry: connection timed out.
            if attempt == MAX_ATTEMPTS:
                raise
            reason = "connection timeout"
        except (requests.Timeout, requests.ConnectionError):
            if not read_only or attempt == MAX_ATTEMPTS:
                raise
            reason = "read connection interrupted"
        else:
            if not read_only or response.status_code not in RETRYABLE_READ_STATUSES or attempt == MAX_ATTEMPTS:
                return response
            reason = f"HTTP {response.status_code}"
            try:
                wait_seconds = retry_wait_seconds(response, minimum_wait=wait_seconds)
            finally:
                response.close()

        print(
            f"    Retrying Salesforce request: {reason}; "
            f"attempt {attempt + 1}/{MAX_ATTEMPTS} in {wait_seconds:g}s",
            flush=True,
        )
        sleep(wait_seconds)
        attempt += 1


def retry_wait_seconds(response: requests.Response, *, minimum_wait: float) -> float:
    """Respect Retry-After without waiting indefinitely or retrying earlier than requested."""
    retry_after = response.headers.get("Retry-After")
    if retry_after is None:
        return minimum_wait

    try:
        if retry_after.strip().isdecimal():
            requested_wait = int(retry_after)
        else:
            retry_time = parsedate_to_datetime(retry_after)
            if retry_time.tzinfo is None:
                return minimum_wait
            requested_wait = (retry_time - datetime.now(timezone.utc)).total_seconds()
    except (ValueError, TypeError, OverflowError):
        return minimum_wait

    wait_seconds = max(minimum_wait, requested_wait)
    if wait_seconds > MAX_RETRY_WAIT_SECONDS:
        raise RuntimeError(
            f"Salesforce requested a retry wait longer than {MAX_RETRY_WAIT_SECONDS}s "
            f"(HTTP {response.status_code}). Try again later."
        )
    return wait_seconds
