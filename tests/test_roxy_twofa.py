# -*- coding: utf-8 -*-
import unittest
from collections import deque
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from config import humanize as humanize_config
from config import roxybrowser as roxy_config
from core import humanize as humanize_runtime
from core import roxy_2fa, roxy_registration


SECRET = "JBSWY3DPEHPK3PXP"
INITIAL_SESSION = {
    "accessToken": "initial-access-token",
    "user": {"id": "user-id", "email": "user@example.com", "name": "User"},
    "account": {"id": "account-id", "planType": "free"},
    "expires": "2026-09-01T00:00:00.000Z",
}
REFRESHED_SESSION = {
    "accessToken": "refreshed-access-token",
    "user": {"id": "user-id", "email": "user@example.com", "name": "User"},
    "account": {"id": "account-id", "planType": "free"},
    "expires": "2026-10-01T00:00:00.000Z",
}


class FakeDriver:
    def __init__(self, async_results, *, body_text="Check your email", has_code_input=True):
        self.async_results = deque(async_results)
        self.body_text = body_text
        self.has_code_input = has_code_input
        self.current_url = "https://chatgpt.com/"
        self.async_calls = []
        self.script_timeouts = []
        self.get_calls = []

    def set_script_timeout(self, value):
        self.script_timeouts.append(value)

    def execute_async_script(self, script, *args):
        self.async_calls.append((script, args))
        result = self.async_results.popleft()
        if isinstance(result, BaseException):
            raise result
        return result

    def execute_script(self, script, *args):
        if "document.body" in script:
            return {"text": self.body_text, "hasCodeInput": self.has_code_input}
        return None

    def get(self, url):
        self.get_calls.append(url)
        self.current_url = url


class HookRecorder:
    def __init__(self, *, otp="123456", outcome="accepted", email_page=True, session=None):
        self.otp = otp
        self.outcome = outcome
        self.email_page = email_page
        self.session = dict(session or REFRESHED_SESSION)
        self.calls = []

    def hooks(self):
        return roxy_2fa.RoxyTwoFactorHooks(
            wait_for_otp=self.wait_for_otp,
            clear_otp_inputs=lambda: self.calls.append(("clear",)),
            type_otp=lambda code: self.calls.append(("type", code)),
            submit_otp=lambda: self.calls.append(("submit",)),
            wait_after_otp_submit=lambda: self.outcome,
            is_email_verification_page=lambda: self.email_page,
            click_resend_otp=lambda: self.calls.append(("resend",)),
            fetch_session=self.fetch_session,
            check_stop=lambda: self.calls.append(("stop",)),
        )

    def wait_for_otp(self, email, *, after_ts):
        self.calls.append(("wait", email, after_ts))
        if isinstance(self.otp, BaseException):
            raise self.otp
        return self.otp

    def fetch_session(self):
        self.calls.append(("session",))
        return dict(self.session)


