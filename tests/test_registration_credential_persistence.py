# -*- coding: utf-8 -*-
"""中间凭据提前保存与完整账号边界的确定性行为回归。

覆盖 OpenSpec change `align-protocol-registration-branches` 的
`registration-credential-persistence` 规范：save_registration_password /
get_account_credentials / is_complete_account 及完整账号消费者过滤，
JSON 与 SQLite 后端各自验证，不 mock 回声或源码字符串。
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.infra import db
from core.mail import email_provider
from core.openai.register import registration_service
from core.infra.sqlite_store import SQLiteRuntimeStore


class _JsonStoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.paths = {
            "_ACCOUNTS_JSON": root / "accounts.json",
            "_LEGACY_ACCOUNTS_JSON": root / "legacy-accounts.json",
            "_OUTLOOK_JSON": root / "outlook.json",
            "_LEGACY_OUTLOOK_JSON": root / "legacy-outlook.json",
            "_GENERIC_API_EMAIL_JSON": root / "generic.json",
            "_ICLOUD_EMAIL_JSON": root / "icloud.json",
            "_DOMAIN_EMAIL_JSON": root / "domain.json",
            "_JOBS_JSON": root / "jobs.json",
            "_LEGACY_JOBS_JSON": root / "legacy-jobs.json",
            "_EMAIL_POOL_LIFECYCLE_JSON": root / "lifecycle.json",
            "_OUTLOOK_TXT": root / "outlook.txt",
            "_GENERIC_API_EMAIL_TXT": root / "generic.txt",
            "_ACCOUNTS_TXT": root / "accounts.txt",
            "_TOKENS_TXT": root / "tokens.txt",
            "_VIEWER_HTML": root / "viewer.html",
        }
        for key, path in self.paths.items():
            if key.endswith("_JSON"):
                path.write_text("[]", encoding="utf-8")
        self.patchers = [patch.object(db, name, value) for name, value in self.paths.items()]
        for patcher in self.patchers:
            patcher.start()
        self.render_patch = patch.object(db, "_render_static_viewer")
        self.render_patch.start()

    def tearDown(self):
        self.render_patch.stop()
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.tempdir.cleanup()


class SaveRegistrationPasswordJsonTests(_JsonStoreTestCase):
    def test_pending_record_is_recoverable_and_not_complete(self):
        account_id = db.save_registration_password(
            "pending@example.com", "secret-password-1",
            extra={"device_id": "dev-1"},
        )
        self.assertIsInstance(account_id, int)

        # 按账号身份可直接查询到中间记录。
        row = db.get_account_by_email("pending@example.com")
        self.assertIsNotNone(row)
        self.assertEqual(row["id"], account_id)
        self.assertFalse(db.is_complete_account(row))
        self.assertEqual(row["registration_state"], "password_set")

        creds = db.get_account_credentials("pending@example.com")
        self.assertEqual(creds["registration_password"], "secret-password-1")
        self.assertEqual(creds["account_id"], account_id)
        self.assertFalse(creds["complete"])

    def test_pending_record_excluded_from_complete_consumers(self):
        db.save_registration_password("pending@example.com", "pw-2")
        db.insert_account(
            email="complete@example.com", access_token="tok-complete", email_source="outlook",
        )

        self.assertEqual(db.count_accounts(), 1)
        self.assertEqual([r["email"] for r in db.list_accounts(limit=10)], ["complete@example.com"])
        self.assertEqual(
            [r["email"] for r in db.list_accounts_page(limit=10)["items"]],
            ["complete@example.com"],
        )
        snapshot = db._viewer_snapshot(outlook_rows=[], account_rows=db._load_accounts())
        self.assertEqual(snapshot["summary"]["accounts"], 1)
        self.assertEqual([r["email"] for r in snapshot["accounts"]], ["complete@example.com"])

    def test_pending_save_does_not_write_derived_outputs_or_mark_pool_used(self):
        db.import_outlook_accounts([
            {"email": "pool@example.com", "password": "m", "client_id": "c", "refresh_token": "r"},
        ])
        db.save_registration_password("pool@example.com", "pw-3")

        pool_row = db.get_outlook_by_email("pool@example.com")
        self.assertEqual(pool_row["status"], "available")
        # 中间保存不刷新派生 TXT 快照。
        self.assertFalse(self.paths["_ACCOUNTS_TXT"].exists())
        self.assertFalse(self.paths["_TOKENS_TXT"].exists())

    def test_complete_save_reuses_same_id_and_merges_password(self):
        pending_id = db.save_registration_password("same@example.com", "pw-4")
        complete_id = db.insert_account(
            email="same@example.com", access_token="tok-4", email_source="outlook",
        )
        self.assertEqual(complete_id, pending_id)
        self.assertEqual(db.count_accounts(), 1)

        creds = db.get_account_credentials("same@example.com")
        self.assertTrue(creds["complete"])
        self.assertEqual(creds["account_id"], pending_id)
        # insert_account 的 extra 未携带新密码，原提前保存密码不得被清空。
        self.assertEqual(creds["registration_password"], "pw-4")

    def test_existing_complete_record_is_not_downgraded_on_early_save(self):
        complete_id = db.insert_account(
            email="keep@example.com", access_token="tok-keep", totp_secret="totp-secret",
            email_source="outlook",
        )
        early_id = db.save_registration_password("keep@example.com", "pw-5")
        self.assertEqual(early_id, complete_id)

        row = db.get_account_by_email("keep@example.com")
        self.assertTrue(db.is_complete_account(row))
        self.assertEqual(row["access_token"], "tok-keep")
        self.assertEqual(row["totp_secret"], "totp-secret")
        creds = db.get_account_credentials("keep@example.com")
        self.assertTrue(creds["complete"])
        self.assertEqual(creds["registration_password"], "pw-5")
        self.assertEqual(creds["totp_secret"], "totp-secret")

    def test_slow_path_access_token_update_preserves_account_fields(self):
        account_id = db.insert_account(
            email="reauth@example.com",
            access_token="old-token",
            totp_secret="totp-secret",
            extra={"registration_password": "saved-password", "note": "keep"},
        )
        self.assertTrue(db.update_account_access_token(account_id, "fresh-token"))
        row = db.get_account_by_email("reauth@example.com")
        self.assertEqual(row["access_token"], "fresh-token")
        self.assertEqual(row["totp_secret"], "totp-secret")
        self.assertEqual(db._extract_registration_password(row), "saved-password")
        self.assertEqual(json.loads(row["extra_json"])["note"], "keep")
        db.update_account_totp_secret(account_id, {"ok": False, "status": "failed", "error": "enroll failed"})
        self.assertEqual(db.get_account_by_email("reauth@example.com")["access_token"], "fresh-token")
    def test_pending_totp_round_trip_promotion_and_existing_secret_preservation(self):
        account_id = db.insert_account(
            email="totp-pending@example.com", access_token="token-old", totp_secret="old-secret",
        )
        self.assertEqual(db.save_pending_totp_secret("totp-pending@example.com", "new-secret"), account_id)
        creds = db.get_account_credentials("totp-pending@example.com")
        self.assertEqual(creds["totp_secret"], "old-secret")
        self.assertEqual(creds["pending_tfa_secret"], "new-secret")
        self.assertEqual(db.promote_pending_totp_secret("totp-pending@example.com"), "new-secret")
        creds = db.get_account_credentials("totp-pending@example.com")
        self.assertEqual(creds["totp_secret"], "new-secret")
        self.assertIsNone(creds["pending_tfa_secret"])

    def test_pending_totp_empty_update_and_failed_write_do_not_mutate_secret(self):
        db.insert_account(email="totp-failure@example.com", access_token="token", totp_secret="known-secret")
        with self.assertRaises(db.CredentialPersistenceError):
            db.save_pending_totp_secret("totp-failure@example.com", " ")
        with patch.object(db, "_save_accounts_backend_only", side_effect=OSError("disk full")):
            with self.assertRaises(db.CredentialPersistenceError):
                db.save_pending_totp_secret("totp-failure@example.com", "uncommitted-secret")
        creds = db.get_account_credentials("totp-failure@example.com")
        self.assertEqual(creds["totp_secret"], "known-secret")
        self.assertIsNone(creds["pending_tfa_secret"])

    def test_full_save_does_not_clear_existing_password_or_secret(self):
        db.insert_account(
            email="lossless@example.com", access_token="tok-a", totp_secret="secret-a",
            extra={"registration_password": "old-pw"}, email_source="outlook",
        )
        db.insert_account(
            email="lossless@example.com", access_token="tok-b", totp_secret=None,
            extra={"user": {"name": "updated"}},
        )
        row = db.get_account_by_email("lossless@example.com")
        self.assertEqual(db._extract_registration_password(row), "old-pw")
        self.assertEqual(row["totp_secret"], "secret-a")
        self.assertEqual(row["access_token"], "tok-b")

    def test_historical_record_without_state_field_is_complete(self):
        self.assertTrue(db.is_complete_account({"id": 1, "email": "legacy@example.com"}))
        self.assertFalse(db.is_complete_account(None))
        self.assertFalse(db.is_complete_account({"id": 2, "registration_state": "password_set"}))

    def test_authoritative_write_failure_raises_credential_persistence_error(self):
        with patch.object(db, "_save_accounts_backend_only", side_effect=OSError("disk full")):
            with self.assertRaises(db.CredentialPersistenceError):
                db.save_registration_password("fail@example.com", "pw-6")
        # 写失败后按账号读取应找不到记录，不伪造成功。
        creds = db.get_account_credentials("fail@example.com")
        self.assertEqual(creds["registration_password"], "")
        self.assertIsNone(creds["account_id"])
        self.assertFalse(creds["complete"])

    def test_empty_password_is_rejected_without_write(self):
        with self.assertRaises(db.CredentialPersistenceError):
            db.save_registration_password("blank@example.com", "")
        self.assertIsNone(db.get_account_by_email("blank@example.com"))

    def test_pending_record_does_not_promote_pool_or_retry_to_success(self):
        db.import_outlook_accounts([{"email": "pending-pool@example.com", "password": "mail-only",
            "client_id": "client", "refresh_token": "refresh"}])
        db.claim_next_outlook(job_id=1)
        pending_id = db.save_registration_password("pending-pool@example.com", "openai-password")
        self.assertNotIn("registered_account_id", db.list_outlook_pool()[0])
        retry = registration_service.get_retry_info({"id": 999, "status": "failed",
            "email": "pending-pool@example.com", "account_id": pending_id})
        self.assertEqual(retry["display_status"], "failed")
        self.assertEqual(retry["retry_action"], "registration")
        email_provider.release_email_if_unconsumed("pending-pool@example.com", note="OTP failed")
        self.assertEqual(db.get_outlook_by_email("pending-pool@example.com")["status"], "failed")
        self.assertEqual(db.get_account_credentials("pending-pool@example.com")["registration_password"],
            "openai-password")

    def test_restart_migration_does_not_treat_password_as_complete_account(self):
        db.import_outlook_accounts([{"email": "restart@example.com", "password": "mail-only",
            "client_id": "client", "refresh_token": "refresh"}])
        db.claim_next_outlook(job_id=1)
        db.save_registration_password("restart@example.com", "openai-password")
        db.migrate_email_pool_statuses()
        self.assertEqual(db.get_outlook_by_email("restart@example.com")["status"], "failed")
        self.assertEqual(db.count_accounts(), 0)


class UpdateJobRegistrationTests(_JsonStoreTestCase):
    def test_registration_facts_round_trip_and_merge(self):
        job = db.create_job(email_source="outlook", email="job@example.com")
        db.update_job(job["id"], status="running", registration={
            "account_kind": "new_password", "auth_step": "password",
            "authenticated": False, "password_effective": False,
            "credential_persistence": "not_attempted", "account_saved": False,
            "twofa_status": "not_attempted", "password_rejected": False,
        })
        db.update_job(job["id"], registration={"password_effective": True, "authenticated": True})

        stored = db.get_job(job["id"])["registration"]
        self.assertEqual(stored["account_kind"], "new_password")
        self.assertEqual(stored["auth_step"], "password")
        self.assertTrue(stored["password_effective"])
        self.assertTrue(stored["authenticated"])
        # 不覆盖此前已写入的其它事实。
        self.assertEqual(stored["credential_persistence"], "not_attempted")
        self.assertEqual(stored["account_saved"], False)


class SaveRegistrationPasswordSqliteTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.db_path = root / "runtime.db"
        self.store = SQLiteRuntimeStore(self.db_path)
        self.store.initialize()
        self.text_paths = {
            "_ACCOUNTS_TXT": root / "accounts.txt",
            "_TOKENS_TXT": root / "tokens.txt",
            "_OUTLOOK_TXT": root / "outlook.txt",
            "_GENERIC_API_EMAIL_TXT": root / "generic.txt",
            "_VIEWER_HTML": root / "viewer.html",
        }
        self.text_patchers = [patch.object(db, name, value) for name, value in self.text_paths.items()]
        for patcher in self.text_patchers:
            patcher.start()
        self.render_patch = patch.object(db, "_render_static_viewer")
        self.render_patch.start()

    def tearDown(self):
        self.render_patch.stop()
        for patcher in reversed(self.text_patchers):
            patcher.stop()
        self.tempdir.cleanup()

    def _patch(self):
        return patch.dict(os.environ, {"RUNTIME_STORAGE_BACKEND": "sqlite"}), \
            patch.object(db, "_RUNTIME_DB", self.db_path), \
            patch.object(db, "_SQLITE_STORE", self.store)

    def test_sqlite_pending_round_trip_and_complete_same_id(self):
        env_patch, db_patch, store_patch = self._patch()
        with env_patch, db_patch, store_patch:
            pending_id = db.save_registration_password("sqlite@example.com", "pw-sqlite")
            records = self.store.load("accounts")
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["registration_state"], "password_set")
            self.assertEqual(
                json.loads(records[0]["extra_json"])["registration_password"], "pw-sqlite",
            )

            complete_id = db.insert_account(
                email="sqlite@example.com", access_token="tok-sqlite", email_source="outlook",
            )
            self.assertEqual(complete_id, pending_id)
            creds = db.get_account_credentials("sqlite@example.com")
            self.assertTrue(creds["complete"])
            self.assertEqual(creds["registration_password"], "pw-sqlite")
            self.assertEqual(db.count_accounts(), 1)

    def test_sqlite_pending_totp_round_trip_and_promotion(self):
        env_patch, db_patch, store_patch = self._patch()
        with env_patch, db_patch, store_patch:
            account_id = db.save_pending_totp_secret("sqlite-totp@example.com", "sqlite-pending-secret")
            creds = db.get_account_credentials("sqlite-totp@example.com")
            self.assertEqual(creds["pending_tfa_secret"], "sqlite-pending-secret")
            self.assertFalse(creds["complete"])
            self.assertEqual(db.promote_pending_totp_secret("sqlite-totp@example.com"), "sqlite-pending-secret")
            creds = db.get_account_credentials("sqlite-totp@example.com")
            self.assertEqual(creds["account_id"], account_id)
            self.assertEqual(creds["totp_secret"], "sqlite-pending-secret")
            self.assertIsNone(creds["pending_tfa_secret"])


if __name__ == "__main__":
    unittest.main()
