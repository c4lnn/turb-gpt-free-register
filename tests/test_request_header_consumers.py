# -*- coding: utf-8 -*-
"""非目标消费者使用真实场景头，网络响应全部由测试提供。"""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from requests.structures import CaseInsensitiveDict

from core.openai.account import account_export
from core.openai.auth import chatgpt_bootstrap
from core.openai.codex import codex_oauth
from core.openai.commerce import chatgpt_plan
from core.openai.transport.session import BrowserSession


_HIGH_ENTROPY = {
    "sec-ch-ua-full-version", "sec-ch-ua-full-version-list",
    "sec-ch-ua-platform-version", "sec-ch-ua-arch", "sec-ch-ua-bitness", "sec-ch-ua-model",
}


def _session():
    session = BrowserSession.__new__(BrowserSession)
    session.browser_profile = {
        "user_agent": "Synthetic Chrome/146.0.0.0",
        "accept_language": "ja-JP,ja;q=0.9",
        "navigator_language": "ja-JP",
        "send_client_hints": True,
        "sec_ch_ua": '"Chromium";v="146"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"Windows"',
        "chrome_full_version": "146.0.1234.5",
        "sec_ch_ua_full_version_list": '"Chromium";v="146.0.1234.5"',
        "sec_ch_ua_platform_version": '"15.0.0"',
        "sec_ch_ua_arch": '"x86"',
        "sec_ch_ua_bitness": '"64"',
        "sec_ch_ua_model": '""',
        "timezone_offset_minutes": 540,
        "geo": {"country": "JP"},
    }
    session.device_id = "synthetic-device"
    session.oai_session_id = "synthetic-session"
    session.datadog_origin = "rum"
    session.datadog_trace_id = "123"
    session.datadog_parent_id = "456"
    session.document_navigation_id = "synthetic-document"
    session.get = Mock()
    session.post = Mock()
    return session


def _response(payload):
    return SimpleNamespace(
        status_code=200, headers={}, text="", json=lambda: payload,
        raise_for_status=lambda: None,
    )


