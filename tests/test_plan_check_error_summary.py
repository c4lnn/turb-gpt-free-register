# -*- coding: utf-8 -*-
import base64
import json
import time
import unittest
from unittest.mock import patch

from core import chatgpt_plan


class FakeResponse:
    def __init__(self, status_code, payload=None, *, text=None, json_error=False):
        self.status_code = status_code
        self.payload = payload
        self.text = json.dumps(payload) if text is None and payload is not None else (text or "")
        self.headers = {}
        self.json_error = json_error

    def json(self):
        if self.json_error:
            raise ValueError("invalid json")
        return self.payload


class FakeBrowserSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.device_id = "device-test"
        self.session = self

    def _get_common_headers(self):
        return {"user-agent": "test"}

    def navigator_language(self):
        return "en-US"

    def get(self, *_args, **_kwargs):
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def close(self):
        return None


def valid_plan_response():
    return FakeResponse(200, {
        "accounts": {
            "default": {
                "account": {"account_id": "acc-test", "plan_type": "free"},
                "entitlement": {},
            }
        }
    })


def jwt_with_payload(payload):
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).rstrip(b"=").decode("ascii")
    return f"header.{encoded}.signature"


class PlanCheckErrorClassificationTests(unittest.TestCase):
    def run_check(self, responses, *, max_attempts=1, token="not-a-jwt"):
        holder = {}
        fake_session = FakeBrowserSession(responses)

        def session_factory(*_args, **_kwargs):
            holder["session"] = fake_session
            return fake_session

        with (
            patch.object(
                chatgpt_plan,
                "resolve_plan_check_route",
                return_value={
                    "proxy": "",
                    "proxy_mode": "direct",
                    "network_route": "direct",
                    "proxy_used": None,
                    "proxy_fallback_reason": None,
                },
            ),
            patch.object(chatgpt_plan, "BrowserSession", side_effect=session_factory),
        ):
            return chatgpt_plan.check_account_plan(
                token,
                max_attempts=max_attempts,
                retry_delay=0,
            )

    def test_local_expired_token_is_classified_without_http_request(self):
        expired_token = jwt_with_payload({"exp": int(time.time()) - 60})
        with patch.object(chatgpt_plan, "BrowserSession") as browser_session:
            result = chatgpt_plan.check_account_plan(expired_token, max_attempts=1)

        self.assertFalse(result["ok"])
        self.assertEqual(result["plan_check_error_kind"], "local_token_expired")
        self.assertIsNone(result["http_status"])
        self.assertTrue(result["token_expired"])
        self.assertTrue(result["needs_live_check"])
        self.assertIn("本地AT已失效", result["error"])
        self.assertTrue(result["token_expires_at"])
        browser_session.assert_not_called()

    def test_future_or_missing_exp_does_not_use_local_expired_classification(self):
        future = self.run_check(
            [valid_plan_response()],
            token=jwt_with_payload({"exp": int(time.time()) + 3600}),
        )
        without_exp = self.run_check(
            [valid_plan_response()],
            token=jwt_with_payload({"sub": "fixture"}),
        )
        boolean_exp = self.run_check(
            [valid_plan_response()],
            token=jwt_with_payload({"exp": True}),
        )

        self.assertTrue(future["ok"])
        self.assertNotIn("plan_check_error_kind", future)
        self.assertTrue(without_exp["ok"])
        self.assertNotIn("plan_check_error_kind", without_exp)
        self.assertTrue(boolean_exp["ok"])
        self.assertNotIn("plan_check_error_kind", boolean_exp)

    def test_timeout_is_network_timeout(self):
        result = self.run_check([TimeoutError("request timed out")])
        self.assertFalse(result["ok"])
        self.assertEqual(result["plan_check_error_kind"], "network_timeout")

    def test_connection_error_is_network_connection(self):
        result = self.run_check([ConnectionError("connection refused")])
        self.assertFalse(result["ok"])
        self.assertEqual(result["plan_check_error_kind"], "network_connection")

    def test_http_4xx_and_5xx_keep_specific_status_for_display(self):
        result_401 = self.run_check([FakeResponse(401, {"error": "AT已失效"})])
        result_4xx = self.run_check([FakeResponse(403, {"error": "forbidden"})])
        result_5xx = self.run_check([FakeResponse(503, {"error": "temporary"})])
        self.assertEqual(result_401["plan_check_error_kind"], "http_4xx")
        self.assertEqual(result_401["http_status"], 401)
        self.assertNotEqual(result_401["plan_check_error_kind"], "local_token_expired")
        self.assertEqual(result_4xx["plan_check_error_kind"], "http_4xx")
        self.assertEqual(result_5xx["plan_check_error_kind"], "http_5xx")
        self.assertEqual(result_4xx["http_status"], 403)
        self.assertEqual(result_5xx["http_status"], 503)

    def test_invalid_json_and_invalid_shape_are_response_format(self):
        invalid_json = self.run_check([FakeResponse(200, text="not-json", json_error=True)])
        invalid_shape = self.run_check([FakeResponse(200, {"accounts": []})])
        self.assertEqual(invalid_json["plan_check_error_kind"], "response_format")
        self.assertEqual(invalid_shape["plan_check_error_kind"], "response_format")

    def test_success_after_retry_does_not_keep_previous_error_kind(self):
        result = self.run_check(
            [TimeoutError("temporary timeout"), valid_plan_response()],
            max_attempts=2,
        )
        self.assertTrue(result["ok"])
        self.assertNotIn("plan_check_error_kind", result)


class PlanCheckErrorClassifierUnitTests(unittest.TestCase):
    def test_local_expired_classifier_is_distinct_and_http_status_wins(self):
        self.assertEqual(
            chatgpt_plan.classify_plan_check_error(local_token_expired=True),
            "local_token_expired",
        )
        self.assertEqual(
            chatgpt_plan.classify_plan_check_error(
                http_status=401,
                local_token_expired=True,
            ),
            "http_4xx",
        )

    def test_http_status_takes_precedence_over_transport_exception(self):
        self.assertEqual(
            chatgpt_plan.classify_plan_check_error(
                http_status=502,
                exc=TimeoutError("response read timeout"),
            ),
            "http_5xx",
        )


if __name__ == "__main__":
    unittest.main()
