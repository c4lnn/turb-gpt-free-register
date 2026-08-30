# -*- coding: utf-8 -*-
import json
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from config import env_loader
from config import proxy as proxy_cfg
from core import chatgpt_plan, live_check_service
from webui import app as web_app
from webui import config_editor


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self.payload = payload
        self.text = json.dumps(payload)
        self.headers = {}

    def json(self):
        return self.payload


class PlanProxyPoolRouteTests(unittest.TestCase):
    def test_pool_ignores_dedicated_proxy_and_masks_selected_pool_proxy(self):
        pool_proxy = "http://pool-user:pool-pass@pool.example:8080"
        with (
            patch.object(proxy_cfg, "PLAN_CHECK_PROXY_MODE", "pool"),
            patch.object(proxy_cfg, "PLAN_CHECK_PROXY", "http://dedicated.example:8080"),
            patch.object(proxy_cfg, "pick_proxy", return_value=pool_proxy) as pick_proxy,
        ):
            route = chatgpt_plan.resolve_plan_check_route(None)

        pick_proxy.assert_called_once_with()
        self.assertEqual(route["proxy"], pool_proxy)
        self.assertEqual(route["proxy_mode"], "pool")
        self.assertEqual(route["network_route"], "proxy")
        self.assertEqual(route["proxy_used"], "http://***:***@pool.example:8080")
        self.assertFalse(route["allow_direct_fallback"])

        public = chatgpt_plan.plan_check_route_metadata(route)
        self.assertNotIn("proxy", public)
        self.assertNotIn("allow_direct_fallback", public)
        self.assertNotIn("pool-pass", str(public))

    def test_pool_rejects_empty_proxy_pool_before_request(self):
        with (
            patch.object(proxy_cfg, "PLAN_CHECK_PROXY_MODE", "pool"),
            patch.object(proxy_cfg, "PLAN_CHECK_PROXY", "http://dedicated.example:8080"),
            patch.object(proxy_cfg, "pick_proxy", return_value=""),
        ):
            with self.assertRaisesRegex(ValueError, "PROXY_POOL 为空"):
                chatgpt_plan.resolve_plan_check_route(None)

    def test_pool_keeps_unavailable_local_pool_proxy_without_direct_fallback(self):
        with (
            patch.object(proxy_cfg, "PLAN_CHECK_PROXY_MODE", "pool"),
            patch.object(proxy_cfg, "PLAN_CHECK_PROXY", "http://dedicated.example:8080"),
            patch.object(proxy_cfg, "pick_proxy", return_value="http://127.0.0.1:65530"),
            patch.object(
                chatgpt_plan,
                "_local_proxy_status",
                return_value=(True, False, "本地代理未监听"),
            ),
        ):
            route = chatgpt_plan.resolve_plan_check_route(None)

        self.assertEqual(route["network_route"], "proxy")
        self.assertEqual(route["proxy"], "http://127.0.0.1:65530")
        self.assertIsNone(route["proxy_fallback_reason"])
        self.assertFalse(route["allow_direct_fallback"])

    def test_request_override_and_existing_modes_remain_compatible(self):
        with (
            patch.object(proxy_cfg, "PLAN_CHECK_PROXY_MODE", "pool"),
            patch.object(proxy_cfg, "pick_proxy") as pick_proxy,
        ):
            request_route = chatgpt_plan.resolve_plan_check_route("http://request.example:9000")
        pick_proxy.assert_not_called()
        self.assertEqual(request_route["proxy_mode"], "request")
        self.assertEqual(request_route["proxy"], "http://request.example:9000")

        with patch.object(proxy_cfg, "PLAN_CHECK_PROXY_MODE", "direct"):
            direct = chatgpt_plan.resolve_plan_check_route(None)
        self.assertEqual(direct["network_route"], "direct")

        with (
            patch.object(proxy_cfg, "PLAN_CHECK_PROXY_MODE", "auto"),
            patch.object(proxy_cfg, "PLAN_CHECK_PROXY", "http://127.0.0.1:65530"),
            patch.object(
                chatgpt_plan,
                "_local_proxy_status",
                return_value=(True, False, "本地代理未监听"),
            ),
        ):
            auto = chatgpt_plan.resolve_plan_check_route(None)
        self.assertEqual(auto["network_route"], "direct_fallback")

        with (
            patch.object(proxy_cfg, "PLAN_CHECK_PROXY_MODE", "proxy"),
            patch.object(proxy_cfg, "PLAN_CHECK_PROXY", "http://dedicated.example:8080"),
            patch.object(chatgpt_plan, "_local_proxy_status", return_value=(False, True, None)),
        ):
            forced = chatgpt_plan.resolve_plan_check_route(None)
        self.assertEqual(forced["network_route"], "proxy")
        self.assertTrue(forced["allow_direct_fallback"])


