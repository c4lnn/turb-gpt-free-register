# -*- coding: utf-8 -*-
import unittest
from unittest.mock import Mock

from core.chatgpt_checkout import (
    CHECKOUT_SESSION_PATH,
    CHECKOUT_SESSION_URL,
    CheckoutSettings,
    check_checkout_session,
    classify_checkout_session_id,
    extract_checkout_session_id,
    public_checkout_result,
)


class FakeResponse:
    def __init__(self, status_code, payload=None, text=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else ("" if payload is None else __import__("json").dumps(payload))
        self.content = self.text.encode("utf-8")
        self.headers = headers or {"content-type": "application/json"}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        pass


class FakeBrowserSession:
    def __init__(self, transport):
        self.device_id = "device-test"
        self.oai_session_id = "session-test"
        self.session = transport

    def get_chatgpt_headers(self, referer="https://chatgpt.com/login"):
        return {
            "user-agent": "fake-agent",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/json",
            "referer": referer,
        }


def settings(**kwargs):
    return CheckoutSettings(
        proxy_mode="direct",
        billing_country="DE",
        billing_currency="EUR",
        max_attempts=kwargs.pop("max_attempts", 2),
        retry_delay=0,
        **kwargs,
    )


class CheckoutProtocolTests(unittest.TestCase):
    def test_request_shape_and_success_are_first_stage_only(self):
        transport = FakeTransport([FakeResponse(200, {"checkout_session_id": "oaics_test-id", "client_secret": "secret"})])
        env = FakeBrowserSession(transport)
        result = check_checkout_session(
            "Bearer at-secret",
            settings=settings(),
            session_factory=lambda **_: env,
            sleep=lambda _: None,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["checkout_session_type"], "oaics")
        self.assertEqual(result["checkout_session_id"], "oaics_test-id")
        self.assertEqual(len(transport.calls), 1)
        args, kwargs = transport.calls[0]
        self.assertEqual(args[0], CHECKOUT_SESSION_URL)
        self.assertEqual(kwargs["allow_redirects"], False)
        self.assertEqual(kwargs["json"]["billing_details"], {"country": "DE", "currency": "EUR"})
        self.assertEqual(kwargs["json"]["promo_campaign"]["promo_campaign_id"], "plus-1-month-free")
        headers = kwargs["headers"]
        self.assertEqual(headers["authorization"], "Bearer at-secret")
        self.assertEqual(headers["origin"], "https://chatgpt.com")
        self.assertEqual(headers["referer"], "https://chatgpt.com/")
        self.assertEqual(headers["oai-device-id"], "device-test")
        self.assertEqual(headers["oai-session-id"], "session-test")
        self.assertEqual(headers["x-openai-target-path"], CHECKOUT_SESSION_PATH)
        self.assertNotIn("client_secret", result)
        self.assertNotIn("payload", result)

    def test_id_extraction_is_priority_aware_and_recursive(self):
        self.assertEqual(
            extract_checkout_session_id({
                "id": "low",
                "session_id": "middle",
                "nested": {"checkout_session_id": "high"},
            }),
            "middle",
        )
        self.assertEqual(
            extract_checkout_session_id({
                "id": "low",
                "session_id": "middle",
                "checkout_session_id": "high",
            }),
            "high",
        )
        self.assertEqual(extract_checkout_session_id({"items": [{"id": "cs_test_nested"}]}), "cs_test_nested")
        self.assertEqual(classify_checkout_session_id("cs_live_demo"), "cs_live")
        self.assertEqual(classify_checkout_session_id("cs_test_demo"), "other_cs")
        self.assertEqual(classify_checkout_session_id("custom_demo"), "unknown")

    def test_terminal_statuses_do_not_retry(self):
        for status in (401, 402, 403, 429, 404, 302):
            transport = FakeTransport([FakeResponse(status, {"error": {"code": "nope", "message": "blocked"}})])
            env = FakeBrowserSession(transport)
            result = check_checkout_session(
                "at-secret",
                settings=settings(max_attempts=4),
                session_factory=lambda **_: env,
                sleep=lambda _: self.fail("终止状态不应等待重试"),
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["attempt_count"], 1)
            self.assertEqual(len(transport.calls), 1)

    def test_status_400_is_retryable(self):
        transport = FakeTransport([
            FakeResponse(400, {"error": {"code": "temporary", "message": "try again"}}),
            FakeResponse(200, {"id": "cs_live_after_400"}),
        ])
        env = FakeBrowserSession(transport)
        result = check_checkout_session(
            "at-secret",
            settings=settings(max_attempts=2),
            session_factory=lambda **_: env,
            sleep=lambda _: None,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["checkout_session_type"], "cs_live")
        self.assertEqual(result["attempt_count"], 2)
        self.assertEqual(len(transport.calls), 2)

    def test_retry_reuses_one_browser_session_and_request_context(self):
        transport = FakeTransport([
            FakeResponse(503, {"error": {"code": "temporary"}}),
            FakeResponse(200, {"session": {"id": "cs_live_final"}}),
        ])
        env = FakeBrowserSession(transport)
        factory = Mock(return_value=env)
        result = check_checkout_session(
            "at-secret",
            settings=settings(max_attempts=2),
            session_factory=factory,
            sleep=lambda _: None,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["checkout_session_type"], "cs_live")
        factory.assert_called_once()
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(transport.calls[0][0][0], transport.calls[1][0][0])
        self.assertEqual(transport.calls[0][1]["json"], transport.calls[1][1]["json"])
        self.assertEqual(transport.calls[0][1]["headers"]["oai-session-id"], "session-test")

    def test_retry_after_is_used_and_explicit_direct_never_passes_proxy(self):
        transport = FakeTransport([
            FakeResponse(503, {"error": {"code": "temporary"}}, headers={"retry-after": "2"}),
            FakeResponse(200, {"id": "custom-id"}),
        ])
        env = FakeBrowserSession(transport)
        factory = Mock(return_value=env)
        direct = settings(max_attempts=2)
        direct = CheckoutSettings(**{**direct.__dict__, "proxy": "http://user:pass@example.invalid:8080"})
        waits = []
        result = check_checkout_session(
            "at-secret",
            settings=direct,
            proxy="",
            session_factory=factory,
            sleep=waits.append,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(waits, [2.0])
        self.assertEqual(factory.call_args.kwargs["proxy"], "")
        self.assertEqual(result["network_route"], "direct")
        self.assertIsNone(result["proxy_used"])

    def test_transport_and_parse_failures_are_retryable(self):
        transport = FakeTransport([
            TimeoutError("proxy password should not leak"),
            FakeResponse(200, None, text="not-json", headers={"content-type": "text/html"}),
        ])
        env = FakeBrowserSession(transport)
        result = check_checkout_session(
            "at-secret",
            settings=settings(max_attempts=2),
            session_factory=lambda **_: env,
            sleep=lambda _: None,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["attempt_count"], 2)
        self.assertEqual(result["error_code"], "invalid_json")
        self.assertNotIn("at-secret", str(result))

    def test_config_error_happens_before_any_downstream_request(self):
        factory = Mock()
        result = check_checkout_session(
            "at-secret",
            settings=CheckoutSettings(proxy_mode="proxy", billing_country="DE", billing_currency="EUR"),
            session_factory=factory,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "configuration_error")
        factory.assert_not_called()

    def test_public_result_removes_full_id(self):
        result = public_checkout_result({
            "ok": True,
            "checkout_session_id": "cs_live_sensitive",
            "proxy": "http://user:pass@example.invalid:8080",
            "error": None,
        })
        self.assertNotIn("checkout_session_id", result)
        self.assertNotIn("proxy", result)


if __name__ == "__main__":
    unittest.main()