class AccountExportHeaderConsumerTests(unittest.TestCase):
    def test_reauth_preserves_standard_csrf_signin_and_token_source(self):
        session = _session()
        session.get.return_value = _response({"csrfToken": "fresh-csrf"})
        session.post.return_value = _response({"url": "https://auth.openai.com/oauth/authorize?state=test"})

        result = account_export._trigger_reauth(session, "test+tag@example.test")

        self.assertEqual(result, "https://auth.openai.com/oauth/authorize?state=test")
        self.assertEqual(session.get.call_args.args, ("https://chatgpt.com/api/auth/csrf",))
        csrf_headers = CaseInsensitiveDict(session.get.call_args.kwargs["headers"])
        signin_headers = CaseInsensitiveDict(session.post.call_args.kwargs["headers"])
        for headers in (csrf_headers, signin_headers):
            self.assertEqual(headers["referer"], "https://chatgpt.com/")
            self.assertEqual(headers["priority"], "u=1, i")
            self.assertEqual(headers["accept"], "*/*")
            self.assertEqual(headers["sec-fetch-site"], "same-origin")
            self.assertEqual(headers["sec-fetch-mode"], "cors")
            self.assertEqual(headers["sec-fetch-dest"], "empty")
            self.assertFalse(_HIGH_ENTROPY.intersection(headers))
            for field in ("cache-control", "pragma", "authorization", "oai-device-id", "traceparent"):
                self.assertNotIn(field, headers)
        self.assertEqual(csrf_headers["content-type"], "application/json")
        self.assertNotIn("origin", csrf_headers)
        self.assertEqual(signin_headers["content-type"], "application/x-www-form-urlencoded")
        self.assertEqual(signin_headers["origin"], "https://chatgpt.com")
        signin_url = urlparse(session.post.call_args.args[0])
        self.assertEqual(signin_url.path, "/api/auth/signin/openai")
        self.assertEqual(parse_qs(signin_url.query), {
            "connection": ["password"], "login_hint": ["test+tag@example.test"],
            "reauth": ["password"], "max_age": ["0"], "ext-oai-did": [session.device_id],
        })
        self.assertEqual(parse_qs(session.post.call_args.kwargs["data"]), {
            "callbackUrl": ["https://chatgpt.com/?action=enable&factor=totp"],
            "csrfToken": ["fresh-csrf"], "json": ["true"],
        })

    def test_reauth_otp_retains_standard_auth_headers_and_validated_credentials(self):
        for so_header in (None, "validated-so"):
            with self.subTest(so_header=so_header):
                session = _session()
                session.post.return_value = _response({"continue_url": "https://chatgpt.com/callback"})
                result = account_export._validate_reauth_otp(session, "012345", "validated-main", so_header)
                self.assertEqual(result, "https://chatgpt.com/callback")
                headers = CaseInsensitiveDict(session.post.call_args.kwargs["headers"])
                self.assertEqual(headers["referer"], "https://auth.openai.com/email-verification")
                self.assertEqual(headers["origin"], "https://auth.openai.com")
                self.assertEqual(headers["priority"], "u=1, i")
                self.assertEqual(headers["openai-sentinel-token"], "validated-main")
                self.assertEqual(headers.get("openai-sentinel-so-token"), so_header)
                self.assertEqual(headers["x-openai-document-navigation-id"], "synthetic-document")
                self.assertIn("traceparent", headers)
                self.assertFalse(_HIGH_ENTROPY.intersection(headers))
                self.assertEqual(json.loads(session.post.call_args.kwargs["data"]), {"code": "012345"})

    def test_enrollment_and_activation_reuse_frontend_context(self):
        session = _session()
        session.post.side_effect = [_response({"secret": "synthetic-secret", "session_id": "enrollment"}),
                                    _response({})]
        secret, enrollment = account_export._enroll_totp(session, "synthetic-token")
        with patch.object(account_export.pyotp, "TOTP") as totp:
            totp.return_value.now.return_value = "012345"
            self.assertTrue(account_export._activate_totp(session, "synthetic-token", secret, enrollment))
        self.assertEqual([call.args[0] for call in session.post.call_args_list], [
            "https://chatgpt.com/backend-api/accounts/mfa/enroll",
            "https://chatgpt.com/backend-api/accounts/mfa/user/activate_enrollment",
        ])
        for call in session.post.call_args_list:
            headers = CaseInsensitiveDict(call.kwargs["headers"])
            self.assertEqual(headers["authorization"], "Bearer synthetic-token")
            self.assertEqual(headers["oai-device-id"], session.device_id)
            self.assertEqual(headers["oai-language"], "ja-JP")
            self.assertEqual(headers["oai-session-id"], session.oai_session_id)
            self.assertEqual(headers["x-datadog-trace-id"], "123")
            self.assertEqual(headers["priority"], "u=1, i")
            self.assertNotIn("openai-sentinel-token", headers)
            self.assertNotIn("traceparent", headers)
            self.assertFalse(_HIGH_ENTROPY.intersection(headers))
        self.assertEqual(json.loads(session.post.call_args_list[0].kwargs["data"]), {"factor_type": "totp"})
        self.assertEqual(json.loads(session.post.call_args_list[1].kwargs["data"]), {
            "code": "012345", "factor_type": "totp", "session_id": "enrollment",
        })


