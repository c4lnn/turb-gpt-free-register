# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from core.infra import db
from webui import app as web_app


class ArchiveRemovalTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.accounts_path = root / "accounts.json"
        self.codex_dir = root / "codex_accounts"
        self.codex_dir.mkdir()
        self.export_state_path = root / "codex_export_state.json"
        self.accounts_path.write_text(json.dumps([
            {"id": 1, "email": "legacy-archived@example.invalid", "archived": True, "archived_at": "legacy"},
            {"id": 2, "email": "active@example.invalid"},
        ]), encoding="utf-8")
        (self.codex_dir / "codex-legacy-archived@example.invalid.json").write_text(
            json.dumps({"email": "legacy-archived@example.invalid", "access_token": "token"}), encoding="utf-8"
        )
        (self.codex_dir / "codex-active@example.invalid.json").write_text(
            json.dumps({"email": "active@example.invalid", "access_token": "token"}), encoding="utf-8"
        )
        self.export_state_path.write_text(json.dumps({
            "codex-legacy-archived@example.invalid.json": {
                "archived": True,
                "archived_at": "legacy",
                "exported_count": 3,
                "exported_at": "2026-01-02T03:04:05",
            }
        }), encoding="utf-8")
        self.stack = ExitStack()
        for name, value in (
            ("_ACCOUNTS_JSON", self.accounts_path),
            ("_CODEX_DIR", self.codex_dir),
            ("_CODEX_EXPORT_STATE", self.export_state_path),
        ):
            self.stack.enter_context(patch.object(db, name, value))
        self.stack.enter_context(patch.object(db, "_render_static_viewer"))
        for name, value in (
            ("validate_runtime_storage", None),
            ("migrate_email_pool_statuses", {}),
            ("migrate_account_status_contracts", {}),
            ("recover_interrupted_codex_operations", 0),
            ("recover_interrupted_plan_checks", 0),
            ("recover_interrupted_live_checks", 0),
            ("recover_interrupted_totp_setups", 0),
        ):
            self.stack.enter_context(patch.object(web_app.db, name, return_value=value))
        self.client = web_app.create_app(auth_code="test-auth").test_client()
        self.headers = {"X-Auth-Code": "test-auth"}

    def tearDown(self):
        self.stack.close()
        self.tempdir.cleanup()

    def test_account_lists_ignore_legacy_archive_query_and_marker(self):
        accounts = self.client.get("/api/accounts?archived=only&paged=1&page_size=10", headers=self.headers)
        statuses = self.client.get("/api/accounts/plan-check-status?archived=only", headers=self.headers)

        self.assertEqual(accounts.status_code, 200)
        self.assertEqual(statuses.status_code, 200)
        self.assertEqual({row["email"] for row in accounts.get_json()["items"]}, {
            "legacy-archived@example.invalid", "active@example.invalid"
        })
        self.assertTrue(all("archived" not in row and "archived_at" not in row for row in accounts.get_json()["items"]))
        self.assertEqual(statuses.get_json()["total"], 2)

    def test_codex_lists_ignore_legacy_archive_marker_and_keep_export_metadata(self):
        response = self.client.get("/api/codex?archived=only", headers=self.headers)

        self.assertEqual(response.status_code, 200)
        rows = {row["email"]: row for row in response.get_json()["accounts"]}
        self.assertEqual(set(rows), {"legacy-archived@example.invalid", "active@example.invalid"})
        legacy = rows["legacy-archived@example.invalid"]
        self.assertEqual(legacy["exported_count"], 3)
        self.assertEqual(legacy["exported_at"], "2026-01-02T03:04:05")
        self.assertNotIn("archived", legacy)
        self.assertNotIn("archived_at", legacy)
        state = json.loads(self.export_state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["codex-legacy-archived@example.invalid.json"]["exported_count"], 3)

    def test_archive_write_routes_are_not_registered(self):
        for path in (
            "/api/accounts/1/archive",
            "/api/accounts/archive-bulk",
            "/api/codex/archive",
            "/api/codex/archive-bulk",
        ):
            with self.subTest(path=path):
                response = self.client.post(path, json={}, headers=self.headers)
                self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
