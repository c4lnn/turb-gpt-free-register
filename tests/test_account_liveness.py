# -*- coding: utf-8 -*-
import json
import tempfile
import logging
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

from config.browser import build_browser_environment
from core.openai.transport.session import BrowserSession

import core.openai.account.account_liveness as liveness


class _DummyHttpSession:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _DummyBrowserSession:
    created = []

    def __init__(self, proxy=None, **kwargs):
        self.proxy = proxy
        self.received_proxy = proxy
        self.device_id = "device-test"
        self.session = _DummyHttpSession()
        self.blocked_until = 0.0
        self.blocked_reason = ""
        self.created.append(self)
        self.kwargs = kwargs

    def fingerprint_summary(self):
        return {
            "proxy": self.proxy or "",
            "user_agent": "test-agent",
            "accept_language": "en-US",
            "timezone_iana": "UTC",
            "timezone_offset_minutes": 0,
            "screen_width": 1440,
            "screen_height": 900,
            "device_pixel_ratio": 2,
            "hardware_concurrency": 8,
            "device_memory": 8,
            "geo_country": "US",
            "geo_city": "Test",
        }

    def fingerprint_summary_text(self):
        return "test-fingerprint"

    def reset_circuit_breaker(self):
        self.blocked_until = 0.0
        self.blocked_reason = ""


