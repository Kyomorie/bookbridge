"""Hardcover API failures must be visible once, then quiet, and actionable.

On 2026-08-20 Hardcover reset long-lived JWTs without notice during its API beta
and its personal-access-token path returned 500s from their own backend. The
bridge logged every attempt at ERROR with the raw response body — a full HTML
error page — with no guidance and no recovery signal, so a dead tracker looked
identical to a working one apart from progress silently ceasing to advance.

These tests use the three response shapes actually observed that day.
"""

import logging
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.api.hardcover_client import HardcoverClient
from src.utils.logging_utils import get_persistent_condition_logger

# The exact bodies Hardcover returned on 2026-08-20.
_401_BODY = (
    '{"error":"invalid_token","error_description":'
    '"Token is not associated with a user"}'
)
_500_BODY = (
    "<!doctype html>\n<html lang=\"en\">\n<head>\n  "
    "<title>Internal Server Error | Hardcover</title>\n  "
    "<meta charset=\"utf-8\">\n</head>\n<body>\n"
    "We're sorry, but something went wrong.\n" + ("padding " * 80) + "</body></html>"
)
_HASURA_ERRORS = [
    {
        "message": 'missing session variable: "x-hasura-visible-user-status-ids"',
        "extensions": {"path": "$", "code": "not-found"},
    }
]

_LEGACY_JWT = "header.payload.signature"
_PAT = "hc_pat_" + "z" * 44


def _response(status, body="", headers=None, json_body=None):
    return SimpleNamespace(
        status_code=status,
        text=body,
        headers=headers or {},
        json=lambda: json_body if json_body is not None else {},
    )


class HardcoverFailureVisibilityTestCase(unittest.TestCase):
    def setUp(self):
        # The condition logger's counters are process-lifetime, so a leaked count
        # would silence a later test's first-occurrence assertion.
        condition_logger = get_persistent_condition_logger()
        self._saved_counts = dict(condition_logger._counts)
        condition_logger._counts.clear()
        self.addCleanup(self._restore_counts)

    def _restore_counts(self):
        condition_logger = get_persistent_condition_logger()
        condition_logger._counts.clear()
        condition_logger._counts.update(self._saved_counts)

    def _client(self, token=_PAT):
        with patch(
            "src.api.hardcover_client.resolve_setting", return_value=token
        ):
            return HardcoverClient(credentials={})

    def _query(self, client, response):
        # Sleep is patched out: a 5xx read is retried with backoff, and real waits
        # would add seconds to the suite for no coverage.
        with patch("src.api.hardcover_client.time.sleep"), patch(
            "src.api.hardcover_client.requests.post", return_value=response
        ):
            return client.query("{ me { id } }")


class TestFrozenLogContract(HardcoverFailureVisibilityTestCase):
    def test_http_prefix_is_unchanged(self):
        """Issue reporters grep for this prefix — it must survive verbatim."""
        client = self._client()

        with self.assertLogs("src.api.hardcover_client", level="ERROR") as captured:
            self._query(client, _response(401, _401_BODY))

        self.assertTrue(
            any("❌ HTTP 401: " in line for line in captured.output), captured.output
        )

    def test_graphql_errors_prefix_is_unchanged(self):
        client = self._client()

        with self.assertLogs("src.api.hardcover_client", level="ERROR") as captured:
            self._query(
                client,
                _response(200, json_body={"errors": _HASURA_ERRORS}),
            )

        self.assertTrue(
            any("❌ GraphQL errors: " in line for line in captured.output),
            captured.output,
        )

    def test_failures_are_still_error_level(self):
        """Severity must not drop just because repeats are suppressed."""
        client = self._client()

        with self.assertLogs("src.api.hardcover_client", level="ERROR") as captured:
            self._query(client, _response(500, _500_BODY))

        self.assertEqual(captured.records[0].levelno, logging.ERROR)


