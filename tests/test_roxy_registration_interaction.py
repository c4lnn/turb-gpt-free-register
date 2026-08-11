# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from selenium.webdriver.common.keys import Keys

from core import roxy_registration as registration


class FakeElement:
    tag_name = "input"

    def __init__(self):
        self.keys = []
        self.clicked = 0

    def send_keys(self, *keys):
        self.keys.append(keys)

    def click(self):
        self.clicked += 1

    def is_displayed(self):
        return True

    def is_enabled(self):
        return True


class CloakElement(FakeElement):
    pass


class FakeDriver:
    def __init__(self, script_result=None):
        self.script_result = script_result
        self.scripts = []

    def execute_script(self, script, *args):
        self.scripts.append((script, args))
        return self.script_result


class RoxyRegistrationInteractionTests(unittest.TestCase):
    def test_roxy_input_uses_human_keys_character_by_character_and_blurs(self):
        driver = FakeDriver()
        element = FakeElement()

        with patch.object(registration, "_human_click") as human_click, \
             patch.object(registration, "_human_scroll_to"), \
             patch.object(registration, "_browser_actions_enabled", return_value=True), \
             patch.object(registration.random, "random", return_value=1.0), \
             patch.object(registration, "human_delay"):
            registration._set_element_value(driver, element, "ab")

        human_click.assert_called_once_with(driver, element, label="input_focus")
        self.assertEqual(
            element.keys,
            [
                (Keys.CONTROL, "a"),
                (Keys.BACKSPACE,),
                ("a",),
                ("b",),
                (Keys.TAB,),
            ],
        )
        self.assertEqual(len(driver.scripts), 1)
        self.assertIn("new Event('input'", driver.scripts[0][0])

    def test_cloak_input_keeps_dom_setter_compatibility(self):
        driver = FakeDriver()
        element = CloakElement()

        with patch.object(registration, "_human_type_text") as human_type:
            registration._set_element_value(driver, element, "value")

        human_type.assert_not_called()
        self.assertEqual(len(driver.scripts), 1)
        self.assertEqual(driver.scripts[0][1], (element, "value"))

    def test_email_submit_uses_human_click_after_safe_dom_selection(self):
        target = FakeElement()
        driver = FakeDriver({"ok": True, "reason": "primary_submit", "target": target})

        with patch.object(registration, "_is_oauth_consent_like", return_value=False), \
             patch.object(registration, "_human_click") as human_click, \
             patch.object(registration, "_assert_not_external_idp"), \
             patch.object(registration.time, "sleep"):
            self.assertTrue(registration._submit_nearest_form_for_active_input(driver))

        human_click.assert_called_once_with(driver, target, label="email_submit")

    def test_otp_submit_timeout_without_error_is_accepted(self):
        driver = FakeDriver()
        with patch.object(registration.time, "time", side_effect=[0, 31]), \
             patch.object(registration, "_is_email_verification_page", return_value=True), \
             patch.object(registration.logger, "warning"):
            result = registration._wait_after_email_otp_submit(driver, timeout=30)

        self.assertEqual(result, "accepted")

    def test_otp_submit_timeout_with_error_is_invalid(self):
        driver = FakeDriver()
        with patch.object(registration.time, "time", side_effect=[0, 0, 31]), \
             patch.object(registration.time, "sleep"), \
             patch.object(registration, "_is_email_verification_page", return_value=True), \
             patch.object(registration, "_email_otp_page_state", return_value={"errors": ["invalid"], "inputs": []}):
            result = registration._wait_after_email_otp_submit(driver, timeout=30)

        self.assertEqual(result, "invalid")


if __name__ == "__main__":
    unittest.main()