class AccountLivenessTests(unittest.TestCase):
    def setUp(self):
        plan_patch = patch("core.openai.commerce.chatgpt_plan.check_account_plan", return_value={"ok": True, "current_plan_type": "plus"})
        self.plan_mock = plan_patch.start()
        self.addCleanup(plan_patch.stop)
        rate_patch = patch("core.openai.commerce.plan_check_service._wait_for_rate_slot")
        rate_patch.start()
        self.addCleanup(rate_patch.stop)
        password_patch = patch.object(liveness, "_account_registration_password", return_value="")
        password_patch.start()
        self.addCleanup(password_patch.stop)
        _DummyBrowserSession.created = []

    def test_preflight_preserves_explicit_direct_route_and_skips_providers(self):
        with patch.object(liveness, "BrowserSession", _DummyBrowserSession), \
             patch.object(liveness, "_warm_login_fingerprint_context"), \
             patch.object(liveness, "probe_auth_session"), \
             patch.object(liveness, "get_csrf_token", return_value="csrf"), \
             patch.object(liveness, "signin_openai", return_value="https://auth.example/authorize"):
            session, authorize_url = liveness._network_preflight_with_retry(
                "user@example.com", "", max_attempts=1
            )

        self.assertEqual(session.proxy, "")
        self.assertEqual(authorize_url, "https://auth.example/authorize")
        self.assertIsNone(session.kwargs["fingerprint_seed"])

    def test_preflight_retries_with_same_session_when_csrf_is_blocked(self):
        csrf_errors = [RuntimeError("HTTP Error 403"), "csrf"]
        with patch.object(liveness, "BrowserSession", _DummyBrowserSession), \
             patch.object(liveness, "_warm_login_fingerprint_context"), \
             patch.object(liveness, "probe_auth_session"), \
             patch.object(liveness, "get_csrf_token", side_effect=csrf_errors), \
             patch.object(liveness, "signin_openai", return_value="authorize"), \
             patch.object(liveness.time, "sleep"):
            session, _ = liveness._network_preflight_with_retry(
                "user@example.com", None, max_attempts=2
            )

        self.assertIs(_DummyBrowserSession.created[-1], session)
        # 403 下发的 CF Cookie 必须留在同一 Cookie Jar 中，不能每轮新建会话。
        self.assertEqual(len(_DummyBrowserSession.created), 1)
        self.assertFalse(_DummyBrowserSession.created[0].session.closed)
        self.assertIsNone(_DummyBrowserSession.created[0].received_proxy)

    def test_callback_403_retries_in_same_session_before_fetching_token(self):
        session = _DummyBrowserSession(proxy="proxy")
        with patch.object(
            liveness,
            "follow_oauth_callback",
            side_effect=[RuntimeError("HTTP 403 from callback/openai"), "https://chatgpt.com/"],
        ) as callback, patch.object(
            liveness,
            "fetch_session",
            return_value={"accessToken": "token"},
        ) as fetch, patch.object(liveness.time, "sleep"):
            result = liveness._follow_continue_and_fetch(
                session,
                "https://auth.openai.com/authorize/continue?state=test",
                referer="https://auth.openai.com/email-verification",
            )

        self.assertEqual(result["accessToken"], "token")
        self.assertEqual(callback.call_count, 2)
        fetch.assert_called_once_with(session)

    def test_fingerprint_identity_is_pinned_when_session_is_recreated_in_one_attempt(self):
        state = {}
        first = MagicMock()
        first.browser_profile = {
            "navigator_language": "ja-JP",
            "timezone_iana": "Asia/Tokyo",
            "screen_width": 1512,
        }
        second = MagicMock()
        second.browser_profile = dict(first.browser_profile)
        with patch.object(liveness, "BrowserSession", side_effect=[first, second]) as factory:
            self.assertIs(
                liveness._new_fingerprint_pinned_session(
                    "user@example.com", "socks5://proxy:1080", state,
                ),
                first,
            )
            self.assertIs(
                liveness._new_fingerprint_pinned_session(
                    "user@example.com", "", state,
                ),
                second,
            )

        first_kwargs = factory.call_args_list[0].kwargs
        second_kwargs = factory.call_args_list[1].kwargs
        self.assertTrue(first_kwargs["detect_exit_geo"])
        self.assertIsNone(first_kwargs["browser_profile"])
        self.assertFalse(second_kwargs["detect_exit_geo"])
        self.assertEqual(second_kwargs["browser_profile"]["navigator_language"], "ja-JP")
        self.assertEqual(second_kwargs["browser_profile"]["timezone_iana"], "Asia/Tokyo")
        self.assertIsNone(first_kwargs["fingerprint_seed"])
        self.assertIsNone(second_kwargs["fingerprint_seed"])

    def test_reuse_mode_uses_registration_seed_for_same_email(self):
        state = {}
        session = MagicMock()
        session.browser_profile = {"navigator_language": "en-US"}
        with patch.object(liveness, "BrowserSession", return_value=session), \
             patch("config.register.PROTOCOL_REUSE_FINGERPRINT_BY_EMAIL", True):
            liveness._new_fingerprint_pinned_session("User@Example.com", "proxy", state)
        self.assertEqual(state["fingerprint_seed"], "registration:user@example.com")
        self.assertEqual(state["fingerprint_mode"], "registration_email_stable")

    def test_direct_fallback_can_force_fresh_seed_in_reuse_mode(self):
        state = {"force_fresh": True}
        session = MagicMock()
        session.browser_profile = {"navigator_language": "en-US"}
        with patch.object(liveness, "BrowserSession", return_value=session), \
             patch("config.register.PROTOCOL_REUSE_FINGERPRINT_BY_EMAIL", True):
            liveness._new_fingerprint_pinned_session("user@example.com", "", state)
        self.assertEqual(state["fingerprint_seed"], "")

    def test_fresh_live_check_uses_same_uuid4_shape_as_registration(self):
        state = {}
        with patch("config.register.PROTOCOL_REUSE_FINGERPRINT_BY_EMAIL", False):
            session = liveness._new_fingerprint_pinned_session(
                "user@example.com", "", state,
            )
        try:
            self.assertEqual(uuid.UUID(session.device_id).version, 4)
            self.assertEqual(uuid.UUID(session.oai_session_id).version, 4)
            self.assertEqual(uuid.UUID(session.auth_session_logging_id).version, 4)
        finally:
            session.close()

    def test_reauth_otp_dead_account_error_is_not_retried(self):
        response = SimpleNamespace(
            status_code=403,
            text='{"error":{"code":"account_deactivated"}}',
        )
        error = RuntimeError("HTTP Error 403")
        error.response = response
        session = _DummyBrowserSession(proxy="proxy")

        with patch.object(liveness, "_validate_reauth_otp", side_effect=error), \
             patch.object(liveness, "wait_for_otp", return_value="123456"):
            with self.assertRaises(liveness.AccountUnusableError) as ctx:
                liveness._validate_reauth_with_retry(session, "user@example.com", 1.0)

        self.assertEqual(ctx.exception.error_code, "account_deactivated")

    def test_existing_access_token_uses_reauth_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _DummyBrowserSession(proxy="")
            with patch.object(liveness, "_LOG_DIR", Path(tmp)), \
                 patch.object(liveness, "_stored_access_token", return_value="old-token"), \
                 patch.object(liveness, "BrowserSession", return_value=session), \
                 patch.object(liveness, "_warm_authenticated_session") as warm, \
                 patch.object(liveness, "human_delay"), \
                 patch.object(liveness, "_login_via_reauth", return_value={
                     "accessToken": "new-token",
                     "user": {"id": "user-1"},
                     "account": {"planType": "free"},
                 }) as reauth:
                result = liveness.check_account_liveness("user@example.com", proxy="")

        self.assertTrue(result["ok"])
        self.assertEqual(result["access_token"], "new-token")
        warm.assert_called_once_with(session, "old-token")
        reauth.assert_called_once()
        self.assertTrue(session.session.closed)


    def test_deactivated_reauth_error_stops_without_full_login_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _DummyBrowserSession(proxy="proxy")
            error = liveness.AccountUnusableError(
                "account banned", error_code="account_banned",
            )
            with patch.object(liveness, "_LOG_DIR", Path(tmp)), \
                 patch.object(liveness, "_stored_access_token", return_value="old-token"), \
                 patch.object(liveness, "_account_totp_secret", return_value=""), \
                 patch.object(liveness, "_new_fingerprint_pinned_session", return_value=session), \
                 patch.object(liveness, "_warm_authenticated_session"), \
                 patch.object(liveness, "human_delay"), \
                 patch.object(liveness, "_login_via_reauth", side_effect=error), \
                 patch.object(liveness, "_login_via_full_web_flow") as full_login:
                result = liveness.check_account_liveness("user@example.com", proxy="proxy")

        self.assertEqual(result["status"], "deactivated")
        self.assertEqual(result["error"], "account_banned")
        self.assertFalse(liveness._is_retryable_network_error(error))
        full_login.assert_not_called()
    def test_reauth_403_falls_back_to_clean_full_web_login_on_same_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            reauth_session = _DummyBrowserSession(proxy="socks5://proxy.example:1080")
            full_session = _DummyBrowserSession(proxy="socks5://proxy.example:1080")
            fresh_info = {
                "accessToken": "new-token",
                "user": {"id": "user-1"},
                "account": {"planType": "free"},
            }
            state = {}
            with patch.object(liveness, "_LOG_DIR", Path(tmp)), \
                 patch.object(liveness, "_stored_access_token", return_value="old-token"), \
                 patch.object(liveness, "_account_totp_secret", return_value=""), \
                 patch.object(liveness, "_new_fingerprint_pinned_session", return_value=reauth_session), \
                 patch.object(liveness, "_warm_authenticated_session"), \
                 patch.object(liveness, "human_delay"), \
                 patch.object(liveness, "_login_via_reauth", side_effect=RuntimeError("HTTP 403 callback")), \
                 patch.object(liveness, "_login_via_full_web_flow", return_value=(
                     full_session, fresh_info,
                 )) as full_login:
                result = liveness.check_account_liveness(
                    "user@example.com",
                    proxy=None,
                    fingerprint_state=state,
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["access_token"], "new-token")
        full_login.assert_called_once_with(
            "user@example.com",
            "socks5://proxy.example:1080",
            email_source=None,
            fingerprint_state=state,
        )
        self.assertTrue(reauth_session.session.closed)
        self.assertTrue(full_session.session.closed)

    def test_live_success_queries_plan_before_session_closes(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _DummyBrowserSession(proxy="")
            def query(token, **kwargs):
                self.assertEqual(token, "fresh-token")
                self.assertIs(kwargs["session"], session)
                self.assertFalse(session.session.closed)
                return {"ok": True, "current_plan_type": "plus"}
            self.plan_mock.side_effect = query
            with patch.object(liveness, "_LOG_DIR", Path(tmp)), \
                 patch.object(liveness, "_stored_access_token", return_value=""), \
                 patch.object(liveness, "_account_totp_secret", return_value=""), \
                 patch.object(liveness, "_login_via_full_web_flow", return_value=(session, {"accessToken": "fresh-token"})):
                result = liveness.check_account_liveness("user@example.com")
            self.assertTrue(result["ok"])
            self.assertTrue(result["plan_result"]["ok"])
            self.assertTrue(session.session.closed)
            self.plan_mock.assert_called_once()

    def test_plan_exception_does_not_turn_live_success_into_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _DummyBrowserSession(proxy="")
            self.plan_mock.side_effect = RuntimeError("plan timeout")
            with patch.object(liveness, "_LOG_DIR", Path(tmp)), \
                 patch.object(liveness, "_stored_access_token", return_value=""), \
                 patch.object(liveness, "_account_totp_secret", return_value=""), \
                 patch.object(liveness, "_login_via_full_web_flow", return_value=(session, {"accessToken": "fresh-token"})):
                result = liveness.check_account_liveness("user@example.com")
            self.assertTrue(result["ok"])
            self.assertEqual(result["access_token"], "fresh-token")
            self.assertFalse(result["plan_result"]["ok"])
            self.assertTrue(session.session.closed)

    def test_full_web_login_failure_closes_session_before_ownership_transfer(self):
        for stage in ('authorize', 'deactivated', 'otp'):
            with self.subTest(stage=stage):
                session = _DummyBrowserSession(proxy='socks5://proxy.example:1080')
                error = RuntimeError('failed at ' + stage)
                with patch.object(liveness, '_network_preflight_with_retry', return_value=(session, 'https://auth.example/authorize')), \
                     patch.object(liveness, 'follow_authorize', side_effect=error if stage == 'authorize' else None,
                                  return_value='https://auth.example/email-verification'), \
                     patch.object(liveness, 'detect_account_unusable_text', return_value='account_deactivated' if stage == 'deactivated' else None), \
                     patch.object(liveness, '_login_via_password_or_otp', side_effect=error if stage == 'otp' else None), \
                     patch.object(liveness, 'close_browser_session', wraps=liveness.close_browser_session) as close:
                    expected = liveness.AccountUnusableError if stage == 'deactivated' else RuntimeError
                    with self.assertRaises(expected):
                        liveness._login_via_full_web_flow('user@example.com', session.proxy,
                            email_source=None, fingerprint_state={})
                close.assert_called_once_with(session)
                self.assertTrue(session.session.closed)

    def test_full_web_login_success_transfers_open_session_to_caller(self):
        session = _DummyBrowserSession(proxy='')
        info = {'accessToken': 'fresh-token'}
        with patch.object(liveness, '_network_preflight_with_retry', return_value=(session, 'https://auth.example/authorize')), \
             patch.object(liveness, 'follow_authorize', return_value='https://auth.example/email-verification'), \
             patch.object(liveness, 'detect_account_unusable_text', return_value=None), \
             patch.object(liveness, '_login_via_password_or_otp', return_value=info), \
             patch.object(liveness, 'close_browser_session') as close:
            returned, result = liveness._login_via_full_web_flow('user@example.com', '',
                email_source=None, fingerprint_state={})
        self.assertIs(returned, session)
        self.assertIs(result, info)
        self.assertFalse(session.session.closed)
        close.assert_not_called()

    def test_full_web_login_cleanup_error_preserves_original_login_error(self):
        session = _DummyBrowserSession(proxy='')
        original = RuntimeError('original authorization failure')
        with patch.object(liveness, '_network_preflight_with_retry', return_value=(session, 'https://auth.example/authorize')), \
             patch.object(liveness, 'follow_authorize', side_effect=original), \
             patch.object(liveness, 'close_browser_session', side_effect=OSError('close failed')) as close:
            with self.assertRaises(RuntimeError) as raised:
                liveness._login_via_full_web_flow('user@example.com', '', email_source=None, fingerprint_state={})
        self.assertIs(raised.exception, original)
        close.assert_called_once_with(session)

    def test_log_handler_close_error_does_not_override_saved_live_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _DummyBrowserSession(proxy='')
            handler = logging.FileHandler(str(Path(tmp) / 'test.log'))
            try:
                with patch.object(liveness, '_LOG_DIR', Path(tmp)), \
                     patch.object(liveness, '_stored_access_token', return_value=''), \
                     patch.object(liveness, '_account_totp_secret', return_value=''), \
                     patch.object(liveness, '_login_via_full_web_flow', return_value=(session, {'accessToken': 'fresh-token'})), \
                     patch.object(liveness.logging, 'FileHandler', return_value=handler), \
                     patch.object(handler, 'close', side_effect=OSError('log close failed')) as close:
                    result = liveness.check_account_liveness('user@example.com')
                self.assertTrue(result['ok'])
                self.assertTrue(result['plan_result']['ok'])
                self.assertTrue(session.session.closed)
                self.assertNotIn(handler, logging.getLogger().handlers)
                self.assertFalse(liveness.is_checking('user@example.com'))
                close.assert_called_once()
            finally:
                logging.getLogger().removeHandler(handler)
                handler.close()

    def test_full_web_header_profile_reaches_csrf_password_and_mfa_without_leaking(self):
        from core.openai.auth import openai_auth

        def response(payload=None, url="", content_type="application/json"):
            return SimpleNamespace(
                status_code=200, url=url, text="", headers={"content-type": content_type},
                json=lambda: payload or {}, raise_for_status=lambda: None,
            )

        class Session(BrowserSession):
            def __init__(self):
                self.proxy = ""
                self.device_id = "fixture-device"
                self.auth_session_logging_id = "fixture-logging"
                self.oai_session_id = "fixture-session"
                self.browser_profile = build_browser_environment()
                self.document_navigation_id = "before-authorize"
                self.datadog_origin = "rum"
                self.requests = []

            def fingerprint_summary_text(self):
                return "synthetic-profile"

            def auth_cookie_header(self):
                return ""

            def get(self, url, *, headers, **_kwargs):
                self.requests.append(("GET", urlparse(url).path, dict(headers), None))
                if urlparse(url).path == "/api/auth/csrf":
                    return response({"csrfToken": "formal-fixture"})
                if urlparse(url).path == "/api/auth/session":
                    return response()
                if urlparse(url).path == "/oauth/authorize":
                    return response(url="https://auth.openai.com/log-in/password", content_type="text/html")
                raise AssertionError("unexpected GET")

            def post(self, url, *, headers, data, **_kwargs):
                path = urlparse(url).path
                self.requests.append(("POST", path, dict(headers), data))
                if path == "/api/auth/signin/openai":
                    return response({"url": "https://auth.openai.com/oauth/authorize?state=fixture"})
                if path == "/backend-api/sentinel/req":
                    flow = json.loads(data)["flow"]
                    return response({"token": "challenge-" + flow, "so": {"required": False}})
                if path == "/api/accounts/password/verify":
                    return response({"page": {"type": "mfa_challenge", "payload": {"factor_id": "factor-fixture"}}})
                if path == "/api/accounts/mfa/issue_challenge":
                    return response()
                if path == "/api/accounts/mfa/verify":
                    return response({"continue_url": "https://chatgpt.com/api/auth/callback/openai"})
                raise AssertionError("unexpected POST")

        class SDK:
            def __init__(self, *, flow, device_id, **_kwargs):
                self.flow, self.device_id = flow, device_id

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def requirements(self):
                return "requirements-fixture"

            def solve(self, challenge):
                return json.dumps({
                    "p": "proof-fixture", "t": "", "c": challenge["token"],
                    "id": self.device_id, "flow": self.flow,
                }), None

        for password in ("snapshot-fixture", ""):
            with self.subTest(password_available=bool(password)):
                session = Session()
                with patch.object(liveness, "_account_registration_password", side_effect=[password, "changed-password"]), \
                     patch.object(liveness, "_new_fingerprint_pinned_session", return_value=session), \
                     patch.object(liveness, "_warm_login_fingerprint_context"), \
                     patch.object(liveness, "_account_totp_secret", return_value="secret-fixture"), \
                     patch.object(liveness, "_account_totp_code", return_value="000000"), \
                     patch.object(liveness, "_follow_continue_and_fetch", return_value={"accessToken": "fixture"}), \
                     patch.object(liveness, "_login_via_email_otp", return_value={"accessToken": "fixture"}), \
                     patch.object(openai_auth, "SentinelSDK", SDK), \
                     patch.object(openai_auth, "_proxy_retry_config", return_value=(1, 0)), \
                     patch("core.openai.transport.session.random.getrandbits", side_effect=[11, 12, 21, 22, 31, 32]), \
                     patch("core.openai.transport.session.uuid.uuid4", side_effect=["nav-fixture", "invoke-1", "invoke-2", "invoke-3"]):
                    returned, info = liveness._login_via_full_web_flow(
                        "fixture+tag@example.test", "", email_source=None, fingerprint_state={},
                    )

                self.assertIs(returned, session)
                self.assertEqual(info["accessToken"], "fixture")
                paths = [item[1] for item in session.requests]
                prefix = ["/api/auth/csrf", "/api/auth/session", "/api/auth/signin/openai", "/oauth/authorize"]
                self.assertEqual(paths[:4], prefix)
                csrf_headers, signin_headers, nav_headers = [session.requests[index][2] for index in (0, 2, 3)]
                self.assertEqual(parse_qs(session.requests[2][3])["csrfToken"], ["formal-fixture"])
                if not password:
                    self.assertEqual(paths, prefix)
                    self.assertEqual(csrf_headers["referer"], "https://chatgpt.com/auth/login")
                    self.assertEqual(csrf_headers["content-type"], "application/json")
                    self.assertIn("priority", nav_headers)
                    self.assertEqual(nav_headers["sec-fetch-user"], "?1")
                    self.assertNotIn("sec-ch-ua-full-version", csrf_headers)
                    continue

                self.assertEqual(paths[4:], [
                    "/backend-api/sentinel/req", "/api/accounts/password/verify",
                    "/api/accounts/mfa/issue_challenge", "/backend-api/sentinel/req",
                    "/api/accounts/mfa/verify",
                ])
                self.assertEqual(csrf_headers["referer"], signin_headers["referer"])
                self.assertEqual(parse_qs(urlparse(csrf_headers["referer"]).query)["login_hint"], ["fixture+tag@example.test"])
                self.assertNotIn("content-type", csrf_headers)
                for headers in (csrf_headers, signin_headers):
                    self.assertEqual(headers["cache-control"], "no-cache")
                    self.assertEqual(headers["pragma"], "no-cache")
                self.assertNotIn("sec-fetch-user", nav_headers)
                self.assertEqual(nav_headers["sec-fetch-site"], "cross-site")
                self.assertEqual(nav_headers["referer"], "https://chatgpt.com/")
                auth_requests = [session.requests[index] for index in (5, 6, 8)]
                self.assertEqual(json.loads(auth_requests[0][3]), {"password": "snapshot-fixture"})
                self.assertEqual([request[2]["accept"] for request in auth_requests], ["application/json", "*/*", "application/json"])
                self.assertNotIn("openai-sentinel-token", auth_requests[1][2])
                self.assertEqual([request[2]["x-datadog-trace-id"] for request in auth_requests], ["11", "21", "31"])
                self.assertEqual({request[2]["x-openai-document-navigation-id"] for request in auth_requests}, {"nav-fixture"})
                for index, request in enumerate(session.requests):
                    headers = request[2]
                    if index == 1:
                        self.assertIn("priority", headers)
                        self.assertNotIn("sec-ch-ua-full-version", headers)
                        continue
                    self.assertNotIn("priority", headers)
                    if index in (4, 7):
                        self.assertNotIn("sec-ch-ua-full-version", headers)
                        self.assertNotIn("x-datadog-trace-id", headers)
                        self.assertEqual(headers["content-type"], "text/plain;charset=UTF-8")
                    else:
                        self.assertEqual(headers["sec-ch-ua-full-version"], session.browser_profile["chrome_full_version"])


    def test_reauth_transport_retry_keeps_liveness_otp_on_final_document_id(self):
        class Response:
            def __init__(self, status_code=200, payload=None, url=""):
                self.status_code = status_code
                self.payload = payload or {}
                self.url = url

            def raise_for_status(self):
                if self.status_code >= 400:
                    error = RuntimeError(f"HTTP {self.status_code}")
                    error.response = self
                    raise error

            def json(self):
                return self.payload

        class ReauthSession(_DummyBrowserSession):
            def __init__(self):
                super().__init__(proxy="proxy")
                self.document_navigation_id = "document-1"
                self.navigation_responses = [TimeoutError("socket timed out"), Response()]
                self.gets = []
                self.posts = []
                self.rotation_history = []

            def get_auth_navigate_headers(self, referer="", **_kwargs):
                return {"referer": referer}

            def get_auth_headers(self, referer=""):
                return {
                    "referer": referer,
                    "x-openai-document-navigation-id": self.document_navigation_id,
                }

            def rotate_document_navigation_id(self):
                previous = self.document_navigation_id
                self.document_navigation_id = previous + "-next"
                self.rotation_history.append((previous, self.document_navigation_id))
                return self.document_navigation_id

            def get(self, url, *, headers, allow_redirects=True):
                self.gets.append((url, self.document_navigation_id))
                if url.endswith("/log-in"):
                    return Response(url=url)
                response = self.navigation_responses.pop(0)
                if isinstance(response, Exception):
                    raise response
                response.url = "https://auth.openai.com/email-verification"
                return response

            def post(self, url, *, headers, data=None):
                self.posts.append((url, headers, data))
                return Response(payload={
                    "continue_url": "https://chatgpt.com/api/auth/callback/openai",
                })

        session = ReauthSession()
        auth_url = "https://auth.openai.com/api/accounts/authorize?state=test"
        with patch.object(liveness, "_trigger_reauth_with_retry", return_value=auth_url), \
             patch.object(liveness, "wait_for_otp", return_value="123456"), \
             patch.object(liveness, "human_delay"), \
             patch.object(liveness, "_follow_continue_and_fetch", return_value={"accessToken": "fresh-token"}), \
             patch("config.twofa.TWOFA_REAUTH_MAX_ATTEMPTS", 2), \
             patch("config.twofa.TWOFA_REAUTH_RETRY_DELAY", 0):
            result = liveness._login_via_reauth(session, "user@example.test", 1.0)

        self.assertEqual(result["accessToken"], "fresh-token")
        self.assertEqual(
            [document_id for url, document_id in session.gets if not url.endswith("/log-in")],
            ["document-1", "document-1"],
        )
        self.assertEqual(session.rotation_history, [("document-1", "document-1-next")])
        self.assertEqual(
            session.posts[0][1]["x-openai-document-navigation-id"],
            "document-1-next",
        )
        self.assertEqual(len(session.posts), 1)
if __name__ == "__main__":
    unittest.main()