class CodexHeaderConsumerTests(unittest.TestCase):
    def test_json_posts_retain_auth_context_and_optional_validated_so(self):
        for so_header in (None, "validated-so"):
            with self.subTest(so_header=so_header):
                session = _session()
                codex_oauth._post_json(
                    session, "https://auth.openai.com/api/accounts/authorize/continue",
                    {"username": {"kind": "email", "value": "test@example.test"}},
                    "https://auth.openai.com/log-in", "validated-main", so_header,
                )
                headers = CaseInsensitiveDict(session.post.call_args.kwargs["headers"])
                self.assertEqual(headers["referer"], "https://auth.openai.com/log-in")
                self.assertEqual(headers["priority"], "u=1, i")
                self.assertEqual(headers["openai-sentinel-token"], "validated-main")
                self.assertEqual(headers.get("openai-sentinel-so-token"), so_header)
                self.assertEqual(headers["x-openai-document-navigation-id"], "synthetic-document")
                self.assertIn("traceparent", headers)
                self.assertFalse(_HIGH_ENTROPY.intersection(headers))
                self.assertFalse(session.post.call_args.kwargs["allow_redirects"])

    def test_code_exchange_uses_only_base_profile_and_form_semantics(self):
        session = _session()
        session.post.return_value = _response({"access_token": "synthetic-access-token"})
        result = codex_oauth.exchange_codex_token(session, "synthetic-code", "synthetic-verifier")
        self.assertEqual(result["access_token"], "synthetic-access-token")
        headers = CaseInsensitiveDict(session.post.call_args.kwargs["headers"])
        self.assertEqual({field.lower() for field in headers}, {
            "user-agent", "accept-language", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
            "content-type", "accept",
        })
        self.assertEqual(headers["user-agent"], session.browser_profile["user_agent"])
        self.assertEqual(headers["content-type"], "application/x-www-form-urlencoded")
        self.assertEqual(headers["accept"], "application/json")
        self.assertEqual(parse_qs(session.post.call_args.kwargs["data"]), {
            "grant_type": ["authorization_code"], "client_id": [codex_oauth._cfg.CODEX_CLIENT_ID],
            "code": ["synthetic-code"], "redirect_uri": [codex_oauth._cfg.CODEX_REDIRECT_URI],
            "code_verifier": ["synthetic-verifier"],
        })


class BootstrapCommerceHeaderConsumerTests(unittest.TestCase):
    def test_anonymous_bootstrap_keeps_frontend_and_generic_fetch_scenes_distinct(self):
        session = _session()
        chatgpt_bootstrap.anonymous_bootstrap(session, strict=True)
        calls = session.get.call_args_list
        self.assertEqual([call.args[0] for call in calls], [
            "https://chatgpt.com/backend-anon/accounts/check/v4-2023-04-27?timezone_offset_min=-540",
            "https://chatgpt.com/ces/v1/projects/oai/settings",
            "https://chatgpt.com/backend-anon/me",
            "https://chatgpt.com/backend-anon/checkout_pricing_config/configs/JP",
        ])
        for index, call in enumerate(calls):
            headers = CaseInsensitiveDict(call.kwargs["headers"])
            self.assertEqual(headers["referer"], "https://chatgpt.com/auth/login")
            self.assertEqual(headers["priority"], "u=1, i")
            self.assertEqual(headers["content-type"], "application/json")
            self.assertFalse(_HIGH_ENTROPY.intersection(headers))
            self.assertNotIn("traceparent", headers)
            self.assertNotIn("openai-sentinel-token", headers)
            self.assertNotIn("authorization", headers)
            if index == 1:
                self.assertNotIn("oai-device-id", headers)
                self.assertNotIn("x-datadog-trace-id", headers)
            else:
                self.assertEqual(headers["oai-device-id"], session.device_id)
                self.assertEqual(headers["x-datadog-trace-id"], "123")

    def test_commerce_fetch_keeps_authorization_target_and_frontend_context(self):
        session = _session()
        headers = CaseInsensitiveDict(chatgpt_plan._common_headers(
            session, "synthetic-token", {"account_id": "synthetic-account"},
        ))
        self.assertEqual(headers["authorization"], "Bearer synthetic-token")
        self.assertEqual(headers["chatgpt-account-id"], "synthetic-account")
        self.assertEqual(headers["x-openai-target-path"], chatgpt_plan.ACCOUNTS_CHECK_PATH)
        self.assertEqual(headers["x-openai-target-route"], chatgpt_plan.ACCOUNTS_CHECK_PATH)
        self.assertEqual(headers["oai-device-id"], session.device_id)
        self.assertEqual(headers["oai-language"], "ja-JP")
        self.assertEqual(headers["x-datadog-trace-id"], "123")
        self.assertEqual(headers["priority"], "u=1, i")
        self.assertNotIn("content-type", headers)
        self.assertNotIn("traceparent", headers)
        self.assertNotIn("openai-sentinel-token", headers)
        self.assertFalse(_HIGH_ENTROPY.intersection(headers))
