# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db


class AccountFilterTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.accounts_path = Path(self.tempdir.name) / "accounts.json"
        self.accounts_path.write_text(json.dumps([
            {"id": 1, "email": "one@example.invalid", "codex_status": "success"},
            {"id": 2, "email": "two@example.invalid", "codex_status": "failed"},
            {"id": 3, "email": "three@example.invalid", "codex_status": "success"},
        ]), encoding="utf-8")
        self.path_patch = patch.object(db, "_ACCOUNTS_JSON", self.accounts_path)
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.tempdir.cleanup()

    def test_codex_success_filter_runs_before_pagination(self):
        page = db.list_accounts_page(
            limit=1,
            offset=1,
            codex_status_filter="success",
        )

        self.assertEqual(page["total"], 2)
        self.assertEqual([item["id"] for item in page["items"]], [1])

    def test_status_snapshot_uses_same_codex_filter(self):
        snapshot = db.list_account_plan_check_statuses(
            limit=20,
            codex_status_filter="success",
        )

        self.assertEqual(snapshot["total"], 2)
        self.assertEqual([item["id"] for item in snapshot["items"]], [3, 1])

    def test_explicit_codex_dimensions_do_not_collide_on_same_code(self):
        self.accounts_path.write_text(json.dumps([
            {
                "id": 1, "email": "auth-success@example.invalid",
                "codex_auth_status": "success", "codex_operation_status": "failed",
            },
            {
                "id": 2, "email": "operation-success@example.invalid",
                "codex_auth_status": "failed", "codex_operation_status": "success",
            },
        ]), encoding="utf-8")

        auth_page = db.list_accounts_page(codex_auth_status_filter="success")
        operation_page = db.list_accounts_page(codex_operation_status_filter="success")

        self.assertEqual([item["id"] for item in auth_page["items"]], [1])
        self.assertEqual([item["id"] for item in operation_page["items"]], [2])

    def test_explicit_status_dimensions_can_be_combined(self):
        self.accounts_path.write_text(json.dumps([
            {
                "id": 1, "email": "match@example.invalid",
                "codex_auth_status": "failed", "codex_operation_status": "running",
                "live_check_status": "live",
            },
            {
                "id": 2, "email": "wrong-live@example.invalid",
                "codex_auth_status": "failed", "codex_operation_status": "running",
                "live_check_status": "deactivated",
            },
        ]), encoding="utf-8")

        page = db.list_accounts_page(
            codex_auth_status_filter="failed",
            codex_operation_status_filter="running",
            live_check_status_filter="live",
        )

        self.assertEqual([item["id"] for item in page["items"]], [1])

    def test_checkout_type_filter_runs_before_pagination(self):
        self.accounts_path.write_text(json.dumps([
            {"id": 1, "email": "oaics@example.invalid", "checkout_session_type": "oaics"},
            {"id": 2, "email": "live-one@example.invalid", "checkout_session_type": "cs_live"},
            {"id": 3, "email": "not-checked@example.invalid"},
            {"id": 4, "email": "live-two@example.invalid", "checkout_session_type": "cs_live"},
        ]), encoding="utf-8")

        page = db.list_accounts_page(
            limit=1,
            offset=1,
            checkout_type_filter="cs_live",
        )

        self.assertEqual(page["total"], 2)
        self.assertEqual([item["id"] for item in page["items"]], [2])

    def test_checkout_type_snapshot_supports_unchecked_accounts(self):
        self.accounts_path.write_text(json.dumps([
            {"id": 1, "email": "oaics@example.invalid", "checkout_session_type": "oaics"},
            {"id": 2, "email": "not-checked@example.invalid"},
        ]), encoding="utf-8")

        snapshot = db.list_account_plan_check_statuses(
            limit=20,
            checkout_type_filter="none",
        )

        self.assertEqual(snapshot["total"], 1)
        self.assertEqual([item["id"] for item in snapshot["items"]], [2])

    def test_plan_category_filter_matches_returned_category_code(self):
        self.accounts_path.write_text(json.dumps([
            {
                "id": 1, "email": "eligible@example.invalid",
                "current_plan_type": "free", "trial_eligibility_known": True,
                "plus_trial_eligible": True,
            },
            {
                "id": 2, "email": "no-trial@example.invalid",
                "current_plan_type": "free", "trial_eligibility_known": True,
                "plus_trial_eligible": False,
            },
            {"id": 3, "email": "paid@example.invalid", "current_plan_type": "plus"},
        ]), encoding="utf-8")
        page = db.list_accounts_page(plan_filter="free_trial_eligible")
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["plan_category_code"], "free_trial_eligible")


if __name__ == "__main__":
    unittest.main()
