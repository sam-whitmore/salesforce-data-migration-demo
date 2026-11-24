import os
import unittest
from unittest.mock import Mock, patch

from customer_migration.salesforce import (
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
    response.json.return_value = data
    return response


class SalesforceTests(unittest.TestCase):
    def setUp(self):
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

    # A non-Salesforce destination is rejected before credentials are sent.
    def test_rejects_an_unexpected_authentication_host(self):
        # Arrange
        self.settings["SALESFORCE_MY_DOMAIN_URL"] = "https://example.com"

        # Act and Assert
        with patch.dict(os.environ, self.settings, clear=True):
            with self.assertRaises(ValueError):
                connect_to_salesforce(self.session)
        self.session.post.assert_not_called()

    # Failed sign-in reports the status without exposing the response body.
    def test_authentication_failure_does_not_expose_credentials(self):
        # Arrange
        self.session.post.return_value = response_with(400, {"error": "example-secret"})

        # Act and Assert
        with patch.dict(os.environ, self.settings, clear=True):
            with self.assertRaisesRegex(RuntimeError, r"^Salesforce authentication failed \(HTTP 400\)$"):
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

    # An ambiguous external-ID match is a failure, not a successful update.
    def test_rejects_an_ambiguous_match(self):
        # Arrange
        self.session.patch.return_value = response_with(300)

        # Act and Assert
        with self.assertRaisesRegex(RuntimeError, "HTTP 300"):
            save_account(self.session, self.instance_url, self.account)

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

    # A successful save is verified by a separate read of the stored values.
    def test_save_and_verify_returns_the_confirmed_account(self):
        # Arrange
        received = {**self.account, "Id": "salesforce-account-1"}
        self.session.patch.return_value = response_with(201)
        self.session.get.return_value = response_with(200, received)

        # Act
        outcome, account = save_and_verify_account(self.session, self.instance_url, self.account)

        # Assert
        self.assertEqual(outcome, "created")
        self.assertEqual(account, received)
        self.session.patch.assert_called_once()
        self.session.get.assert_called_once()

    # API v68 returns HTTP 200 for an update; the saved values must still be verified.
    def test_http_200_update_is_read_back_and_verified(self):
        # Arrange
        received = {**self.account, "Id": "existing-account"}
        self.session.patch.return_value = response_with(200, {
            "id": "existing-account",
            "success": True,
            "errors": [],
            "created": False,
        })
        self.session.get.return_value = response_with(200, received)

        # Act
        outcome, account = save_and_verify_account(self.session, self.instance_url, self.account)

        # Assert
        self.assertEqual(outcome, "updated")
        self.assertEqual(account, received)
        self.session.get.assert_called_once()

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

    # A failed write never proceeds to verification.
    def test_save_and_verify_stops_after_a_save_failure(self):
        # Arrange
        self.session.patch.return_value = response_with(400)

        # Act and Assert
        with self.assertRaisesRegex(RuntimeError, "HTTP 400"):
            save_and_verify_account(self.session, self.instance_url, self.account)
        self.session.get.assert_not_called()

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
