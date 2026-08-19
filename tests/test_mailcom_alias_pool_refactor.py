# -*- coding: utf-8 -*-
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db
from core.mailcom_alias_cleanup import process_plan_result
from core.mailcom_alias_pool_service import enqueue_parent_sync, sync_parent_now
import core.mailcom_alias_pool_service as alias_pool_service
from core.mailcom_alias_service import MailComAliasError, MailComAliasService, mother_alias_lock
from core.mailcom_settings_client import MailComSettingsConflictError, MailComSettingsError
from core.mailcom_provider import MailComProvider
from webui import app as web_app


class MailComAliasPoolRefactorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.patches = [
            patch.dict(os.environ, {"RUNTIME_STORAGE_BACKEND": "json"}),
            patch.object(db, "_MAILCOM_EMAIL_JSON", root / "parents.json"),
            patch.object(db, "_MAILCOM_ALIAS_JSON", root / "aliases.json"),
            patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"),
            patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"),
            patch.object(db, "_TOKENS_TXT", root / "tokens.txt"),
        ]
        for item in self.patches:
            item.start()
        with alias_pool_service._PENDING_LOCK:
            alias_pool_service._PENDING.clear()
        db.import_mailcom_emails([{"email": "mother@mail.com", "password": "pw"}])
        for name in ("a", "b"):
            db.create_mailcom_alias(
                alias_email=f"{name}@example.com",
                parent_email="mother@mail.com",
                local_part=name,
                domain="example.com",
            )

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_same_parent_alias_claims_are_serialized(self):
        first = db.claim_next_mailcom_alias(job_id=1)
        self.assertEqual(first["alias_email"], "a@example.com")
        self.assertIsNone(db.claim_next_mailcom_alias(job_id=2))
        self.assertTrue(db.release_mailcom_registration_lease(
            first["alias_email"], job_id=1, alias_status="available"
        ))
        second = db.claim_next_mailcom_alias(job_id=2)
        self.assertEqual(second["alias_email"], "a@example.com")

    def test_snapshot_replacement_refuses_active_registration_lease(self):
        claimed = db.claim_next_mailcom_alias(job_id=7)
        self.assertEqual(claimed["alias_email"], "a@example.com")
        replacement = db.replace_mailcom_alias_snapshot(
            "mother@mail.com",
            ["a@example.com", "remote@example.com"],
        )
        self.assertIsNone(replacement)
        self.assertEqual(db.get_mailcom_alias_internal("a@example.com")["status"], "leased")
        self.assertIsNone(db.get_mailcom_alias_internal("remote@example.com"))

    def test_provider_claims_existing_alias_without_creating(self):
        with patch("core.registration_service._THREAD_CTX.job_id", 31, create=True):
            picked = MailComProvider().pick_account()
        self.assertEqual(picked.email, "a@example.com")
        self.assertEqual(db.get_mailcom_alias_internal("a@example.com")["status"], "leased")
        self.assertEqual(db.get_mailcom_internal_record("mother@mail.com")["registration_lease_job_id"], 31)

    def test_sync_discovers_remote_aliases_and_fills_to_nine(self):
        class Settings:
            def __init__(self):
                self.addresses = [
                    {"address": "mother@mail.com", "state": "ACTIVE", "deletable": False},
                    {"address": "remote@example.com", "state": "ACTIVE", "deletable": True},
                ]

            def authenticate(self, email, password):
                return None

            def list_addresses(self):
                return list(self.addresses)

            def validate_address(self, address):
                return None

            def create_address(self, address):
                self.addresses.append({"address": address, "state": "ACTIVE", "deletable": True})

        settings = Settings()
        names = iter(f"new-{index}" for index in range(20))
        service = MailComAliasService(settings_client_factory=lambda: settings)
        with (
            patch("core.mailcom_alias_service.generate_alias_local_part", side_effect=lambda: next(names)),
            patch("core.mailcom_alias_service.choose_alias_domain", return_value="example.com"),
        ):
            result = service.sync_parent_aliases(db.get_mailcom_internal_record("mother@mail.com"))
        self.assertEqual(result["remote_active_alias_count"], 9)
        self.assertEqual(result["created_count"], 8)
        self.assertIsNotNone(db.get_mailcom_alias_internal("remote@example.com"))
        self.assertEqual(db.mailcom_alias_summary("mother@mail.com")["available"], 9)

    def test_snapshot_replacement_matches_accounts_and_removes_missing_aliases(self):
        account_id = db.insert_account(
            email="a@example.com",
            access_token="at",
            email_source="mailcom",
            plan_type="free",
        )
        db.archive_account(account_id, archived=True)
        other_parent = "other@mail.com"
        db.import_mailcom_emails([{"email": other_parent, "password": "pw"}])
        db.create_mailcom_alias(
            alias_email="other@example.com",
            parent_email=other_parent,
            local_part="other",
            domain="example.com",
        )

        replacement = db.replace_mailcom_alias_snapshot(
            "mother@mail.com",
            ["a@example.com", "new@example.com"],
        )

        self.assertEqual({row["alias_email"] for row in replacement}, {"a@example.com", "new@example.com"})
        local = {row["email"]: row for row in db.list_mailcom_aliases(parent_email="mother@mail.com")}
        self.assertEqual(set(local), {"a@example.com", "new@example.com"})
        self.assertEqual(local["a@example.com"]["registered_account_id"], account_id)
        self.assertEqual(local["a@example.com"]["account_plan_type"], "free")
        self.assertTrue(local["a@example.com"]["account_archived"])
        self.assertEqual(local["new@example.com"]["status"], "available")
        self.assertEqual(
            {row["email"] for row in db.list_mailcom_aliases(parent_email=other_parent)},
            {"other@example.com"},
        )

    def test_empty_snapshot_clears_only_selected_parent(self):
        self.assertIsNotNone(db.get_mailcom_alias_internal("a@example.com"))
        replacement = db.replace_mailcom_alias_snapshot("mother@mail.com", [])
        self.assertEqual(replacement, [])
        self.assertEqual(db.list_mailcom_aliases(parent_email="mother@mail.com"), [])

    def test_final_remote_read_failure_preserves_local_snapshot(self):
        class FinalReadFailure:
            def __init__(self):
                self.calls = 0

            def authenticate(self, email, password):
                return None

            def list_addresses(self):
                self.calls += 1
                if self.calls > 1:
                    raise MailComSettingsError("network", error_type="network_error")
                return [
                    {"address": "mother@mail.com", "state": "ACTIVE", "deletable": False},
                    {"address": "remote@example.com", "state": "ACTIVE", "deletable": True},
                ]

            def validate_address(self, address):
                return None

            def create_address(self, address):
                raise MailComSettingsError("network", error_type="network_error")

        settings = FinalReadFailure()
        service = MailComAliasService(settings_client_factory=lambda: settings)
        with self.assertRaisesRegex(Exception, "最终列表查询失败"):
            service.sync_parent_aliases(db.get_mailcom_internal_record("mother@mail.com"))
        self.assertEqual(
            {row["email"] for row in db.list_mailcom_aliases(parent_email="mother@mail.com")},
            {"a@example.com", "b@example.com"},
        )

    def test_initial_remote_read_failure_does_not_replace_local_snapshot(self):
        class InitialReadFailure:
            def authenticate(self, email, password):
                return None

            def list_addresses(self):
                raise MailComSettingsError("network", error_type="network_error")

        service = MailComAliasService(settings_client_factory=InitialReadFailure)
        with self.assertRaisesRegex(MailComAliasError, "同步查询失败"):
            service.sync_parent_aliases(db.get_mailcom_internal_record("mother@mail.com"))
        self.assertEqual(
            {row["email"] for row in db.list_mailcom_aliases(parent_email="mother@mail.com")},
            {"a@example.com", "b@example.com"},
        )

    def test_initial_three_aliases_issue_six_create_requests_without_retry_loop(self):
        class CreateFailure:
            def __init__(self):
                self.list_calls = 0
                self.create_calls = []

            def authenticate(self, email, password):
                return None

            def list_addresses(self):
                self.list_calls += 1
                return [
                    {"address": "mother@mail.com", "state": "ACTIVE", "deletable": False},
                    *[
                        {"address": f"remote-{i}@example.com", "state": "ACTIVE", "deletable": True}
                        for i in range(3)
                    ],
                ]

            def validate_address(self, address):
                return None

            def create_address(self, address):
                self.create_calls.append(address)
                raise MailComSettingsError("network", error_type="network_error")

        settings = CreateFailure()
        service = MailComAliasService(settings_client_factory=lambda: settings)
        with patch("core.mailcom_alias_service.generate_alias_local_part", side_effect=lambda: f"new-{len(settings.create_calls)}"), \
                patch("core.mailcom_alias_service.choose_alias_domain", return_value="example.com"):
            result = service.sync_parent_aliases(db.get_mailcom_internal_record("mother@mail.com"))
        self.assertEqual(result["create_opportunity_count"], 6)
        self.assertEqual(result["create_request_count"], 6)
        self.assertEqual(len(settings.create_calls), 6)
        self.assertEqual(settings.list_calls, 2)
        self.assertEqual(result["remote_active_alias_count"], 3)

    def test_validation_retries_three_times_then_skips_create_opportunity(self):
        class ValidationFailure:
            def __init__(self):
                self.validate_calls = 0
                self.create_calls = 0

            def authenticate(self, email, password):
                return None

            def list_addresses(self):
                return [
                    {"address": "mother@mail.com", "state": "ACTIVE", "deletable": False},
                    *[
                        {"address": f"remote-{i}@example.com", "state": "ACTIVE", "deletable": True}
                        for i in range(8)
                    ],
                ]

            def validate_address(self, address):
                self.validate_calls += 1
                raise MailComSettingsConflictError()

            def create_address(self, address):
                self.create_calls += 1

        settings = ValidationFailure()
        service = MailComAliasService(settings_client_factory=lambda: settings)
        with patch("core.mailcom_alias_service.generate_alias_local_part", side_effect=lambda: f"new-{settings.validate_calls}"), \
                patch("core.mailcom_alias_service.choose_alias_domain", return_value="example.com"):
            result = service.sync_parent_aliases(db.get_mailcom_internal_record("mother@mail.com"))
        self.assertEqual(settings.validate_calls, 3)
        self.assertEqual(settings.create_calls, 0)
        self.assertEqual(result["create_opportunity_count"], 1)
        self.assertEqual(result["remote_active_alias_count"], 8)

    def test_sync_status_reflects_ready_partial_and_failed_results(self):
        with patch(
            "core.mailcom_alias_pool_service.sync_parent_aliases",
            return_value={"remote_active_alias_count": 9},
        ):
            ready = sync_parent_now("mother@mail.com")
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(db.get_mailcom_internal_record("mother@mail.com")["sync_status"], "ready")

        with patch(
            "core.mailcom_alias_pool_service.sync_parent_aliases",
            return_value={"remote_active_alias_count": 0},
        ):
            partial = sync_parent_now("mother@mail.com")
        self.assertEqual(partial["status"], "partial")
        self.assertEqual(db.get_mailcom_internal_record("mother@mail.com")["sync_status"], "partial")

        with patch(
            "core.mailcom_alias_pool_service.sync_parent_aliases",
            side_effect=MailComAliasError("broken", error_type="network_error"),
        ):
            failed = sync_parent_now("mother@mail.com")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(db.get_mailcom_internal_record("mother@mail.com")["sync_status"], "failed")

    def test_manual_delete_rejects_leased_alias_before_remote_request(self):
        db.claim_next_mailcom_alias(job_id=99)
        result = alias_pool_service.delete_alias_now("a@example.com")
        self.assertEqual(result["status"], 409)
        self.assertEqual(result["error"], "alias_leased")

    def test_manual_delete_does_not_reacquire_service_parent_lock(self):
        parent_email = "lock-test@mail.com"
        alias_email = "lock-test@example.com"
        db.import_mailcom_emails([{"email": parent_email, "password": "pw"}])
        db.create_mailcom_alias(
            alias_email=alias_email,
            parent_email=parent_email,
            local_part="lock-test",
            domain="example.com",
        )
        completed = threading.Event()
        outcome = {}

        def locked_delete(alias):
            with mother_alias_lock(alias["parent_email"]):
                return True

        def run_delete():
            try:
                outcome["result"] = alias_pool_service.delete_alias_now(alias_email)
            finally:
                completed.set()

        with (
            patch("core.mailcom_alias_pool_service.delete_alias", side_effect=locked_delete),
            patch("core.mailcom_alias_pool_service.enqueue_parent_sync") as enqueue,
        ):
            worker = threading.Thread(target=run_delete, daemon=True)
            worker.start()
            self.assertTrue(completed.wait(timeout=1), "手动删除重复获取母号锁并发生死锁")
            worker.join(timeout=1)

        self.assertTrue(outcome["result"]["ok"])
        enqueue.assert_not_called()

    def test_parent_sync_queue_deduplicates_requests(self):
        started = threading.Event()
        release = threading.Event()

        def sync_fn(parent):
            started.set()
            release.wait(timeout=2)
            return {"remote_active_alias_count": 9, "created_count": 0}

        with patch("core.mailcom_alias_pool_service.sync_parent_aliases", sync_fn):
            first = enqueue_parent_sync("mother@mail.com")
            self.assertTrue(first["accepted"])
            self.assertTrue(started.wait(timeout=2))
            second = enqueue_parent_sync("mother@mail.com")
            self.assertTrue(second["busy"])
            release.set()
            deadline = time.time() + 2
            while time.time() < deadline and enqueue_parent_sync("mother@mail.com").get("busy"):
                time.sleep(0.02)

    def test_delete_requires_real_unarchived_free_no_trial_account(self):
        account_id = db.insert_account(
            email="a@example.com",
            access_token="at",
            email_source="mailcom",
            plan_type="free",
        )
        db.update_account_plan_check(
            acc_id=account_id,
            result={
                "ok": True,
                "current_plan_type": "free",
                "trial_eligibility_known": True,
                "plus_trial_eligible": False,
            },
        )
        with (
            patch("core.mailcom_alias_cleanup.email_cfg.MAILCOM_DELETE_ALIAS_IF_NO_TRIAL", True),
            patch("core.mailcom_alias_pool_service.enqueue_parent_sync") as enqueue,
        ):
            outcome = process_plan_result(
                account_id=account_id,
                result={
                    "ok": True,
                    "current_plan_type": "free",
                    "trial_eligibility_known": True,
                    "plus_trial_eligible": False,
                },
                delete_alias_fn=lambda _: True,
            )
        self.assertTrue(outcome["deleted"])
        self.assertEqual(db.get_mailcom_alias_internal("a@example.com")["status"], "deleted")
        enqueue.assert_called_once_with("mother@mail.com")

    def test_archived_account_is_never_deleted(self):
        account_id = db.insert_account(
            email="a@example.com",
            access_token="at",
            email_source="mailcom",
            plan_type="free",
        )
        db.archive_account(account_id, archived=True)
        with patch("core.mailcom_alias_cleanup.email_cfg.MAILCOM_DELETE_ALIAS_IF_NO_TRIAL", True):
            outcome = process_plan_result(
                account_id=account_id,
                result={
                    "ok": True,
                    "current_plan_type": "free",
                    "trial_eligibility_known": True,
                    "plus_trial_eligible": False,
                },
                delete_alias_fn=lambda _: self.fail("归档账号不应删除 alias"),
            )
        self.assertEqual(outcome["reason"], "account_archived")
        self.assertNotEqual(db.get_mailcom_alias_internal("a@example.com")["status"], "deleted")

    def test_unconfirmed_cleanup_stays_pending_and_is_not_retried(self):
        account_id = db.insert_account(
            email="a@example.com", access_token="at", email_source="mailcom", plan_type="free"
        )
        result = {
            "ok": True,
            "current_plan_type": "free",
            "trial_eligibility_known": True,
            "plus_trial_eligible": False,
        }
        calls = []
        with patch("core.mailcom_alias_cleanup.email_cfg.MAILCOM_DELETE_ALIAS_IF_NO_TRIAL", True):
            first = process_plan_result(
                account_id=account_id,
                result=result,
                delete_alias_fn=lambda row: calls.append(row["alias_email"]) or False,
            )
            second = process_plan_result(
                account_id=account_id,
                result=result,
                delete_alias_fn=lambda _: self.fail("cleanup_pending 不应自动重试"),
            )
        self.assertEqual(first["reason"], "delete_unconfirmed")
        self.assertEqual(second["reason"], "cleanup_already_handled")
        self.assertEqual(calls, ["a@example.com"])

    def test_parent_and_alias_api_keep_credentials_out_of_public_rows(self):
        parents = db.list_mailcom_parents()
        self.assertEqual(len(parents), 1)
        self.assertNotIn("password", parents[0])
        aliases = db.list_mailcom_aliases(parent_email="mother@mail.com")
        self.assertEqual({row["email"] for row in aliases}, {"a@example.com", "b@example.com"})
        self.assertTrue(all("parent_email" not in row for row in aliases))

    def test_deleted_alias_is_removed_from_default_pool_and_parent_detail(self):
        db.mark_mailcom_alias_deleted("a@example.com")

        self.assertEqual(
            {row["email"] for row in db.list_mailcom_email_pool()},
            {"b@example.com"},
        )
        self.assertEqual(
            {row["email"] for row in db.list_mailcom_aliases(parent_email="mother@mail.com")},
            {"b@example.com"},
        )
        self.assertEqual(
            {row["email"] for row in db.list_mailcom_aliases(parent_email="mother@mail.com", status="disabled")},
            {"a@example.com"},
        )

    def test_webui_parent_view_and_manual_delete_endpoint(self):
        stack = []
        try:
            for name in (
                "recover_interrupted_plan_checks",
                "recover_interrupted_checkout_sessions",
                "recover_interrupted_extract_links",
                "recover_interrupted_live_checks",
                "recover_interrupted_codex_agents",
            ):
                patcher = patch.object(web_app.db, name, return_value=0)
                patcher.start()
                stack.append(patcher)
            client = web_app.create_app(auth_code="test-auth").test_client()
            headers = {"X-Auth-Code": "test-auth"}
            parents = client.get("/api/mailcom", headers=headers).get_json()
            self.assertEqual(len(parents["items"]), 1)
            parent_id = parents["items"][0]["id"]
            detail = client.get(f"/api/mailcom/aliases?parent_id={parent_id}", headers=headers).get_json()
            self.assertEqual(len(detail["items"]), 2)
            with patch("core.mailcom_alias_pool_service.delete_alias_now", return_value={"ok": True, "deleted": True}):
                response = client.post(
                    "/api/mailcom/aliases/delete",
                    json={"alias_email": "a@example.com"},
                    headers=headers,
                )
            self.assertEqual(response.status_code, 200)
        finally:
            for patcher in reversed(stack):
                patcher.stop()


if __name__ == "__main__":
    unittest.main()
