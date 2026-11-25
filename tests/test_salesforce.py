import os
import unittest
from contextlib import ExitStack, redirect_stdout
from io import StringIO
from unittest.mock import Mock, patch

import requests

from p2_migration.salesforce import (
    compare_account,
    connect_to_salesforce,
    read_account,
    save_account,
    save_and_verify_account,
)


def response_with(status, data=None):
    """Provide an HTTP response without making a network request."""
    response = Mock()
    response.status_code = status
    response.headers = {}
    response.json.return_value = data
    return response


class SalesforceTests(unittest.TestCase):
    def setUp(self):
        resources = ExitStack()
        self.addCleanup(resources.close)
        self.sleep = resources.enter_context(patch("p2_migration.retries.sleep"))
        resources.enter_context(redirect_stdout(StringIO()))
        self.session = Mock()
        self.session.headers = {}
        self.instance_url = "https://example.my.salesforce.com"
        self.settings = {
            "SALESFORCE_MY_DOMAIN_URL": self.instance_url,
            "SALESFORCE_CLIENT_ID": "example-client",
            "SALESFORCE_CLIENT_SECRET": "example-secret",
        }
        self.account = {
            "Source_Customer_ID__c": "customer-1",
            "Name": "Example Company",
            "BillingCity": "Whangārei",
            "BillingPostalCode": "0110",
            "BillingCountryCode": "NZ",
        }

    # Authentication stores the token on the session for later requests.
    def test_authenticates_using_client_credentials(self):
        # Arrange
        self.session.post.return_value = response_with(200, {
            "instance_url": self.instance_url,
            "access_token": "example-token",
        })

        # Act
        with patch.dict(os.environ, self.settings, clear=True):
            instance_url = connect_to_salesforce(self.session)

        # Assert
        self.assertEqual(instance_url, self.instance_url)
        self.assertEqual(self.session.headers["Authorization"], "Bearer example-token")
        self.session.post.assert_called_once_with(
            self.instance_url + "/services/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "example-client",
                "client_secret": "example-secret",
            },
            timeout=30,
            allow_redirects=False,
        )

    # Credentials must only be sent to an HTTPS Salesforce My Domain root URL.
    def test_rejects_unsafe_authentication_urls_before_sending_credentials(self):
        # Arrange
        unsafe_urls = (
            "https://example.com",
            "http://example.my.salesforce.com",
            "https://example.my.salesforce.com.attacker.example",
            "https://user:password@example.my.salesforce.com",
            "https://example.my.salesforce.com/unexpected/path",
            "https://example.my.salesforce.com?unexpected=query",
            "https://example.my.salesforce.com:8080",
        )

        for url in unsafe_urls:
            with self.subTest(url=url):
                settings = {**self.settings, "SALESFORCE_MY_DOMAIN_URL": url}

                # Act and Assert
                with patch.dict(os.environ, settings, clear=True), self.assertRaises(ValueError):
                    connect_to_salesforce(self.session)
                self.session.post.assert_not_called()

    # Authentication cannot redirect subsequent bearer-token requests to an unsafe host.
    def test_rejects_an_unsafe_returned_instance_url_before_storing_the_token(self):
        # Arrange
        self.session.post.return_value = response_with(200, {
            "instance_url": "https://attacker.example",
            "access_token": "example-token",
        })

        # Act and Assert
        with (
            patch.dict(os.environ, self.settings, clear=True),
            self.assertRaises(ValueError),
        ):
            connect_to_salesforce(self.session)
        self.assertEqual(self.session.headers, {})

    # Missing credentials are reported before starting an authentication request.
    def test_missing_settings_prevent_authentication(self):
        # Arrange
        del self.settings["SALESFORCE_CLIENT_SECRET"]

        # Act and Assert
        with (
            patch.dict(os.environ, self.settings, clear=True),
            self.assertRaisesRegex(ValueError, "Missing Salesforce settings: SALESFORCE_CLIENT_SECRET"),
        ):
            connect_to_salesforce(self.session)
        self.session.post.assert_not_called()

    # Failed sign-in reports the status without exposing the response body.
    def test_authentication_failure_does_not_expose_credentials(self):
        # Arrange
        self.session.post.return_value = response_with(400, {"error": "example-secret"})

        # Act and Assert
        with (
            patch.dict(os.environ, self.settings, clear=True),
            self.assertRaisesRegex(RuntimeError, r"^Salesforce authentication failed \(HTTP 400\)$"),
        ):
            connect_to_salesforce(self.session)
        self.assertEqual(self.session.headers, {})

    # Both a new record and a later update use the same external-ID endpoint.
    def test_create_and_update_use_the_same_customer_id(self):
        # Arrange
        self.session.patch.side_effect = [response_with(201), response_with(200)]
        original_account = self.account.copy()

        # Act
        first_outcome = save_account(self.session, self.instance_url, self.account)
        second_outcome = save_account(self.session, self.instance_url, self.account)
        first_request, second_request = self.session.patch.call_args_list
        sent_fields = first_request.kwargs["json"]

        # Assert
        self.assertEqual(first_outcome, "created")
        self.assertEqual(second_outcome, "updated")
        self.assertEqual(first_request, second_request)
        self.assertTrue(first_request.args[0].endswith("/Account/Source_Customer_ID__c/customer-1"))
        self.assertEqual(sent_fields, {
            "Name": "Example Company",
            "BillingCity": "Whangārei",
            "BillingPostalCode": "0110",
            "BillingCountryCode": "NZ",
        })
        self.assertEqual(self.account, original_account)

    # The read uses the same external-ID endpoint and preserves the returned data.
    def test_reads_the_saved_account(self):
        # Arrange
        saved_account = {**self.account, "Id": "salesforce-account-1"}
        self.session.get.return_value = response_with(200, saved_account)

        # Act
        received = read_account(self.session, self.instance_url, "customer-1")

        # Assert
        self.assertEqual(received, saved_account)
        self.session.get.assert_called_once_with(
            self.instance_url + "/services/data/v68.0/sobjects/Account/Source_Customer_ID__c/customer-1",
            timeout=30,
            allow_redirects=False,
        )

    # A failed read cannot be treated as a verified record.
    def test_reports_a_failed_read(self):
        # Arrange
        self.session.get.return_value = response_with(404)

        # Act and Assert
        with self.assertRaisesRegex(RuntimeError, "HTTP 404"):
            read_account(self.session, self.instance_url, "customer-1")

    # Both HTTP 201 creates and HTTP 200 updates must be saved before they are read back.
    def test_saves_then_verifies_both_created_and_updated_accounts(self):
        # Arrange
        received = {**self.account, "Id": "salesforce-account-1"}
        outcomes = ((201, "created"), (200, "updated"))

        for status, expected_outcome in outcomes:
            with self.subTest(status=status, outcome=expected_outcome):
                self.session.reset_mock()
                self.session.patch.return_value = response_with(status)
                self.session.get.return_value = response_with(200, received)

                # Act
                outcome, account = save_and_verify_account(self.session, self.instance_url, self.account)

                # Assert
                self.assertEqual(outcome, expected_outcome)
                self.assertEqual(account, received)
                self.assertEqual([call[0] for call in self.session.method_calls], ["patch", "get"])
                self.sleep.assert_not_called()

    # A successful HTTP save is insufficient if the stored values differ.
    def test_save_and_verify_rejects_a_read_back_mismatch(self):
        # Arrange
        self.session.patch.return_value = response_with(200)
        self.session.get.return_value = response_with(200, {
            **self.account, "BillingPostalCode": "110",
        })

        # Act and Assert
        with self.assertRaisesRegex(ValueError, "was saved.*BillingPostalCode"):
            save_and_verify_account(self.session, self.instance_url, self.account)

    # Ambiguous matches, invalid data and server errors never retry a save or proceed to verification.
    def test_failed_saves_are_not_retried_or_verified(self):
        # Arrange
        statuses = (300, 400, 503)

        for status in statuses:
            with self.subTest(status=status):
                self.session.reset_mock()
                self.session.patch.return_value = response_with(status)

                # Act and Assert
                with self.assertRaisesRegex(RuntimeError, f"HTTP {status}"):
                    save_and_verify_account(self.session, self.instance_url, self.account)
                self.session.patch.assert_called_once()
                self.session.get.assert_not_called()
                self.sleep.assert_not_called()

    # Retrying verification does not repeat a successful save.
    def test_verification_read_recovers_without_saving_again(self):
        # Arrange
        received = {**self.account, "Id": "existing-account"}
        self.session.patch.return_value = response_with(200)
        self.session.get.side_effect = [requests.ReadTimeout(), response_with(200, received)]

        # Act
        outcome, account = save_and_verify_account(self.session, self.instance_url, self.account)

        # Assert
        self.assertEqual(outcome, "updated")
        self.assertEqual(account, received)
        self.session.patch.assert_called_once()
        self.assertEqual(self.session.get.call_count, 2)
        self.sleep.assert_called_once_with(1)

    # A connection timeout retries the same account fields and external ID.
    def test_save_connection_timeout_retries_the_same_request(self):
        # Arrange
        self.session.patch.side_effect = [requests.ConnectTimeout(), response_with(201)]

        # Act
        outcome = save_account(self.session, self.instance_url, self.account)

        # Assert
        self.assertEqual(outcome, "created")
        first_request, second_request = self.session.patch.call_args_list
        self.assertEqual(first_request, second_request)
        self.sleep.assert_called_once_with(1)

    # A lost save response leaves the result uncertain and must not trigger another save.
    def test_save_read_timeout_stops_without_replaying_the_write(self):
        # Arrange
        self.session.patch.side_effect = requests.ReadTimeout()

        # Act
        with self.assertRaises(requests.ReadTimeout):
            save_and_verify_account(self.session, self.instance_url, self.account)

        # Assert
        self.session.patch.assert_called_once()
        self.session.get.assert_not_called()
        self.sleep.assert_not_called()

    # Authentication can recover when its connection times out before sending credentials.
    def test_authentication_retries_a_connection_timeout(self):
        # Arrange
        self.session.post.side_effect = [
            requests.ConnectTimeout(),
            response_with(200, {"instance_url": self.instance_url, "access_token": "example-token"}),
        ]

        # Act
        with patch.dict(os.environ, self.settings, clear=True):
            instance_url = connect_to_salesforce(self.session)

        # Assert
        self.assertEqual(instance_url, self.instance_url)
        self.assertEqual(self.session.post.call_count, 2)
        self.sleep.assert_called_once_with(1)

    # Exhausted verification retries do not turn a saved account into a verified result.
    def test_verification_stops_after_repeated_server_errors(self):
        # Arrange
        self.session.patch.return_value = response_with(200)
        self.session.get.return_value = response_with(503)

        # Act
        with self.assertRaisesRegex(RuntimeError, "HTTP 503"):
            save_and_verify_account(self.session, self.instance_url, self.account)

        # Assert
        self.session.patch.assert_called_once()
        self.assertEqual(self.session.get.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)

    # Salesforce may add fields of its own; only the mapped values are compared.
    def test_matching_values_ignore_additional_salesforce_fields(self):
        # Arrange
        received = {**self.account, "Id": "salesforce-account-1", "OwnerId": "owner-1"}

        # Act
        differences = compare_account(self.account, received)

        # Assert
        self.assertEqual(differences, [])

    # Missing fields and lost leading zeros are both reported.
    def test_reports_missing_and_changed_values(self):
        # Arrange
        received = self.account.copy()
        del received["Name"]
        received["BillingPostalCode"] = "110"

        # Act
        differences = compare_account(self.account, received)

        # Assert
        self.assertEqual(differences, ["Name", "BillingPostalCode"])


if __name__ == "__main__":
    unittest.main()
