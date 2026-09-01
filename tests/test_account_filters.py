# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from core import db
from webui import app as web_app


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

    def test_checkout_none_does_not_match_explicit_unknown_type(self):
        self.accounts_path.write_text(json.dumps([
            {"id": 1, "email": "not-checked@example.invalid"},
            {"id": 2, "email": "unknown-type@example.invalid", "checkout_session_type": "unknown"},
        ]), encoding="utf-8")

        unchecked = db.list_accounts_page(checkout_type_filter="none")
        unknown = db.list_accounts_page(checkout_type_filter="unknown")

        self.assertEqual([item["id"] for item in unchecked["items"]], [1])
        self.assertEqual([item["id"] for item in unknown["items"]], [2])

    def test_email_source_filter_runs_before_pagination_and_preserves_raw_values(self):
        self.accounts_path.write_text(json.dumps([
            {"id": 1, "email": "outlook@example.invalid", "email_source": "outlook"},
            {"id": 2, "email": "mailcom-one@example.invalid", "email_source": "mailcom"},
            {"id": 3, "email": "missing@example.invalid"},
            {"id": 4, "email": "legacy@example.invalid", "email_source": "legacy_mail"},
            {"id": 5, "email": "mailcom-two@example.invalid", "email_source": "mailcom"},
        ]), encoding="utf-8")

        page = db.list_accounts_page(limit=1, offset=1, email_source_filter="mailcom")
        unknown = db.list_accounts_page(email_source_filter="unknown")

        self.assertEqual(page["total"], 2)
        self.assertEqual([item["id"] for item in page["items"]], [2])
        self.assertEqual([item["id"] for item in unknown["items"]], [4, 3])
        self.assertEqual(unknown["items"][0]["email_source"], "legacy_mail")

    def test_email_source_filter_is_shared_by_status_snapshot(self):
        self.accounts_path.write_text(json.dumps([
            {"id": 1, "email": "outlook@example.invalid", "email_source": "outlook"},
            {"id": 2, "email": "mailcom@example.invalid", "email_source": "mailcom"},
        ]), encoding="utf-8")

        snapshot = db.list_account_plan_check_statuses(
            limit=20, email_source_filter="mailcom"
        )

        self.assertEqual(snapshot["total"], 1)
        self.assertEqual(snapshot["items"][0]["email_source"], "mailcom")

    def test_status_snapshot_applies_date_filter_before_pagination(self):
        self.accounts_path.write_text(json.dumps([
            {"id": 1, "email": "old@example.invalid", "created_at": "2026-07-31T23:59:59"},
            {"id": 2, "email": "in-range-one@example.invalid", "created_at": "2026-08-01T00:00:00"},
            {"id": 3, "email": "in-range-two@example.invalid", "created_at": "2026-08-02T23:59:59"},
        ]), encoding="utf-8")

        snapshot = db.list_account_plan_check_statuses(
            limit=1,
            offset=1,
            date_from="2026-08-01",
            date_to="2026-08-02",
        )

        self.assertEqual(snapshot["total"], 2)
        self.assertEqual([item["id"] for item in snapshot["items"]], [2])

    def test_plan_status_endpoint_forwards_complete_filter_set(self):
        with tempfile.TemporaryDirectory() as tempdir:
            accounts_path = Path(tempdir) / "accounts.json"
            accounts_path.write_text("[]", encoding="utf-8")
            with ExitStack() as stack:
                stack.enter_context(patch.object(web_app.db, "_ACCOUNTS_JSON", accounts_path))
                stack.enter_context(patch.object(web_app.db, "_render_static_viewer"))
                stack.enter_context(patch.object(web_app.db, "list_account_plan_check_statuses", return_value={
                    "items": [], "total": 0, "offset": 3, "limit": 3, "revision": "0",
                }))
                stack.enter_context(patch.object(web_app.plan_check_service, "queue_settings", return_value={}))
                stack.enter_context(patch.object(web_app.checkout_session_service, "queue_settings", return_value={}))
                for name in (
                    "recover_interrupted_plan_checks",
                    "recover_interrupted_checkout_sessions",
                    "recover_interrupted_extract_links",
                    "recover_interrupted_live_checks",
                ):
                    stack.enter_context(patch.object(web_app.db, name, return_value=0))
                app = web_app.create_app(auth_code="test-auth")
                client = app.test_client()
                response = client.get(
                    "/api/accounts/plan-check-status?page=2&page_size=3&archived=all"
                    "&email_source=mailcom&plan_category=paid&codex_auth_status=failed"
                    "&codex_operation_status=running&live_check_status=live"
                    "&checkout_type=cs_live&q=needle&date_from=2026-08-01&date_to=2026-08-02",
                    headers={"X-Auth-Code": "test-auth"},
                )

                self.assertEqual(response.status_code, 200)
                kwargs = web_app.db.list_account_plan_check_statuses.call_args.kwargs
                self.assertEqual(kwargs["archived"], "all")
                self.assertEqual(kwargs["email_source_filter"], "mailcom")
                self.assertEqual(kwargs["plan_filter"], "paid")
                self.assertEqual(kwargs["codex_auth_status_filter"], "failed")
                self.assertEqual(kwargs["codex_operation_status_filter"], "running")
                self.assertEqual(kwargs["live_check_status_filter"], "live")
                self.assertEqual(kwargs["checkout_type_filter"], "cs_live")
                self.assertEqual(kwargs["q"], "needle")
                self.assertEqual(kwargs["date_from"], "2026-08-01")
                self.assertEqual(kwargs["date_to"], "2026-08-02")

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
