# -*- coding: utf-8 -*-
"""注册编排的资料分流、初始化错误及任务资源边界。"""
import os
import tempfile
import json
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import main as registration
from core.openai.codex import codex_oauth
from core.infra import db, job_context
from core.mail import email_provider
from core.link import flow_trigger
from core.openai.register import registration_service
from config import register as register_config


class _Session:
    proxy = ""
    device_id = "device-for-test"
    browser_profile = {}

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class RegistrationPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.stack = ExitStack()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {"RUNTIME_STORAGE_BACKEND": "json"}))
        for name in (
            "_OUTLOOK_JSON", "_OUTLOOK_TXT", "_GENERIC_API_EMAIL_JSON", "_GENERIC_API_EMAIL_TXT",
            "_ICLOUD_EMAIL_JSON", "_DOMAIN_EMAIL_JSON", "_ACCOUNTS_JSON", "_ACCOUNTS_TXT",
            "_TOKENS_TXT", "_JOBS_JSON", "_VIEWER_HTML", "_EMAIL_POOL_LIFECYCLE_JSON",
            "_LEGACY_ACCOUNTS_JSON", "_LEGACY_OUTLOOK_JSON", "_LEGACY_JOBS_JSON", "_LEGACY_SQLITE",
        ):
            self.stack.enter_context(patch.object(db, name, self.root / name))
        self.stack.enter_context(patch.object(db, "_DATA_DIR", self.root))
        self.stack.enter_context(patch.object(db, "_LOG_DIR", self.root / "logs"))
        self.stack.enter_context(patch.object(register_config, "AUTO_PLAN_CHECK_AFTER_REGISTER", False))

    @contextmanager
    def _pipeline(self, otp_result, kind="new_password", step="password"):
        session = _Session()
        observed = {"created": False, "saved": False, "flows": []}

        def request_sentinel(_session, flow):
            observed["flows"].append(flow)
            return {
                "token": "challenge", "_sentinel_flow": flow,
                "_sentinel_device_id": session.device_id,
                "_sentinel_header": json.dumps({
                    "p": "proof", "c": "challenge",
                    "id": session.device_id, "flow": flow,
                }),
                "_sentinel_so_header": None,
            }

        def create_account(*_args):
            observed["created"] = True
            return {"continue_url": "https://chatgpt.com/api/auth/callback/openai"}

        original_save = registration.save_account_data

        def save_account(**kwargs):
            observed["saved"] = True
            kwargs["batch_dir"] = self.root / "batch"
            return original_save(**kwargs)

        with ExitStack() as stack:
            for name, value in {
                "_prepare_persisted_email_registration": False,
                "BrowserSession": session,
                "registration_preflight": None,
                "human_delay": None,
                "get_csrf_token": "csrf",
                "signin_openai": "https://auth.openai.com/authorize",
                "follow_authorize": "https://auth.openai.com/create-account/password",
                "get_registration_state": {"kind": kind, "step": step,
                    "page": {"type": "login_password" if kind == "existing" else "signup_password"},
                    "continue_url": "https://auth.openai.com/log-in/password" if kind == "existing"
                    else "https://auth.openai.com/create-account/password"},
                "navigate_create_account_password": None,
                "generate_registration_password": "Test-only-password!",
                "register_user": {"continue_url": "/api/accounts/email-otp/send"},
                "navigate_email_otp_send": None,
                "validate_email_otp": otp_result,
                "_finalize_registration_session": ({
                    "user": {"id": "test-user", "email": "user@example.com"},
                    "account": {"planType": "free"}, "expires": "2030-01-01T00:00:00Z",
                }, "test-access"),
                "get_mfa_enabled": False,
                "release_email_if_unconsumed": False,
            }.items():
                stack.enter_context(patch.object(registration, name, return_value=value))
            stack.enter_context(patch.object(registration, "request_sentinel_token", side_effect=request_sentinel))
            stack.enter_context(patch.object(registration, "create_account", side_effect=create_account))
            stack.enter_context(patch.object(registration, "save_account_data", side_effect=save_account))
            stack.enter_context(patch.object(registration._twofa_cfg, "ENABLE_2FA", False))
            stack.enter_context(patch.multiple(
                registration._protocol_cfg, CHATGPT_ANON_BOOTSTRAP_ENABLED=False,
                CHATGPT_AUTH_BOOTSTRAP_ENABLED=False, SEND_SENTINEL_ON_EMAIL_OTP_VALIDATE=False,
            ))
            stack.enter_context(patch.object(codex_oauth, "run_codex_oauth", return_value={"status": "skipped"}))
            stack.enter_context(patch.object(flow_trigger, "trigger_flow", return_value={"status": "skipped"}))
            stack.enter_context(patch.object(email_provider, "resolve_email_source", return_value="manual"))
            yield session, observed

    def test_direct_callback_does_not_submit_profile_or_request_its_proof(self):
        with self._pipeline({
            "page": {"type": "external_url"},
            "continue_url": "https://chatgpt.com/api/auth/callback/openai",
        }) as (session, observed):
            result = registration.run_registration(
                "user@example.com", "Test User", "1995-01-01", otp_code="123456",
            )
        self.assertTrue(result["success"])
        self.assertTrue(observed["saved"])
        self.assertFalse(observed["created"])
        self.assertTrue(session.closed)

    def test_profile_branch_submits_once_after_validation_without_an_extra_navigation(self):
        with self._pipeline({
            "page": {"type": "about_you"}, "continue_url": "/about-you",
        }) as (session, observed):
            result = registration.run_registration(
                "user@example.com", "Test User", "1995-01-01", otp_code="123456",
            )
        self.assertTrue(result["success"])
        self.assertTrue(observed["created"])
        self.assertTrue(observed["saved"])
        self.assertTrue(session.closed)

    def test_sdk_failure_preserves_its_stage_and_prevents_password_submission(self):
        from core.openai.auth.sentinel_runner import SentinelSDKError
        with self._pipeline({}) as (session, observed), patch.object(
            registration, "request_sentinel_token",
            side_effect=SentinelSDKError("SDK collection failed", "sdk_exec_failed", "requirements"),
        ), patch.object(registration, "register_user") as submit:
            result = registration.run_registration(
                "user@example.com", "Test User", "1995-01-01", otp_code="123456",
            )
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "sdk_exec_failed")
        self.assertEqual(result["error_stage"], "requirements")
        submit.assert_not_called()
        self.assertFalse(observed["saved"])
        self.assertTrue(session.closed)

    def test_constructor_failure_reaches_task_error_without_attempting_authentication(self):
        class InitializationFailure(RuntimeError):
            category = "geo_incomplete"
            stage = "validate"

        with patch.object(registration, "_prepare_persisted_email_registration", return_value=False), patch.object(
            registration, "BrowserSession", side_effect=InitializationFailure("地域信息不完整")
        ), patch.object(registration, "release_email_if_unconsumed", return_value=False), patch.object(
            registration, "signin_openai", side_effect=AssertionError("must not authenticate")
        ):
            result = registration.run_registration("user@example.com", "Test User", "1995-01-01")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "geo_incomplete")
        self.assertEqual(result["error_stage"], "validate")

    def test_unknown_landing_sentinel_probe_failure_fails_without_guessing_account_type(self):
        from core.openai.auth.openai_auth import SentinelRequestError

        error = SentinelRequestError("Sentinel challenge 请求失败（Timeout，HTTP=None）", "sentinel_network_failed")
        with self._pipeline({
            "page": {"type": "external_url"},
            "continue_url": "https://chatgpt.com/api/auth/callback/openai",
        }, kind="unknown") as (session, observed), \
                patch.object(registration, "probe_registration_state", side_effect=error) as probe:
            result = self._run()

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "sentinel_network_failed")
        self.assertEqual(result["error_stage"], "sentinel_challenge")
        self.assertEqual(result["registration"]["account_kind"], "unknown")
        self.assertEqual(observed["flows"], [])
        self.assertFalse(observed["saved"])
        probe.assert_called_once_with(
            session, "user@example.com",
            referer="https://auth.openai.com/", screen_hint="signup",
        )
        self.assertTrue(session.closed)

    def test_email_otp_sentinel_failure_stops_before_validation(self):
        from core.openai.auth.openai_auth import SentinelRequestError

        error = SentinelRequestError("Sentinel challenge 请求失败（Timeout，HTTP=None）", "sentinel_network_failed")
        with self._pipeline({
            "page": {"type": "external_url"},
            "continue_url": "https://chatgpt.com/api/auth/callback/openai",
        }) as (session, observed):
            def fail_sentinel(current_session, flow):
                observed["flows"].append(flow)
                if flow == "email_otp_validate":
                    raise error
                return {
                    "token": "challenge", "_sentinel_flow": flow,
                    "_sentinel_device_id": current_session.device_id,
                    "_sentinel_header": json.dumps({
                        "p": "proof", "c": "challenge",
                        "id": current_session.device_id, "flow": flow,
                    }),
                    "_sentinel_so_header": None,
                }

            with patch.object(registration._protocol_cfg, "SEND_SENTINEL_ON_EMAIL_OTP_VALIDATE", True), \
                    patch.object(registration, "request_sentinel_token", side_effect=fail_sentinel), \
                    patch.object(registration, "validate_email_otp") as validate:
                result = self._run()

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "sentinel_network_failed")
        self.assertEqual(result["error_stage"], "sentinel_challenge")
        self.assertEqual(observed["flows"], ["username_password_create", "email_otp_validate"])
        validate.assert_not_called()
        self.assertFalse(observed["saved"])
        self.assertTrue(session.closed)

    def test_profile_sentinel_failure_stops_before_create_account(self):
        from core.openai.auth.openai_auth import SentinelRequestError

        error = SentinelRequestError("Sentinel challenge 响应缺少有效 token", "sentinel_challenge_failed")
        otp_result = {
            "page": {"type": "about_you"},
            "continue_url": "https://auth.openai.com/about-you",
        }

        def request_sentinel(current_session, flow):
            observed["flows"].append(flow)
            if flow == "oauth_create_account":
                raise error
            return {
                "token": "challenge", "_sentinel_flow": flow,
                "_sentinel_device_id": current_session.device_id,
                "_sentinel_header": json.dumps({
                    "p": "proof", "c": "challenge",
                    "id": current_session.device_id, "flow": flow,
                }),
                "_sentinel_so_header": None,
            }

        with self._pipeline(otp_result) as (session, observed), \
                patch.object(registration, "request_sentinel_token", side_effect=request_sentinel), \
                patch.object(registration, "create_account") as create:
            result = self._run()

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "sentinel_challenge_failed")
        self.assertEqual(result["error_stage"], "sentinel_challenge")
        self.assertEqual(observed["flows"], ["username_password_create", "oauth_create_account"])
        create.assert_not_called()
        self.assertFalse(observed["saved"])
        self.assertTrue(session.closed)

    def test_sentinel_failure_task_log_has_safe_reason_stage_and_category(self):
        from core.openai.auth.openai_auth import SentinelRequestError

        secrets = (
            "sentinel-token-fixture", "sentinel-so-fixture", "challenge-fixture",
            "cookie-fixture", "otp-fixture", "device-fixture",
            "proxy-user-fixture:proxy-pass-fixture@proxy.example",
        )
        error = SentinelRequestError(
            "sensitive " + " ".join(secrets), "sentinel_network_failed",
        )
        job = db.create_job(email_source="manual", email="user@example.com")

        with self._pipeline({}, kind="unknown"), \
                patch.object(registration_service, "_prepare_registration_args",
                    return_value=("user@example.com", "Test User", "1995-01-01")), \
                patch.object(registration, "probe_registration_state", side_effect=error):
            registration_service._run_one_job(job["id"], job["log_file"])

        stored_job = db.get_job(job["id"])
        task_log = registration_service.read_job_log(job["id"])
        self.assertEqual(stored_job["status"], "failed")
        self.assertEqual(stored_job["error_code"], "sentinel_network_failed")
        self.assertEqual(stored_job["error_stage"], "sentinel_challenge")
        self.assertIn("Sentinel flow 执行失败", stored_job["error_message"])
        self.assertIn("stage=sentinel_challenge", task_log)
        self.assertIn("error_code=sentinel_network_failed", task_log)
        self.assertIn("reason=Sentinel flow 执行失败", task_log)
        for secret in secrets:
            self.assertNotIn(secret, task_log)
            self.assertNotIn(secret, stored_job["error_message"])

    def test_existing_fallback_sends_otp_before_validation(self):
        events = []

        def send_otp(*_args, **_kwargs):
            events.append("send")

        def validate(*_args, **_kwargs):
            self.assertEqual(events, ["send"])
            return {
                "page": {"type": "external_url"},
                "continue_url": "https://chatgpt.com/api/auth/callback/openai",
            }

        with self._pipeline({}, kind="unknown") as (session, _), \
                patch.object(registration, "probe_registration_state", return_value={
                    "kind": "existing", "step": "otp", "page": {"type": "unknown_page"},
                    "continue_url": "", "classification_source": "authorize_continue",
                    "otp_delivery_required": True,
                }) as probe, \
                patch.object(registration, "send_email_otp", side_effect=send_otp) as send, \
                patch.object(registration, "validate_email_otp", side_effect=validate) as validate_otp, \
                patch.object(registration, "register_user") as register:
            result = self._run()

        self.assertTrue(result["success"])
        probe.assert_called_once_with(
            session, "user@example.com",
            referer="https://auth.openai.com/", screen_hint="signup",
        )
        send.assert_called_once_with(session, referer="https://auth.openai.com/create-account/password")
        validate_otp.assert_called_once()
        register.assert_not_called()
        self.assertTrue(session.closed)

    def _run(self):
        return registration.run_registration(
            "user@example.com", "Test User", "1995-01-01", otp_code="123456",
        )

    def test_password_is_logged_before_submission_and_survives_later_otp_failure(self):
        def submit(*args):
            self.assertIn("Test-only-password!", "\n".join(captured.output))
            return {"continue_url": "/api/accounts/email-otp/send"}

        def send(*args):
            credentials = db.get_account_credentials("user@example.com")
            self.assertEqual(credentials["registration_password"], "Test-only-password!")
            self.assertFalse(credentials["complete"])
            self.assertEqual(db.count_accounts(), 0)
            raise TimeoutError("OTP delivery failed")

        with self._pipeline({}) as (session, _), self.assertLogs("main", level="INFO") as captured, \
                patch.object(registration, "register_user", side_effect=submit), \
                patch.object(registration, "navigate_email_otp_send", side_effect=send):
            result = self._run()
        self.assertFalse(result["success"])
        self.assertTrue(result["registration"]["password_effective"])
        self.assertEqual(result["registration"]["credential_persistence"], "saved")
        self.assertEqual(result["error_stage"], "otp")
        self.assertFalse(result["registration"]["authenticated"])
        self.assertEqual(db.get_account_credentials("user@example.com")["account_id"], result["account_id"])
        self.assertTrue(session.closed)

    def test_password_persistence_failure_stops_all_later_authentication(self):
        with self._pipeline({}) as (session, _), \
                patch.object(db, "save_registration_password", side_effect=OSError("disk full")), \
                patch.object(registration, "navigate_email_otp_send") as send, \
                patch.object(registration, "validate_email_otp") as validate, \
                patch.object(registration, "setup_2fa") as enroll:
            result = self._run()
        self.assertEqual(result["error_stage"], "credential_persistence")
        self.assertTrue(result["registration"]["password_effective"])
        self.assertEqual(result["registration"]["credential_persistence"], "failed")
        self.assertFalse(result["registration"]["authenticated"])
        send.assert_not_called()
        validate.assert_not_called()
        enroll.assert_not_called()
        self.assertTrue(session.closed)

    def test_passwordless_uncertain_submission_does_not_save_or_fall_back(self):
        with self._pipeline({}, kind="new_passwordless") as (session, _), \
                patch.object(registration, "register_user", side_effect=TimeoutError("connection lost")), \
                patch.object(registration, "request_login_otp") as fallback:
            result = self._run()
        self.assertFalse(result["success"])
        self.assertEqual(result["registration"]["password_state"], "submission_unknown")
        self.assertFalse(result["registration"]["password_effective"])
        self.assertIsNone(db.get_account_by_email("user@example.com"))
        fallback.assert_not_called()
        self.assertTrue(session.closed)

    def test_existing_probe_result_never_submits_password_or_profile(self):
        probe_state = {
            "kind": "existing", "step": "otp", "page": {"type": "email_otp_verification"},
            "continue_url": "https://auth.openai.com/email-verification",
            "classification_source": "authorize_continue", "otp_delivery_required": False,
        }
        with self._pipeline({
            "continue_url": "https://chatgpt.com/api/auth/callback/openai",
        }, kind="unknown", step="unknown") as (session, _), \
                patch.object(registration, "probe_registration_state", return_value=probe_state) as probe, \
                patch.object(registration, "register_user") as password, \
                patch.object(registration, "create_account") as profile:
            result = self._run()
        self.assertTrue(result["success"])
        probe.assert_called_once_with(
            session, "user@example.com",
            referer="https://auth.openai.com/", screen_hint="signup",
        )
        password.assert_not_called()
        profile.assert_not_called()
        self.assertTrue(session.closed)

    def test_existing_password_login_preserves_original_identity_and_credentials(self):
        original_id = db.insert_account(email="user@example.com", access_token="old-token",
            totp_secret="JBSWY3DPEHPK3PXP", extra={"registration_password": "stored-password", "note": "keep"})
        with self._pipeline({}, kind="existing") as (session, _), \
                patch.object(registration, "verify_login_password", return_value={
                    "continue_url": "https://chatgpt.com/api/auth/callback/openai"}), \
                patch.object(registration, "register_user") as create:
            result = self._run()
        self.assertTrue(result["success"])
        self.assertEqual(result["account_id"], original_id)
        self.assertEqual(result["registration"]["password_state"], "verified")
        row = db.get_account_by_email("user@example.com")
        self.assertEqual(row["access_token"], "test-access")
        self.assertEqual(json.loads(row["extra_json"])["note"], "keep")
        credentials = db.get_account_credentials("user@example.com")
        self.assertEqual(credentials["registration_password"], "stored-password")
        self.assertEqual(credentials["totp_secret"], "JBSWY3DPEHPK3PXP")
        create.assert_not_called()
        self.assertTrue(session.closed)


    def test_existing_password_deactivated_error_skips_otp_and_preserves_account(self):
        original_id = db.insert_account(
            email="user@example.com", access_token="old-token",
            extra={"registration_password": "stored-password"},
        )
        error = registration.AccountUnusableError(
            "account_deactivated", error_code="account_deactivated",
        )
        with self._pipeline({}, kind="existing") as (session, _), \
                patch.object(registration, "verify_login_password", side_effect=error) as verify, \
                patch.object(registration, "request_login_otp") as otp, \
                patch.object(registration, "_finalize_registration_session") as callback, \
                patch.object(registration, "release_email_if_unconsumed") as release:
            result = self._run()

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "account_deactivated")
        self.assertEqual(result["error_stage"], "password")
        verify.assert_called_once_with(session, "stored-password")
        otp.assert_not_called()
        callback.assert_not_called()
        release.assert_not_called()
        self.assertEqual(db.get_account_by_email("user@example.com")["id"], original_id)
        self.assertEqual(db.get_account_by_email("user@example.com")["access_token"], "old-token")

    def test_existing_mfa_deactivated_error_skips_callback_and_preserves_account(self):
        original_id = db.insert_account(
            email="user@example.com", access_token="old-token", totp_secret="secret-placeholder",
            extra={"registration_password": "stored-password"},
        )
        error = registration.AccountUnusableError(
            "account_deactivated", error_code="account_deactivated",
        )
        mfa_state = {
            "page": {"type": "mfa_challenge", "payload": {"factor_id": "factor-placeholder"}},
            "continue_url": "https://auth.openai.com/mfa-challenge/factor-placeholder",
        }
        with self._pipeline({}, kind="existing") as (session, _), \
                patch.object(registration, "verify_login_password", return_value=mfa_state), \
                patch.object(registration, "verify_login_totp", side_effect=error) as verify, \
                patch.object(registration, "request_login_otp") as otp, \
                patch.object(registration, "_finalize_registration_session") as callback, \
                patch.object(registration, "release_email_if_unconsumed") as release:
            result = self._run()

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "account_deactivated")
        self.assertEqual(result["error_stage"], "totp")
        verify.assert_called_once_with(session, mfa_state, "secret-placeholder")
        otp.assert_not_called()
        callback.assert_not_called()
        release.assert_not_called()
        self.assertEqual(db.get_account_by_email("user@example.com")["id"], original_id)
        self.assertEqual(db.get_account_by_email("user@example.com")["access_token"], "old-token")
    def test_existing_otp_can_create_local_record_without_inventing_password(self):
        with self._pipeline({"continue_url": "https://chatgpt.com/api/auth/callback/openai"},
                kind="existing", step="otp") as (session, _), \
                patch.object(registration, "get_registration_state", return_value={
                    "kind": "existing", "step": "otp", "page": {"type": "email_otp"},
                    "continue_url": "https://auth.openai.com/email-verification"}), \
                patch.object(registration, "verify_login_password") as password:
            result = self._run()
        self.assertTrue(result["success"])
        self.assertTrue(result["registration"]["authenticated"])
        self.assertFalse(db.get_account_credentials("user@example.com")["registration_password"])
        password.assert_not_called()
        self.assertTrue(session.closed)

    def test_existing_profile_transition_is_rejected_without_modification(self):
        db.insert_account(email="user@example.com", access_token="old-token",
            extra={"registration_password": "stored-password"})
        with self._pipeline({}, kind="existing") as (session, _), \
                patch.object(registration, "verify_login_password", return_value={
                    "page": {"type": "about_you"}, "continue_url": "/about-you"}), \
                patch.object(registration, "create_account") as create, \
                patch.object(registration, "release_email_if_unconsumed") as release:
            result = self._run()
        self.assertEqual(result["error_code"], "existing_profile_unexpected")
        self.assertEqual(db.get_account_by_email("user@example.com")["access_token"], "old-token")
        create.assert_not_called()
        release.assert_not_called()
        self.assertTrue(session.closed)

    def test_callback_failure_is_not_reported_as_authenticated(self):
        with self._pipeline({"continue_url": "https://chatgpt.com/api/auth/callback/openai"}) as (_, _), \
                patch.object(registration, "_finalize_registration_session", side_effect=TimeoutError("no session")):
            result = self._run()
        self.assertFalse(result["success"])
        self.assertFalse(result["registration"]["authenticated"])
        self.assertEqual(result["registration"]["credential_persistence"], "saved")
        self.assertFalse(db.get_account_credentials("user@example.com")["complete"])

    def test_codex_failure_does_not_affect_saved_registration(self):
        with self._pipeline({"continue_url": "https://chatgpt.com/api/auth/callback/openai"}), \
                patch.object(codex_oauth, "run_codex_oauth", return_value={"ok": False, "status": "failed"}) as codex:
            result = self._run()
        self.assertTrue(result["success"])
        self.assertTrue(result["registration"]["authenticated"])
        self.assertTrue(result["registration"]["account_saved"])
        self.assertEqual(result["codex"]["status"], "not_started")
        codex.assert_not_called()
        self.assertTrue(db.get_account_credentials("user@example.com")["complete"])

    def test_twofa_only_enrolls_when_server_disabled_and_config_enabled(self):
        def enroll_inline(_session, _token, *, on_pending_secret):
            secret = "JBSWY3DPEHPK3PXP"
            on_pending_secret(secret)
            return secret

        for server_state, enabled, expected in (
            (True, True, "failed"), (False, False, "disabled_by_config"),
            (False, True, "enabled"), (None, True, "enabled"),
        ):
            with self.subTest(server_state=server_state, config=enabled), \
                    self._pipeline({"continue_url": "https://chatgpt.com/api/auth/callback/openai"}), \
                    patch.object(registration._twofa_cfg, "ENABLE_2FA", enabled), \
                    patch.object(registration, "get_mfa_enabled", return_value=server_state,
                        side_effect=RuntimeError("unknown state") if server_state is None else None) as mfa_query, \
                    patch.object(registration, "setup_2fa_inline", side_effect=enroll_inline) as enroll:
                result = self._run()
            if server_state is True:
                self.assertFalse(result["success"])
            else:
                self.assertTrue(result["success"])
            self.assertEqual(result["registration"]["twofa_status"], expected)
            if enabled:
                mfa_query.assert_called_once()
            else:
                mfa_query.assert_not_called()
    def test_existing_login_failure_or_stop_does_not_poison_claimed_email(self):
        for outcome in ("failure", "stop", "exception"):
            with self.subTest(outcome=outcome):
                address = f"{outcome}@example.com"
                db.import_outlook_accounts([{"email": address, "password": "mail-password",
                    "client_id": "client", "refresh_token": "refresh"}])
                job = db.create_job(email_source="outlook", email=address)
                db.claim_next_outlook(job_id=job["id"])

                def fail_login(*args, **kwargs):
                    if outcome == "stop":
                        job_context.request_stop(job["id"])
                        raise job_context.StopRequested("stopped during existing login")
                    raise TimeoutError("/log-in/password OTP timeout")

                def raise_after_classification(*args, **kwargs):
                    job_context.record_progress({"account_kind": "existing"})
                    raise RuntimeError("/log-in/password authentication failed")

                with self._pipeline({}, kind="existing", step="otp"), \
                        patch.object(registration, "get_registration_state", return_value={
                            "kind": "existing", "step": "otp", "page": {"type": "email_otp"},
                            "continue_url": "https://auth.openai.com/email-verification"}), \
                        patch.object(registration_service, "_prepare_registration_args",
                            return_value=(address, "Test User", "1995-01-01")), \
                        patch.object(registration, "wait_for_otp", return_value="123456"), \
                        patch.object(registration._email_cfg, "USE_EMAIL_SERVICE", True), \
                        patch.object(registration, "validate_email_otp", side_effect=fail_login), \
                        patch.object(registration, "release_email_if_unconsumed",
                            wraps=email_provider.release_email_if_unconsumed):
                    if outcome == "exception":
                        with patch.object(registration, "run_registration", side_effect=raise_after_classification):
                            registration_service._run_one_job(job["id"], str(self.root / f"{outcome}.log"))
                    else:
                        registration_service._run_one_job(job["id"], str(self.root / f"{outcome}.log"))
                stored_job = db.get_job(job["id"])
                self.assertEqual(stored_job["status"], "stopped" if outcome == "stop" else "failed")
                self.assertEqual(stored_job["registration"]["account_kind"], "existing")
                self.assertEqual(db.get_outlook_by_email(address)["status"], "registering")
                self.assertIsNone(db.get_account_by_email(address))

    def test_passwordless_rejection_can_finish_without_saving_candidate(self):
        rejection = registration.PasswordRegistrationRejectedError("password setting rejected", {
            "page": {"type": "email_otp"}, "continue_url": "/email-verification",
        })
        with self._pipeline({"continue_url": "https://chatgpt.com/api/auth/callback/openai"},
                kind="new_passwordless"), \
                patch.object(registration, "register_user", side_effect=rejection), \
                patch.object(registration, "request_login_otp", return_value=rejection.result):
            result = self._run()
        self.assertTrue(result["success"])
        self.assertEqual(result["registration"]["password_state"], "rejected")
        self.assertFalse(result["registration"]["password_effective"])
        self.assertFalse(db.get_account_credentials("user@example.com")["registration_password"])

    def test_passwordless_success_discards_code_from_previous_challenge(self):
        with self._pipeline({"continue_url": "https://chatgpt.com/api/auth/callback/openai"},
                kind="new_passwordless"), \
                patch.object(registration._email_cfg, "USE_EMAIL_SERVICE", True), \
                patch.object(registration, "wait_for_otp", return_value="678901"), \
                patch.object(registration, "validate_email_otp", return_value={
                    "continue_url": "https://chatgpt.com/api/auth/callback/openai"}) as validate:
            result = self._run()
        self.assertTrue(result["success"])
        self.assertEqual(validate.call_args.args[1], "678901")
        self.assertEqual(db.get_account_credentials("user@example.com")["registration_password"],
            "Test-only-password!")


    def test_registration_uses_inline_totp_and_promotes_persisted_secret(self):
        def bind_inline(session, token, *, on_pending_secret):
            self.assertEqual(session.device_id, "device-for-test")
            self.assertEqual(token, "test-access")
            on_pending_secret("fast-secret")
            return "fast-secret"

        with self._pipeline({"continue_url": "https://chatgpt.com/api/auth/callback/openai"}) as (session, _), \
                patch.object(registration._twofa_cfg, "ENABLE_2FA", True), \
                patch.object(registration, "setup_2fa_inline", side_effect=bind_inline) as fast:
            result = self._run()
        self.assertTrue(result["success"])
        fast.assert_called_once()
        credentials = db.get_account_credentials("user@example.com")
        self.assertEqual(credentials["totp_secret"], "fast-secret")
        self.assertIsNone(credentials["pending_tfa_secret"])
        self.assertTrue(session.closed)

    def test_registration_totp_fallback_uses_independent_session_and_same_proxy(self):
        reauth_session = _Session()
        reauth_session.proxy = "http://proxy.example:8080"

        def bind_slow(session, email, *, on_pending_secret, **_kwargs):
            self.assertIs(session, reauth_session)
            self.assertEqual(email, "user@example.com")
            on_pending_secret("slow-secret")
            return "slow-secret"

        with self._pipeline({"continue_url": "https://chatgpt.com/api/auth/callback/openai"}) as (session, _), \
                patch.object(registration._twofa_cfg, "ENABLE_2FA", True), \
                patch.object(registration, "BrowserSession", side_effect=[session, reauth_session]) as session_factory, \
                patch.object(registration, "setup_2fa_inline", side_effect=RuntimeError("fast path rejected")), \
                patch.object(registration, "setup_2fa", side_effect=bind_slow):
            session.proxy = reauth_session.proxy
            result = self._run()
        self.assertTrue(result["success"])
        self.assertEqual(session_factory.call_args_list[1].kwargs["proxy"], reauth_session.proxy)
        self.assertTrue(reauth_session.closed)
        credentials = db.get_account_credentials("user@example.com")
        self.assertEqual(credentials["totp_secret"], "slow-secret")
        self.assertIsNone(credentials["pending_tfa_secret"])
    def test_pending_secret_persistence_failure_stops_before_slow_path(self):
        session = _Session()
        with patch.object(
            registration, "setup_2fa_inline",
            side_effect=lambda _session, _token, *, on_pending_secret: on_pending_secret("pending-secret"),
        ), patch.object(
            db, "save_pending_totp_secret", side_effect=db.CredentialPersistenceError("disk full"),
        ), patch.object(registration, "setup_2fa") as slow_path, patch.object(
            registration, "BrowserSession",
        ) as session_factory:
            with self.assertRaises(db.CredentialPersistenceError):
                registration._setup_registration_totp(
                    session, "user@example.com", "access-token", "registration-password",
                )
        slow_path.assert_not_called()
        session_factory.assert_not_called()
    def test_activation_failure_recovers_pending_secret_without_reenrolling(self):
        session = _Session()
        with patch.object(
            registration, "setup_2fa_inline",
            side_effect=lambda _session, _token, *, on_pending_secret: (
                on_pending_secret("pending-after-timeout") or (_ for _ in ()).throw(TimeoutError("activation response lost"))
            ),
        ), patch.object(
            registration, "setup_2fa",
            side_effect=lambda _session, _email, *, on_already_enabled, **_kwargs: on_already_enabled(),
        ) as slow_path, patch.object(
            registration, "BrowserSession", return_value=_Session(),
        ):
            secret = registration._setup_registration_totp(
                session, "recovery@example.test", "access-token", "login-password",
            )
        self.assertEqual(secret, "pending-after-timeout")
        slow_path.assert_called_once()
        credentials = db.get_account_credentials("recovery@example.test")
        self.assertEqual(credentials["totp_secret"], "pending-after-timeout")
        self.assertIsNone(credentials["pending_tfa_secret"])
    def test_totp_slow_path_without_openai_password_fails_explicitly(self):
        session = _Session()
        with patch.object(
            registration, "setup_2fa_inline", side_effect=RuntimeError("recent_auth_required"),
        ), patch.object(registration, "setup_2fa") as slow_path, patch.object(
            registration, "BrowserSession",
        ) as session_factory:
            with self.assertRaisesRegex(RuntimeError, "需要已保存的 OpenAI 登录密码"):
                registration._setup_registration_totp(session, "user@example.test", "token", None)
        slow_path.assert_not_called()
        session_factory.assert_not_called()


    def test_rejected_password_is_not_retried_or_overwritten_after_otp_login(self):
        account_id = db.insert_account(email="user@example.com", access_token="old-token",
            extra={"registration_password": "rejected-old-password"})
        rejection = registration.PasswordRejectedError("incorrect password", {
            "page": {"type": "email_otp"}, "continue_url": "/email-verification",
        })
        with self._pipeline({"continue_url": "https://chatgpt.com/api/auth/callback/openai"}, kind="existing"), \
                patch.object(registration, "verify_login_password", side_effect=rejection) as password, \
                patch.object(registration, "request_login_otp", return_value=rejection.result):
            result = self._run()
        self.assertTrue(result["success"])
        self.assertEqual(result["account_id"], account_id)
        self.assertEqual(result["registration"]["password_state"], "rejected")
        self.assertFalse(result["registration"]["password_effective"])
        self.assertEqual(password.call_count, 1)
        self.assertEqual(db.get_account_credentials("user@example.com")["registration_password"],
            "rejected-old-password")

    def test_otp_to_totp_requires_saved_factor_secret_before_final_session(self):
        factor = {"page": {"type": "mfa_challenge", "payload": {"factor_id": "factor-test"}}}
        for has_secret in (False, True):
            with self.subTest(has_secret=has_secret):
                if has_secret:
                    db.insert_account(email="user@example.com", access_token="old-token",
                        totp_secret="JBSWY3DPEHPK3PXP")
                issued = []

                def post(url, **kwargs):
                    payload = json.loads(kwargs["data"])
                    self.assertEqual(payload["id"], "factor-test")
                    if url.endswith("/issue_challenge"):
                        issued.append("challenge")
                        reply = {}
                    else:
                        self.assertEqual(issued, ["challenge"])
                        self.assertEqual(payload["code"], "678901")
                        issued.append("verified")
                        reply = {"continue_url": "https://chatgpt.com/api/auth/callback/openai"}
                    return SimpleNamespace(
                        status_code=200, json=lambda: reply, raise_for_status=lambda: None,
                    )

                sentinel_flows = []

                def sentinel_response(current_session, flow):
                    sentinel_flows.append(flow)
                    return {
                        "token": "sentinel-challenge",
                        "so": {"required": False},
                        "_sentinel_flow": flow,
                        "_sentinel_device_id": current_session.device_id,
                        "_sentinel_header": json.dumps({
                            "p": "sentinel-proof", "c": "sentinel-challenge",
                            "id": current_session.device_id, "flow": flow,
                        }),
                        "_sentinel_so_header": None,
                    }

                with self._pipeline(factor, kind="existing", step="otp") as (session, _), \
                        patch.object(registration, "get_registration_state", return_value={
                            "kind": "existing", "page": {"type": "email_otp_verification"},
                            "continue_url": "https://auth.openai.com/email-verification"}), \
                        patch.object(session, "get_auth_headers", create=True, return_value={}), \
                        patch.object(session, "post", create=True, side_effect=post), \
                        patch("core.openai.auth.openai_auth.request_sentinel_token", side_effect=sentinel_response), \
                        patch("core.openai.auth.openai_auth._totp_code", return_value="678901"):
                    result = self._run()
                self.assertEqual(result["success"], has_secret)
                self.assertEqual(result["registration"]["authenticated"], has_secret)
                self.assertEqual(sentinel_flows, ["verify_mfa"] if has_secret else [])
                self.assertTrue(session.closed)

    def test_write_exception_with_confirmed_readback_does_not_lose_password(self):
        original_save = db.save_registration_password

        def partial_write(*args, **kwargs):
            original_save(*args, **kwargs)
            raise OSError("failure after authoritative commit")

        with self._pipeline({"continue_url": "https://chatgpt.com/api/auth/callback/openai"}), \
                patch.object(db, "save_registration_password", side_effect=partial_write):
            result = self._run()
        self.assertTrue(result["success"])
        self.assertEqual(result["registration"]["credential_persistence"], "saved")
        self.assertEqual(db.get_account_credentials("user@example.com")["registration_password"],
            "Test-only-password!")

    def test_unreadable_partial_write_stops_without_claiming_not_saved(self):
        original_save = db.save_registration_password
        original_read = db.get_account_credentials
        committed = False

        def partial_write(*args, **kwargs):
            nonlocal committed
            original_save(*args, **kwargs)
            committed = True
            raise OSError("failure after commit")

        def read_credentials(email):
            if committed:
                raise OSError("readback unavailable")
            return original_read(email)

        with self._pipeline({}) as (session, _), \
                patch.object(db, "save_registration_password", side_effect=partial_write), \
                patch.object(db, "get_account_credentials", side_effect=read_credentials), \
                patch.object(registration, "navigate_email_otp_send") as send:
            result = self._run()
        self.assertEqual(result["error_stage"], "credential_persistence")
        self.assertTrue(result["registration"]["password_effective"])
        self.assertEqual(result["registration"]["credential_persistence"], "unknown")
        self.assertFalse(db.get_account_credentials("user@example.com")["complete"])
        send.assert_not_called()
        self.assertTrue(session.closed)


if __name__ == "__main__":
    unittest.main()