class PlanProxyPoolConsumerTests(unittest.TestCase):
    def test_plan_retry_reuses_selected_pool_proxy_and_public_metadata(self):
        responses = [
            FakeResponse(503, {"error": "temporary"}),
            FakeResponse(200, {
                "accounts": {
                    "default": {
                        "account": {"account_id": "acc-test", "plan_type": "free"},
                        "entitlement": {},
                    }
                }
            }),
        ]
        created_with = []

        class FakeBrowserSession:
            def __init__(self, proxy=None, detect_exit_geo=True):
                created_with.append(proxy)
                self.device_id = "device-test"
                self.session = self

            def _get_common_headers(self):
                return {"user-agent": "test"}

            def navigator_language(self):
                return "en-US"

            def get(self, *_args, **_kwargs):
                return responses.pop(0)

            def close(self):
                return None

        with (
            patch.object(proxy_cfg, "PLAN_CHECK_PROXY_MODE", "pool"),
            patch.object(proxy_cfg, "PLAN_CHECK_PROXY", "http://dedicated.example:8080"),
            patch.object(proxy_cfg, "pick_proxy", return_value="http://pool.example:8080"),
            patch.object(chatgpt_plan, "BrowserSession", FakeBrowserSession),
        ):
            result = chatgpt_plan.check_account_plan(
                "not-a-jwt",
                max_attempts=2,
                retry_delay=0,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(created_with, ["http://pool.example:8080", "http://pool.example:8080"])
        self.assertEqual(result["proxy_mode"], "pool")
        self.assertEqual(result["network_route"], "proxy")
        self.assertNotIn("allow_direct_fallback", result)

    def test_live_check_pool_403_does_not_fallback_direct(self):
        first = {"ok": False, "status": "failed", "error": "HTTP 403"}
        checker = Mock(return_value=first)
        with (
            patch.object(live_check_service.db, "mark_account_live_check_running", return_value=True),
            patch.object(live_check_service.db, "update_account_liveness", return_value=True),
            patch.object(live_check_service, "_append_log"),
            patch.object(live_check_service, "_QUEUE_SLOTS", Mock()),
            patch.object(
                live_check_service,
                "resolve_plan_check_route",
                return_value={
                    "proxy": "http://pool.example:8080",
                    "proxy_mode": "pool",
                    "network_route": "proxy",
                    "proxy_used": "http://pool.example:8080",
                    "proxy_fallback_reason": None,
                    "allow_direct_fallback": False,
                },
            ),
            patch.object(live_check_service, "check_account_liveness", checker),
        ):
            result = live_check_service._run_live_check(
                account_id=1,
                email="user@example.com",
                proxy=None,
                trigger="manual",
            )

        self.assertEqual(result, first)
        checker.assert_called_once_with(
            "user@example.com",
            proxy="http://pool.example:8080",
            clear_log=False,
        )

    def test_live_check_existing_proxy_fallback_is_preserved(self):
        checker = Mock(side_effect=[
            {"ok": False, "status": "failed", "error": "HTTP 403"},
            {"ok": True, "status": "live"},
        ])
        with (
            patch.object(live_check_service.db, "mark_account_live_check_running", return_value=True),
            patch.object(live_check_service.db, "update_account_liveness", return_value=True),
            patch.object(live_check_service, "_append_log"),
            patch.object(live_check_service, "_QUEUE_SLOTS", Mock()),
            patch.object(
                live_check_service,
                "resolve_plan_check_route",
                return_value={
                    "proxy": "http://dedicated.example:8080",
                    "proxy_mode": "proxy",
                    "network_route": "proxy",
                    "proxy_used": "http://dedicated.example:8080",
                    "proxy_fallback_reason": None,
                    "allow_direct_fallback": True,
                },
            ),
            patch.object(live_check_service, "check_account_liveness", checker),
        ):
            result = live_check_service._run_live_check(
                account_id=1,
                email="user@example.com",
                proxy=None,
                trigger="manual",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(checker.call_args_list[0].kwargs["proxy"], "http://dedicated.example:8080")
        self.assertEqual(checker.call_args_list[1].kwargs["proxy"], "")


class PlanProxyPoolConfigTests(unittest.TestCase):
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
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    def tearDown(self):
        self.recovery_patches.close()

    def test_invalid_mode_is_rejected_before_config_write(self):
        with patch.object(config_editor, "update_config") as update_config:
            response = self.client.post(
                "/api/config",
                json={"updates": {"PLAN_CHECK_PROXY_MODE": "invalid"}},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("auto / proxy / pool / direct", response.get_json()["error"])
        update_config.assert_not_called()

    def test_pool_mode_is_saved_and_hot_reloaded(self):
        result = {
            "updated": ["PLAN_CHECK_PROXY_MODE"],
            "ignored": [],
            "preserved": [],
            "env_updated": ["PLAN_CHECK_PROXY_MODE"],
        }
        with (
            patch.object(config_editor, "update_config", return_value=result) as update_config,
            patch("config.reload_all", return_value=["config.proxy"]) as reload_all,
        ):
            response = self.client.post(
                "/api/config",
                json={"updates": {"PLAN_CHECK_PROXY_MODE": "POOL"}},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["reloaded"])
        update_config.assert_called_once_with({"PLAN_CHECK_PROXY_MODE": "pool"})
        reload_all.assert_called_once_with()

    def test_pool_mode_save_preserves_existing_dedicated_proxy(self):
        secret_proxy = "http://proxy-user:proxy-pass@proxy.example:8080"
        with (
            patch.object(
                env_loader,
                "read_env_file",
                return_value={"PLAN_CHECK_PROXY": secret_proxy},
            ),
            patch.object(env_loader, "write_env_values", return_value=["PLAN_CHECK_PROXY_MODE"]) as write_values,
            patch.object(env_loader, "load_env"),
        ):
            result = config_editor.update_config({
                "PLAN_CHECK_PROXY_MODE": "pool",
                "PLAN_CHECK_PROXY": "",
            })

        write_values.assert_called_once_with({"PLAN_CHECK_PROXY_MODE": "pool"})
        self.assertEqual(result["preserved"], ["PLAN_CHECK_PROXY"])
        self.assertNotIn(secret_proxy, str(result))

    def test_template_exposes_four_mode_choices_and_pool_help(self):
        source = (
            Path(__file__).parents[1] / "webui" / "templates" / "index.html"
        ).read_text(encoding="utf-8")
        self.assertIn("key === 'PLAN_CHECK_PROXY_MODE'", source)
        for value in ("auto", "proxy", "pool", "direct"):
            self.assertIn(f"value:'{value}'", source)
        self.assertIn("强制代理池", source)

        fields = {item["key"]: item for item in config_editor.EDITABLE_FIELDS}
        help_text = fields["PLAN_CHECK_PROXY_MODE"]["help"]
        self.assertIn("仅使用 PROXY_POOL", help_text)
        self.assertIn("忽略专用代理且不直连", help_text)


if __name__ == "__main__":
    unittest.main()