class TestRepeatSuppression(HardcoverFailureVisibilityTestCase):
    def test_repeat_failures_drop_below_error(self):
        client = self._client()
        response = _response(401, _401_BODY)

        with self.assertLogs("src.api.hardcover_client", level="DEBUG") as captured:
            for _ in range(5):
                self._query(client, response)

        errors = [r for r in captured.records if r.levelno >= logging.ERROR]
        self.assertEqual(len(errors), 1, [r.getMessage() for r in captured.records])

    def test_a_different_failure_shape_still_surfaces(self):
        """401 then 500 are separate conditions; the second must not be swallowed."""
        client = self._client()

        with self.assertLogs("src.api.hardcover_client", level="DEBUG") as captured:
            self._query(client, _response(401, _401_BODY))
            self._query(client, _response(500, _500_BODY))

        errors = [r.getMessage() for r in captured.records if r.levelno >= logging.ERROR]
        self.assertEqual(len(errors), 2, errors)
        self.assertTrue(any("HTTP 401" in m for m in errors), errors)
        self.assertTrue(any("HTTP 500" in m for m in errors), errors)

    def test_recovery_is_announced(self):
        client = self._client()
        self._query(client, _response(401, _401_BODY))

        with self.assertLogs("src.api.hardcover_client", level="INFO") as captured:
            result = self._query(
                client, _response(200, json_body={"data": {"me": {"id": 1}}})
            )

        self.assertEqual(result, {"me": {"id": 1}})
        self.assertTrue(
            any("Hardcover API reachable again" in line for line in captured.output),
            captured.output,
        )

    def test_recovery_resets_so_a_new_outage_is_loud_again(self):
        client = self._client()
        self._query(client, _response(401, _401_BODY))
        self._query(client, _response(200, json_body={"data": {"me": {"id": 1}}}))

        with self.assertLogs("src.api.hardcover_client", level="ERROR") as captured:
            self._query(client, _response(401, _401_BODY))

        self.assertTrue(
            any("❌ HTTP 401: " in line for line in captured.output), captured.output
        )


class TestActionableGuidance(HardcoverFailureVisibilityTestCase):
    def test_legacy_jwt_is_told_to_generate_a_pat(self):
        client = self._client(token=_LEGACY_JWT)

        with self.assertLogs("src.api.hardcover_client", level="ERROR") as captured:
            self._query(client, _response(401, _401_BODY))

        message = captured.output[0]
        self.assertIn("legacy JWT", message)
        self.assertIn("hc_pat_", message)

    def test_pat_rejection_mentions_expiry_not_the_jwt_advice(self):
        client = self._client(token=_PAT)

        with self.assertLogs("src.api.hardcover_client", level="ERROR") as captured:
            self._query(client, _response(401, _401_BODY))

        message = captured.output[0]
        self.assertIn("expired", message)
        self.assertNotIn("legacy JWT", message)

    def test_server_error_is_attributed_to_hardcover_with_a_request_id(self):
        """A 500 is theirs; the request id is what their support can act on."""
        client = self._client()
        response = _response(
            500, _500_BODY, headers={"X-Request-Id": "b40b1948-4917-48b8"}
        )

        with self.assertLogs("src.api.hardcover_client", level="ERROR") as captured:
            self._query(client, response)

        message = captured.output[0]
        self.assertIn("error inside Hardcover", message)
        self.assertIn("b40b1948-4917-48b8", message)
        self.assertNotIn("Account -> Integrations", message)

    def test_server_error_without_a_request_id_still_logs(self):
        client = self._client()

        with self.assertLogs("src.api.hardcover_client", level="ERROR") as captured:
            self._query(client, _response(500, _500_BODY))

        self.assertIn("error inside Hardcover", captured.output[0])


