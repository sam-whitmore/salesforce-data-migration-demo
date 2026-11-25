import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from unittest.mock import Mock, call, patch

import requests

from p2_migration.retries import request_with_retries


def response_with(status, retry_after=None):
    """Create an HTTP response without making a request."""
    response = requests.Response()
    response.status_code = status
    response.raw = Mock()
    if retry_after is not None:
        response.headers["Retry-After"] = retry_after
    return response


class RetryTests(unittest.TestCase):
    def setUp(self):
        """Use simulated requests and replace waiting so tests run immediately."""
        resources = ExitStack()
        self.addCleanup(resources.close)
        self.sleep = resources.enter_context(patch("p2_migration.retries.sleep"))
        self.output = StringIO()
        resources.enter_context(redirect_stdout(self.output))
        self.request = Mock()
        self.url = "https://example.my.salesforce.com/example"

    # A temporary read failure repeats only that request and can recover.
    def test_read_recovers_from_a_temporary_http_failure(self):
        # Arrange
        statuses = (429, 500, 502, 503, 504)

        for status in statuses:
            with self.subTest(status=status):
                self.sleep.reset_mock()
                success = response_with(200)
                request = Mock(side_effect=[response_with(status), success])

                # Act
                response = request_with_retries(request, self.url, read_only=True)

                # Assert
                self.assertIs(response, success)
                self.assertEqual(request.call_count, 2)
                self.assertEqual(request.call_args_list[0], request.call_args_list[1])
                self.sleep.assert_called_once_with(1)
        self.assertIn("attempt 2/3", self.output.getvalue())

    # Repeated server failures stop after three attempts and two waits.
    def test_http_retries_are_bounded(self):
        # Arrange
        failure = response_with(503)
        self.request.return_value = failure

        # Act
        response = request_with_retries(self.request, self.url, read_only=True)

        # Assert
        self.assertIs(response, failure)
        self.assertEqual(self.request.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [call(1), call(2)])

    # Switching between HTTP failures and timeouts does not reset the attempt budget.
    def test_mixed_failures_share_one_attempt_limit(self):
        # Arrange
        final_failure = response_with(503)
        self.request.side_effect = [response_with(503), requests.ReadTimeout(), final_failure]

        # Act
        response = request_with_retries(self.request, self.url, read_only=True)

        # Assert
        self.assertIs(response, final_failure)
        self.assertEqual(self.request.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [call(1), call(2)])

    # Read timeouts cannot keep the command retrying forever.
    def test_read_timeouts_are_bounded_and_do_not_expose_error_details(self):
        # Arrange
        self.request.side_effect = requests.ReadTimeout("private request details")

        # Act
        with self.assertRaises(requests.ReadTimeout):
            request_with_retries(self.request, self.url, read_only=True)

        # Assert
        self.assertEqual(self.request.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [call(1), call(2)])
        self.assertNotIn("private request details", self.output.getvalue())
        self.assertNotIn(self.url, self.output.getvalue())

    # Authentication, permission and data errors are not repaired by repeating a read.
    def test_permanent_http_failures_are_not_retried(self):
        # Arrange
        statuses = (400, 401, 403, 404, 409, 422)

        for status in statuses:
            with self.subTest(status=status):
                request = Mock(return_value=response_with(status))

                # Act
                response = request_with_retries(request, self.url, read_only=True)

                # Assert
                self.assertEqual(response.status_code, status)
                request.assert_called_once()
        self.sleep.assert_not_called()

    # Write responses are returned to the caller without replaying the write.
    def test_http_write_failures_are_not_retried(self):
        # Arrange
        statuses = (429, 500, 502, 503, 504)

        for status in statuses:
            with self.subTest(status=status):
                request = Mock(return_value=response_with(status))

                # Act
                response = request_with_retries(request, self.url)

                # Assert
                self.assertEqual(response.status_code, status)
                request.assert_called_once()
        self.sleep.assert_not_called()

    # A timed-out connection can be retried even for a write.
    def test_connection_timeout_can_recover_for_a_write(self):
        # Arrange
        success = response_with(201)
        self.request.side_effect = [requests.ConnectTimeout(), success]

        # Act
        response = request_with_retries(self.request, self.url, json={"Name": "Example"})

        # Assert
        self.assertIs(response, success)
        self.assertEqual(self.request.call_count, 2)
        self.assertEqual(self.request.call_args_list[0], self.request.call_args_list[1])
        self.sleep.assert_called_once_with(1)

    # Safe-to-retry connection failures still have a firm attempt limit.
    def test_connection_timeouts_are_bounded_for_writes(self):
        # Arrange
        self.request.side_effect = requests.ConnectTimeout()

        # Act
        with self.assertRaises(requests.ConnectTimeout):
            request_with_retries(self.request, self.url)

        # Assert
        self.assertEqual(self.request.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [call(1), call(2)])

    # A lost write response does not prove the write failed, so it must not be replayed.
    def test_uncertain_writes_are_not_retried(self):
        # Arrange
        errors = (requests.ReadTimeout(), requests.ConnectionError())

        for error in errors:
            with self.subTest(error=type(error).__name__):
                request = Mock(side_effect=error)

                # Act
                with self.assertRaises(type(error)):
                    request_with_retries(request, self.url)

                # Assert
                request.assert_called_once()
        self.sleep.assert_not_called()

    # A broken read connection may recover without changing Salesforce records.
    def test_read_recovers_from_a_connection_error(self):
        # Arrange
        success = response_with(200)
        self.request.side_effect = [requests.ConnectionError(), success]

        # Act
        response = request_with_retries(self.request, self.url, read_only=True)

        # Assert
        self.assertIs(response, success)
        self.sleep.assert_called_once_with(1)

    # Certificate failures require correction rather than repeated requests.
    def test_certificate_errors_are_not_retried(self):
        # Arrange
        self.request.side_effect = requests.exceptions.SSLError()

        # Act
        with self.assertRaises(requests.exceptions.SSLError):
            request_with_retries(self.request, self.url, read_only=True)

        # Assert
        self.request.assert_called_once()
        self.sleep.assert_not_called()

    # Retry-After seconds are accepted up to and including the 30-second limit.
    def test_retry_after_seconds_are_respected(self):
        # Arrange
        accepted_waits = (1, 7, 30)

        for wait_seconds in accepted_waits:
            with self.subTest(wait_seconds=wait_seconds):
                self.sleep.reset_mock()
                request = Mock(side_effect=[
                    response_with(429, retry_after=str(wait_seconds)), response_with(200),
                ])

                # Act
                request_with_retries(request, self.url, read_only=True)

                # Assert
                self.sleep.assert_called_once_with(wait_seconds)

    # Retry-After also supports an absolute HTTP date.
    def test_retry_after_date_is_respected(self):
        # Arrange
        self.request.side_effect = [
            response_with(503, retry_after="Wed, 25 Nov 2026 12:00:07 GMT"),
            response_with(200),
        ]

        # Act
        with patch("p2_migration.retries.datetime") as clock:
            clock.now.return_value = datetime(2026, 11, 25, 12, 0, 0, tzinfo=timezone.utc)
            request_with_retries(self.request, self.url, read_only=True)

        # Assert
        self.sleep.assert_called_once_with(7)

    # A lengthy server-requested wait stops the run instead of retrying too early.
    def test_long_retry_after_stops_without_waiting_or_retrying(self):
        # Arrange
        self.request.return_value = response_with(503, retry_after="120")

        # Act
        with self.assertRaisesRegex(RuntimeError, "longer than 30s"):
            request_with_retries(self.request, self.url, read_only=True)

        # Assert
        self.request.assert_called_once()
        self.sleep.assert_not_called()

    # Malformed or expired Retry-After values retain the normal backoff.
    def test_invalid_or_expired_retry_after_uses_normal_wait(self):
        # Arrange
        values = ("invalid", "-5", "0", "Sat, 01 Jan 2000 00:00:00 GMT")

        for value in values:
            with self.subTest(value=value):
                self.sleep.reset_mock()
                request = Mock(side_effect=[response_with(503, retry_after=value), response_with(200)])

                # Act
                request_with_retries(request, self.url, read_only=True)

                # Assert
                self.sleep.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
