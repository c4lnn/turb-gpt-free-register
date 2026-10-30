# -*- coding: utf-8 -*-
"""注册编排的资料分流、初始化错误及任务资源边界。"""
import json
import unittest
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import main as registration
from core import codex_oauth, email_provider, flow_trigger


class _Session:
    proxy = ""
    device_id = "device-for-test"
    browser_profile = {}

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class RegistrationPipelineTests(unittest.TestCase):
    @contextmanager
    def _pipeline(self, otp_result):
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

        def save_account(**_kwargs):
            observed["saved"] = True
            return 123

        with ExitStack() as stack:
            for name, value in {
                "_prepare_persisted_email_registration": False,
                "BrowserSession": session,
                "network_preflight": None,
                "human_delay": None,
                "get_csrf_token": "csrf",
                "signin_openai": "https://auth.openai.com/authorize",
                "follow_authorize": "https://auth.openai.com/create-account/password",
                "navigate_create_account_password": None,
                "generate_registration_password": "Test-only-password!",
                "register_user": {"continue_url": "/api/accounts/email-otp/send"},
                "navigate_email_otp_send": None,
                "validate_email_otp": otp_result,
                "_finalize_registration_session": ({"user": {"id": "test-user"}}, "test-access"),
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
        self.assertEqual(observed["flows"], ["username_password_create"])
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
        self.assertEqual(observed["flows"], ["username_password_create", "oauth_create_account"])
        self.assertTrue(session.closed)

    def test_sdk_failure_preserves_its_stage_and_prevents_password_submission(self):
        from core.sentinel_runner import SentinelSDKError
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


if __name__ == "__main__":
    unittest.main()
