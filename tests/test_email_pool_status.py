# -*- coding: utf-8 -*-
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from core import db, email_provider, gptmail_client, registration_service
from core.email_pool_status import (
    EMAIL_POOL_STATUSES,
    can_transition,
    canonical_status,
    status_counts,
)
from core.sqlite_store import SQLiteRuntimeStore
from config import email as email_config
from webui import app as web_app


class EmailPoolStatusContractTests(unittest.TestCase):
    def test_contract_and_legacy_mapping(self):
        self.assertEqual(
            EMAIL_POOL_STATUSES,
            ("available", "registering", "used", "failed", "disabled"),
        )
        self.assertEqual(canonical_status("leased"), "registering")
        self.assertEqual(canonical_status("registered"), "used")
        self.assertEqual(canonical_status("registration_failed"), "failed")
        self.assertEqual(canonical_status("deleted"), "disabled")
        self.assertEqual(canonical_status(None), "disabled")
        self.assertEqual(canonical_status("unknown-value"), "disabled")

    def test_terminal_transitions_cannot_revive_a_row(self):
        self.assertTrue(can_transition("available", "registering"))
        self.assertTrue(can_transition("registering", "used"))
        self.assertTrue(can_transition("registering", "failed"))
        self.assertFalse(can_transition("failed", "available"))
        self.assertFalse(can_transition("failed", "registering"))
        self.assertFalse(can_transition("disabled", "available"))
        self.assertEqual(
            status_counts([
                {"status": "available"},
                {"status": "registering"},
                {"status": "used"},
                {"status": "failed"},
                {"status": "disabled"},
                {"status": "not-a-status"},
            ]),
            {
                "available": 1,
                "registering": 1,
                "used": 1,
                "failed": 1,
                "disabled": 2,
                "total": 6,
            },
        )


class EmailPoolProviderStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.stack = ExitStack()
        self.stack.enter_context(patch.dict(os.environ, {"RUNTIME_STORAGE_BACKEND": "json"}))
        self.stack.enter_context(patch.object(email_config, "EMAIL_SOURCE", "outlook"))
        for name, filename in {
            "_OUTLOOK_JSON": "outlook.json",
            "_OUTLOOK_TXT": "outlook.txt",
            "_GENERIC_API_EMAIL_JSON": "generic.json",
            "_GENERIC_API_EMAIL_TXT": "generic.txt",
            "_DOMAIN_EMAIL_JSON": "domain.json",
            "_ICLOUD_EMAIL_JSON": "icloud.json",
            "_MAILCOM_EMAIL_JSON": "parents.json",
            "_MAILCOM_ALIAS_JSON": "aliases.json",
            "_ACCOUNTS_JSON": "accounts.json",
            "_ACCOUNTS_TXT": "accounts.txt",
            "_TOKENS_TXT": "tokens.txt",
            "_JOBS_JSON": "jobs.json",
        }.items():
            self.stack.enter_context(patch.object(db, name, root / filename))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def test_persistent_sources_claim_registering_and_failed_is_not_claimable(self):
        db.import_outlook_accounts([{
            "email": "outlook@example.com",
            "password": "pw",
            "client_id": "client",
            "refresh_token": "refresh",
        }])
        db.import_generic_api_emails([{"email": "generic@example.com", "code_url": "https://code.test/1"}])
        db.import_icloud_emails(["icloud@example.com"])

        outlook = db.claim_next_outlook(job_id=1)
        generic = db.claim_next_generic_api_email(job_id=2)
        icloud = db.claim_next_icloud_email(job_id=3)
        self.assertEqual(outlook["status"], "registering")
        self.assertEqual(generic["status"], "registering")
        self.assertEqual(icloud["status"], "registering")

        self.assertTrue(db.mark_registration_failed("outlook@example.com", "OTP failed", stage="otp", job_id=1))
        self.assertTrue(db.mark_registration_failed("generic@example.com", "network", stage="create", job_id=2))
        self.assertTrue(db.mark_registration_failed("icloud@example.com", "stopped", stage="stop", job_id=3))
        self.assertIsNone(db.claim_next_outlook(job_id=4))
        self.assertIsNone(db.claim_next_generic_api_email(job_id=5))
        self.assertIsNone(db.claim_next_icloud_email(job_id=6))
        self.assertEqual(db.get_outlook_by_email("outlook@example.com")["status"], "failed")
        self.assertEqual(db.get_generic_api_email_by_email("generic@example.com")["status"], "failed")
        self.assertEqual(db.get_icloud_email_by_email("icloud@example.com")["status"], "failed")

    def test_domain_and_mailcom_claim_success_and_failure_boundaries(self):
        domain = db.claim_next_domain_email("domain@example.com", job_id=10)
        self.assertEqual(domain["status"], "registering")
        self.assertTrue(db.release_unconsumed_domain_email("domain@example.com", note="driver failed"))
        self.assertIsNone(db.claim_next_domain_email("domain@example.com", job_id=11))

        db.import_mailcom_emails([{"email": "mother@mail.com", "password": "pw"}])
        db.create_mailcom_alias(
            alias_email="alias@example.com",
            parent_email="mother@mail.com",
            local_part="alias",
            domain="example.com",
        )
        claimed = db.claim_next_mailcom_alias(job_id=12)
        self.assertEqual(claimed["status"], "registering")
        account_id = db.insert_account(
            email="alias@example.com",
            access_token="at",
            email_source="mailcom",
        )
        self.assertEqual(account_id, 1)
        self.assertEqual(db.get_mailcom_alias_internal("alias@example.com")["status"], "used")
        self.assertEqual(db.get_mailcom_internal_record("mother@mail.com")["status"], "available")
        self.assertTrue(db.mark_registration_failed("alias@example.com", "late failure"))
        self.assertEqual(db.get_mailcom_alias_internal("alias@example.com")["status"], "used")

        db.create_mailcom_alias(
            alias_email="failed@example.com",
            parent_email="mother@mail.com",
            local_part="failed",
            domain="example.com",
        )
        self.assertIsNotNone(db.claim_next_mailcom_alias(job_id=13))
        self.assertTrue(db.release_unconsumed_mailcom_email("failed@example.com", note="create failed"))
        self.assertIsNone(db.claim_next_mailcom_alias(job_id=14))
        self.assertEqual(db.get_mailcom_alias_internal("failed@example.com")["status"], "failed")

    def test_registered_account_post_failure_keeps_used(self):
        db.import_outlook_accounts([{
            "email": "saved@example.com",
            "password": "pw",
            "client_id": "client",
            "refresh_token": "refresh",
        }])
        db.claim_next_outlook(job_id=20)
        db.insert_account(email="saved@example.com", access_token="at", email_source="outlook")
        self.assertEqual(db.get_outlook_by_email("saved@example.com")["status"], "used")
        self.assertTrue(db.mark_registration_failed("saved@example.com", "Codex failed", stage="codex", job_id=20))
        self.assertEqual(db.get_outlook_by_email("saved@example.com")["status"], "used")

    def test_service_release_and_retry_never_reuses_failed_email(self):
        db.import_outlook_accounts([{
            "email": "retry-source@example.com",
            "password": "pw",
            "client_id": "client",
            "refresh_token": "refresh",
        }])
        db.claim_next_outlook(job_id=50)
        registration_service._release_unconsumed_job_email(
            "retry-source@example.com", "create failed", job_id=50
        )
        self.assertEqual(db.get_outlook_by_email("retry-source@example.com")["status"], "failed")

        source = db.create_job(email_source="outlook", email="retry-source@example.com")
        db.update_job(source["id"], status="failed", email="retry-source@example.com")

        class _Executor:
            def __init__(self):
                self.calls = []

            def submit(self, fn, *args):
                self.calls.append((fn, args))

        executor = _Executor()
        with patch.object(registration_service, "get_executor", return_value=executor):
            result = registration_service.retry_job(source["id"], workers=1)
        self.assertTrue(result["ok"])
        self.assertEqual(result["retry_action"], "registration")
        self.assertEqual(len(executor.calls), 1)
        self.assertIsNone(db.claim_next_outlook(job_id=51))

        account_id = db.insert_account(
            email="post@example.com", access_token="at", email_source="outlook"
        )
        post = db.create_job(email_source="outlook", email="post@example.com", account_id=account_id)
        db.update_job(post["id"], status="failed", email="post@example.com", account_id=account_id)
        with patch.object(registration_service.codex_retry_service, "reserve", return_value=True), \
                patch.object(registration_service, "get_executor", return_value=executor):
            post_result = registration_service.retry_job(post["id"], workers=1)
        self.assertTrue(post_result["ok"])
        self.assertEqual(post_result["retry_action"], "codex")

    def test_delete_keeps_disabled_tombstone_and_reimport_cannot_revive(self):
        db.import_icloud_emails(["tombstone@example.com"])
        self.assertTrue(db.delete_icloud_email("tombstone@example.com"))
        self.assertEqual(db.get_icloud_email_by_email("tombstone@example.com")["status"], "disabled")
        inserted, skipped = db.import_icloud_emails(["tombstone@example.com"])
        self.assertEqual((inserted, skipped), (0, 1))
        self.assertIsNone(db.claim_next_icloud_email(job_id=30))

    def test_temporary_email_failure_clears_context_without_persistent_row(self):
        gptmail_client._CONTEXT_CACHE.clear()
        gptmail_client._CONTEXT_CACHE["temp@example.com"] = gptmail_client.GPTMailAccount("temp@example.com")
        with patch.object(email_provider, "resolve_email_source", return_value="gptmail"):
            self.assertTrue(email_provider.release_email_if_unconsumed("temp@example.com", note="driver failed"))
        self.assertIsNone(gptmail_client.get_account_context("temp@example.com"))

    def test_mailcom_recovery_releases_parent_and_keeps_alias_terminal(self):
        db.import_mailcom_emails([{"email": "mother@mail.com", "password": "pw"}])
        db.create_mailcom_alias(
            alias_email="orphan@example.com",
            parent_email="mother@mail.com",
            local_part="orphan",
            domain="example.com",
        )
        db.claim_next_mailcom_alias(job_id=900)
        job = db.create_job(email_source="mailcom")
        db.update_job(job["id"], status="failed", email="orphan@example.com")
        # 将租约绑定到实际终止任务，模拟进程重启前的快照。
        parents = db._load_mailcom_emails()
        parents[0]["registration_lease_job_id"] = job["id"]
        db._save_mailcom_emails(parents)
        recovered = db.recover_interrupted_mailcom_state()
        self.assertEqual(recovered["lease"], 1)
        self.assertEqual(db.get_mailcom_alias_internal("orphan@example.com")["status"], "failed")
        self.assertEqual(db.get_mailcom_internal_record("mother@mail.com")["status"], "available")

    def test_active_registration_job_id_prevents_migration_reclaim(self):
        job = db.create_job(email_source="outlook")
        db.update_job(job["id"], status="running", email="active@example.com")
        rows_path = db._OUTLOOK_JSON
        rows_path.write_text(
            '[{"id": 1, "email": "active@example.com", "status": "registering", '
            f'"registration_job_id": {job["id"]}' + "}]",
            encoding="utf-8",
        )
        db.migrate_email_pool_statuses()
        self.assertEqual(db.get_outlook_by_email("active@example.com")["status"], "registering")


