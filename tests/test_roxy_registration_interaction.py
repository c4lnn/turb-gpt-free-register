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
    def test_roxy_input_uses_native_keys_character_by_character(self):
        driver = FakeDriver()
        element = FakeElement()

        with patch.object(registration, "_native_click") as native_click, \
             patch.object(registration, "_typing_pause"):
            registration._set_element_value(driver, element, "ab")

        native_click.assert_called_once_with(driver, element)
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
        self.assertEqual(driver.scripts, [])

    def test_cloak_input_keeps_dom_setter_compatibility(self):
        driver = FakeDriver()
        element = CloakElement()

        with patch.object(registration, "_type_element") as native_type:
            registration._set_element_value(driver, element, "value")

        native_type.assert_not_called()
        self.assertEqual(len(driver.scripts), 1)
        self.assertEqual(driver.scripts[0][1], (element, "value"))

    def test_email_submit_uses_native_click_after_safe_dom_selection(self):
        target = FakeElement()
        driver = FakeDriver({"ok": True, "reason": "native_primary_submit", "target": target})

        with patch.object(registration, "_is_oauth_consent_like", return_value=False), \
             patch.object(registration, "_native_click") as native_click, \
             patch.object(registration, "_assert_not_external_idp"), \
             patch.object(registration.time, "sleep"):
            self.assertTrue(registration._submit_nearest_form_for_active_input(driver))

        native_click.assert_called_once_with(driver, target)


if __name__ == "__main__":
    unittest.main()
