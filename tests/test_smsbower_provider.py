# -*- coding: utf-8 -*-
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from config import codex as codex_config
from config import env_loader
from core import db
from core import sms_provider
from core.smsbower_provider import SmsBowerProvider, validate_price_range
from webui import config_editor
from webui.app import create_app


class _Resp:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


class _Http:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def get(self, url, params=None):
        self.calls.append({"url": url, "params": dict(params or {})})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


def _cfg(**overrides):
    values = {
        "SMSBOWER_API_BASE": "https://smsbower.page/stubs/handler_api.php",
        "SMSBOWER_API_KEY": "secret-api-key",
        "SMSBOWER_SERVICE": "openai-service",
        "SMSBOWER_COUNTRY": "10",
        "SMSBOWER_MIN_PRICE": "",
        "SMSBOWER_MAX_PRICE": "",
        "SMS_CODE_WAIT": 1,
        "SMS_POLL_INTERVAL": 0,
        "SMS_REQUEST_TIMEOUT": 30,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class SmsBowerConfigTests(unittest.TestCase):
    def test_config_declares_independent_smsbower_fields(self):
        source = Path(codex_config.__file__).read_text(encoding="utf-8")
        for key in (
            "SMSBOWER_API_BASE", "SMSBOWER_API_KEY", "SMSBOWER_SERVICE",
            "SMSBOWER_COUNTRY", "SMSBOWER_MIN_PRICE", "SMSBOWER_MAX_PRICE",
        ):
            self.assertIn(key, source)
        self.assertIn("SMSBOWER_API_KEY", env_loader.SECRET_ENV_KEYS)

        fields = {item["key"]: item for item in config_editor.EDITABLE_FIELDS}
        self.assertTrue(fields["SMSBOWER_API_KEY"]["secret"])
        self.assertEqual(fields["SMSBOWER_MIN_PRICE"]["group"], "接码平台")

    def test_env_overrides_smsbower_fields(self):
        namespace = {
            "SMSBOWER_API_BASE": "https://default.invalid",
            "SMSBOWER_SERVICE": "",
            "SMSBOWER_MIN_PRICE": "",
        }
        with patch.dict(os.environ, {
            "SMSBOWER_API_BASE": "https://smsbower.page/stubs/handler_api.php",
            "SMSBOWER_SERVICE": "svc",
            "SMSBOWER_MIN_PRICE": "0.05",
        }, clear=True):
            env_loader.apply_env_overrides(namespace, {
                "SMSBOWER_API_BASE": "str", "SMSBOWER_SERVICE": "str", "SMSBOWER_MIN_PRICE": "str",
            })
        self.assertEqual(namespace["SMSBOWER_SERVICE"], "svc")
        self.assertEqual(namespace["SMSBOWER_MIN_PRICE"], "0.05")

    def test_price_range_normalizes_optional_decimal_values(self):
        self.assertEqual(validate_price_range("", ""), (None, None))
        self.assertEqual(validate_price_range("0.050", ""), ("0.05", None))
        self.assertEqual(validate_price_range("", "0.1500"), (None, "0.15"))
        self.assertEqual(validate_price_range("0.05", "0.15"), ("0.05", "0.15"))

    def test_price_range_rejects_invalid_values(self):
        for min_price, max_price in (
            ("-0.01", ""), ("abc", ""), ("NaN", ""), ("Infinity", ""), ("0.2", "0.1"),
        ):
            with self.subTest(min_price=min_price, max_price=max_price):
                with self.assertRaises(sms_provider.SmsProviderError):
                    validate_price_range(min_price, max_price)

    def test_secret_config_read_is_masked(self):
        with patch.object(env_loader, "load_env"), patch.object(
            env_loader, "read_env_file", return_value={"SMSBOWER_API_KEY": "secret-api-key"}
        ), patch.dict(os.environ, {}, clear=True):
            fields = config_editor.get_config()

        item = next(row for row in fields if row["key"] == "SMSBOWER_API_KEY")
        self.assertEqual(item["value"], "")
        self.assertTrue(item["configured"])
        self.assertNotIn("secret-api-key", str(fields))

    def test_blank_secret_update_preserves_existing_value(self):
        with patch.object(env_loader, "read_env_file", return_value={"SMSBOWER_API_KEY": "stored-secret"}), patch.object(
            env_loader, "write_env_values"
        ) as write_values, patch.object(env_loader, "load_env"):
            result = config_editor.update_config({"SMSBOWER_API_KEY": ""})
        write_values.assert_not_called()
        self.assertEqual(result["preserved"], ["SMSBOWER_API_KEY"])

    def test_valid_smsbower_config_can_be_formatted_for_env_write(self):
        updates = {
            "SMS_PROVIDER": "smsbower",
            "SMSBOWER_API_BASE": "https://smsbower.page/stubs/handler_api.php",
            "SMSBOWER_API_KEY": "secret-api-key",
            "SMSBOWER_SERVICE": "svc",
            "SMSBOWER_COUNTRY": "10",
            "SMSBOWER_MIN_PRICE": "0.05",
            "SMSBOWER_MAX_PRICE": "0.15",
        }
        with patch.object(env_loader, "read_env_file", return_value={}), patch.object(
            env_loader, "write_env_values", return_value=list(updates)
        ) as write_values, patch.object(env_loader, "load_env"):
            result = config_editor.update_config(updates)
        sent = write_values.call_args.args[0]
        self.assertEqual(sent["SMSBOWER_MIN_PRICE"], "0.05")
        self.assertEqual(sent["SMSBOWER_MAX_PRICE"], "0.15")
        self.assertIn("SMSBOWER_API_KEY", result["updated"])

    def test_webui_rejects_invalid_price_before_write(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            accounts = root / "accounts.json"
            accounts.write_text("[]", encoding="utf-8")
            with patch.object(db, "_ACCOUNTS_JSON", accounts), patch.object(
                db, "_LEGACY_ACCOUNTS_JSON", root / "legacy-accounts.json"
            ), patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"), patch.object(
                db, "_TOKENS_TXT", root / "tokens.txt"
            ), patch.object(db, "_VIEWER_HTML", root / "viewer.html"):
                app = create_app(auth_code="test-auth")
                client = app.test_client()
                with patch.object(config_editor, "update_config") as update_config:
                    response = client.post(
                        "/api/config",
                        json={"updates": {"SMSBOWER_MIN_PRICE": "0.2", "SMSBOWER_MAX_PRICE": "0.1"}},
                        headers={"X-Auth-Code": "test-auth"},
                    )
        self.assertEqual(response.status_code, 400)
        update_config.assert_not_called()
        self.assertIn("不能大于", response.get_json()["error"])

    def test_webui_accepts_valid_smsbower_settings_and_reloads(self):
        updates = {
            "SMS_PROVIDER": "smsbower",
            "SMSBOWER_API_BASE": "https://smsbower.page/stubs/handler_api.php",
            "SMSBOWER_API_KEY": "secret-api-key",
            "SMSBOWER_SERVICE": "svc",
            "SMSBOWER_COUNTRY": "10",
            "SMSBOWER_MIN_PRICE": "0.05",
            "SMSBOWER_MAX_PRICE": "0.15",
        }
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            accounts = root / "accounts.json"
            accounts.write_text("[]", encoding="utf-8")
            with patch.object(db, "_ACCOUNTS_JSON", accounts), patch.object(
                db, "_LEGACY_ACCOUNTS_JSON", root / "legacy-accounts.json"
            ), patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"), patch.object(
                db, "_TOKENS_TXT", root / "tokens.txt"
            ), patch.object(db, "_VIEWER_HTML", root / "viewer.html"):
                app = create_app(auth_code="test-auth")
                client = app.test_client()
                with patch.object(
                    config_editor, "update_config", return_value={"updated": list(updates), "ignored": [], "preserved": [], "env_updated": list(updates)}
                ) as update_config, patch("config.reload_all", return_value=["config.codex"]) as reload_all:
                    response = client.post(
                        "/api/config", json={"updates": updates}, headers={"X-Auth-Code": "test-auth"},
                    )
        self.assertEqual(response.status_code, 200)
        update_config.assert_called_once_with(updates)
        reload_all.assert_called_once()

    def test_frontend_exposes_smsbower_provider_and_separate_section(self):
        template_dir = Path(__file__).parents[1] / "webui" / "templates"
        source = (template_dir / "index.html").read_text(encoding="utf-8")
        self.assertIn("{value:'smsbower', label:'SMSBower'}", source)
        self.assertIn("key.startsWith('SMSBOWER_')", source)
        self.assertIn("'GrizzlySMS', 'SMSBower', 'H 接码'", source)
        self.assertIn("/api/smsbower/metadata", source)
        self.assertIn("SMSBOWER_SERVICE", source)
        self.assertIn("SMSBOWER_COUNTRY", source)
        self.assertIn("刷新选项", source)
        self.assertIn("data-smsbower-search", source)
        self.assertIn("搜索名称、代码或 ID", source)
        self.assertIn("filterSmsbowerSelect", source)

    def test_metadata_endpoint_returns_dropdown_options(self):
        metadata = {
            "services": [{"value": "kt", "label": "KakaoTalk (kt)"}],
            "countries": [{"value": "1003", "label": "百慕大 / Bermuda (1003)"}],
        }
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            accounts = root / "accounts.json"
            accounts.write_text("[]", encoding="utf-8")
            with patch.object(db, "_ACCOUNTS_JSON", accounts), patch.object(
                db, "_LEGACY_ACCOUNTS_JSON", root / "legacy-accounts.json"
            ), patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"), patch.object(
                db, "_TOKENS_TXT", root / "tokens.txt"
            ), patch.object(db, "_VIEWER_HTML", root / "viewer.html"), patch(
                "core.smsbower_provider.SmsBowerProvider.get_metadata", return_value=metadata
            ) as get_metadata:
                client = create_app(auth_code="test-auth").test_client()
                response = client.post(
                    "/api/smsbower/metadata",
                    json={"api_base": "https://smsbower.page/stubs/handler_api.php", "api_key": "secret"},
                    headers={"X-Auth-Code": "test-auth"},
                )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["services"], metadata["services"])
        get_metadata.assert_called_once_with()


