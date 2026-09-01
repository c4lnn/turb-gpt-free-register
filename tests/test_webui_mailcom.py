# -*- coding: utf-8 -*-
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from config import email as email_config
from webui import app as web_app
from webui import config_editor


class MailComWebUITests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        for name in (
            "recover_interrupted_plan_checks",
            "recover_interrupted_checkout_sessions",
            "recover_interrupted_extract_links",
            "recover_interrupted_live_checks",
        ):
            self.stack.enter_context(patch.object(web_app.db, name, return_value=0))
        self.temp = tempfile.TemporaryDirectory()
        self.stack.enter_context(patch.object(web_app.db, "_MAILCOM_EMAIL_JSON", Path(self.temp.name) / "mailcom.json"))
        self.stack.enter_context(patch.object(web_app.db, "_MAILCOM_ALIAS_JSON", Path(self.temp.name) / "mailcom-aliases.json"))
        self.stack.enter_context(patch.dict(os.environ, {"RUNTIME_STORAGE_BACKEND": "json"}))
        self.client = web_app.create_app(auth_code="test-auth").test_client()
        self.headers = {"X-Auth-Code": "test-auth"}

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def test_import_and_list_are_partial_success_and_redacted(self):
        response = self.client.post(
            "/api/mailcom/import",
            json={"text": "one@mail.com----secret-one\ninvalid-line\ntwo@mail.com----secret-two"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["inserted"], 2)
        self.assertIn("errors", response.get_json())
        self.assertNotIn("secret-one", response.get_data(as_text=True))

        listed = self.client.get("/api/mailcom", headers=self.headers)
        self.assertEqual(listed.status_code, 200)
        body = listed.get_json()
        self.assertEqual(len(body["items"]), 2)
        self.assertNotIn("password", body["items"][0])
        self.assertNotIn("mail_access_token", body["items"][0])

    def test_pool_status_and_delete_require_mailcom_source(self):
        self.client.post("/api/mailcom/import", json={"text": "one@mail.com----secret"}, headers=self.headers)
        with patch.object(web_app.db, "release_mailcom_email", return_value=True) as release:
            response = self.client.post(
                "/api/outlook/status",
                json={"email": "one@mail.com", "source": "mailcom", "status": "disabled"},
                headers=self.headers,
            )
        self.assertEqual(response.status_code, 200)
        release.assert_called_once_with("one@mail.com", status="disabled", note=None)

        with patch("core.mailcom_alias_pool_service.delete_alias_now", return_value={"ok": True, "deleted": True}) as delete:
            response = self.client.post(
                "/api/outlook/delete",
                json={"email": "one@mail.com", "source": "mailcom"},
                headers=self.headers,
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"], "mailcom_alias_management_required")
        delete.assert_not_called()

    def test_config_and_job_precheck_distinguish_missing_credentials(self):
        with patch.object(web_app.db, "mailcom_pool_health", return_value={"configured": 0, "has_available_credentials": False}):
            config_response = self.client.post(
                "/api/config",
                json={"updates": {"EMAIL_SOURCE": "mailcom"}},
                headers=self.headers,
            )
        self.assertEqual(config_response.status_code, 400)
        self.assertIn("账号和密码", config_response.get_json()["error"])

        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(email_config, "EMAIL_SOURCE", "mailcom"), patch.object(
            web_app.db, "mailcom_pool_health", return_value={"configured": 0, "has_available_credentials": False}
        ):
            job_response = self.client.post("/api/jobs", json={"count": 1}, headers=self.headers)
        self.assertEqual(job_response.status_code, 400)
        self.assertIn("账号和密码", job_response.get_json()["error"])

    def test_config_allows_mailcom_when_pool_ready_without_returning_secrets(self):
        result = {"updated": ["EMAIL_SOURCE"], "ignored": [], "preserved": [], "env_updated": ["EMAIL_SOURCE"]}
        with (
            patch.object(web_app.db, "mailcom_pool_health", return_value={"configured": 1, "has_available_credentials": True}),
            patch.object(web_app.config_editor, "update_config", return_value=result) as update,
            patch("config.reload_all", return_value=["config.email"]),
        ):
            response = self.client.post(
                "/api/config",
                json={"updates": {"EMAIL_SOURCE": "mailcom"}},
                headers=self.headers,
            )
        self.assertEqual(response.status_code, 200)
        update.assert_called_once_with({"EMAIL_SOURCE": "mailcom"})
        self.assertNotIn("password", response.get_data(as_text=True).lower())

    def test_password_update_clears_existing_at_and_response_stays_redacted(self):
        self.client.post("/api/mailcom/import", json={"text": "one@mail.com----old-password"}, headers=self.headers)
        web_app.db.update_mailcom_auth("one@mail.com", "old-mailbox-at", 2_000_000_000)

        response = self.client.post(
            "/api/mailcom/config",
            json={"email": "one@mail.com", "password": "new-password"},
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("new-password", response.get_data(as_text=True))
        self.assertNotIn("old-mailbox-at", response.get_data(as_text=True))
        internal = web_app.db.get_mailcom_internal_record("one@mail.com")
        self.assertEqual(internal["password"], "new-password")
        self.assertEqual(internal["mail_access_token"], "")

    def test_config_metadata_lists_mailcom_without_credentials(self):
        source_field = next(field for field in config_editor.EDITABLE_FIELDS if field["key"] == "EMAIL_SOURCE")
        self.assertIn("mailcom", [item["value"] for item in source_field["options"]])
        html = (Path(__file__).parents[1] / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn("mail.com 账号池", html)
        self.assertIn("mail.com 母号", html)
        self.assertIn("<th>账号</th>", html)
        self.assertNotIn("任务 / 账号", html)
        self.assertIn("data-mailcom-alias-delete", html)
        self.assertIn("btnDeleteSelectedMailcomAliasesV2", html)
        self.assertIn("deleteSelectedMailcomAliases", html)
        self.assertIn("mailcom-alias-row-check", html)
        self.assertIn("正在串行删除", html)
        self.assertIn("button.textContent = '删除中...'", html)
        self.assertIn("正在删除 mail.com 别名，请稍候", html)
        self.assertIn("该别名已从远端删除", html)
        self.assertIn("showToast('mail.com 别名已删除')", html)
        self.assertNotIn("删除成功后会异步为所属母号补齐别名", html)
        self.assertIn("删除远端别名（不会自动补齐）", html)
        self.assertNotIn("删除远端别名并异步补齐", html)
        self.assertIn("btnOpenMailcomManagementV2", html)
        self.assertIn('id="tab-mailcom"', html)
        self.assertIn("qMailcomDomainV2", html)
        self.assertIn("data-mailcom-domain-toggle", html)
        self.assertIn("loadMailcomDomains", html)
        self.assertIn("btnEnableAllMailcomDomainsV2", html)
        self.assertIn("btnDisableAllMailcomDomainsV2", html)
        self.assertIn("/api/mailcom/domains/bulk-status", html)
        self.assertIn("remote_lifetime_alias_count", html)
        self.assertIn("刷新历史", html)
        self.assertIn("/api/mailcom/parents/${encodeURIComponent(parentId)}/history-refresh", html)

    def test_parent_history_refresh_is_read_only_async_and_list_uses_snapshot(self):
        self.client.post("/api/mailcom/import", json={"text": "one@mail.com----secret"}, headers=self.headers)
        parent = self.client.get("/api/mailcom", headers=self.headers).get_json()["items"][0]
        self.assertEqual(parent["remote_lifetime_alias_count"], None)
        self.assertEqual(parent["remote_lifetime_alias_limit"], 99)
        self.assertEqual(parent["remote_capacity_status"], "unknown")
        with patch(
            "core.mailcom_alias_pool_service.enqueue_parent_history_refresh",
            return_value={"accepted": True, "busy": False, "parent_email": "one@mail.com"},
        ) as enqueue:
            response = self.client.post(
                f"/api/mailcom/parents/{parent['id']}/history-refresh",
                headers=self.headers,
            )
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["accepted"])
        enqueue.assert_called_once_with("one@mail.com")
        # 普通列表读取只返回本地快照，不会隐式调用历史刷新队列。
        with patch("core.mailcom_alias_pool_service.enqueue_parent_history_refresh") as unexpected:
            listed = self.client.get("/api/mailcom", headers=self.headers)
        self.assertEqual(listed.status_code, 200)
        unexpected.assert_not_called()

    def test_alias_api_is_redacted_and_cleanup_switch_requires_json_boolean(self):
        web_app.db.create_mailcom_alias(
            alias_email="alias@example.com",
            parent_email="mother@mail.com",
            local_part="alias",
            domain="example.com",
            job_id=12,
        )

        aliases = self.client.get("/api/mailcom/aliases", headers=self.headers)
        self.assertEqual(aliases.status_code, 200)
        payload = aliases.get_json()
        row = payload["items"][0]
        self.assertEqual(row["alias_email"], "alias@example.com")
        self.assertEqual(payload["summary"]["remote_active_alias_limit"], 9)
        self.assertEqual(row["parent_email"], "mother@mail.com")
        self.assertEqual(row["plan_category_code"], "unknown")
        self.assertIn("can_cleanup", row["cleanup_capabilities"])
        self.assertNotIn("password", aliases.get_data(as_text=True).lower())

        bad = self.client.post(
            "/api/config",
            json={"updates": {"MAILCOM_DELETE_ALIAS_IF_NO_TRIAL": "yes"}},
            headers=self.headers,
        )
        self.assertEqual(bad.status_code, 400)
        self.assertIn("JSON 布尔值", bad.get_json()["error"])

        result = {"updated": ["MAILCOM_DELETE_ALIAS_IF_NO_TRIAL"], "ignored": [], "preserved": [], "env_updated": ["MAILCOM_DELETE_ALIAS_IF_NO_TRIAL"]}
        with (
            patch.object(web_app.config_editor, "update_config", return_value=result) as update,
            patch("config.reload_all", return_value=["config.email"]),
        ):
            good = self.client.post(
                "/api/config",
                json={"updates": {"MAILCOM_DELETE_ALIAS_IF_NO_TRIAL": True}},
                headers=self.headers,
            )
        self.assertEqual(good.status_code, 200)
        update.assert_called_once_with({"MAILCOM_DELETE_ALIAS_IF_NO_TRIAL": True})

    def test_compact_account_exposes_unknown_trial_state_without_coercing_it(self):
        row = web_app._compact_account_for_list({
            "id": 7,
            "email": "alias@example.com",
            "plan_type": "free",
            "current_plan_type": "free",
            "plan_check_status": "success",
            "plus_trial_eligible": None,
            "trial_eligibility_known": False,
        })
        self.assertIn("plus_trial_eligible", row)
        self.assertIsNone(row["plus_trial_eligible"])
        self.assertIs(row["trial_eligibility_known"], False)

    def test_config_editor_rejects_non_boolean_alias_cleanup_value(self):
        with self.assertRaisesRegex(ValueError, "JSON 布尔值"):
            config_editor.update_config({"MAILCOM_DELETE_ALIAS_IF_NO_TRIAL": "yes"})


if __name__ == "__main__":
    unittest.main()
