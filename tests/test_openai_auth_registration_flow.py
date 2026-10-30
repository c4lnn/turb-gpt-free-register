# -*- coding: utf-8 -*-
import json
import unittest
from unittest.mock import patch

import core.openai_auth as openai_auth
from core.session import BrowserSession


class _Response:
    def __init__(self, *, status_code=200, url="", payload=None, text=""):
        self.status_code = status_code
        self.url = url
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _ProtocolSession:
    def __init__(self):
        self.device_id = "device-id"
        self.document_navigation_id = "document-1"
        self.browser_profile = {"user_agent": "Test UA", "navigator_language": "ja-JP"}
        self.posts = []
        self.gets = []

    def get_auth_headers(self, referer=""):
        return {"referer": referer, "x-openai-document-navigation-id": self.document_navigation_id}

    def get_auth_navigate_headers(self, referer="", **_kwargs):
        return {"referer": referer}

    def get_sentinel_headers(self):
        return {"content-type": "text/plain;charset=UTF-8"}

    def get_sentinel_frame_headers(self, user_initiated=False):
        return {"sec-fetch-dest": "iframe", "user-initiated": user_initiated}

    def auth_cookie_header(self):
        return "oai-did=device-id"

    def rotate_document_navigation_id(self):
        old = self.document_navigation_id
        self.document_navigation_id = old + "-next"
        return self.document_navigation_id

    def post(self, url, *, headers, data):
        self.posts.append((url, headers, data))
        if url.endswith("/sentinel/req"):
            return _Response(payload={"token": "challenge", "persona": "p"})
        return _Response(payload={"continue_url": "/api/accounts/email-otp/send"})

    def get(self, url, *, headers, allow_redirects):
        self.gets.append((url, headers, allow_redirects))
        if "sentinel/frame.html" in url:
            return _Response(url=url)
        return _Response(url="https://auth.openai.com/email-verification")


class OpenAIRegistrationFlowTests(unittest.TestCase):


    def test_navigate_create_account_password_switches_from_passwordless_page(self):
        class Session(_ProtocolSession):
            def get(self, url, *, headers, allow_redirects):
                self.gets.append((url, headers, allow_redirects))
                return _Response(url="https://auth.openai.com/create-account/password")

        session = Session()
        final_url = openai_auth.navigate_create_account_password(
            session, "https://auth.openai.com/email-verification"
        )

        url, headers, redirects = session.gets[-1]
        self.assertEqual(url, "https://auth.openai.com/create-account/password")
        self.assertEqual(headers["referer"], "https://auth.openai.com/email-verification")
        self.assertEqual(headers["sec-fetch-site"], "same-origin")
        self.assertEqual(headers["sec-fetch-user"], "?1")
        self.assertTrue(redirects)
        self.assertEqual(final_url, "https://auth.openai.com/create-account/password")
        self.assertEqual(session.document_navigation_id, "document-1-next")

    def test_email_otp_send_accepts_relative_url_and_rotates_document_id(self):
        session = _ProtocolSession()

        final_url = openai_auth.navigate_email_otp_send(
            session, "/api/accounts/email-otp/send"
        )

        url, headers, redirects = session.gets[-1]
        self.assertEqual(url, "https://auth.openai.com/api/accounts/email-otp/send")
        self.assertEqual(headers["referer"], "https://auth.openai.com/create-account/password")
        self.assertEqual(headers["sec-fetch-site"], "same-origin")
        self.assertEqual(headers["sec-fetch-user"], "?1")
        self.assertTrue(redirects)
        self.assertEqual(final_url, "https://auth.openai.com/email-verification")
        self.assertEqual(session.document_navigation_id, "document-1-next")





    def test_challenge_network_failure_closes_sdk_without_exposing_credentials(self):
        class SDK:
            closed = False

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.closed = True

            def requirements(self):
                return "request-proof"

            def solve(self, _challenge):
                raise AssertionError("a failed HTTP challenge must not be solved")

        class Session(_ProtocolSession):
            def post(self, _url, *, headers, data):
                raise ConnectionError("proxy credential must-not-leak")

        sdk = SDK()
        with patch.object(openai_auth, "SentinelSDK", return_value=sdk), patch.object(
            openai_auth, "_proxy_retry_config", return_value=(1, 0)
        ):
            with self.assertRaises(openai_auth.SentinelRequestError) as raised:
                openai_auth.request_sentinel_token(Session(), "username_password_create")
        self.assertTrue(sdk.closed)
        self.assertEqual(raised.exception.category, "sentinel_network_failed")
        self.assertNotIn("must-not-leak", str(raised.exception))

    def _solved_response(self, session, flow, *, so_required=False):
        return {
            "token": "current-challenge",
            "so": {"required": so_required},
            "_sentinel_flow": flow,
            "_sentinel_device_id": session.device_id,
            "_sentinel_header": json.dumps({
                "p": "proof", "c": "current-challenge",
                "id": session.device_id, "flow": flow,
            }),
            "_sentinel_so_header": None,
        }

    def test_result_from_another_flow_cannot_be_consumed(self):
        session = _ProtocolSession()
        result = self._solved_response(session, "username_password_create")
        with self.assertRaises(openai_auth.SentinelRequestError):
            openai_auth.build_sentinel_header(session, result, "oauth_create_account")

    def test_result_from_another_device_cannot_be_consumed(self):
        session = _ProtocolSession()
        result = self._solved_response(session, "username_password_create")
        session.device_id = "other-device"
        with self.assertRaises(openai_auth.SentinelRequestError):
            openai_auth.build_sentinel_header(session, result, "username_password_create")

    def test_proof_from_another_challenge_cannot_be_consumed(self):
        session = _ProtocolSession()
        result = self._solved_response(session, "username_password_create")
        result["token"] = "next-challenge"
        with self.assertRaises(openai_auth.SentinelRequestError):
            openai_auth.build_sentinel_header(session, result, "username_password_create")

    def test_empty_so_is_only_accepted_when_current_challenge_does_not_require_it(self):
        session = _ProtocolSession()
        result = self._solved_response(session, "username_password_create")
        _header, so = openai_auth.build_sentinel_header(session, result, "username_password_create")
        self.assertIsNone(so)
        result["so"]["required"] = True
        with self.assertRaises(openai_auth.SentinelRequestError):
            openai_auth.build_sentinel_header(session, result, "username_password_create")

    def test_browser_session_keeps_document_id_until_explicit_navigation(self):
        session = object.__new__(BrowserSession)
        session.document_navigation_id = "document-a"
        session.datadog_trace_id = "1"
        session.datadog_parent_id = "2"
        session.datadog_origin = "rum"

        first = session._attach_auth_rum_headers({})
        second = session._attach_auth_rum_headers({})
        self.assertEqual(first["x-openai-document-navigation-id"], "document-a")
        self.assertEqual(second["x-openai-document-navigation-id"], "document-a")
        self.assertNotEqual(first["x-access-flow-invocation-id"], second["x-access-flow-invocation-id"])
        self.assertNotEqual(first["x-datadog-trace-id"], second["x-datadog-trace-id"])
        self.assertNotEqual(first["x-datadog-parent-id"], second["x-datadog-parent-id"])

        rotated = session.rotate_document_navigation_id()
        third = session._attach_auth_rum_headers({})
        self.assertNotEqual(rotated, "document-a")
        self.assertEqual(third["x-openai-document-navigation-id"], rotated)


if __name__ == "__main__":
    unittest.main()