class SmsBowerProviderTests(unittest.TestCase):
    def test_metadata_parses_service_and_country_dropdown_options(self):
        http = _Http([
            _Resp('{"status":"success","services":[{"code":"kt","name":"KakaoTalk"}]}'),
            _Resp('[{"id":1003,"chn":"百慕大","eng":"Bermuda"}]'),
        ])
        provider = SmsBowerProvider(config=_cfg(), http_factory=lambda: http)

        metadata = provider.get_metadata()

        self.assertEqual(metadata["services"], [{"value": "kt", "label": "KakaoTalk (kt)"}])
        self.assertEqual(
            metadata["countries"],
            [{"value": "1003", "label": "百慕大 / Bermuda (1003)"}],
        )
        self.assertEqual([call["params"]["action"] for call in http.calls], ["getServicesList", "getCountries"])

    def test_country_metadata_accepts_object_top_level(self):
        http = _Http([_Resp('{"1003":{"chn":"百慕大","eng":"Bermuda"}}')])
        provider = SmsBowerProvider(config=_cfg())

        countries = provider.list_countries(http=http)

        self.assertEqual(countries, [{"value": "1003", "label": "百慕大 / Bermuda (1003)"}])

    def test_acquire_number_uses_independent_config_and_price_range(self):
        http = _Http([_Resp("ACCESS_NUMBER:act-1:+12025550123")])
        provider = SmsBowerProvider(
            config=_cfg(SMSBOWER_MIN_PRICE="0.050", SMSBOWER_MAX_PRICE="0.1500"),
            sleep=lambda _: None,
        )

        activation_id, phone = provider.acquire_number(http=http)

        self.assertEqual((activation_id, phone), ("act-1", "12025550123"))
        self.assertEqual(http.calls[0]["url"], "https://smsbower.page/stubs/handler_api.php")
        self.assertEqual(http.calls[0]["params"]["service"], "openai-service")
        self.assertEqual(http.calls[0]["params"]["country"], "10")
        self.assertEqual(http.calls[0]["params"]["minPrice"], "0.05")
        self.assertEqual(http.calls[0]["params"]["maxPrice"], "0.15")

    def test_acquire_number_logs_service_country_and_price_without_api_key(self):
        key = "top-secret-key"
        provider = SmsBowerProvider(config=_cfg(
            SMSBOWER_API_KEY=key,
            SMSBOWER_SERVICE="openai",
            SMSBOWER_COUNTRY="187",
            SMSBOWER_MIN_PRICE="0.05",
            SMSBOWER_MAX_PRICE="0.15",
        ))
        with self.assertLogs("core.smsbower_provider", level="INFO") as logs:
            with self.assertRaises(sms_provider.SmsNoNumbersError):
                provider.acquire_number(http=_Http([_Resp("NO_NUMBERS")]))

        output = "\n".join(logs.output)
        self.assertIn("service=openai", output)
        self.assertIn("country=187(国家ID/区号)", output)
        self.assertIn("minPrice=0.05", output)
        self.assertIn("maxPrice=0.15", output)
        self.assertNotIn(key, output)

    def test_acquire_number_logs_unlimited_for_empty_price_bounds(self):
        provider = SmsBowerProvider(config=_cfg(SMSBOWER_MIN_PRICE="", SMSBOWER_MAX_PRICE=""))
        with self.assertLogs("core.smsbower_provider", level="INFO") as logs:
            provider.acquire_number(http=_Http([_Resp("ACCESS_NUMBER:act-1:+12025550123")]))

        output = "\n".join(logs.output)
        self.assertIn("minPrice=不限", output)
        self.assertIn("maxPrice=不限", output)

    def test_acquire_number_rejects_missing_config_before_http(self):
        http = _Http([])
        provider = SmsBowerProvider(config=_cfg(SMSBOWER_API_KEY=""))
        with self.assertRaisesRegex(sms_provider.SmsProviderError, "SMSBOWER_API_KEY"):
            provider.acquire_number(http=http)
        self.assertEqual(http.calls, [])

    def test_acquire_number_rejects_invalid_price_before_http(self):
        http = _Http([])
        provider = SmsBowerProvider(config=_cfg(SMSBOWER_MIN_PRICE="2", SMSBOWER_MAX_PRICE="1"))
        with self.assertRaises(sms_provider.SmsProviderError):
            provider.acquire_number(http=http)
        self.assertEqual(http.calls, [])

    def test_wait_for_sms_code_handles_wait_retry_and_success(self):
        http = _Http([
            _Resp("STATUS_WAIT_CODE"),
            _Resp("STATUS_WAIT_RETRY:111111"),
            _Resp("STATUS_OK:222222"),
        ])
        provider = SmsBowerProvider(config=_cfg(), sleep=lambda _: None)
        code = provider.wait_for_sms_code("act-1", http=http, max_wait=1, poll_interval=0)
        self.assertEqual(code, "222222")
        self.assertEqual(len(http.calls), 3)

    def test_existing_activation_can_be_polled_after_price_config_becomes_invalid(self):
        http = _Http([_Resp("STATUS_OK:222222")])
        provider = SmsBowerProvider(config=_cfg(SMSBOWER_MIN_PRICE="2", SMSBOWER_MAX_PRICE="1"))
        self.assertEqual(provider.wait_for_sms_code("act-1", http=http, max_wait=1), "222222")

    def test_wait_for_sms_code_rejects_cancel_and_unknown_status(self):
        for response in ("STATUS_CANCEL", "SOMETHING_NEW"):
            with self.subTest(response=response):
                provider = SmsBowerProvider(config=_cfg(), sleep=lambda _: None)
                with self.assertRaises(sms_provider.SmsProviderError):
                    provider.wait_for_sms_code(
                        "act-1", http=_Http([_Resp(response)]), max_wait=1, poll_interval=0,
                    )

    def test_set_status_accepts_only_documented_states(self):
        http = _Http([_Resp("ACCESS_READY"), _Resp("ACCESS_RETRY_GET"), _Resp("ACCESS_ACTIVATION"), _Resp("ACCESS_CANCEL")])
        provider = SmsBowerProvider(config=_cfg())
        for status in (1, 3, 6, 8):
            provider.set_status("act-1", status, http=http)
        self.assertEqual([call["params"]["status"] for call in http.calls], ["1", "3", "6", "8"])
        with self.assertRaises(sms_provider.SmsProviderError):
            provider.set_status("act-1", 9, http=_Http([]))

    def test_errors_do_not_expose_key_or_full_response(self):
        key = "top-secret-key"
        phone = "12025550123"
        provider = SmsBowerProvider(config=_cfg(SMSBOWER_API_KEY=key))
        with self.assertRaises(sms_provider.SmsProviderError) as ctx:
            provider.acquire_number(http=_Http([_Resp(f"UNKNOWN:{key}:{phone}:654321")]))
        message = str(ctx.exception)
        self.assertNotIn(key, message)
        self.assertNotIn(phone, message)
        self.assertNotIn("654321", message)
        self.assertIn("getNumber", message)

    def test_facade_log_helpers_mask_smsbower_phone_code_and_activation(self):
        with patch.object(codex_config, "SMS_PROVIDER", "smsbower"):
            self.assertNotIn("12025550123", sms_provider.phone_for_log("+12025550123"))
            self.assertEqual(sms_provider.code_for_log("123456"), "***")
            self.assertNotIn("activation-123", sms_provider.activation_for_log("activation-123"))

    def test_error_classification(self):
        cases = {
            "BAD_KEY": sms_provider.SmsProviderError,
            "NO_BALANCE": sms_provider.SmsNoBalanceError,
            "NO_NUMBERS": sms_provider.SmsNoNumbersError,
            "NO_ACTIVATION": sms_provider.SmsProviderError,
            "BAD_COUNTRY": sms_provider.SmsProviderError,
        }
        for response, exc_type in cases.items():
            with self.subTest(response=response):
                provider = SmsBowerProvider(config=_cfg())
                with self.assertRaises(exc_type):
                    provider.acquire_number(http=_Http([_Resp(response)]))

    def test_no_numbers_does_not_relax_price_or_retry(self):
        http = _Http([_Resp("NO_NUMBERS")])
        provider = SmsBowerProvider(config=_cfg(SMSBOWER_MAX_PRICE="0.1"))
        with self.assertRaises(sms_provider.SmsNoNumbersError):
            provider.acquire_number(http=http)
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(http.calls[0]["params"]["maxPrice"], "0.1")

    def test_complete_failure_cleans_tracking_without_leaking_response(self):
        key = "top-secret-key"
        phone = "12025550123"
        provider = SmsBowerProvider(config=_cfg(SMSBOWER_API_KEY=key))
        provider.acquired_at["act-1"] = 1
        with self.assertLogs("core.smsbower_provider", level="WARNING") as logs:
            provider.complete("act-1", http=_Http([_Resp(f"UNKNOWN:{key}:{phone}:123456")]))
        output = "\n".join(logs.output)
        self.assertNotIn(key, output)
        self.assertNotIn(phone, output)
        self.assertNotIn("123456", output)
        self.assertNotIn("act-1", provider.acquired_at)

    def test_polling_honors_stop_request(self):
        provider = SmsBowerProvider(config=_cfg(), sleep=lambda _: None)
        with patch("core.registration_service.check_stop_requested", side_effect=RuntimeError("stop-now")):
            with self.assertRaisesRegex(RuntimeError, "stop-now"):
                provider.wait_for_sms_code("act-1", http=_Http([]), max_wait=1, poll_interval=0)

    def test_cancel_is_immediate_synchronous_and_cleans_tracking(self):
        http = _Http([_Resp("ACCESS_CANCEL")])
        sleeps = []
        provider = SmsBowerProvider(config=_cfg(), sleep=sleeps.append, time_fn=lambda: 1)
        provider.acquired_at["act-1"] = 1

        provider.cancel("act-1", http=http)

        self.assertEqual(len(http.calls), 1)
        self.assertEqual(http.calls[0]["params"]["status"], "8")
        self.assertNotIn("act-1", provider.acquired_at)
        self.assertEqual(sleeps, [])

    def test_early_cancel_error_uses_short_retry_without_120_second_wait(self):
        http = _Http([_Resp("EARLY_CANCEL_DENIED"), _Resp("ACCESS_CANCEL")])
        sleeps = []
        provider = SmsBowerProvider(config=_cfg(), sleep=sleeps.append)

        provider.cancel("act-1", http=http)

        self.assertEqual(len(http.calls), 2)
        self.assertEqual(sleeps, [1])

    def test_cancel_final_failure_is_logged_and_not_reported_as_success(self):
        http = _Http([_Resp("BAD_ACTION"), _Resp("BAD_ACTION")])
        provider = SmsBowerProvider(config=_cfg(), sleep=lambda _: None)
        provider.acquired_at["act-1"] = 0
        with self.assertLogs("core.smsbower_provider", level="WARNING") as logs:
            provider.cancel("act-1", http=http, background=False)
        self.assertEqual(len(http.calls), 2)
        self.assertIn("取消最终失败", "\n".join(logs.output))
        self.assertIn("act-1", provider.acquired_at)

    def test_facade_routes_to_independent_smsbower_provider(self):
        fake = SimpleNamespace(
            acquire_number=lambda **kwargs: ("act-x", "12025550123"),
            wait_for_sms_code=lambda activation_id, **kwargs: "123456",
            set_status=lambda activation_id, status, **kwargs: "ACCESS_READY",
            complete=lambda activation_id, **kwargs: None,
            cancel=lambda activation_id, **kwargs: None,
        )
        with patch.object(codex_config, "SMS_PROVIDER", "smsbower"), patch(
            "core.smsbower_provider.get_provider", return_value=fake
        ):
            self.assertEqual(sms_provider.acquire_number(http=_Http([]))[0], "act-x")
            self.assertEqual(sms_provider.wait_for_sms_code("act-x", http=_Http([])), "123456")
            self.assertEqual(sms_provider.set_status("act-x", 1, http=_Http([])), "ACCESS_READY")
            sms_provider.complete("act-x", http=_Http([]))
            sms_provider.cancel("act-x", http=_Http([]), background=False)

    def test_existing_grizzly_route_does_not_load_smsbower_adapter(self):
        http = _Http([_Resp("ACCESS_NUMBER:legacy-1:12025550123")])
        with patch.object(codex_config, "SMS_PROVIDER", "grizzly"), patch.object(
            codex_config, "SMS_API_KEY", "legacy-key"
        ), patch.object(codex_config, "SMS_API_BASE", "https://legacy.invalid/handler"), patch.object(
            codex_config, "SMS_SERVICE", "dr"
        ), patch.object(codex_config, "SMS_COUNTRY", "187"), patch(
            "core.smsbower_provider.get_provider"
        ) as get_provider:
            activation_id, _phone = sms_provider.acquire_number(http=http)
        self.assertEqual(activation_id, "legacy-1")
        get_provider.assert_not_called()


if __name__ == "__main__":
    unittest.main()
