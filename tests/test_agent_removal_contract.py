# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from config import proxy as proxy_cfg
from config import sub2api as sub2api_cfg
from core import chatgpt_plan, codex_oauth, db
from webui import app as web_app
from webui import config_editor


REMOVED_MARKERS = (
    "codex_agent_",
    "agent_identity",
    "agent_runtime_id",
    "agent_private_key",
)


class RemovedAgentContractTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.accounts_path = root / "accounts.json"
        self.viewer_path = root / "accounts-viewer.html"
        self.legacy_row = {
            "id": 1,
            "email": "legacy@example.invalid",
            "access_token": "oauth-access-token",
            "plan_type": "free",
            "codex_status": "success",
            "codex_agent_status": "success",
            "codex_agent_token": json.dumps({
                "auth_mode": "agent_identity",
                "agent_identity": {
                    "agent_runtime_id": "legacy-runtime",
                    "agent_private_key": "legacy-private-key",
                },
            }),
        }
        self.accounts_path.write_text(json.dumps([self.legacy_row]), encoding="utf-8")

        self.stack = ExitStack()
        self.stack.enter_context(patch.object(db, "_ACCOUNTS_JSON", self.accounts_path))
        self.stack.enter_context(patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"))
        self.stack.enter_context(patch.object(db, "_VIEWER_HTML", self.viewer_path))
        self.stack.enter_context(patch.object(db, "validate_runtime_storage"))
        self.stack.enter_context(patch.object(db, "migrate_email_pool_statuses", return_value={"rows_normalized": 0}))
        for name in (
            "recover_interrupted_plan_checks",
            "recover_interrupted_checkout_sessions",
            "recover_interrupted_extract_links",
            "recover_interrupted_live_checks",
            "recover_interrupted_mailcom_state",
        ):
            result = {"sync": 0, "lease": 0} if name.endswith("mailcom_state") else 0
            self.stack.enter_context(patch.object(db, name, return_value=result))
        self.client = web_app.create_app(auth_code="test-auth").test_client()
        self.headers = {"X-Auth-Code": "test-auth"}

    def tearDown(self):
        self.stack.close()
        self.tempdir.cleanup()

    def assert_no_removed_markers(self, value):
        encoded = json.dumps(value, ensure_ascii=False, default=str).lower()
        for marker in REMOVED_MARKERS:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, encoded)

    def test_legacy_fields_are_filtered_from_all_account_boundaries(self):
        ordinary = db.list_accounts(limit=10)
        paged = db.list_accounts_page(limit=10)
        status_snapshot = db.list_account_plan_check_statuses(limit=10)
        single = db.get_account(1)

        for value in (ordinary, paged, status_snapshot, single):
            self.assert_no_removed_markers(value)
        self.assertEqual(single["access_token"], "oauth-access-token")

        ordinary_response = self.client.get("/api/accounts?limit=10", headers=self.headers)
        paged_response = self.client.get(
            "/api/accounts?paged=1&page=1&page_size=10",
            headers=self.headers,
        )
        status_response = self.client.get(
            "/api/accounts/plan-check-status?limit=10",
            headers=self.headers,
        )
        for response in (ordinary_response, paged_response, status_response):
            self.assertEqual(response.status_code, 200)
            self.assert_no_removed_markers(response.get_json())

        db._render_static_viewer(outlook_rows=[], account_rows=[self.legacy_row])
        self.assert_no_removed_markers(self.viewer_path.read_text(encoding="utf-8"))

    def test_removed_routes_and_secret_fields_are_rejected(self):
        secret = self.client.get(
            "/api/accounts/1/secret?field=codex_agent_token",
            headers=self.headers,
        )
        self.assertEqual(secret.status_code, 400)
        self.assertIn("field 仅支持", secret.get_json()["error"])

        bulk_secret = self.client.post(
            "/api/accounts/secret-bulk",
            json={"account_ids": [1], "field": "codex_agent_token"},
            headers=self.headers,
        )
        self.assertEqual(bulk_secret.status_code, 400)

        old_routes = (
            ("post", "/api/accounts/codex-agent", {}),
            ("post", "/api/accounts/codex-agent-bulk", {}),
            ("post", "/api/accounts/1/codex-agent/upload-sub2", {}),
            ("post", "/api/accounts/codex-agent/upload-sub2-bulk", {}),
            ("get", "/api/accounts/1/codex-agent/download", None),
            ("post", "/api/accounts/codex-agent/download-bulk", {}),
        )
        for method, path, payload in old_routes:
            with self.subTest(path=path):
                response = getattr(self.client, method)(path, json=payload, headers=self.headers)
                self.assertEqual(response.status_code, 404)

        routes = {rule.rule for rule in self.client.application.url_map.iter_rules()}
        self.assertFalse(any("codex-agent" in route for route in routes))

    def test_oauth_and_plan_configuration_boundaries_remain_usable(self):
        fields = {item["key"]: item for item in config_editor.EDITABLE_FIELDS}
        removed_keys = {
            "SUB2API_AUTO_EXPORT",
            "SUB2API_SYNC_MODE",
            "SUB2API_API_URL",
            "SUB2API_OUTPUT_PATH",
            "SUB2API_PROXY_KEY",
        }
        for key in removed_keys:
            self.assertNotIn(key, fields)
            self.assertFalse(hasattr(sub2api_cfg, key))

        shared_sub2_fields = {item["key"]: item for item in config_editor.EDITABLE_FIELDS}
        for key in (
            "SUB2API_API_BASE",
            "SUB2API_API_KEY",
            "SUB2API_API_TOKEN",
            "SUB2API_API_AUTH_HEADER",
            "SUB2API_API_AUTH_PREFIX",
            "SUB2API_API_TIMEOUT",
            "SUB2_CODEX_API_BASE",
            "SUB2_CODEX_API_TOKEN",
            "SUB2_CODEX_AUTH_HEADER",
            "SUB2_CODEX_AUTH_PREFIX",
        ):
            self.assertTrue(hasattr(sub2api_cfg, key))
        for key in (
            "SUB2API_API_BASE",
            "SUB2API_API_KEY",
            "SUB2API_API_TIMEOUT",
        ):
            self.assertIn(key, shared_sub2_fields)

        for key in (
            "PLAN_CHECK_PROXY_MODE",
            "PLAN_CHECK_PROXY",
            "PLAN_CHECK_TIMEOUT",
            "PLAN_CHECK_MAX_ATTEMPTS",
            "PLAN_CHECK_RETRY_DELAY",
            "PLAN_CHECK_WORKERS",
            "PLAN_CHECK_QUEUE_LIMIT",
            "PLAN_CHECK_MIN_INTERVAL",
            "PLAN_CHECK_JITTER",
        ):
            self.assertIn(key, fields)

        for source in ("local", "cpa", "sub2"):
            with patch.object(codex_oauth._cfg, "CODEX_AUTH_URL_SOURCE", source):
                self.assertEqual(codex_oauth._codex_auth_url_source(), source)

        with patch.object(codex_oauth._cfg, "CPA_MANAGEMENT_URL", "https://cpa.example.invalid/admin/oauth"):
            self.assertEqual(codex_oauth._cpa_management_origin(), "https://cpa.example.invalid")

        with (
            patch.object(sub2api_cfg, "SUB2API_API_BASE", "https://sub2.example.invalid"),
            patch.object(sub2api_cfg, "SUB2_CODEX_API_TOKEN", ""),
            patch.object(sub2api_cfg, "SUB2API_API_KEY", "shared-key"),
            patch.object(sub2api_cfg, "SUB2API_API_AUTH_HEADER", "x-api-key"),
        ):
            self.assertEqual(codex_oauth._sub2_codex_base(), "https://sub2.example.invalid")
            self.assertEqual(codex_oauth._sub2_codex_headers()["x-api-key"], "shared-key")

        with patch.object(proxy_cfg, "PLAN_CHECK_PROXY_MODE", "direct"):
            route = chatgpt_plan.resolve_plan_check_route(None)
        self.assertEqual(route["network_route"], "direct")


if __name__ == "__main__":
    unittest.main()
