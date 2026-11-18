# -*- coding: utf-8 -*-
import unittest
from urllib.parse import parse_qs, urlparse
from types import SimpleNamespace
from unittest.mock import Mock

from config.browser import build_browser_environment
from core.openai.transport.session import BrowserSession, HeaderProfile

from core.openai.auth.chatgpt_auth import (
    _ensure_authorize_context, build_login_referer, get_csrf_token, signin_openai,
)


class _Session:
    device_id = "did-123"
    auth_session_logging_id = "log-456"


class ChatgptAuthContextTests(unittest.TestCase):
    def test_context_normalization_preserves_authorization_state_and_pkce(self):
        url = (
            "https://auth.openai.com/api/accounts/authorize?"
            "state=opaque%2Bstate%2Fwith%3Dpadding&code_challenge=proof%2Bvalue"
            "&code_challenge_method=S256&redirect_uri=http%3A%2F%2Flocalhost%2Fcallback%3Fx%3D1"
            "&resource=first&resource=second&empty=&device_id=stale#continue"
        )
        before = parse_qs(urlparse(url).query, keep_blank_values=True)
        result = urlparse(_ensure_authorize_context(url, _Session(), "user@example.com"))
        after = parse_qs(result.query, keep_blank_values=True)
        for key in ("state", "code_challenge", "code_challenge_method",
                    "redirect_uri", "resource", "empty"):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(result.fragment, "continue")
        self.assertEqual(after["device_id"], [_Session.device_id])

    def test_uninitialized_device_cannot_be_bound_to_authorization(self):
        session = _Session()
        session.device_id = ""
        with self.assertRaises(RuntimeError):
            _ensure_authorize_context(
                "https://auth.openai.com/api/accounts/authorize?state=opaque",
                session, "user@example.com",
            )


    def test_signin_uses_dynamic_browser_context_headers_and_preserves_payload(self):
        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"url": "https://auth.openai.com/oauth/authorize?state=opaque", "csrfToken": "formal-csrf"}

        class SigninSession(BrowserSession):
            def __init__(self):
                self.posts = []
                self.gets = []
                self.browser_profile = build_browser_environment()
                self.device_id = _Session.device_id
                self.auth_session_logging_id = _Session.auth_session_logging_id

            def get(self, url, *, headers):
                self.gets.append((url, dict(headers)))
                return Response()

            def post(self, url, *, headers, data):
                self.posts.append((url, dict(headers), data))
                return Response()

        session = SigninSession()
        email = "test+tag@example.test"

        login_referer = build_login_referer(session, email)
        csrf = get_csrf_token(session, login_referer=login_referer)
        authorize_url = signin_openai(session, csrf, email, login_referer=login_referer)

        request_url, headers, body = session.posts[0]
        referer = urlparse(headers["referer"])
        csrf_url, csrf_headers = session.gets[0]
        self.assertEqual(csrf_url, "https://chatgpt.com/api/auth/csrf")
        self.assertEqual(csrf_headers["referer"], headers["referer"])
        self.assertNotIn("content-type", csrf_headers)
        for request_headers in (csrf_headers, headers):
            self.assertEqual(request_headers["cache-control"], "no-cache")
            self.assertEqual(request_headers["pragma"], "no-cache")
        referer_query = parse_qs(referer.query, keep_blank_values=True)
        self.assertEqual(referer.scheme, "https")
        self.assertEqual(referer.netloc, "chatgpt.com")
        self.assertEqual(referer.path, "/auth/login_with")
        self.assertEqual(referer_query, {
            "callback_path": ["/"],
            "screen_hint": ["login_or_signup"],
            "login_hint": [email],
            "auth_session_logging_id": [session.auth_session_logging_id],
            "ext-oai-did": [session.device_id],
        })
        self.assertEqual(headers["origin"], "https://chatgpt.com")
        self.assertEqual(headers["content-type"], "application/x-www-form-urlencoded")

        signin_query = parse_qs(urlparse(request_url).query, keep_blank_values=True)
        self.assertEqual(signin_query, {
            "prompt": ["login"],
            "auth_session_logging_id": [session.auth_session_logging_id],
            "screen_hint": ["login_or_signup"],
            "login_hint": [email],
        })
        self.assertEqual(parse_qs(body, keep_blank_values=True), {
            "callbackUrl": ["https://chatgpt.com/"],
            "csrfToken": ["formal-csrf"],
            "json": ["true"],
        })
        authorize_query = parse_qs(urlparse(authorize_url).query, keep_blank_values=True)
        self.assertEqual(authorize_query["state"], ["opaque"])
        self.assertEqual(authorize_query["device_id"], [session.device_id])
        self.assertEqual(authorize_query["login_hint"], [email])

    def test_nonregistration_auth_retains_its_request_semantics(self):

        session = BrowserSession.__new__(BrowserSession)
        session.browser_profile = {}
        session.device_id = "test-device"
        session.auth_session_logging_id = "test-logging"
        session.get = Mock(return_value=SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: {"csrfToken": "login-csrf"},
        ))
        session.post = Mock(return_value=SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"url": "https://auth.openai.com/oauth/authorize?state=test"},
        ))

        csrf = get_csrf_token(session, login_referer=None)
        signin_openai(session, csrf, "test+login@example.test", login_referer=None)

        csrf_headers = session.get.call_args.kwargs["headers"]
        self.assertEqual(csrf_headers["referer"], "https://chatgpt.com/auth/login")
        self.assertEqual(csrf_headers["content-type"], "application/json")
        signin_headers = session.post.call_args.kwargs["headers"]
        self.assertEqual(parse_qs(urlparse(signin_headers["referer"]).query)["login_hint"],
                         ["test+login@example.test"])
        for headers in (csrf_headers, signin_headers):
            self.assertNotIn("cache-control", headers)
            self.assertNotIn("pragma", headers)

    def test_password_profile_csrf_and_signin_share_context_and_formal_token(self):
        session = BrowserSession.__new__(BrowserSession)
        session.browser_profile = build_browser_environment()
        session.device_id = "fixture-device"
        session.auth_session_logging_id = "fixture-logging"
        session.get = Mock(return_value=SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: {"csrfToken": "formal-fixture"},
        ))
        session.post = Mock(return_value=SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"url": "https://auth.openai.com/oauth/authorize?state=fixture"},
        ))
        email = "fixture+tag@example.test"
        referer = build_login_referer(session, email)
        profile = HeaderProfile.PASSWORD_TOTP_LOGIN
        csrf = get_csrf_token(session, login_referer=referer, header_profile=profile)
        signin_openai(session, csrf, email, login_referer=referer, header_profile=profile)

        csrf_args = session.get.call_args
        signin_args = session.post.call_args
        self.assertEqual(csrf_args.args[0], "https://chatgpt.com/api/auth/csrf")
        csrf_headers = csrf_args.kwargs["headers"]
        signin_headers = signin_args.kwargs["headers"]
        self.assertNotIn("content-type", csrf_headers)
        self.assertNotIn("origin", csrf_headers)
        self.assertEqual(signin_headers["content-type"], "application/x-www-form-urlencoded")
        self.assertEqual(signin_headers["origin"], "https://chatgpt.com")
        for headers in (csrf_headers, signin_headers):
            self.assertEqual(headers["referer"], referer)
            self.assertEqual(headers["cache-control"], "no-cache")
            self.assertEqual(headers["pragma"], "no-cache")
            self.assertEqual(headers["sec-ch-ua-full-version"], session.browser_profile["chrome_full_version"])
            self.assertNotIn("priority", headers)
        self.assertEqual(parse_qs(urlparse(referer).query)["login_hint"], [email])
        self.assertEqual(parse_qs(signin_args.kwargs["data"])["csrfToken"], ["formal-fixture"])

if __name__ == "__main__":
    unittest.main()
