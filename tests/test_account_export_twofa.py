# -*- coding: utf-8 -*-
import json
import unittest
from unittest.mock import Mock, patch

import core.openai.account.account_export as account_export
import core.openai.auth.openai_auth as openai_auth

class _CircuitSession:
    def __init__(self):
        self.blocked_until = 0.0
        self.blocked_reason = ""
        self.reset_count = 0

    def reset_circuit_breaker(self):
        self.reset_count += 1
        self.blocked_until = 0.0
        self.blocked_reason = ""


class _Response:
    def __init__(self, status, payload=None, headers=None, text=""):
        self.status_code = status
        self.payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            exc = RuntimeError(f"HTTP {self.status_code}")
            exc.response = self
            raise exc


class _OtpSession(_CircuitSession):
    def __init__(self, responses):
        super().__init__()
        self.responses = iter(responses)
        self.calls = []

    def get_auth_headers(self, **kwargs):
        return {"referer": kwargs["referer"]}

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        if response.status_code in (403, 429):
            self.blocked_until = 9999999999.0
        return response


class _ReauthSession(_CircuitSession):
    def __init__(self, navigation_responses, post_responses=()):
        super().__init__()
        self.document_navigation_id = "document-1"
        self.navigation_responses = iter(navigation_responses)
        self.post_responses = iter(post_responses)
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
        old = self.document_navigation_id
        self.document_navigation_id = old + "-next"
        self.rotation_history.append((old, self.document_navigation_id))
        return self.document_navigation_id

    def get(self, url, *, headers, allow_redirects=True):
        self.gets.append((url, self.document_navigation_id))
        if url.endswith("/log-in"):
            return _Response(200)
        response = next(self.navigation_responses)
        if isinstance(response, Exception):
            raise response
        response.url = "https://auth.openai.com/email-verification"
        return response

    def post(self, url, *, headers, data=None, allow_redirects=False):
        self.posts.append((url, headers, data, allow_redirects))
        response = next(self.post_responses)
        if isinstance(response, Exception):
            raise response
        return response

