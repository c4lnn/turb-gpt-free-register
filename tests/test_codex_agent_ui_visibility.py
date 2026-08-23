import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from webui import app as web_app


TEMPLATE_PATH = Path(__file__).parents[1] / "webui" / "templates" / "index.html"
AGENT_CONFIG_KEYS = (
    "SUB2API_AUTO_EXPORT",
    "SUB2API_SYNC_MODE",
    "SUB2API_OUTPUT_PATH",
    "SUB2API_PROXY_KEY",
)
SHARED_CONFIG_KEYS = (
    "SUB2API_API_BASE",
    "SUB2API_API_KEY",
    "SUB2API_API_TIMEOUT",
)


class CodexAgentUiVisibilityTests(unittest.TestCase):
    def test_account_page_does_not_expose_agent_controls(self):
        source = TEMPLATE_PATH.read_text(encoding="utf-8")

        for marker in (
            "btnGenerateSelectedAgentV2",
            "btnDownloadSelectedAgentV2",
            "btnUploadSelectedAgentSub2V2",
            'class="col-agent"',
            "function _codexAgentCell",
            "data-codex-agent",
            "codex_agent",
        ):
            self.assertNotIn(marker, source)

    def test_config_filter_hides_agent_only_fields_and_keeps_shared_fields(self):
        source = TEMPLATE_PATH.read_text(encoding="utf-8")

        self.assertIn("HIDDEN_CODEX_AGENT_CONFIG_KEYS", source)
        for key in AGENT_CONFIG_KEYS:
            self.assertIn(f"'{key}'", source)
        for key in SHARED_CONFIG_KEYS:
            self.assertIn(f"'{key}'", source)
        self.assertNotIn("sub2api Agent Token', 'Codex Agent Token", source)

    def test_existing_agent_data_remains_available_to_backend_boundary(self):
        auth_json = '{"auth_mode":"agent_identity"}'
        row = {
            "id": 1,
            "email": "user@example.com",
            "codex_agent_status": "success",
            "codex_agent_token": auth_json,
            "codex_agent_runtime_id": "runtime-test",
        }

        compact = web_app._compact_account_for_list(row)

        self.assertEqual(compact["codex_agent_status"], "success")
        self.assertNotIn("codex_agent_token", compact)
        self.assertEqual(web_app._account_secret_value(row, "codex_agent_token"), auth_json)

    def test_agent_api_routes_remain_registered(self):
        recovery_patches = ExitStack()
        for name in (
            "recover_interrupted_plan_checks",
            "recover_interrupted_checkout_sessions",
            "recover_interrupted_extract_links",
            "recover_interrupted_live_checks",
            "recover_interrupted_codex_agents",
        ):
            recovery_patches.enter_context(patch.object(web_app.db, name, return_value=0))
        recovery_patches.enter_context(
            patch.object(
                web_app.db,
                "recover_interrupted_mailcom_state",
                return_value={"sync": 0, "lease": 0},
            )
        )
        try:
            app = web_app.create_app(auth_code="test-auth")
        finally:
            recovery_patches.close()

        routes = {rule.rule for rule in app.url_map.iter_rules()}
        self.assertIn("/api/accounts/codex-agent", routes)
        self.assertIn("/api/accounts/codex-agent-bulk", routes)
        self.assertIn("/api/accounts/<int:acc_id>/codex-agent/upload-sub2", routes)
        self.assertIn("/api/accounts/<int:acc_id>/codex-agent/download", routes)
        self.assertIn("/api/accounts/codex-agent/download-bulk", routes)


if __name__ == "__main__":
    unittest.main()
