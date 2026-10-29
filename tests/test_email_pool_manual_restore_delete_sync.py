# -*- coding: utf-8 -*-
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from core import db
from core.sqlite_store import SQLiteRuntimeStore


class EmailPoolManualLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.stack = ExitStack()
        self.stack.enter_context(patch.dict(os.environ, {"RUNTIME_STORAGE_BACKEND": "json"}))
        for name, filename in {
            "_OUTLOOK_JSON": "outlook.json",
            "_OUTLOOK_TXT": "outlook.txt",
            "_GENERIC_API_EMAIL_JSON": "generic.json",
            "_GENERIC_API_EMAIL_TXT": "generic.txt",
            "_DOMAIN_EMAIL_JSON": "domain.json",
            "_ICLOUD_EMAIL_JSON": "icloud.json",
            "_EMAIL_POOL_LIFECYCLE_JSON": "lifecycle.json",
            "_ACCOUNTS_JSON": "accounts.json",
            "_ACCOUNTS_TXT": "accounts.txt",
            "_TOKENS_TXT": "tokens.txt",
            "_JOBS_JSON": "jobs.json",
        }.items():
            self.stack.enter_context(patch.object(db, name, root / filename))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def test_terminal_restore_is_explicit_and_audited(self):
        db.import_icloud_emails(["restore@example.com"])
        db.claim_next_icloud_email(job_id=7)
        db.mark_registration_failed("restore@example.com", "otp failed", job_id=7)

        with self.assertRaises(db.EmailPoolLifecycleError) as missing_reason:
            db.set_email_pool_status("restore@example.com", "available", source="icloud")
        self.assertEqual(missing_reason.exception.code, "restore_reason_required")
        result = db.set_email_pool_status(
            "restore@example.com",
            "available",
            source="icloud",
            reason="人工确认凭据已修复",
        )
        self.assertEqual(result["previous_status"], "failed")
        row = db.get_icloud_email_by_email("restore@example.com")
        self.assertEqual(row["status"], "available")
        self.assertEqual(row["manual_reactivated_from"], "failed")
        self.assertEqual(row["status_change_source"], "manual")

    def test_physical_delete_is_protected_and_reimport_creates_new_row(self):
        db.import_outlook_accounts([{
            "email": "delete@example.com",
            "password": "pw",
            "client_id": "client",
            "refresh_token": "refresh",
        }])
        db.insert_account(email="delete@example.com", access_token="at", email_source="outlook")
        with self.assertRaises(db.EmailPoolLifecycleError) as active:
            db.delete_email_pool_entry("delete@example.com", source="outlook")
        self.assertEqual(active.exception.code, "used_account_protected")
        self.assertTrue(db.delete_email_pool_entry("delete@example.com", source="outlook", force=True, reason="凭据泄露")["deleted"])
        self.assertIsNone(db.get_outlook_by_email("delete@example.com"))
        inserted, skipped = db.import_outlook_accounts([{
            "email": "delete@example.com",
            "password": "pw2",
            "client_id": "client2",
            "refresh_token": "refresh2",
        }])
        self.assertEqual((inserted, skipped), (1, 0))

    def test_sqlite_lifecycle_kind_round_trips(self):
        path = Path(self.temp.name) / "runtime.db"
        store = SQLiteRuntimeStore(path)
        store.initialize()
        store.replace_all("email_pool_lifecycle", [{"id": 1, "kind": "alias", "key": "a@example.com"}])
        self.assertEqual(store.load("email_pool_lifecycle")[0]["key"], "a@example.com")

    def test_explicit_import_restore_and_registered_import_do_not_revive_terminal(self):
        db.import_outlook_accounts([{
            "email": "import-restore@example.com",
            "password": "pw",
            "client_id": "client",
            "refresh_token": "refresh",
        }])
        db.claim_next_outlook(job_id=11)
        db.mark_registration_failed("import-restore@example.com", "failed", job_id=11)
        self.assertEqual(
            db.import_outlook_accounts([{
                "email": "import-restore@example.com",
                "password": "new",
                "client_id": "new-client",
                "refresh_token": "new-refresh",
            }]),
            (0, 1),
        )
        self.assertEqual(
            db.import_outlook_accounts([{
                "email": "import-restore@example.com",
                "password": "new",
                "client_id": "new-client",
                "refresh_token": "new-refresh",
            }], reactivate_existing=True),
            (1, 0),
        )
        self.assertEqual(db.get_outlook_by_email("import-restore@example.com")["status"], "available")

        db.claim_next_outlook(job_id=12)
        db.mark_registration_failed("import-restore@example.com", "failed again", job_id=12)
        inserted, skipped = db.import_registered_email_accounts([{
            "email": "import-restore@example.com",
            "password": "new",
            "client_id": "new-client",
            "refresh_token": "new-refresh",
        }], source="outlook")
        self.assertEqual((inserted, skipped), (1, 0))
        self.assertEqual(db.get_outlook_by_email("import-restore@example.com")["status"], "failed")

    def test_restore_and_delete_lifecycle_also_work_with_sqlite_backend(self):
        root = Path(self.temp.name)
        runtime = root / "runtime.db"
        store = SQLiteRuntimeStore(runtime)
        store.initialize()
        paths = {name: root / f"{name}.json" for name in (
            "outlook", "generic", "icloud", "accounts", "jobs", "domain",
        )}
        binding = {
            "_OUTLOOK_JSON": paths["outlook"],
            "_GENERIC_API_EMAIL_JSON": paths["generic"],
            "_ICLOUD_EMAIL_JSON": paths["icloud"],
            "_ACCOUNTS_JSON": paths["accounts"],
            "_JOBS_JSON": paths["jobs"],
            "_DOMAIN_EMAIL_JSON": paths["domain"],
        }
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"RUNTIME_STORAGE_BACKEND": "sqlite"}))
            stack.enter_context(patch.object(db, "_RUNTIME_DB", runtime))
            stack.enter_context(patch.object(db, "_SQLITE_STORE", store))
            stack.enter_context(patch.object(db, "_SQLITE_PATH_BINDINGS", binding))
            db.import_icloud_emails(["sqlite@example.com"])
            db.claim_next_icloud_email(job_id=21)
            db.mark_registration_failed("sqlite@example.com", "failed", job_id=21)
            db.set_email_pool_status("sqlite@example.com", "available", source="icloud", reason="sqlite restore")
            self.assertEqual(db.get_icloud_email_by_email("sqlite@example.com")["status"], "available")
            db.delete_email_pool_entry("sqlite@example.com", source="icloud")
            self.assertIsNotNone(db.list_email_pool_lifecycle(kind="email", key="sqlite@example.com"))


if __name__ == "__main__":
    unittest.main()