class AccountExportTwofaTests(unittest.TestCase):

    def test_fetch_session_missing_access_token_logs_sanitized_response_shape(self):
        session = Mock()
        session.get_nextauth_headers.return_value = {}
        response = Mock(
            status_code=200,
            url="https://chatgpt.com/api/auth/session?secret-query=do-not-log",
            headers={"Content-Type": "application/json"},
        )
        response.json.return_value = {
            "user": {"id": "private-user-id", "email": "private@example.test"},
            "account": {"planType": "free"},
        }
        session.get.return_value = response

        with self.assertLogs(account_export.logger, level="ERROR") as captured:
            with self.assertRaisesRegex(RuntimeError, "未拿到 accessToken"):
                account_export.fetch_session(session)

        output = "\n".join(captured.output)
        self.assertIn("status=200", output)
        self.assertIn("url=chatgpt.com/api/auth/session", output)
        self.assertIn("content_type=application/json", output)
        self.assertIn("payload_keys=account,user", output)
        self.assertIn("user_fields=email,id", output)
        self.assertIn("account_fields=planType", output)
        self.assertIn("expires_present=False", output)
        self.assertNotIn("secret-query", output)
        self.assertNotIn("private-user-id", output)
        self.assertNotIn("private@example.test", output)
    def test_otp_temporary_403_reuses_same_code_session_and_backs_off(self):
        session = _OtpSession([_Response(403), _Response(403), _Response(200, {"continue_url": "https://chatgpt.com/callback"})])
        with patch("config.twofa.TWOFA_REAUTH_RETRY_DELAY", 2), \
             patch("config.twofa.TWOFA_REAUTH_MAX_ATTEMPTS", 3), \
             patch.object(account_export.time, "sleep") as sleep, \
             patch("core.openai.auth.openai_auth.send_email_otp") as resend:
            result = account_export._validate_twofa_otp_with_retry(session, "012345")
        self.assertEqual(result, "https://chatgpt.com/callback")
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4])
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(session.calls[0], session.calls[1])
        self.assertEqual(session.reset_count, 2)
        resend.assert_not_called()

    def test_otp_403_exhaustion_has_diagnostics_without_logging_code(self):
        session = _OtpSession([_Response(403, headers={"CF-Ray": "abc123-NRT"}) for _ in range(2)])
        with patch("config.twofa.TWOFA_REAUTH_MAX_ATTEMPTS", 2), \
             patch("config.twofa.TWOFA_REAUTH_RETRY_DELAY", 0), \
             self.assertLogs(account_export.logger, level="INFO") as captured:
            with self.assertRaises(account_export.TwofaOtpRequestError) as caught:
                account_export._validate_twofa_otp_with_retry(session, "012345")
        self.assertIn("HTTP 403", str(caught.exception))
        self.assertIn("abc123-NRT", str(caught.exception))
        self.assertNotIn("012345", str(caught.exception))
        self.assertNotIn("012345", str(captured.output))
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.reset_count, 1)

    def test_otp_business_403_and_interactive_challenge_are_not_retried(self):
        responses = [
            _Response(403, {"error": {"code": "account_deactivated"}}),
            _Response(403, {"error": {"message": "permission denied"}}),
            _Response(403, {"error": "access_denied"}),
            _Response(403, headers={"cf-mitigated": "challenge"}),
            _Response(403, text="<html>cf-chl-platform</html>"),
        ]
        for response in responses:
            with self.subTest(payload=response.payload, headers=response.headers, text=response.text):
                session = _OtpSession([response])
                with patch.object(account_export.time, "sleep") as sleep:
                    with self.assertRaises(account_export.TwofaOtpRequestError):
                        account_export._validate_twofa_otp_with_retry(session, "012345")
                self.assertEqual(len(session.calls), 1)
                self.assertEqual(session.reset_count, 0)
                sleep.assert_not_called()

    def test_otp_rate_limit_respects_retry_after_and_does_not_shorten_long_cooldown(self):
        session = _OtpSession([_Response(429, headers={"Retry-After": "10"}), _Response(200, {"continue_url": "ok"})])
        with patch("config.twofa.TWOFA_REAUTH_RETRY_DELAY", 1), patch.object(account_export.time, "sleep") as sleep:
            self.assertEqual(account_export._validate_twofa_otp_with_retry(session, "012345"), "ok")
        sleep.assert_called_once_with(10)
        for retry_after in ("900", "Wed, 21 Oct 2030 07:28:00 GMT", "9" * 100):
            session = _OtpSession([_Response(429, headers={"Retry-After": retry_after})])
            with patch.object(account_export.time, "sleep") as sleep:
                with self.assertRaises(account_export.TwofaOtpRequestError):
                    account_export._validate_twofa_otp_with_retry(session, "012345")
            sleep.assert_not_called()
            self.assertEqual(session.reset_count, 0)

    def test_otp_unknown_transport_outcome_and_401_are_not_replayed(self):
        for response in (_Response(401), TimeoutError("post timed out")):
            session = _OtpSession([response])
            with self.assertRaises(Exception):
                account_export._validate_twofa_otp_with_retry(session, "012345")
            self.assertEqual(len(session.calls), 1)
            self.assertEqual(session.reset_count, 0)

    def test_setup_uses_bounded_otp_retry_without_resending_mail(self):
        session = _OtpSession([_Response(403), _Response(200, {"continue_url": "https://chatgpt.com/callback"})])
        events = []
        state = {
            "page": {"type": "email_otp_verification"},
            "continue_url": "https://auth.openai.com/email-verification",
            "otp_delivery_required": False,
        }
        with patch.object(account_export, "_trigger_reauth_with_retry", return_value="auth-url"), \
             patch.object(account_export, "_follow_reauth_with_retry"), \
             patch("core.openai.auth.openai_auth.probe_registration_state", return_value=state), \
             patch("core.openai.auth.openai_auth.request_sentinel_token", return_value={"proof": "otp"}), \
             patch("core.openai.auth.openai_auth.build_sentinel_header", return_value=("otp-header", "otp-so")), \
             patch.object(account_export, "human_delay"), \
             patch.object(account_export, "_exchange_new_token", return_value="token"), \
             patch.object(account_export, "_enroll_totp", return_value=("secret", "session")), \
             patch.object(account_export, "_activate_totp", side_effect=lambda *args: events.append("activate")) as activate, \
             patch("config.twofa.TWOFA_REAUTH_RETRY_DELAY", 0):
            self.assertEqual(account_export.setup_2fa(
                session, "a@example.test", otp_code="012345",
                on_pending_secret=lambda secret: events.append(("pending", secret)),
            ), "secret")
        activate.assert_called_once_with(session, "token", "secret", "session")
        self.assertEqual(events, [("pending", "secret"), "activate"])

    def test_inline_setup_persists_before_activation_and_stops_on_persistence_failure(self):
        session = Mock()
        events = []
        with patch.object(account_export, "_enroll_totp", return_value=("secret", "session-id")), \
             patch.object(account_export, "human_delay"), \
             patch.object(account_export, "_activate_totp", side_effect=lambda *args: events.append("activate")):
            result = account_export.setup_2fa_inline(
                session, "access-token", on_pending_secret=lambda secret: events.append(("pending", secret)),
            )
        self.assertEqual(result, "secret")
        self.assertEqual(events, [("pending", "secret"), "activate"])

        with patch.object(account_export, "_enroll_totp", return_value=("secret", "session-id")), \
             patch.object(account_export, "_activate_totp") as activate:
            with self.assertRaisesRegex(OSError, "disk full"):
                account_export.setup_2fa_inline(
                    session, "access-token",
                    on_pending_secret=lambda _secret: (_ for _ in ()).throw(OSError("disk full")),
                )
        activate.assert_not_called()

    def test_setup_still_fetches_one_fresh_code_after_401(self):
        session = _OtpSession([_Response(401), _Response(200, {"continue_url": "https://chatgpt.com/api/auth/callback/openai"})])
        state = {
            "page": {"type": "email_otp_verification"},
            "continue_url": "https://auth.openai.com/email-verification",
            "otp_delivery_required": False,
        }
        with patch.object(account_export, "_trigger_reauth_with_retry", return_value="auth-url"), \
             patch.object(account_export, "_follow_reauth_with_retry"), \
             patch("core.openai.auth.openai_auth.probe_registration_state", return_value=state), \
             patch("core.openai.auth.openai_auth.request_sentinel_token", return_value={"proof": "otp"}), \
             patch("core.openai.auth.openai_auth.build_sentinel_header", return_value=("otp-header", "otp-so")), \
             patch.object(account_export, "human_delay"), \
             patch.object(account_export, "_exchange_new_token", return_value="token"), \
             patch.object(account_export, "_enroll_totp", return_value=("secret", "session")), \
             patch.object(account_export, "_activate_totp"), \
             patch("config.email.USE_EMAIL_SERVICE", True), \
             patch("core.mail.email_provider.wait_for_otp", return_value="654321") as wait:
            self.assertEqual(account_export.setup_2fa(session, "a@example.test", otp_code="012345"), "secret")
        self.assertEqual(len(session.calls), 2)
        wait.assert_called_once()
        self.assertIn('012345', session.calls[0][1]['data'])
        self.assertIn('654321', session.calls[1][1]['data'])
        self.assertEqual(session.reset_count, 0)

    def test_password_landing_does_not_reuse_authorize_continue_sentinel(self):
        session = Mock()
        sentinel = {"_sentinel_flow": "authorize_continue", "_sentinel_device_id": "device"}
        state = {
            "page": {"type": "login_password"},
            "continue_url": "https://auth.openai.com/log-in/password",
            "_sentinel_response": sentinel,
        }
        with patch.object(account_export, "_trigger_reauth_with_retry", return_value="auth-url"), \
             patch.object(account_export, "_follow_reauth_with_retry"), \
             patch("core.openai.auth.openai_auth.probe_registration_state", return_value=state), \
             patch("core.openai.auth.openai_auth.verify_login_password", return_value={
                 "continue_url": "https://chatgpt.com/api/auth/callback/openai",
             }) as verify, \
             patch.object(account_export, "_exchange_new_token", return_value="new-token"), \
             patch.object(account_export, "_enroll_totp", return_value=("secret", "session-id")), \
             patch.object(account_export, "_activate_totp"), \
             patch.object(account_export, "human_delay"):
            self.assertEqual(account_export.setup_2fa(
                session, "user@example.test", password="saved-password",
            ), "secret")
        verify.assert_called_once_with(session, "saved-password")

    def test_independent_setup_uses_existing_access_token_before_reauth(self):
        session = Mock()
        events = []
        with patch("core.openai.auth.openai_auth.get_mfa_enabled", return_value=False), \
             patch.object(account_export, "_enroll_totp", return_value=("secret", "session-id")), \
             patch.object(account_export, "_activate_totp", side_effect=lambda *args: events.append("activate")), \
             patch.object(account_export, "_verify_mfa_after_activation"), \
             patch.object(account_export, "_trigger_reauth_with_retry") as trigger, \
             patch.object(account_export, "human_delay"):
            result = account_export.setup_2fa(
                session,
                "user@example.test",
                access_token="old-token",
                try_existing_access_token=True,
                on_pending_secret=lambda secret: events.append(("pending", secret)),
            )
        self.assertEqual(result, "secret")
        self.assertEqual(events, [("pending", "secret"), "activate"])
        trigger.assert_not_called()

    def test_callback_failure_does_not_fetch_or_use_stale_session_token(self):
        session = Mock()
        session.get.return_value = _Response(403)
        with patch.object(account_export, "fetch_session") as fetch:
            with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
                account_export._exchange_new_token(session, "https://chatgpt.com/callback")
        fetch.assert_not_called()

    def test_authorize_403_reuses_cf_cookie_and_retries(self):
        session = _CircuitSession()
        calls = []

        def follow(_session, _auth_url):
            calls.append(1)
            if len(calls) == 1:
                session.blocked_until = 9999999999.0
                session.blocked_reason = "HTTP 403 from authorize"
                raise RuntimeError("HTTP 403 from authorize")
            return "https://auth.openai.com/email-verification"

        with patch.object(account_export, "_follow_reauth", side_effect=follow), patch(
            "config.twofa.TWOFA_REAUTH_MAX_ATTEMPTS", 3
        ), patch("config.twofa.TWOFA_REAUTH_RETRY_DELAY", 0), patch.object(
            account_export.time, "sleep"
        ) as sleep:
            result = account_export._follow_reauth_with_retry(
                session, "https://auth.openai.com/api/accounts/authorize?state=test"
            )

        self.assertEqual(result, "https://auth.openai.com/email-verification")
        self.assertEqual(len(calls), 2)
        self.assertEqual(session.reset_count, 1)
        sleep.assert_not_called()

    def test_reauth_403_stops_without_circuit_reset(self):
        session = _CircuitSession()
        calls = []

        def trigger(_session, _email):
            calls.append(1)
            if len(calls) < 3:
                session.blocked_until = 9999999999.0
                session.blocked_reason = "HTTP 403 from csrf"
                raise RuntimeError("HTTP 403 from csrf")
            return "https://auth.example/authorize"

        with patch.object(account_export, "_trigger_reauth", side_effect=trigger), patch(
            "config.twofa.TWOFA_REAUTH_MAX_ATTEMPTS", 3
        ), patch("config.twofa.TWOFA_REAUTH_RETRY_DELAY", 0), patch.object(
            account_export.time, "sleep"
        ) as sleep:
            with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
                account_export._trigger_reauth_with_retry(
                    session, "user@example.com"
                )

        self.assertEqual(len(calls), 1)
        self.assertEqual(session.reset_count, 0)
        sleep.assert_not_called()

    def test_reauth_business_400_is_not_retried(self):
        session = _CircuitSession()
        with patch.object(
            account_export, "_trigger_reauth", side_effect=RuntimeError("HTTP 400 invalid request")
        ) as trigger, patch("config.twofa.TWOFA_REAUTH_MAX_ATTEMPTS", 3):
            with self.assertRaisesRegex(RuntimeError, "HTTP 400"):
                account_export._trigger_reauth_with_retry(
                    session, "user@example.com"
                )

        trigger.assert_called_once()
        self.assertEqual(session.reset_count, 0)

    def test_optional_bootstrap_403_does_not_block_reauth(self):
        session = _CircuitSession()

        def bootstrap_with_403(*_args, **_kwargs):
            session.blocked_until = 9999999999.0
            session.blocked_reason = (
                "HTTP 403 from https://chatgpt.com/backend-api/accounts/optimized/check"
            )

        def assert_reauth_can_start(_session, _email):
            self.assertEqual(session.blocked_until, 0.0)
            self.assertEqual(session.blocked_reason, "")
            raise RuntimeError("stop-after-assert")

        with patch(
            "core.openai.auth.chatgpt_bootstrap.authenticated_bootstrap",
            side_effect=bootstrap_with_403,
        ), patch.object(account_export, "human_delay"), patch.object(
            account_export, "_trigger_reauth", side_effect=assert_reauth_can_start
        ):
            with self.assertRaisesRegex(RuntimeError, "stop-after-assert"):
                account_export.setup_2fa(
                    session,
                    "user@example.com",
                    access_token="access-token",
                )

        self.assertEqual(session.reset_count, 1)

    def test_bootstrap_exception_also_clears_circuit(self):
        session = _CircuitSession()

        def bootstrap_failure(*_args, **_kwargs):
            session.blocked_until = 9999999999.0
            session.blocked_reason = "HTTP 403 from optional bootstrap"
            raise RuntimeError("bootstrap failed")

        def assert_reauth_can_start(_session, _email):
            self.assertEqual(session.blocked_until, 0.0)
            raise RuntimeError("stop-after-assert")

        with patch(
            "core.openai.auth.chatgpt_bootstrap.authenticated_bootstrap",
            side_effect=bootstrap_failure,
        ), patch.object(account_export, "human_delay"), patch.object(
            account_export, "_trigger_reauth", side_effect=assert_reauth_can_start
        ):
            with self.assertRaisesRegex(RuntimeError, "stop-after-assert"):
                account_export.setup_2fa(
                    session,
                    "user@example.com",
                    access_token="access-token",
                )

        self.assertEqual(session.reset_count, 1)


    def test_reauth_navigation_id_is_reused_by_authorize_password_and_otp_posts(self):
        auth_url = "https://auth.openai.com/api/accounts/authorize?state=test"
        session = _ReauthSession(
            [_Response(200)],
            [
                _Response(200, {
                    "page": {"type": "login_password"},
                    "continue_url": "https://auth.openai.com/log-in/password",
                }),
                _Response(200, {
                    "page": {"type": "email_otp_verification"},
                    "continue_url": "https://auth.openai.com/email-verification",
                }),
                _Response(200, {
                    "continue_url": "https://chatgpt.com/api/auth/callback/openai",
                }),
            ],
        )

        with patch.object(account_export, "_trigger_reauth_with_retry", return_value=auth_url), \
             patch.object(account_export, "human_delay"), \
             patch("core.openai.auth.openai_auth.request_sentinel_token", side_effect=[
                 {"token": "continue"}, {"token": "password"}, {"token": "otp"},
             ]) as sentinel, \
             patch("core.openai.auth.openai_auth.build_sentinel_header", return_value=("sentinel-header", None)), \
             patch.object(account_export, "_exchange_new_token", return_value="fresh-token"), \
             patch.object(account_export, "_enroll_totp", return_value=("totp-secret", "session-id")), \
             patch.object(account_export, "_activate_totp"), \
             patch.object(account_export, "_verify_mfa_after_activation"):
            result = account_export.setup_2fa(
                session,
                "user@example.test",
                otp_code="123456",
                password="saved-password",
            )

        self.assertEqual(result, "totp-secret")
        self.assertEqual(
            [call.args[1] for call in sentinel.call_args_list],
            ["authorize_continue", "password_verify", "email_otp_validate"],
        )
        self.assertEqual(session.rotation_history, [("document-1", "document-1-next")])
        self.assertEqual(
            [request[1]["x-openai-document-navigation-id"] for request in session.posts],
            ["document-1-next"] * 3,
        )
        self.assertEqual(
            [request[0].rsplit("/", 1)[-1] for request in session.posts],
            ["continue", "verify", "validate"],
        )
        self.assertNotIn("screen_hint", json.loads(session.posts[0][2]))
        self.assertEqual(
            session.posts[0][1]["referer"], "https://auth.openai.com/create-account",
        )

    def test_authorize_http_error_rotates_before_retry_and_probe_uses_success_id(self):
        auth_url = "https://auth.openai.com/api/accounts/authorize?state=test"
        session = _ReauthSession(
            [_Response(403), _Response(200)],
            [_Response(200, {
                "page": {"type": "email_otp_verification"},
                "continue_url": "https://auth.openai.com/email-verification",
            })],
        )

        with patch("config.twofa.TWOFA_REAUTH_MAX_ATTEMPTS", 2), \
             patch("config.twofa.TWOFA_REAUTH_RETRY_DELAY", 0), \
             patch.object(account_export.time, "sleep"), \
             patch("core.openai.auth.openai_auth.request_sentinel_token", return_value={"token": "continue"}), \
             patch("core.openai.auth.openai_auth.build_sentinel_header", return_value=("sentinel-header", None)):
            landing_url = account_export._follow_reauth_with_retry(session, auth_url)
            state = openai_auth.probe_registration_state(session, "user@example.test", strict=True)

        self.assertEqual(landing_url, "https://auth.openai.com/email-verification")
        self.assertEqual(state["step"], "otp")
        self.assertEqual(
            [document_id for url, document_id in session.gets if not url.endswith("/log-in")],
            ["document-1", "document-1-next"],
        )
        self.assertEqual(
            session.rotation_history,
            [
                ("document-1", "document-1-next"),
                ("document-1-next", "document-1-next-next"),
            ],
        )
        self.assertEqual(
            session.posts[0][1]["x-openai-document-navigation-id"],
            "document-1-next-next",
        )

    def test_reauth_transport_error_keeps_id_until_response_retry(self):
        auth_url = "https://auth.openai.com/api/accounts/authorize?state=test"
        session = _ReauthSession([TimeoutError("timed out"), _Response(200)])

        with patch("config.twofa.TWOFA_REAUTH_MAX_ATTEMPTS", 2), \
             patch("config.twofa.TWOFA_REAUTH_RETRY_DELAY", 0), \
             patch.object(account_export.time, "sleep"):
            landing_url = account_export._follow_reauth_with_retry(session, auth_url)

        self.assertEqual(landing_url, "https://auth.openai.com/email-verification")
        self.assertEqual(
            [document_id for url, document_id in session.gets if not url.endswith("/log-in")],
            ["document-1", "document-1"],
        )
        self.assertEqual(session.rotation_history, [("document-1", "document-1-next")])
        self.assertEqual(session.reset_count, 1)
if __name__ == "__main__":
    unittest.main()