class TestBodyTrimming(HardcoverFailureVisibilityTestCase):
    def test_html_error_page_is_reduced_to_its_title(self):
        client = self._client()

        with self.assertLogs("src.api.hardcover_client", level="ERROR") as captured:
            self._query(client, _response(500, _500_BODY))

        message = captured.output[0]
        self.assertIn("Internal Server Error | Hardcover", message)
        self.assertNotIn("<!doctype html>", message)
        self.assertNotIn("padding padding", message)

    def test_json_error_body_is_preserved(self):
        """The useful case must not be trimmed away with the noisy one."""
        client = self._client()

        with self.assertLogs("src.api.hardcover_client", level="ERROR") as captured:
            self._query(client, _response(401, _401_BODY))

        self.assertIn("Token is not associated with a user", captured.output[0])

    def test_a_long_non_html_body_is_bounded(self):
        client = self._client()

        with self.assertLogs("src.api.hardcover_client", level="ERROR") as captured:
            self._query(client, _response(418, "x" * 5000))

        self.assertLess(len(captured.output[0]), 1000)


if __name__ == "__main__":
    unittest.main()


class TestTransientServerErrorRetry(HardcoverFailureVisibilityTestCase):
    """Hardcover's beta API fails in clusters; reads should ride over a blip.

    Measured 2026-08-20: 4/20 success on an identical query, longest failure
    streak 12. Their docs mark 503 "safe to retry".
    """

    def _query_with(self, client, responses):
        with patch("src.api.hardcover_client.time.sleep") as slept, patch(
            "src.api.hardcover_client.requests.post", side_effect=responses
        ) as posted:
            return client.query("{ me { id } }"), posted, slept

    def test_read_recovers_when_a_retry_succeeds(self):
        client = self._client()
        ok = _response(200, json_body={"data": {"me": {"id": 30726}}})

        result, posted, _ = self._query_with(
            client, [_response(500, _500_BODY), ok]
        )

        self.assertEqual(result, {"me": {"id": 30726}})
        self.assertEqual(posted.call_count, 2)

    def test_read_gives_up_after_bounded_attempts(self):
        client = self._client()
        responses = [_response(500, _500_BODY) for _ in range(5)]

        with self.assertLogs("src.api.hardcover_client", level="ERROR") as captured:
            result, posted, _ = self._query_with(client, responses)

        self.assertIsNone(result)
        self.assertEqual(posted.call_count, 3, "retries must stay bounded")
        self.assertTrue(any("❌ HTTP 500: " in line for line in captured.output))

    def test_retry_backs_off_between_attempts(self):
        client = self._client()
        responses = [_response(500, _500_BODY) for _ in range(3)]

        _result, _posted, slept = self._query_with(client, responses)

        self.assertEqual(slept.call_count, 2)

    def test_retry_after_header_is_honoured(self):
        client = self._client()
        responses = [
            _response(503, "", headers={"Retry-After": "7"}),
            _response(200, json_body={"data": {"me": {"id": 30726}}}),
        ]

        _result, _posted, slept = self._query_with(client, responses)

        slept.assert_called_once_with(7.0)

    def test_a_mutation_is_never_retried_on_5xx(self):
        """A write may already have applied server-side — retrying could double it."""
        client = self._client()

        with patch("src.api.hardcover_client.time.sleep"), patch(
            "src.api.hardcover_client.requests.post",
            side_effect=[_response(500, _500_BODY) for _ in range(3)],
        ) as posted:
            result = client.query("mutation { insert_user_book(object: {}) { id } }")

        self.assertIsNone(result)
        self.assertEqual(posted.call_count, 1)

    def test_auth_failure_is_not_retried(self):
        """A 401 will not fix itself; retrying just burns the rate limit."""
        client = self._client()

        with patch("src.api.hardcover_client.time.sleep"), patch(
            "src.api.hardcover_client.requests.post",
            side_effect=[_response(401, _401_BODY) for _ in range(3)],
        ) as posted:
            result = client.query("{ me { id } }")

        self.assertIsNone(result)
        self.assertEqual(posted.call_count, 1)