class EmailPoolMigrationTests(unittest.TestCase):
    def test_json_and_sqlite_apply_safe_legacy_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, {"RUNTIME_STORAGE_BACKEND": "json"}))
                stack.enter_context(patch.object(db, "_OUTLOOK_JSON", root / "outlook.json"))
                stack.enter_context(patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"))
                stack.enter_context(patch.object(db, "_JOBS_JSON", root / "jobs.json"))
                (root / "outlook.json").write_text(
                    '[{"id": 1, "email": "old@example.com", "status": "used"}, '
                    '{"id": 2, "email": "bad@example.com", "status": "mystery"}]',
                    encoding="utf-8",
                )
                result = db.migrate_email_pool_statuses()
                self.assertGreaterEqual(result["rows_failed"], 1)
                rows = {row["email"]: row for row in db.list_outlook_pool()}
                self.assertEqual(rows["old@example.com"]["status"], "failed")
                self.assertEqual(rows["bad@example.com"]["status"], "disabled")
                self.assertTrue(rows["bad@example.com"].get("status_migration_reason"))

            runtime = root / "runtime.db"
            store = SQLiteRuntimeStore(runtime)
            store.initialize()
            store.replace_all("outlook_emails", [
                {"id": 1, "email": "leased@example.com", "status": "leased"},
                {"id": 2, "email": "unknown@example.com", "status": "???"},
            ])
            bindings = dict(db._SQLITE_PATH_BINDINGS)
            with (
                patch.dict(os.environ, {"RUNTIME_STORAGE_BACKEND": "sqlite"}),
                patch.object(db, "_RUNTIME_DB", runtime),
                patch.object(db, "_SQLITE_STORE", store),
                patch.object(db, "_SQLITE_PATH_BINDINGS", bindings),
            ):
                db.migrate_email_pool_statuses()
                rows = {row["email"]: row for row in db.list_outlook_pool()}
                self.assertEqual(rows["leased@example.com"]["status"], "failed")
                self.assertEqual(rows["unknown@example.com"]["status"], "disabled")


class EmailPoolApiStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.stack = ExitStack()
        self.stack.enter_context(patch.dict(os.environ, {"RUNTIME_STORAGE_BACKEND": "json"}))
        self.stack.enter_context(patch.object(email_config, "EMAIL_SOURCE", "outlook"))
        for name, filename in {
            "_OUTLOOK_JSON": "outlook.json",
            "_GENERIC_API_EMAIL_JSON": "generic.json",
            "_DOMAIN_EMAIL_JSON": "domain.json",
            "_ICLOUD_EMAIL_JSON": "icloud.json",
            "_MAILCOM_EMAIL_JSON": "parents.json",
            "_MAILCOM_ALIAS_JSON": "aliases.json",
            "_ACCOUNTS_JSON": "accounts.json",
            "_JOBS_JSON": "jobs.json",
        }.items():
            self.stack.enter_context(patch.object(db, name, root / filename))
        for name in (
            "recover_interrupted_plan_checks",
            "recover_interrupted_checkout_sessions",
            "recover_interrupted_extract_links",
            "recover_interrupted_live_checks",
        ):
            self.stack.enter_context(patch.object(web_app.db, name, return_value=0))
        self.client = web_app.create_app(auth_code="test-auth").test_client()
        self.headers = {"X-Auth-Code": "test-auth"}

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def test_api_exposes_five_state_filter_and_rejects_failed_revival(self):
        db.import_outlook_accounts([{
            "email": "api@example.com",
            "password": "pw",
            "client_id": "client",
            "refresh_token": "refresh",
        }])
        db.claim_next_outlook(job_id=40)
        db.mark_registration_failed("api@example.com", "create failed", job_id=40)

        listed = self.client.get(
            "/api/outlook?source=outlook&status=failed",
            headers=self.headers,
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()[0]["status"], "failed")

        revive = self.client.post(
            "/api/outlook/status",
            json={"email": "api@example.com", "source": "outlook", "status": "available"},
            headers=self.headers,
        )
        self.assertEqual(revive.status_code, 409)
        self.assertEqual(db.get_outlook_by_email("api@example.com")["status"], "failed")

        summary = self.client.get("/api/summary", headers=self.headers)
        self.assertEqual(summary.status_code, 200)
        pool = summary.get_json()["email_pool"]
        self.assertEqual(set(EMAIL_POOL_STATUSES), set(pool) - {"total"})
        self.assertEqual(pool["failed"], 1)


if __name__ == "__main__":
    unittest.main()
