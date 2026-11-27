# -*- coding: utf-8 -*-
import json
import unittest
from urllib.parse import parse_qs, urlparse
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import requests

from config.browser import build_browser_environment
from core.infra import job_context
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

    def test_registration_opt_in_preserves_formal_auth_semantics(self):
        session = BrowserSession.__new__(BrowserSession)
        session.browser_profile = build_browser_environment()
        session.browser_profile.update({
            "send_client_hints": True, "chrome_full_version": "152.3.4.5",
            "sec_ch_ua_full_version_list": '"Chromium";v="152.3.4.5"',
            "sec_ch_ua_platform_version": '"19.2.0"', "sec_ch_ua_arch": '"arm"',
            "sec_ch_ua_bitness": '"64"', "sec_ch_ua_model": '""',
        })
        session.device_id = "registration-device"
        session.auth_session_logging_id = "registration-logging"
        session.get = Mock(return_value=SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: {"csrfToken": "formal-token"},
        ))
        session.post = Mock(return_value=SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"url": "https://auth.openai.com/oauth/authorize?state=opaque"},
        ))
        expected = {
            "sec-ch-ua-full-version": "152.3.4.5",
            "sec-ch-ua-full-version-list": '"Chromium";v="152.3.4.5"',
            "sec-ch-ua-platform-version": '"19.2.0"', "sec-ch-ua-arch": '"arm"',
            "sec-ch-ua-bitness": '"64"', "sec-ch-ua-model": '""',
        }
        referer = build_login_referer(session, "register@example.test")
        for enabled in (False, True):
            with self.subTest(include_high_entropy=enabled):
                options = {"include_high_entropy": True} if enabled else {}
                token = get_csrf_token(session, login_referer=referer, **options)
                signin_openai(session, token, "register@example.test", login_referer=referer, **options)
                for headers in (session.get.call_args.kwargs["headers"],
                                session.post.call_args.kwargs["headers"]):
                    if enabled:
                        self.assertEqual({key: headers[key] for key in expected}, expected)
                    else:
                        self.assertTrue(expected.keys().isdisjoint(headers))
                    self.assertEqual(headers["referer"], referer)
                    self.assertEqual(headers["cache-control"], "no-cache")
                    self.assertEqual(headers["pragma"], "no-cache")
                self.assertEqual(parse_qs(session.post.call_args.kwargs["data"])["csrfToken"], ["formal-token"])


class Csrf403RetryTests(unittest.TestCase):
    """``get_csrf_token`` 对 403 的同会话重试（design D3）。响应用真实的 ``requests.Response``，
    ``raise_for_status`` 的行为与消息由 requests 生成，不在替身里手写。"""

    URL = "https://chatgpt.com/api/auth/csrf"

    def setUp(self):
        patcher = patch.object(job_context, "interruptible_sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)
        session = BrowserSession.__new__(BrowserSession)
        session.browser_profile = build_browser_environment()
        session.device_id = "csrf-device"
        session.auth_session_logging_id = "csrf-logging"
        session.reset_circuit_breaker = Mock()
        self.session = session

    @staticmethod
    def _response(status, body):
        response = requests.Response()
        response.status_code = status
        response.url = Csrf403RetryTests.URL
        response._content = json.dumps(body).encode()
        response._content_consumed = True
        return response

    def _given(self, *outcomes):
        """按顺序给 session.get 设定返回值或要抛的异常。"""
        self.session.get = Mock(side_effect=[
            outcome if isinstance(outcome, BaseException) else self._response(*outcome)
                for outcome in outcomes
            ])

    def _sleeps(self):
        return [call.args[0] for call in self.sleep.call_args_list]

    def test_403_then_200_retries_in_same_session(self):
        self._given((403, {}), (200, {"csrfToken": "formal-token"}))
        token = get_csrf_token(self.session, login_referer="https://chatgpt.com/auth/login")
        self.assertEqual(token, "formal-token")
        self.assertEqual(self.session.get.call_count, 2)
        # 同一会话、同一请求：两次调用的地址与请求头完全相同。
        first, second = self.session.get.call_args_list
        self.assertEqual(first, second)
        self.assertEqual(first.args[0], self.URL)
        self.assertEqual(self._sleeps(), [5.0])
        self.assertEqual(self.session.reset_circuit_breaker.call_count, 1)

    def test_circuit_is_reset_before_each_retry(self):
        order = []
        self.session.reset_circuit_breaker = Mock(side_effect=lambda: order.append("reset"))
        self.sleep.side_effect = lambda seconds: order.append("sleep")
        gets = []
        self.session.get = Mock(side_effect=lambda *args, **kwargs: (
            order.append("get") or gets.append(1) or self._response(403 if len(gets) < 3 else 200, {"csrfToken": "t"})))
        token = get_csrf_token(self.session)
        self.assertEqual(token, "t")
        # 每次重试前先清熔断再等待；最后一次 403 之后不再有重试，也就不清。
        self.assertEqual(order, ["get", "reset", "sleep", "get", "reset", "sleep", "get"])
        self.assertEqual(self._sleeps(), [5.0, 10.0])

    def test_three_403_raise_http_error_with_status(self):
        self._given((403, {}), (403, {}), (403, {}))
        with self.assertRaises(requests.HTTPError) as caught:
            get_csrf_token(self.session)
        self.assertIn("403", str(caught.exception))
        self.assertEqual(self.session.get.call_count, 3)
        self.assertEqual(self._sleeps(), [5.0, 10.0])
        # 第 3 次 403 之后直接抛出：不再清熔断、不再等待。
        self.assertEqual(self.session.reset_circuit_breaker.call_count, 2)

    def test_429_is_not_retried(self):
        self._given((429, {}))
        with self.assertRaises(requests.HTTPError) as caught:
            get_csrf_token(self.session)
        self.assertIn("429", str(caught.exception))
        self.assertEqual(self.session.get.call_count, 1)
        self.assertEqual(self._sleeps(), [])
        self.session.reset_circuit_breaker.assert_not_called()

    def test_transport_error_passes_through_unchanged(self):
        # 传输层的链路瞬断重试在 WreqSession 内部；耗尽后抛出的异常原样穿出，这里不再重试。
        timeout = requests.Timeout("timed out")
        self._given(timeout)
        with self.assertRaises(requests.Timeout) as caught:
            get_csrf_token(self.session)
        self.assertIs(caught.exception, timeout)
        self.assertEqual(self.session.get.call_count, 1)
        self.assertEqual(self._sleeps(), [])
        self.session.reset_circuit_breaker.assert_not_called()

    def test_network_error_after_403_is_not_retried(self):
        # 403 之后的重试请求遇到网络错误：同样原样穿出，不进入新一轮 403 重试。
        connection_error = requests.ConnectionError("is_connect error")
        self._given((403, {}), connection_error)
        with self.assertRaises(requests.ConnectionError) as caught:
            get_csrf_token(self.session)
        self.assertIs(caught.exception, connection_error)
        self.assertEqual(self.session.get.call_count, 2)
        self.assertEqual(self._sleeps(), [5.0])

    def test_non_403_success_is_unchanged(self):
        self._given((200, {"csrfToken": "first-try"}))
        self.assertEqual(get_csrf_token(self.session), "first-try")
        self.assertEqual(self.session.get.call_count, 1)
        self.assertEqual(self._sleeps(), [])
        self.session.reset_circuit_breaker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
