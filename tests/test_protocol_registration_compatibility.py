# -*- coding: utf-8 -*-
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from config import SENTINEL_FRAME_URL, SENTINEL_SDK_SHA256, SENTINEL_SDK_URL, SENTINEL_SV
from core import openai_auth, sentinel_runner
from core.session import BrowserSession


class _Response:
    def __init__(self, status_code=200, payload=None, url="https://auth.openai.com/"):
        self.status_code = status_code
        self.payload = payload if payload is not None else {}
        self.text = json.dumps(self.payload, ensure_ascii=False)
        self.headers = {}
        self.history = []
        self.url = url

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _SentinelSession:
    device_id = "device-123"
    sentinel_sid = "sid-123"
    browser_profile = {"user_agent": "test-agent"}

    def __init__(self, responses=None):
        self.responses = list(responses or [_Response(payload={"token": "challenge"})])
        self.calls = []

    def get_sentinel_headers(self):
        return {"content-type": "text/plain;charset=UTF-8"}

    def post(self, url, headers=None, data=None):
        self.calls.append((url, headers or {}, data or ""))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def auth_cookie_header(self):
        return "oai-did=device-123"


class _AuthApiSession:
    device_id = "device-123"

    def __init__(self, navigation_id="nav-123"):
        self.auth_document_navigation_id = navigation_id
        self.calls = []

    def get_auth_headers(self, referer):
        return {"content-type": "application/json", "referer": referer}

    def attach_auth_document_navigation_header(self, headers):
        if self.auth_document_navigation_id:
            headers["x-openai-document-navigation-id"] = self.auth_document_navigation_id
        return headers

    def post(self, url, headers=None, data=None):
        self.calls.append((url, headers or {}, data or ""))
        payload = {"page": {"type": "about_you"}, "continue_url": "https://auth.openai.com/about-you"}
        return _Response(payload=payload, url=url)


class SentinelResourceTests(unittest.TestCase):
    def test_config_and_local_resource_match_current_version(self):
        self.assertEqual(SENTINEL_SV, "20260810913b")
        self.assertEqual(SENTINEL_SDK_URL, "https://sentinel.openai.com/sentinel/20260810913b/sdk.js")
        self.assertEqual(SENTINEL_FRAME_URL, "https://sentinel.openai.com/backend-api/sentinel/frame.html?sv=20260810913b")
        self.assertEqual(len(SENTINEL_SDK_SHA256), 64)
        sentinel_runner.validate_sentinel_resource_consistency()

    def test_sdk_hash_normalizes_windows_line_endings(self):
        original_path = sentinel_runner._SDK_PATH
        try:
            sentinel_runner._SDK_PATH = SimpleNamespace(read_bytes=lambda: b"sdk-line\r\n")
            expected = __import__("hashlib").sha256(b"sdk-line\n").hexdigest()
            self.assertEqual(sentinel_runner._sdk_sha256(), expected)
        finally:
            sentinel_runner._SDK_PATH = original_path

    def test_resource_mismatch_stops_before_runner(self):
        with patch.object(sentinel_runner, "SENTINEL_SDK_SHA256", "0" * 64):
            with self.assertRaisesRegex(RuntimeError, "SDK 资源版本不一致") as ctx:
                sentinel_runner.validate_sentinel_resource_consistency()
        self.assertNotIn("token", str(ctx.exception).lower())
        self.assertNotIn("cookie", str(ctx.exception).lower())

    def test_runner_debug_command_redacts_cookie(self):
        process = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"p": "p", "c": "sentinel-placeholder", "id": "device", "flow": "authorize_continue"}),
            stderr="",
        )
        with patch.object(sentinel_runner, "_ensure_runner_environment"), patch.object(
            sentinel_runner.subprocess, "run", return_value=process
        ):
            with self.assertLogs("core.sentinel_runner", level="DEBUG") as captured:
                sentinel_runner.generate_sentinel_token(
                    {},
                    "authorize_continue",
                    "device",
                    cookie="cookie-secret-placeholder",
                )
        output = "\n".join(captured.output)
        self.assertNotIn("cookie-secret-placeholder", output)
        self.assertIn("<redacted>", output)