class RoxyTwoFactorTests(unittest.TestCase):
    def _success_driver(self):
        return FakeDriver([
            {"ok": True, "status": 200, "url": "https://auth.openai.com/email-verification?state=fixture"},
            {"ok": True, "status": 200, "data": {"secret": SECRET, "session_id": "enrollment-session"}},
            {"ok": True, "status": 200, "data": {"success": True}},
            {"ok": True, "status": 200, "data": {}},
        ])

    def test_setup_uses_current_page_context_and_returns_refreshed_credentials(self):
        driver = self._success_driver()
        recorder = HookRecorder()

        result = roxy_2fa.setup_roxy_2fa(
            driver,
            "user@example.com",
            hooks=recorder.hooks(),
            clock=lambda: 5.0,
            sleep=lambda _seconds: None,
            script_timeout_restore=12,
        )

        self.assertEqual(result["status"], "enabled")
        self.assertEqual(result["secret"], SECRET)
        self.assertEqual(result["access_token"], "refreshed-access-token")
        self.assertEqual(result["session_info"], REFRESHED_SESSION)
        self.assertEqual(driver.get_calls, ["https://auth.openai.com/email-verification?state=fixture"])
        meaningful_calls = [call for call in recorder.calls if call[0] != "stop"]
        self.assertEqual([call[0] for call in meaningful_calls], ["wait", "clear", "type", "submit", "session"])
        self.assertEqual(meaningful_calls[0][1:], ("user@example.com", 5.0))
        self.assertEqual(driver.script_timeouts, [30, 12, 30, 12, 30, 12, 30, 12])
        self.assertIn("credentials: 'include'", driver.async_calls[0][0])
        self.assertIn("reauth: 'password'", driver.async_calls[0][0])
        self.assertIn("mfa/enroll", roxy_2fa._CHATGPT_MFA_ENROLL_URL)
        activation_body = driver.async_calls[2][1][2]
        self.assertEqual(activation_body["factor_type"], "totp")
        self.assertEqual(activation_body["session_id"], "enrollment-session")
        self.assertRegex(activation_body["code"], r"^\d{6}$")

    def test_disabled_result_never_touches_driver_or_mailbox(self):
        driver = MagicMock()
        recorder = HookRecorder()

        result = roxy_2fa.disabled_result()

        self.assertEqual(result["status"], "disabled")
        driver.assert_not_called()
        self.assertEqual(recorder.calls, [])

    def test_untrusted_reauth_url_fails_before_otp(self):
        driver = FakeDriver([{"ok": True, "status": 200, "url": "https://evil.example/steal"}])
        recorder = HookRecorder()

        result = roxy_2fa.setup_roxy_2fa(
            driver,
            "user@example.com",
            hooks=recorder.hooks(),
            sleep=lambda _seconds: None,
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["stage"], "reauth")
        self.assertEqual(result["error"]["code"], "reauth_url_untrusted")
        self.assertFalse(driver.get_calls)
        self.assertNotIn("wait", [call[0] for call in recorder.calls])

    def test_invalid_reauth_otp_is_reported_without_enrollment(self):
        driver = FakeDriver([{"ok": True, "status": 200, "url": "https://auth.openai.com/email-verification"}])
        recorder = HookRecorder(outcome="invalid")

        result = roxy_2fa.setup_roxy_2fa(
            driver,
            "user@example.com",
            hooks=recorder.hooks(),
            max_otp_attempts=1,
            sleep=lambda _seconds: None,
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["stage"], "otp")
        self.assertEqual(result["error"]["code"], "reauth_otp_invalid")
        self.assertEqual(len(driver.async_calls), 1)

    def test_reauth_otp_timeout_is_reported_without_enrollment(self):
        driver = FakeDriver([{"ok": True, "status": 200, "url": "https://auth.openai.com/email-verification"}])
        recorder = HookRecorder(otp=TimeoutError("mailbox timeout"))

        result = roxy_2fa.setup_roxy_2fa(
            driver,
            "user@example.com",
            hooks=recorder.hooks(),
            max_otp_attempts=1,
            sleep=lambda _seconds: None,
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["stage"], "otp")
        self.assertEqual(result["error"]["code"], "reauth_otp_timeout")
        self.assertEqual(len(driver.async_calls), 1)

    def test_malformed_enrollment_never_activates(self):
        driver = FakeDriver([
            {"ok": True, "status": 200, "url": "https://auth.openai.com/email-verification"},
            {"ok": True, "status": 200, "data": {"session_id": "missing-secret"}},
        ])
        recorder = HookRecorder()

        result = roxy_2fa.setup_roxy_2fa(
            driver,
            "user@example.com",
            hooks=recorder.hooks(),
            clock=lambda: 5.0,
            sleep=lambda _seconds: None,
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["stage"], "enroll")
        self.assertEqual(result["error"]["code"], "totp_enroll_response_invalid")
        self.assertEqual(len(driver.async_calls), 2)

    def test_session_fetch_failure_is_classified_as_session_stage(self):
        driver = FakeDriver([
            {"ok": True, "status": 200, "url": "https://auth.openai.com/email-verification"},
        ])
        recorder = HookRecorder()
        recorder.session = RuntimeError("response contains private token")

        result = roxy_2fa.setup_roxy_2fa(
            driver,
            "user@example.com",
            hooks=recorder.hooks(),
            sleep=lambda _seconds: None,
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["stage"], "session")
        self.assertEqual(result["error"]["code"], "reauth_session_fetch_failed")

    def test_activation_failure_is_distinguished(self):
        driver = FakeDriver([
            {"ok": True, "status": 200, "url": "https://auth.openai.com/email-verification"},
            {"ok": True, "status": 200, "data": {"secret": SECRET, "session_id": "s1"}},
            {"ok": False, "status": 403, "data": {"success": False}},
        ])
        recorder = HookRecorder()

        result = roxy_2fa.setup_roxy_2fa(
            driver,
            "user@example.com",
            hooks=recorder.hooks(),
            clock=lambda: 5.0,
            sleep=lambda _seconds: None,
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["stage"], "activate")
        self.assertEqual(result["error"]["code"], "totp_activate_failed")
        self.assertEqual(result["error"]["http_status"], 403)

    def test_existing_authenticator_returns_already_enabled_without_secret_overwrite(self):
        driver = FakeDriver(
            [{"ok": True, "status": 200, "url": "https://auth.openai.com/email-verification"}],
            body_text="Enter a code from your authenticator app",
        )
        recorder = HookRecorder(email_page=False)

        result = roxy_2fa.setup_roxy_2fa(
            driver,
            "user@example.com",
            hooks=recorder.hooks(),
            sleep=lambda _seconds: None,
        )

        self.assertEqual(result["status"], "already_enabled")
        self.assertIsNone(result["secret"])
        self.assertNotIn("wait", [call[0] for call in recorder.calls])
        self.assertEqual(len(driver.async_calls), 1)

    def test_authenticator_text_without_visible_code_input_does_not_false_positive(self):
        driver = FakeDriver(
            [{"ok": True, "status": 200, "url": "https://auth.openai.com/email-verification"}],
            body_text="Learn about two-factor authentication",
            has_code_input=False,
        )
        recorder = HookRecorder(otp=TimeoutError("mailbox timeout"))

        result = roxy_2fa.setup_roxy_2fa(
            driver,
            "user@example.com",
            hooks=recorder.hooks(),
            max_otp_attempts=1,
            sleep=lambda _seconds: None,
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "reauth_otp_timeout")

    def test_page_script_failure_restores_timeout_and_redacts_result(self):
        driver = FakeDriver([RuntimeError("secret=https://example.invalid/token")])
        recorder = HookRecorder()

        result = roxy_2fa.setup_roxy_2fa(
            driver,
            "user@example.com",
            hooks=recorder.hooks(),
            sleep=lambda _seconds: None,
            script_timeout_restore=9,
        )
        summary = roxy_2fa.public_result({
            **result,
            "secret": SECRET,
            "access_token": "access-token-not-for-log",
            "session_info": {"accessToken": "access-token-not-for-log"},
        })

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["stage"], "reauth")
        self.assertEqual(result["error"]["code"], "reauth_request_failed")
        self.assertEqual(driver.script_timeouts, [30, 9])
        rendered = repr(summary)
        self.assertNotIn(SECRET, rendered)
        self.assertNotIn("access-token-not-for-log", rendered)

    def test_stop_request_propagates_without_becoming_twofa_failure(self):
        from core.registration_service import StopRequested

        driver = MagicMock()
        recorder = HookRecorder()

        def stop():
            raise StopRequested("stop")

        with self.assertRaises(StopRequested):
            roxy_2fa.setup_roxy_2fa(
                driver,
                "user@example.com",
                hooks=replace(recorder.hooks(), check_stop=stop),
                sleep=lambda _seconds: None,
            )


