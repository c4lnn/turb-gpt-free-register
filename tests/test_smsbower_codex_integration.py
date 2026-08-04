# -*- coding: utf-8 -*-
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from config import codex as codex_config
from core import browser_use_codex_oauth, codex_oauth, roxy_codex_oauth


class _Http:
    def close(self):
        pass


def _adapter():
    adapter = Mock()
    adapter.acquire_number.return_value = ("act-1", "12025550123")
    adapter.wait_for_sms_code.return_value = "123456"
    adapter.set_status.return_value = "ACCESS_READY"
    return adapter


class SmsBowerCodexIntegrationTests(unittest.TestCase):
    def test_roxy_wait_treats_remaining_add_phone_as_send_failure(self):
        with patch.object(roxy_codex_oauth, "_is_phone_code_state", return_value=False), patch.object(
            roxy_codex_oauth, "_is_phone_code_page", return_value=False
        ), patch.object(roxy_codex_oauth, "_is_add_phone_page", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "send_not_accepted"):
                roxy_codex_oauth._wait_after_phone_send(object(), timeout=0)

    def test_browser_use_wait_treats_add_phone_url_as_still_form(self):
        with patch.object(browser_use_codex_oauth, "_is_add_phone_url", return_value=True), patch.object(
            browser_use_codex_oauth, "_read_phone_input_value", return_value=""
        ):
            state = browser_use_codex_oauth._wait_after_phone_send(object(), timeout=0)

        self.assertEqual(state, "still_form")

    def test_protocol_driver_uses_smsbower_facade_lifecycle(self):
        adapter = _adapter()
        send_resp = SimpleNamespace(status_code=200, text="{}")
        validate_resp = SimpleNamespace(status_code=200, text="{}")
        with patch.object(codex_config, "SMS_PROVIDER", "smsbower"), patch.object(
            codex_config, "SMS_MAX_RETRIES", 1
        ), patch("core.smsbower_provider.get_provider", return_value=adapter), patch.object(
            codex_oauth.sms_provider, "_http", return_value=_Http()
        ), patch.object(codex_oauth, "_post_json", side_effect=[send_resp, validate_resp]), patch.object(
            codex_oauth, "human_delay"
        ):
            codex_oauth._do_phone_verification(object())

        adapter.acquire_number.assert_called_once()
        adapter.set_status.assert_called_once_with("act-1", 1, http=unittest.mock.ANY)
        adapter.wait_for_sms_code.assert_called_once()
        adapter.complete.assert_called_once()
        adapter.cancel.assert_not_called()

    def test_protocol_driver_cancels_smsbower_activation_on_send_failure(self):
        adapter = _adapter()
        send_resp = SimpleNamespace(status_code=400, text='{"error":"invalid phone"}')
        with patch.object(codex_config, "SMS_PROVIDER", "smsbower"), patch.object(
            codex_config, "SMS_MAX_RETRIES", 1
        ), patch("core.smsbower_provider.get_provider", return_value=adapter), patch.object(
            codex_oauth.sms_provider, "_http", return_value=_Http()
        ), patch.object(codex_oauth, "_post_json", return_value=send_resp), patch.object(
            codex_oauth, "human_delay"
        ):
            with self.assertRaisesRegex(RuntimeError, "手机号验证重试"):
                codex_oauth._do_phone_verification(object())
        adapter.cancel.assert_called_once()
        adapter.wait_for_sms_code.assert_not_called()
        adapter.complete.assert_not_called()

    def test_protocol_driver_cancels_before_acquiring_replacement_number(self):
        adapter = _adapter()
        adapter.acquire_number.side_effect = [
            ("act-1", "12025550123"),
            ("act-2", "12025550124"),
        ]
        send_failed = SimpleNamespace(status_code=400, text='{"error":"invalid phone"}')
        send_ok = SimpleNamespace(status_code=200, text="{}")
        validate_ok = SimpleNamespace(status_code=200, text="{}")
        with patch.object(codex_config, "SMS_PROVIDER", "smsbower"), patch.object(
            codex_config, "SMS_MAX_RETRIES", 2
        ), patch("core.smsbower_provider.get_provider", return_value=adapter), patch.object(
            codex_oauth.sms_provider, "_http", return_value=_Http()
        ), patch.object(
            codex_oauth, "_post_json", side_effect=[send_failed, send_ok, validate_ok]
        ), patch.object(codex_oauth, "_sleep_before_phone_retry"):
            codex_oauth._do_phone_verification(object())

        lifecycle = [call[0] for call in adapter.method_calls]
        self.assertEqual(lifecycle[:3], ["acquire_number", "cancel", "acquire_number"])

    def test_roxy_driver_cancels_when_submit_stays_on_add_phone(self):
        adapter = _adapter()
        phone_fill = {
            "e164": "+12025550123", "actualVisible": "+12025550123", "hiddenValue": "",
            "dialCode": "+1", "selectedText": "US", "selectedChanged": False,
        }
        with patch.object(codex_config, "SMS_PROVIDER", "smsbower"), patch.object(
            codex_config, "SMS_MAX_RETRIES", 1
        ), patch("core.smsbower_provider.get_provider", return_value=adapter), patch.object(
            roxy_codex_oauth.sms_provider, "_http", return_value=_Http()
        ), patch.object(roxy_codex_oauth, "_has_strict_add_phone_form", return_value=True), patch.object(
            roxy_codex_oauth, "_is_phone_code_page", return_value=False
        ), patch.object(roxy_codex_oauth, "_ensure_add_phone_input"), patch.object(
            roxy_codex_oauth, "_set_phone_value", return_value=phone_fill
        ), patch.object(roxy_codex_oauth, "_blur_active_input_and_wait"), patch.object(
            roxy_codex_oauth, "_verify_add_phone_value_before_submit", return_value={"visibleValue": "+12025550123", "hiddenValue": ""}
        ), patch.object(roxy_codex_oauth, "_select_sms_channel_or_raise"), patch.object(
            roxy_codex_oauth, "_click_add_phone_continue_button", return_value={"clicked": True}
        ), patch.object(roxy_codex_oauth, "_wait_page_settle_after_submit"), patch.object(
            roxy_codex_oauth, "_wait_after_phone_send", side_effect=RuntimeError("send_not_accepted: add-phone")
        ), patch.object(roxy_codex_oauth, "_find_any", side_effect=RuntimeError("still add-phone")), patch.object(
            roxy_codex_oauth, "_is_add_phone_page", return_value=True
        ):
            with self.assertRaisesRegex(RuntimeError, "Roxy 手机验证重试"):
                roxy_codex_oauth._do_phone_verification_if_present(object())

        adapter.cancel.assert_called_once_with("act-1", http=unittest.mock.ANY, background=True)
        adapter.set_status.assert_not_called()
        adapter.wait_for_sms_code.assert_not_called()

    def test_browser_use_driver_cancels_when_submit_stays_on_add_phone(self):
        adapter = _adapter()
        page = object()
        with patch.object(codex_config, "SMS_PROVIDER", "smsbower"), patch.object(
            codex_config, "SMS_MAX_RETRIES", 1
        ), patch("core.smsbower_provider.get_provider", return_value=adapter), patch.object(
            browser_use_codex_oauth.sms_provider, "_http", return_value=_Http()
        ), patch.object(browser_use_codex_oauth, "_has_phone_prompt", return_value=True), patch.object(
            browser_use_codex_oauth, "_page_url", return_value="https://auth.openai.com/add-phone"
        ), patch.object(browser_use_codex_oauth, "_ensure_add_phone_form", return_value=True), patch.object(
            browser_use_codex_oauth, "_fill_phone", return_value="+12025550123"
        ), patch.object(browser_use_codex_oauth, "_bu_delay"), patch.object(
            browser_use_codex_oauth, "_wait_after_phone_send", return_value="still_form"
        ):
            with self.assertRaisesRegex(RuntimeError, "手机验证失败"):
                browser_use_codex_oauth._do_phone_verification_if_present(page)

        adapter.cancel.assert_called_once_with("act-1", http=unittest.mock.ANY, background=True)
        adapter.set_status.assert_not_called()
        adapter.wait_for_sms_code.assert_not_called()

    def test_roxy_driver_uses_smsbower_facade_lifecycle(self):
        adapter = _adapter()
        phone_fill = {"e164": "+12025550123", "actualVisible": "+12025550123", "hiddenValue": "", "dialCode": "+1", "selectedText": "US", "selectedChanged": False}
        with patch.object(codex_config, "SMS_PROVIDER", "smsbower"), patch.object(
            codex_config, "SMS_MAX_RETRIES", 1
        ), patch("core.smsbower_provider.get_provider", return_value=adapter), patch.object(
            roxy_codex_oauth.sms_provider, "_http", return_value=_Http()
        ), patch.object(roxy_codex_oauth, "_has_strict_add_phone_form", return_value=True), patch.object(
            roxy_codex_oauth, "_is_phone_code_page", return_value=False
        ), patch.object(roxy_codex_oauth, "_ensure_add_phone_input"), patch.object(
            roxy_codex_oauth, "_set_phone_value", return_value=phone_fill
        ), patch.object(roxy_codex_oauth, "_blur_active_input_and_wait"), patch.object(
            roxy_codex_oauth, "_verify_add_phone_value_before_submit", return_value={"visibleValue": "+12025550123", "hiddenValue": ""}
        ), patch.object(roxy_codex_oauth, "_select_sms_channel_or_raise"), patch.object(
            roxy_codex_oauth, "_click_add_phone_continue_button", return_value={"clicked": True}
        ), patch.object(roxy_codex_oauth, "_wait_page_settle_after_submit"), patch.object(
            roxy_codex_oauth, "_wait_after_phone_send"
        ), patch.object(roxy_codex_oauth, "_type_otp"), patch.object(
            roxy_codex_oauth, "human_delay"
        ), patch.object(roxy_codex_oauth, "_click_if_present", return_value=True), patch.object(
            roxy_codex_oauth, "_wait_after_phone_otp_submit", return_value="accepted"
        ):
            roxy_codex_oauth._do_phone_verification_if_present(object())

        adapter.acquire_number.assert_called_once()
        adapter.set_status.assert_called_once()
        adapter.wait_for_sms_code.assert_called_once()
        adapter.complete.assert_called_once()

    def test_browser_use_driver_uses_smsbower_facade_lifecycle(self):
        adapter = _adapter()
        page = object()
        with patch.object(codex_config, "SMS_PROVIDER", "smsbower"), patch.object(
            codex_config, "SMS_MAX_RETRIES", 1
        ), patch("core.smsbower_provider.get_provider", return_value=adapter), patch.object(
            browser_use_codex_oauth.sms_provider, "_http", return_value=_Http()
        ), patch.object(browser_use_codex_oauth, "_has_phone_prompt", return_value=True), patch.object(
            browser_use_codex_oauth, "_page_url", return_value="https://auth.openai.com/add-phone"
        ), patch.object(browser_use_codex_oauth, "_ensure_add_phone_form", return_value=True), patch.object(
            browser_use_codex_oauth, "_fill_phone", return_value="+12025550123"
        ), patch.object(browser_use_codex_oauth, "_bu_delay"), patch.object(
            browser_use_codex_oauth, "_wait_after_phone_send", return_value="code_page"
        ), patch.object(browser_use_codex_oauth, "_clear_otp_inputs"), patch.object(
            browser_use_codex_oauth, "_type_otp"
        ), patch.object(browser_use_codex_oauth, "_click_first_any_frame", return_value=True), patch.object(
            browser_use_codex_oauth, "_wait_after_phone_otp", return_value="accepted"
        ):
            browser_use_codex_oauth._do_phone_verification_if_present(page)

        adapter.acquire_number.assert_called_once()
        adapter.set_status.assert_called_once()
        adapter.wait_for_sms_code.assert_called_once()
        adapter.complete.assert_called_once()


if __name__ == "__main__":
    unittest.main()