class SentinelFlowTests(unittest.TestCase):
    def _runner_header(self, flow, *, device_id="device-123", include_so=False):
        payload = {"p": "p", "c": "enforcement-placeholder", "id": device_id, "flow": flow}
        if include_so:
            payload["so"] = "so-placeholder"
        return json.dumps(payload, separators=(",", ":"))

    def test_fresh_headers_use_target_flow_and_optional_so(self):
        session = _SentinelSession()
        with patch.object(
            openai_auth,
            "generate_sentinel_token",
            return_value=self._runner_header("authorize_continue", include_so=True),
        ):
            sentinel_header, so_header = openai_auth.get_fresh_sentinel_headers(session, "authorize_continue")

        request_body = json.loads(session.calls[0][2])
        self.assertEqual(request_body["id"], session.device_id)
        self.assertEqual(request_body["flow"], "authorize_continue")
        self.assertEqual(json.loads(sentinel_header)["flow"], "authorize_continue")
        self.assertEqual(json.loads(so_header)["flow"], "authorize_continue")

    def test_create_account_flow_is_distinct_and_mismatch_is_rejected(self):
        session = _SentinelSession()
        with patch.object(
            openai_auth,
            "generate_sentinel_token",
            return_value=self._runner_header("oauth_create_account"),
        ):
            header, so_header = openai_auth.get_fresh_sentinel_headers(session, "oauth_create_account")
        self.assertEqual(json.loads(header)["flow"], "oauth_create_account")
        self.assertIsNone(so_header)
        self.assertEqual(json.loads(session.calls[0][2])["flow"], "oauth_create_account")

        with patch.object(
            openai_auth,
            "generate_sentinel_token",
            return_value=self._runner_header("oauth_create_account"),
        ):
            with self.assertRaisesRegex(RuntimeError, "flow"):
                openai_auth.build_sentinel_header(
                    session,
                    {"token": "challenge"},
                    "authorize_continue",
                )

    def test_transient_retry_gets_new_challenge_for_same_flow(self):
        session = _SentinelSession([
            ConnectionError("connection reset"),
            _Response(payload={"token": "challenge-2"}),
        ])
        with patch.object(openai_auth, "generate_requirements_token", side_effect=["p-one", "p-two"]), patch.object(
            openai_auth.time, "sleep"
        ):
            openai_auth.request_sentinel_token(session, "authorize_continue")

        self.assertEqual(len(session.calls), 2)
        self.assertEqual(json.loads(session.calls[0][2])["flow"], "authorize_continue")
        self.assertEqual(json.loads(session.calls[1][2])["flow"], "authorize_continue")
        self.assertNotEqual(json.loads(session.calls[0][2])["p"], json.loads(session.calls[1][2])["p"])

    def test_unsupported_flow_is_rejected_without_network(self):
        session = _SentinelSession()
        with self.assertRaisesRegex(ValueError, "不支持"):
            openai_auth.request_sentinel_token(session, "username_password_create")
        self.assertEqual(session.calls, [])


class AuthContextAndBoundaryTests(unittest.TestCase):
    def test_navigation_id_is_captured_from_auth_redirect_history_case_insensitively(self):
        session = BrowserSession.__new__(BrowserSession)
        session.blocked_until = 0.0
        session.blocked_reason = ""
        session._cf_cookie_seen = {}
        session.auth_document_navigation_id = ""
        history = _Response(url="https://auth.openai.com/log-in")
        history.headers = {"X-OpenAI-Document-Navigation-Id": "nav-from-history"}
        response = _Response(url="https://auth.openai.com/email-verification")
        response.history = [history]

        session._observe_response_for_circuit_breaker(response, response.url)
        self.assertEqual(session.auth_document_navigation_id, "nav-from-history")
        session.reset_auth_document_navigation_context()
        self.assertEqual(session.auth_document_navigation_id, "")

    def test_auth_business_requests_conditionally_forward_navigation_id(self):
        session = _AuthApiSession("nav-real")
        openai_auth.validate_email_otp(session, "123456", "sentinel-placeholder", "so-placeholder")
        openai_auth.create_account(session, "Alice Example", "1990-01-01", "sentinel-placeholder")
        self.assertEqual(
            session.calls[0][1]["x-openai-document-navigation-id"],
            "nav-real",
        )
        self.assertEqual(
            session.calls[1][1]["x-openai-document-navigation-id"],
            "nav-real",
        )

        missing = _AuthApiSession("")
        openai_auth.validate_email_otp(missing, "123456", "sentinel-placeholder")
        self.assertNotIn("x-openai-document-navigation-id", missing.calls[0][1])

    def test_auth_logs_do_not_include_otp_or_token_values(self):
        session = _AuthApiSession("nav-real")
        with self.assertLogs("core.openai_auth", level="INFO") as captured:
            openai_auth.validate_email_otp(session, "123456", "token-secret-placeholder")
        output = "\n".join(captured.output)
        self.assertNotIn("123456", output)
        self.assertNotIn("token-secret-placeholder", output)

    def test_authorize_403_opens_session_circuit_and_stops_follow_up_network(self):
        class Transport:
            def __init__(self):
                self.calls = []

            def get(self, url, headers=None, **kwargs):
                self.calls.append(("GET", url))
                return _Response(403, url=url)

            def post(self, url, headers=None, **kwargs):
                self.calls.append(("POST", url))
                return _Response(200, url=url)

        transport = Transport()
        session = BrowserSession.__new__(BrowserSession)
        session.session = transport
        session.blocked_until = 0.0
        session.blocked_reason = ""
        session._cf_cookie_seen = {}
        session.auth_document_navigation_id = ""
        session.datadog_origin = "rum"
        session.datadog_trace_id = "1"
        session.datadog_parent_id = "2"
        authorize_url = "https://auth.openai.com/api/accounts/authorize?client_id=app_x&state=state-placeholder"

        with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
            openai_auth.follow_authorize(session, authorize_url)
        self.assertEqual(transport.calls, [("GET", authorize_url)])
        self.assertGreater(session.blocked_until, time.time())
        with self.assertRaisesRegex(RuntimeError, "熔断"):
            session.get("https://auth.openai.com/api/accounts/email-otp/send")
        self.assertEqual(transport.calls, [("GET", authorize_url)])


if __name__ == "__main__":
    unittest.main()
