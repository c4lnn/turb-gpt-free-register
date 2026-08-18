# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db


class CheckoutSessionDbTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.accounts_path = Path(self.tempdir.name) / "accounts.json"
        self.accounts_path.write_text(json.dumps([{
            "id": 1,
            "email": "checkout@example.invalid",
            "access_token": "at-secret",
            "plan_type": "free",
        }]), encoding="utf-8")
        self.path_patch = patch.object(db, "_ACCOUNTS_JSON", self.accounts_path)
        self.path_patch.start()
        self.render_viewer = db._render_static_viewer
        self.viewer_patch = patch.object(db, "_render_static_viewer")
        self.viewer_patch.start()

    def tearDown(self):
        self.viewer_patch.stop()
        self.path_patch.stop()
        self.tempdir.cleanup()

    def row(self):
        return json.loads(self.accounts_path.read_text(encoding="utf-8"))[0]

    def test_success_then_failure_preserves_last_success(self):
        with patch.object(db, "_now", return_value="2026-08-16T10:00:00"):
            self.assertTrue(db.claim_account_checkout_session(1, trigger="manual"))
            self.assertTrue(db.mark_account_checkout_session_running(1))
            self.assertTrue(db.update_account_checkout_session(1, {
                "ok": True,
                "checked_at": "2026-08-16T10:00:00",
                "http_status": 200,
                "checkout_session_id": "oaics_full_sensitive_id",
                "checkout_session_type": "oaics",
                "attempt_count": 1,
                "max_attempts": 2,
                "network_route": "direct",
            }))
        self.assertEqual(self.row()["checkout_session_type"], "oaics")
        self.assertEqual(self.row()["checkout_session_id"], "oaics_full_sensitive_id")

        with patch.object(db, "_now", return_value="2026-08-16T10:01:00"):
            self.assertTrue(db.claim_account_checkout_session(1))
            self.assertTrue(db.update_account_checkout_session(1, {
                "ok": False,
                "http_status": 401,
                "error_code": "token_revoked",
                "error_message": "token revoked",
                "attempt_count": 1,
            }))
        row = self.row()
        self.assertEqual(row["checkout_check_status"], "failed")
        self.assertEqual(row["checkout_check_http_status"], 401)
        self.assertEqual(row["checkout_session_id"], "oaics_full_sensitive_id")
        self.assertEqual(row["checkout_session_type"], "oaics")

    def test_recovery_does_not_resubmit_or_clear_id(self):
        row = self.row()
        row.update({
            "checkout_check_status": "running",
            "checkout_session_id": "cs_live_sensitive",
            "checkout_session_type": "cs_live",
        })
        self.accounts_path.write_text(json.dumps([row]), encoding="utf-8")
        with patch.object(db, "_now", return_value="2026-08-16T10:02:00"):
            self.assertEqual(db.recover_interrupted_checkout_sessions(), 1)
        recovered = self.row()
        self.assertEqual(recovered["checkout_check_status"], "failed")
        self.assertIn("可能已创建 Session", recovered["checkout_check_error_message"])
        self.assertEqual(recovered["checkout_session_id"], "cs_live_sensitive")

    def test_public_account_and_status_snapshot_do_not_include_full_id(self):
        row = self.row()
        row.update({
            "checkout_check_status": "success",
            "checkout_check_ok": True,
            "checkout_session_id": "cs_live_sensitive",
            "checkout_session_type": "cs_live",
            "checkout_check_updated_at": "2026-08-16T10:03:00",
        })
        self.accounts_path.write_text(json.dumps([row]), encoding="utf-8")
        account = db.get_account(1)
        safe_account = db._decorate_account(self.row(), include_checkout_session_id=False)
        snapshot = db.list_account_plan_check_statuses()
        self.assertIn("checkout_session_id", account)
        self.assertNotIn("checkout_session_id", safe_account)
        self.assertNotIn("checkout_session_id", snapshot["items"][0])
        self.assertEqual(snapshot["items"][0]["checkout_session_type"], "cs_live")
        self.assertNotIn("cs_live_sensitive", json.dumps(snapshot, ensure_ascii=False))

    def test_static_viewer_does_not_embed_checkout_id_or_response_secret(self):
        viewer_path = Path(self.tempdir.name) / "accounts-viewer.html"
        row = {
            "id": 1,
            "email": "checkout@example.invalid",
            "access_token": "at-secret",
            "checkout_session_id": "oaics_full_sensitive_id",
            "checkout_session_type": "oaics",
            "checkout_check_error_message": "proxy-user:proxy-pass@proxy.example client_secret_fixture",
            "checkout_check_result_json": json.dumps({"client_secret": "client_secret_fixture"}),
        }
        with patch.object(db, "_VIEWER_HTML", viewer_path):
            self.render_viewer(outlook_rows=[], account_rows=[row])
        html = viewer_path.read_text(encoding="utf-8")
        self.assertNotIn("oaics_full_sensitive_id", html)
        self.assertNotIn("client_secret_fixture", html)
        self.assertNotIn("proxy-user", html)


if __name__ == "__main__":
    unittest.main()