class RoxyRegistrationTwoFactorIntegrationTests(unittest.TestCase):
    def test_apply_roxy_twofa_disabled_skips_browser_flow(self):
        driver = MagicMock()
        with patch.object(roxy_registration._twofa_cfg, "ENABLE_2FA", False), patch.object(
            roxy_registration, "human_delay"
        ) as delay:
            session, token, secret, summary = roxy_registration._apply_roxy_twofa(
                driver,
                "user@example.com",
                dict(INITIAL_SESSION),
                "initial-access-token",
            )

        self.assertEqual(session, INITIAL_SESSION)
        self.assertEqual(token, "initial-access-token")
        self.assertIsNone(secret)
        self.assertEqual(summary["status"], "disabled")
        delay.assert_not_called()
        driver.assert_not_called()

    def test_apply_roxy_twofa_success_uses_refreshed_credentials_and_safe_summary(self):
        driver = MagicMock()
        events = []
        full_result = {
            "status": "enabled",
            "secret": SECRET,
            "access_token": "refreshed-access-token",
            "session_info": dict(REFRESHED_SESSION),
            "activated_at": "2026-08-21T00:00:00+00:00",
            "error": None,
            "validation": {"status": "ok", "code": "token_valid"},
        }
        def record_delay(kind):
            events.append(("delay", kind))

        def setup_twofa(*args, **kwargs):
            events.append(("setup",))
            return full_result

        with patch.object(roxy_registration._twofa_cfg, "ENABLE_2FA", True), patch.object(
            roxy_registration, "human_delay", side_effect=record_delay
        ), patch.object(
            roxy_2fa, "setup_roxy_2fa", side_effect=setup_twofa
        ) as setup:
            session, token, secret, summary = roxy_registration._apply_roxy_twofa(
                driver,
                "user@example.com",
                dict(INITIAL_SESSION),
                "initial-access-token",
            )

        setup.assert_called_once()
        self.assertEqual(events[:2], [("delay", "twofa_start"), ("setup",)])
        self.assertEqual(session, REFRESHED_SESSION)
        self.assertEqual(token, "refreshed-access-token")
        self.assertEqual(secret, SECRET)
        self.assertEqual(summary["status"], "enabled")
        self.assertNotIn("secret", summary)
        self.assertNotIn("access_token", summary)
        self.assertEqual(driver.set_script_timeout.call_args_list[0].args, (12,))

    def test_apply_roxy_twofa_failure_preserves_initial_credentials(self):
        driver = MagicMock()
        failed_result = {
            "status": "failed",
            "error": {"stage": "activate", "code": "totp_activate_failed", "message": "failure"},
        }
        with patch.object(roxy_registration._twofa_cfg, "ENABLE_2FA", True), patch.object(
            roxy_registration, "human_delay"
        ), patch.object(
            roxy_2fa, "setup_roxy_2fa", return_value=failed_result
        ):
            session, token, secret, summary = roxy_registration._apply_roxy_twofa(
                driver,
                "user@example.com",
                dict(INITIAL_SESSION),
                "initial-access-token",
            )

        self.assertEqual(session, INITIAL_SESSION)
        self.assertEqual(token, "initial-access-token")
        self.assertIsNone(secret)
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["error"]["code"], "totp_activate_failed")

    def test_apply_roxy_twofa_already_enabled_preserves_initial_credentials(self):
        driver = MagicMock()
        already_enabled = {
            "status": "already_enabled",
            "error": {"stage": "reauth", "code": "already_enabled", "message": "already enabled"},
        }
        with patch.object(roxy_registration._twofa_cfg, "ENABLE_2FA", True), patch.object(
            roxy_registration, "human_delay"
        ), patch.object(roxy_2fa, "setup_roxy_2fa", return_value=already_enabled):
            session, token, secret, summary = roxy_registration._apply_roxy_twofa(
                driver,
                "user@example.com",
                dict(INITIAL_SESSION),
                "initial-access-token",
            )

        self.assertEqual(session, INITIAL_SESSION)
        self.assertEqual(token, "initial-access-token")
        self.assertIsNone(secret)
        self.assertEqual(summary["status"], "already_enabled")

    def test_apply_roxy_twofa_incomplete_success_preserves_initial_credentials(self):
        driver = MagicMock()
        incomplete_result = {
            "status": "enabled",
            "secret": "",
            "access_token": "",
            "session_info": dict(REFRESHED_SESSION),
            "error": None,
        }
        with patch.object(roxy_registration._twofa_cfg, "ENABLE_2FA", True), patch.object(
            roxy_registration, "human_delay"
        ), patch.object(roxy_2fa, "setup_roxy_2fa", return_value=incomplete_result):
            session, token, secret, summary = roxy_registration._apply_roxy_twofa(
                driver,
                "user@example.com",
                dict(INITIAL_SESSION),
                "initial-access-token",
            )

        self.assertEqual(session, INITIAL_SESSION)
        self.assertEqual(token, "initial-access-token")
        self.assertIsNone(secret)
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["error"]["code"], "twofa_success_result_invalid")

    def test_twofa_start_delay_has_expected_default_range(self):
        self.assertEqual(humanize_config.HUMANIZE_DELAYS["twofa_start"], (1.5, 4.0))
        self.assertTrue(humanize_config.ENABLE_HUMANIZE_DELAY)
        self.assertEqual(humanize_config.HUMANIZE_DELAY_FACTOR, 1.0)

    def test_disabled_humanization_skips_sleep_for_twofa_start(self):
        with patch.object(humanize_config, "ENABLE_HUMANIZE_DELAY", False), patch.object(
            humanize_runtime.time, "sleep"
        ) as sleep:
            result = humanize_runtime.delay("twofa_start")

        self.assertEqual(result, 0.0)
        sleep.assert_not_called()

    def test_config_and_documentation_describe_roxy_page_context(self):
        from pathlib import Path

        root = Path(__file__).parents[1]
        config_source = (root / "config" / "twofa.py").read_text(encoding="utf-8")
        env_source = (root / ".env.example").read_text(encoding="utf-8")
        webui_source = (root / "webui" / "config_editor.py").read_text(encoding="utf-8")
        readme_source = (root / "README.md").read_text(encoding="utf-8")
        self.assertIn("Selenium/Roxy Profile", config_source)
        self.assertIn("当前 Selenium Profile", env_source)
        self.assertIn("当前 Selenium Profile", webui_source)
        self.assertIn("enroll/activate TOTP", readme_source)
        self.assertNotIn("当前 Roxy 自动化路径暂不执行 2FA，已跳过", readme_source)

    def test_roxy_registration_does_not_log_full_email_otp(self):
        from pathlib import Path

        source = (Path(__file__).parents[1] / "core" / "roxy_registration.py").read_text(encoding="utf-8")
        self.assertNotIn('收到验证码：%s", current_otp', source)
        self.assertNotIn("直接重试提交：%s (fallback)", source)

    def test_account_list_exposes_only_safe_twofa_status(self):
        from webui import app as web_app

        row = {
            "id": 1,
            "email": "user@example.com",
            "totp_secret": "",
            "access_token": "access-token-not-for-list",
            "extra_json": (
                '{"twofa":{"status":"failed","error":{'
                '"stage":"activate","code":"totp_activate_failed",'
                '"message":"secret and token must not escape"}}}'
            ),
        }
        compact = web_app._compact_account_for_list(row)

        self.assertEqual(compact["twofa_status"], "failed")
        self.assertEqual(compact["twofa_stage"], "activate")
        self.assertEqual(compact["twofa_error_code"], "totp_activate_failed")
        rendered = repr(compact)
        self.assertNotIn(SECRET, rendered)
        self.assertNotIn("access-token-not-for-list", rendered)
        self.assertNotIn("secret and token must not escape", rendered)

    def test_public_twofa_result_replaces_untrusted_error_message(self):
        summary = roxy_2fa.public_result({
            "status": "failed",
            "error": {
                "stage": "activate",
                "code": "totp_activate_failed",
                "message": "token=private; secret=private",
            },
        })

        self.assertEqual(summary["error"]["message"], "2FA activation 失败")
        self.assertNotIn("token=private", repr(summary))
        self.assertNotIn("secret=private", repr(summary))

    def test_account_list_prefers_persisted_totp_secret_over_stale_failure_summary(self):
        from webui import app as web_app

        compact = web_app._compact_account_for_list({
            "id": 1,
            "email": "user@example.com",
            "totp_secret": SECRET,
            "extra_json": '{"twofa":{"status":"failed","error":{"code":"totp_activate_failed"}}}',
        })

        self.assertEqual(compact["twofa_status"], "enabled")
        self.assertNotIn(SECRET, repr(compact))

    def test_registration_saves_refreshed_token_and_secret_once(self):
        client = MagicMock()
        opened = SimpleNamespace(profile_id="profile-1", raw={})
        client.open_profile.return_value = opened
        driver = MagicMock()
        twofa = {"status": "enabled", "activated_at": "2026-08-21T00:00:00+00:00", "error": None}

        with patch.object(roxy_registration, "RoxyBrowserClient", return_value=client), patch.object(
            roxy_registration, "_build_driver", return_value=driver
        ), patch.object(roxy_registration, "_center_browser_window"), patch.object(
            roxy_registration, "_safe_get"
        ), patch.object(roxy_registration, "_page_warmup"), patch.object(
            roxy_registration, "_maybe_accept"
        ), patch.object(roxy_registration, "_check_manual_stop"), patch.object(
            roxy_registration, "_submit_email_and_wait_next", return_value="otp"
        ), patch.object(roxy_registration, "_clear_otp_inputs"), patch.object(
            roxy_registration, "_type_otp"
        ), patch.object(roxy_registration, "_click_continue"), patch.object(
            roxy_registration, "_wait_after_email_otp_submit", return_value="accepted"
        ), patch.object(roxy_registration, "_complete_profile_page", return_value=True), patch.object(
            roxy_registration, "_fetch_chatgpt_session", return_value=dict(INITIAL_SESSION)
        ), patch.object(
            roxy_registration,
            "_apply_roxy_twofa",
            return_value=(dict(REFRESHED_SESSION), "refreshed-access-token", SECRET, twofa),
        ) as apply_twofa, patch.object(
            roxy_registration, "save_account_data", return_value=101
        ) as save:
            result = roxy_registration.run_roxy_registration(
                "user@example.com", "User", "1990-01-01", otp_code="123456"
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["access_token"], "refreshed-access-token")
        self.assertEqual(result["totp_secret"], SECRET)
        self.assertEqual(result["twofa"], twofa)
        apply_twofa.assert_called_once()
        save.assert_called_once()
        kwargs = save.call_args.kwargs
        self.assertEqual(kwargs["access_token"], "refreshed-access-token")
        self.assertEqual(kwargs["totp_secret"], SECRET)
        self.assertEqual(kwargs["extra"]["twofa"], twofa)
        self.assertEqual(kwargs["extra"]["user"], REFRESHED_SESSION["user"])
        client.cleanup_profile.assert_called_once_with(opened)

    def test_registration_saves_original_token_once_when_twofa_fails(self):
        client = MagicMock()
        opened = SimpleNamespace(profile_id="profile-1", raw={})
        client.open_profile.return_value = opened
        driver = MagicMock()
        twofa = {
            "status": "failed",
            "activated_at": None,
            "error": {"stage": "activate", "code": "totp_activate_failed", "message": "failure"},
        }

        with patch.object(roxy_registration, "RoxyBrowserClient", return_value=client), patch.object(
            roxy_registration, "_build_driver", return_value=driver
        ), patch.object(roxy_registration, "_center_browser_window"), patch.object(
            roxy_registration, "_safe_get"
        ), patch.object(roxy_registration, "_page_warmup"), patch.object(
            roxy_registration, "_maybe_accept"
        ), patch.object(roxy_registration, "_check_manual_stop"), patch.object(
            roxy_registration, "_submit_email_and_wait_next", return_value="otp"
        ), patch.object(roxy_registration, "_clear_otp_inputs"), patch.object(
            roxy_registration, "_type_otp"
        ), patch.object(roxy_registration, "_click_continue"), patch.object(
            roxy_registration, "_wait_after_email_otp_submit", return_value="accepted"
        ), patch.object(roxy_registration, "_complete_profile_page", return_value=True), patch.object(
            roxy_registration, "_fetch_chatgpt_session", return_value=dict(INITIAL_SESSION)
        ), patch.object(
            roxy_registration,
            "_apply_roxy_twofa",
            return_value=(dict(INITIAL_SESSION), "initial-access-token", None, twofa),
        ), patch.object(
            roxy_registration, "save_account_data", return_value=102
        ) as save:
            result = roxy_registration.run_roxy_registration(
                "user@example.com", "User", "1990-01-01", otp_code="123456"
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["access_token"], "initial-access-token")
        self.assertIsNone(result["totp_secret"])
        self.assertEqual(result["twofa"], twofa)
        save.assert_called_once()
        kwargs = save.call_args.kwargs
        self.assertEqual(kwargs["access_token"], "initial-access-token")
        self.assertIsNone(kwargs["totp_secret"])
        self.assertEqual(kwargs["extra"]["twofa"], twofa)

    def test_registration_saves_initial_token_once_when_twofa_disabled(self):
        client = MagicMock()
        opened = SimpleNamespace(profile_id="profile-1", raw={})
        client.open_profile.return_value = opened
        driver = MagicMock()

        with patch.object(roxy_registration, "RoxyBrowserClient", return_value=client), patch.object(
            roxy_registration, "_build_driver", return_value=driver
        ), patch.object(roxy_registration, "_center_browser_window"), patch.object(
            roxy_registration, "_safe_get"
        ), patch.object(roxy_registration, "_page_warmup"), patch.object(
            roxy_registration, "_maybe_accept"
        ), patch.object(roxy_registration, "_check_manual_stop"), patch.object(
            roxy_registration, "_submit_email_and_wait_next", return_value="otp"
        ), patch.object(roxy_registration, "_clear_otp_inputs"), patch.object(
            roxy_registration, "_type_otp"
        ), patch.object(roxy_registration, "_click_continue"), patch.object(
            roxy_registration, "_wait_after_email_otp_submit", return_value="accepted"
        ), patch.object(roxy_registration, "_complete_profile_page", return_value=True), patch.object(
            roxy_registration, "_fetch_chatgpt_session", return_value=dict(INITIAL_SESSION)
        ), patch.object(roxy_registration._twofa_cfg, "ENABLE_2FA", False), patch.object(
            roxy_registration, "save_account_data", return_value=103
        ) as save:
            result = roxy_registration.run_roxy_registration(
                "user@example.com", "User", "1990-01-01", otp_code="123456"
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["access_token"], "initial-access-token")
        self.assertIsNone(result["totp_secret"])
        self.assertEqual(result["twofa"]["status"], "disabled")
        save.assert_called_once()
        kwargs = save.call_args.kwargs
        self.assertEqual(kwargs["access_token"], "initial-access-token")
        self.assertIsNone(kwargs["totp_secret"])
        self.assertEqual(kwargs["extra"]["twofa"]["status"], "disabled")


if __name__ == "__main__":
    unittest.main()
