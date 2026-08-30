# -*- coding: utf-8 -*-
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from core import email_provider
from webui import app as web_app
from webui import config_editor


class EmailSourceConfigTests(unittest.TestCase):
    def test_options_match_runtime_email_source_contract(self):
        field = next(item for item in config_editor.EDITABLE_FIELDS if item["key"] == "EMAIL_SOURCE")

        self.assertEqual(
            [item["value"] for item in field["options"]],
            list(email_provider.VALID_EMAIL_SOURCES),
        )
        self.assertTrue(all(item["label"] for item in field["options"]))

    def test_normalizes_valid_ordered_sources_to_legacy_string_format(self):
        self.assertEqual(
            config_editor.normalize_email_source_value("outlook;generic_api|mailnest"),
            "outlook,generic_api,mailnest",
        )
        self.assertEqual(
            config_editor.normalize_email_source_value(["cloudflare", "gptmail", "mailnest"]),
            "cloudflare,gptmail,mailnest",
        )

    def test_rejects_empty_unknown_and_duplicate_sources(self):
        cases = (
            ("", "至少需要选择一个"),
            ("outlook,", "空邮箱来源"),
            ("outlook,legacy_mail", "未知邮箱来源"),
            ("outlook,outlook", "重复邮箱来源"),
        )
        for value, message in cases:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, message):
                    config_editor.normalize_email_source_value(value)

    def test_get_config_exposes_options_and_preserves_unknown_history_value(self):
        with (
            patch("config.env_loader.load_env"),
            patch(
                "config.env_loader.read_env_file",
                return_value={"EMAIL_SOURCE": "outlook,legacy_mail"},
            ),
        ):
            fields = config_editor.get_config()

        field = next(item for item in fields if item["key"] == "EMAIL_SOURCE")
        self.assertEqual(field["value"], "outlook,legacy_mail")
        self.assertEqual(
            [item["value"] for item in field["options"]],
            list(email_provider.VALID_EMAIL_SOURCES),
        )

    def test_update_config_serializes_valid_sources_without_writing_invalid_value(self):
        with (
            patch("config.env_loader.read_env_file", return_value={}),
            patch("config.env_loader.write_env_values", return_value=["EMAIL_SOURCE"]) as write_values,
            patch("config.env_loader.load_env"),
        ):
            result = config_editor.update_config({"EMAIL_SOURCE": ["cloudflare", "gptmail"]})

        write_values.assert_called_once_with({"EMAIL_SOURCE": "cloudflare,gptmail"})
        self.assertEqual(result["updated"], ["EMAIL_SOURCE"])


class EmailSourceConfigApiTests(unittest.TestCase):
    def setUp(self):
        self.recovery_patches = ExitStack()
        for name in (
            "recover_interrupted_plan_checks",
            "recover_interrupted_checkout_sessions",
            "recover_interrupted_extract_links",
            "recover_interrupted_live_checks",
        ):
            self.recovery_patches.enter_context(patch.object(web_app.db, name, return_value=0))
        self.client = web_app.create_app(auth_code="test-auth").test_client()
        self.headers = {"X-Auth-Code": "test-auth"}

    def tearDown(self):
        self.recovery_patches.close()

    def test_api_rejects_invalid_sources_before_config_write(self):
        cases = (
            ("", "至少需要选择一个"),
            ("outlook,legacy_mail", "未知邮箱来源"),
            ("outlook,outlook", "重复邮箱来源"),
        )
        for value, message in cases:
            with self.subTest(value=value), patch.object(config_editor, "update_config") as update_config:
                response = self.client.post(
                    "/api/config",
                    json={"updates": {"EMAIL_SOURCE": value}},
                    headers=self.headers,
                )

            self.assertEqual(response.status_code, 400)
            self.assertIn(message, response.get_json()["error"])
            update_config.assert_not_called()

    def test_api_normalizes_ordered_source_list_before_write(self):
        result = {
            "updated": ["EMAIL_SOURCE"],
            "ignored": [],
            "preserved": [],
            "env_updated": ["EMAIL_SOURCE"],
        }
        with (
            patch.object(config_editor, "update_config", return_value=result) as update_config,
            patch("config.reload_all", return_value=["config.email"]) as reload_all,
        ):
            response = self.client.post(
                "/api/config",
                json={"updates": {"EMAIL_SOURCE": ["cloudflare", "gptmail", "mailnest"]}},
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200)
        update_config.assert_called_once_with({"EMAIL_SOURCE": "cloudflare,gptmail,mailnest"})
        reload_all.assert_called_once_with()


class EmailSourceTemplateTests(unittest.TestCase):
    def test_template_exposes_ordered_multi_select_controls_and_client_validation(self):
        source = (Path(__file__).parents[1] / "webui" / "templates" / "index.html").read_text(encoding="utf-8")

        for token in (
            "renderEmailSourceField",
            "data-email-source-picker-toggle",
            "data-email-source-option",
            "data-email-source-move",
            "data-email-source-remove-index",
            "emailSourceValidationMessage",
        ):
            self.assertIn(token, source)

        field = next(item for item in config_editor.EDITABLE_FIELDS if item["key"] == "EMAIL_SOURCE")
        self.assertIn("第一个为主来源，后续来源按顺序兜底", field["help"])


if __name__ == "__main__":
    unittest.main()
