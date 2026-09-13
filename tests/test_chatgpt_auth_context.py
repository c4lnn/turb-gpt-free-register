# -*- coding: utf-8 -*-
import unittest
from urllib.parse import parse_qs, urlparse

from core.chatgpt_auth import _ensure_authorize_context, signin_openai


class _Session:
    device_id = "did-123"
    auth_session_logging_id = "log-456"


class _SigninResponse:
    status_code = 200
    text = ""

    def raise_for_status(self):
        return None

    def json(self):
        return {"url": "https://auth.openai.com/api/accounts/authorize?client_id=app_x&state=s"}


class _SigninSession(_Session):
    def __init__(self):
        self.calls = []

    def get_nextauth_headers(self, referer):
        return {"accept": "*/*"}

    def post(self, url, headers, data):
        self.calls.append((url, headers, data))
        return _SigninResponse()


class ChatgptAuthContextTests(unittest.TestCase):
    def test_ensure_authorize_context_matches_current_capture_shape(self):
        url = "https://auth.openai.com/api/accounts/authorize?client_id=app_x&state=s"
        out = _ensure_authorize_context(url, _Session(), "user@example.com")
        qs = parse_qs(urlparse(out).query)
        self.assertEqual(qs["ext-oai-did"], ["did-123"])
        self.assertEqual(qs["auth_session_logging_id"], ["log-456"])
        self.assertEqual(qs["screen_hint"], ["login_or_signup"])
        self.assertEqual(qs["login_hint"], ["user@example.com"])
        self.assertEqual(qs["ccaps"], ["login_methods chatgpt_login_finalizer_v1"])

    def test_ensure_authorize_context_preserves_upstream_oauth_and_passkey_values(self):
        url = (
            "https://auth.openai.com/api/accounts/authorize?"
            "client_id=app_x&scope=openid&audience=api&redirect_uri=https%3A%2F%2Fexample.test%2Fcb&"
            "state=s&device_id=upstream-device&ext-passkey-client-capabilities=upstream&ccaps=upstream"
        )
        out = _ensure_authorize_context(url, _Session(), "user@example.com")
        qs = parse_qs(urlparse(out).query)
        self.assertEqual(qs["client_id"], ["app_x"])
        self.assertEqual(qs["scope"], ["openid"])
        self.assertEqual(qs["audience"], ["api"])
        self.assertEqual(qs["redirect_uri"], ["https://example.test/cb"])
        self.assertEqual(qs["state"], ["s"])
        self.assertEqual(qs["device_id"], ["upstream-device"])
        self.assertEqual(qs["ext-passkey-client-capabilities"], ["upstream"])
        self.assertEqual(qs["ccaps"], ["upstream"])

    def test_signin_query_contains_returning_intent_without_forced_passkey(self):
        session = _SigninSession()
        out = signin_openai(session, "csrf-placeholder", "user@example.com")

        self.assertEqual(len(session.calls), 1)
        signin_url, headers, body = session.calls[0]
        query = parse_qs(urlparse(signin_url).query)
        self.assertEqual(query["prompt"], ["login"])
        self.assertEqual(query["ext-oai-did"], ["did-123"])
        self.assertEqual(query["auth_session_logging_id"], ["log-456"])
        self.assertEqual(query["returning_browser_login_intent"], ["true"])
        self.assertEqual(query["screen_hint"], ["login_or_signup"])
        self.assertEqual(query["login_hint"], ["user@example.com"])
        self.assertNotIn("ext-passkey-client-capabilities", query)
        self.assertEqual(parse_qs(body), {"callbackUrl": ["https://chatgpt.com/"], "csrfToken": ["csrf-placeholder"], "json": ["true"]})
        self.assertEqual(headers["content-type"], "application/x-www-form-urlencoded")
        self.assertEqual(parse_qs(urlparse(out).query)["ccaps"], ["login_methods chatgpt_login_finalizer_v1"])


if __name__ == "__main__":
    unittest.main()
