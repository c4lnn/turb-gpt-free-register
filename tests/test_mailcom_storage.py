# -*- coding: utf-8 -*-
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db
from core.sqlite_store import SQLiteRuntimeStore


class MailComStorageTests(unittest.TestCase):
    def _patch_json(self, root: Path):
        return (
            patch.dict(os.environ, {"RUNTIME_STORAGE_BACKEND": "json"}),
            patch.object(db, "_MAILCOM_EMAIL_JSON", root / "mailcom.json"),
        )

    def test_json_pool_claim_release_and_public_redaction(self):
        with tempfile.TemporaryDirectory() as directory:
            env, path = self._patch_json(Path(directory))
            with env, path:
                inserted, skipped, errors = db.import_mailcom_emails([
                    {"email": "one@mail.com", "password": "password-one"},
                    {"email": "two@mail.com", "password": "password-two"},
                ])
                self.assertEqual((inserted, skipped, errors), (2, 0, []))
                claimed = db.claim_next_mailcom_email()
                self.assertEqual(claimed["email"], "one@mail.com")
                self.assertTrue(db.update_mailcom_auth("one@mail.com", "secret-at", 2_000_000_000))
                public = db.get_mailcom_email_by_email("one@mail.com")
                self.assertTrue(public["mail_access_token_present"])
                self.assertNotIn("password", public)
                self.assertNotIn("mail_access_token", public)
                self.assertTrue(db.release_unconsumed_mailcom_email("one@mail.com"))
                self.assertEqual(db.get_mailcom_internal_record("one@mail.com")["status"], "available")

    def test_conditional_token_write_preserves_newer_at(self):
        with tempfile.TemporaryDirectory() as directory:
            env, path = self._patch_json(Path(directory))
            with env, path:
                db.import_mailcom_emails([{"email": "one@mail.com", "password": "password"}])
                self.assertTrue(db.update_mailcom_auth("one@mail.com", "new-at", 2_000_000_000))
                self.assertFalse(db.update_mailcom_auth("one@mail.com", "stale-at", 2_000_000_001, expected_token="old-at"))
                self.assertEqual(db.get_mailcom_internal_record("one@mail.com")["mail_access_token"], "new-at")

    def test_sqlite_kind_isolated_from_existing_pools(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime.db"
            store = SQLiteRuntimeStore(runtime)
            store.initialize()
            store.replace_all("outlook_emails", [{"id": 1, "email": "old@outlook.com", "status": "available"}])
            bindings = dict(db._SQLITE_PATH_BINDINGS)
            with (
                patch.dict(os.environ, {"RUNTIME_STORAGE_BACKEND": "sqlite"}),
                patch.object(db, "_RUNTIME_DB", runtime),
                patch.object(db, "_SQLITE_STORE", store),
                patch.object(db, "_SQLITE_PATH_BINDINGS", bindings),
            ):
                db.import_mailcom_emails([{"email": "one@mail.com", "password": "password"}])
                self.assertEqual(store.load("outlook_emails")[0]["email"], "old@outlook.com")
                self.assertEqual(store.load("mailcom_emails")[0]["email"], "one@mail.com")
                self.assertEqual(db.list_mailcom_email_pool(), [])
                self.assertEqual(db.list_mailcom_parents()[0]["email_masked"], "o***@mail.com")

    def test_sqlite_alias_mapping_is_unique_and_separate_from_parent_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime.db"
            store = SQLiteRuntimeStore(runtime)
            store.initialize()
            bindings = dict(db._SQLITE_PATH_BINDINGS)
            with (
                patch.dict(os.environ, {"RUNTIME_STORAGE_BACKEND": "sqlite"}),
                patch.object(db, "_RUNTIME_DB", runtime),
                patch.object(db, "_SQLITE_STORE", store),
                patch.object(db, "_SQLITE_PATH_BINDINGS", bindings),
            ):
                db.import_mailcom_emails([{"email": "mother@mail.com", "password": "secret"}])
                first = db.create_mailcom_alias(
                    alias_email="Alias@Example.com",
                    parent_email="Mother@Mail.com",
                    local_part="alias",
                    domain="example.com",
                )
                repeated = db.create_mailcom_alias(
                    alias_email="alias@example.com",
                    parent_email="mother@mail.com",
                    local_part="alias",
                    domain="example.com",
                )
                self.assertEqual(first["id"], repeated["id"])
                stored = store.load("mailcom_aliases")
                self.assertEqual(len(stored), 1)
                self.assertEqual(stored[0]["alias_email"], "alias@example.com")
                self.assertNotIn("password", stored[0])
                public = db.list_mailcom_aliases()
                self.assertEqual(public[0]["parent_email_masked"], "m***@mail.com")
                self.assertNotIn("parent_email", public[0])

                replaced = db.replace_mailcom_alias_snapshot(
                    "mother@mail.com",
                    ["new@example.com"],
                )
                self.assertEqual([row["alias_email"] for row in replaced], ["new@example.com"])
                self.assertEqual(
                    [row["alias_email"] for row in store.load("mailcom_aliases")],
                    ["new@example.com"],
                )

    def test_alias_account_link_does_not_consume_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            env, path = self._patch_json(Path(directory))
            alias_path = patch.object(db, "_MAILCOM_ALIAS_JSON", Path(directory) / "aliases.json")
            with env, path, alias_path:
                db.import_mailcom_emails([{"email": "mother@mail.com", "password": "password"}])
                db.create_mailcom_alias(
                    alias_email="alias@example.com", parent_email="mother@mail.com",
                    local_part="alias", domain="example.com", job_id=3,
                )
                account_id = db.insert_account(
                    email="alias@example.com", access_token="openai-at", email_source="mailcom"
                )
                self.assertEqual(account_id, 1)
                self.assertEqual(db.get_mailcom_internal_record("mother@mail.com")["status"], "available")
                alias = db.get_mailcom_alias_internal("alias@example.com")
                self.assertEqual(alias["registered_account_id"], 1)
                self.assertEqual(alias["status"], "registered")

    def test_unconsumed_alias_marks_alias_only(self):
        with tempfile.TemporaryDirectory() as directory:
            env, path = self._patch_json(Path(directory))
            alias_path = patch.object(db, "_MAILCOM_ALIAS_JSON", Path(directory) / "aliases.json")
            with env, path, alias_path:
                db.import_mailcom_emails([{"email": "mother@mail.com", "password": "password"}])
                db.create_mailcom_alias(
                    alias_email="alias@example.com", parent_email="mother@mail.com",
                    local_part="alias", domain="example.com",
                )
                self.assertTrue(db.release_unconsumed_mailcom_email("alias@example.com", note="failed"))
                self.assertEqual(db.get_mailcom_alias_internal("alias@example.com")["status"], "registration_failed")
                self.assertEqual(db.get_mailcom_internal_record("mother@mail.com")["status"], "available")


if __name__ == "__main__":
    unittest.main()
