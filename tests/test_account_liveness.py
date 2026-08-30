# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from core import account_liveness, db
from webui import app as web_app


class AccountLivenessNetworkTests(unittest.TestCase):
    def _run_preflight(self, proxy):
        created_with = []

        class FakeBrowserSession:
            def __init__(self, proxy=None):
                created_with.append(proxy)
                self.proxy = proxy
                self.device_id = "test-device"

        with (
            patch.object(account_liveness, "BrowserSession", FakeBrowserSession),
            patch.object(account_liveness, "get_providers"),
            patch.object(account_liveness, "get_csrf_token", return_value="test-csrf"),
            patch.object(account_liveness, "signin_openai", return_value="https://auth.openai.com/test"),
        ):
            account_liveness._network_preflight_with_retry(
                "user@example.com", proxy, max_attempts=1,
            )
        return created_with

    def test_preflight_preserves_explicit_direct_connection(self):
        self.assertEqual(self._run_preflight(""), [""])

    def test_preflight_preserves_explicit_proxy(self):
        self.assertEqual(
            self._run_preflight("http://proxy.example:8080"),
            ["http://proxy.example:8080"],
        )


class AccountLivenessDbTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.accounts_path = root / "accounts.json"
        self.accounts_path.write_text(json.dumps([{
            "id": 1,
            "email": "user@example.com",
            "access_token": "old-at",
            "user_id": "old-user",
            "plan_type": "free",
            "codex_status": "success",
            "at_refresh_status": "success",
            "at_refreshed_at": "2026-08-01T00:00:00",
        }]), encoding="utf-8")
        self.patchers = [
            patch.object(db, "_ACCOUNTS_JSON", self.accounts_path),
            patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy-accounts.json"),
            patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"),
            patch.object(db, "_TOKENS_TXT", root / "tokens.txt"),
            patch.object(db, "_VIEWER_HTML", root / "viewer.html"),
        ]
        for item in self.patchers:
            item.start()

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.tempdir.cleanup()

    def _account(self):
        return json.loads(self.accounts_path.read_text(encoding="utf-8"))[0]

    def test_live_result_refreshes_token_and_preserves_historical_fields(self):
        result = {
            "ok": True,
            "status": "live",
            "checked_at": "2026-08-11T12:00:00",
            "access_token": "new-at",
            "session": {
                "user": {"id": "new-user", "name": "New Name"},
                "account": {"planType": "plus"},
                "expires": "2026-09-01T00:00:00.000Z",
            },
            "device_id": "device-new",
            "proxy_used": "http://proxy.example:8080",
        }

        self.assertTrue(db.update_account_liveness(1, result))
        row = self._account()
        self.assertEqual(row["live_check_status"], "live")
        self.assertEqual(row["access_token"], "new-at")
        self.assertEqual(row["user_id"], "new-user")
        self.assertEqual(row["plan_type"], "plus")
        self.assertEqual(row["at_refresh_status"], "success")
        self.assertEqual(row["at_refreshed_at"], "2026-08-01T00:00:00")

    def test_failed_result_preserves_old_token(self):
        self.assertTrue(db.update_account_liveness(1, {
            "ok": False,
            "status": "failed",
            "checked_at": "2026-08-11T12:01:00",
            "error": "network timeout",
        }))
        row = self._account()
        self.assertEqual(row["live_check_status"], "failed")
        self.assertEqual(row["live_check_error"], "network timeout")
        self.assertEqual(row["access_token"], "old-at")
        self.assertEqual(row["codex_status"], "success")

    def test_deactivated_result_preserves_token_and_marks_codex(self):
        self.assertTrue(db.update_account_liveness(1, {
            "ok": False,
            "status": "deactivated",
            "checked_at": "2026-08-11T12:02:00",
            "error": "account_deactivated",
        }))
        row = self._account()
        self.assertEqual(row["live_check_status"], "deactivated")
        self.assertEqual(row["access_token"], "old-at")
        self.assertEqual(row["codex_status"], "deactivated")
        self.assertEqual(row["codex_error"], "account_deactivated")


class AccountLivenessWebUiTests(unittest.TestCase):
    def setUp(self):
        self.recovery_patches = ExitStack()
        for name in (
            "recover_interrupted_plan_checks",
            "recover_interrupted_extract_links",
            "recover_interrupted_live_checks",
        ):
            self.recovery_patches.enter_context(patch.object(web_app.db, name, return_value=0))
        self.client = web_app.create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    def tearDown(self):
        self.recovery_patches.close()

    def test_only_liveness_api_is_registered(self):
        self.assertEqual(self.client.post("/api/accounts/1/refresh-at", json={}).status_code, 404)
        self.assertEqual(self.client.post("/api/accounts/refresh-at-bulk", json={"account_ids": [1]}).status_code, 404)
        self.assertNotEqual(self.client.post("/api/accounts/check-live-bulk", json={}).status_code, 404)

    def test_template_keeps_liveness_and_removes_at_refresh_controls(self):
        source = (Path(__file__).parents[1] / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn("/api/accounts/check-live-bulk", source)
        self.assertIn("查活刷新AT", source)
        self.assertIn("查活日志", source)
        self.assertEqual(source.count('id="btnCheckSelectedLiveV2"'), 1)
        self.assertNotIn("btnCheckSelectedLiveTopV2", source)
        self.assertIn('<span class="acc-v2-btn-group-title">账号状态</span>', source)
        self.assertIn('onclick="checkSelectedLive(null, this)"', source)
        self.assertIn("btn.textContent = '查活中…'", source)
        self.assertIn("btn.disabled = false", source)
        self.assertNotIn("data-at-refresh", source)
        self.assertNotIn("refresh-at-bulk", source)
        self.assertNotIn("批量重新获取 AT", source)


if __name__ == "__main__":
    unittest.main()
