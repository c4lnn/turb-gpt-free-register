import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db as runtime_db
from core.sqlite_store import SQLiteRuntimeStore, StorageError, migrate_legacy_snapshots
from tools.runtime_storage_admin import _restore


class SQLiteRuntimeStoreTests(unittest.TestCase):
    def test_transactional_records_and_integrity(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "runtime.db"
            store = SQLiteRuntimeStore(db)
            store.initialize()
            store.replace_all("accounts", [{"id": 1, "email": "a@example.com"}])
            self.assertEqual(store.load("accounts")[0]["email"], "a@example.com")
            store.integrity_check()

    def test_corrupt_database_is_not_treated_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "runtime.db"
            db.write_bytes(b"not sqlite")
            with self.assertRaises(StorageError):
                SQLiteRuntimeStore(db).load("accounts")

    def test_migration_records_hash_and_is_repeatable_only_with_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "accounts.json"
            source.write_text(json.dumps([{"id": 3, "email": "x@example.com"}]), encoding="utf-8")
            db = root / "runtime.db"
            text_source = root / "accounts.txt"
            text_source.write_text("x@example.com\n", encoding="utf-8")
            report = migrate_legacy_snapshots(
                db, {"accounts": source}, text_snapshots={"accounts_txt": text_source}
            )
            self.assertTrue(report["ok"])
            self.assertEqual(report["snapshots"]["accounts"]["count"], 1)
            self.assertEqual(report["text_snapshots"]["accounts_txt"]["nonempty_lines"], 1)
            self.assertEqual(SQLiteRuntimeStore(db).load("accounts")[0]["id"], 3)
            with self.assertRaises(StorageError):
                migrate_legacy_snapshots(db, {"accounts": source})

    def test_backup_is_integrity_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SQLiteRuntimeStore(root / "runtime.db")
            store.initialize()
            store.replace_all("jobs", [{"id": 7, "status": "pending"}])
            backup = store.backup(root / "backups" / "runtime.db")
            self.assertEqual(SQLiteRuntimeStore(backup).load("jobs")[0]["id"], 7)

    def test_failed_migration_keeps_report_and_temp_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "accounts.json"
            source.write_text(json.dumps([{"email": "missing-id@example.com"}]), encoding="utf-8")
            with self.assertRaises(StorageError):
                migrate_legacy_snapshots(root / "runtime.db", {"accounts": source})
            leftovers = list(root.glob("runtime-migration-*/migration-report.json"))
            self.assertEqual(len(leftovers), 1)
            self.assertFalse((root / "runtime.db").exists())

    def test_db_module_uses_existing_sqlite_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.db"
            store = SQLiteRuntimeStore(path)
            store.initialize()
            store.replace_all("accounts", [{"id": 9, "email": "sqlite@example.com"}])
            with patch.dict("os.environ", {"RUNTIME_STORAGE_BACKEND": "sqlite"}), \
                    patch.object(runtime_db, "_RUNTIME_DB", path), patch.object(runtime_db, "_SQLITE_STORE", store):
                runtime_db.validate_runtime_storage()
                self.assertEqual(runtime_db.count_accounts(), 1)
                self.assertEqual(runtime_db.get_account(9)["email"], "sqlite@example.com")

    def test_concurrent_writes_do_not_corrupt_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteRuntimeStore(Path(tmp) / "runtime.db")
            store.initialize()
            errors = []

            def write(index):
                try:
                    store.replace_all("jobs", [{"id": index, "status": "pending"}])
                except Exception as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            threads = [threading.Thread(target=write, args=(index,)) for index in range(1, 9)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            store.integrity_check()
            self.assertEqual(len(store.load("jobs")), 1)

    def test_invalid_replace_does_not_remove_committed_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteRuntimeStore(Path(tmp) / "runtime.db")
            store.initialize()
            store.replace_all("accounts", [{"id": 1, "email": "kept@example.com"}])
            with self.assertRaises(StorageError):
                store.replace_all("accounts", [{"email": "missing-id@example.com"}])
            self.assertEqual(store.load("accounts")[0]["email"], "kept@example.com")

    def test_multi_collection_replace_rolls_back_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteRuntimeStore(Path(tmp) / "runtime.db")
            store.initialize()
            store.replace_many({
                "accounts": [{"id": 1, "email": "old@example.com"}],
                "outlook_emails": [{"id": 1, "email": "old@example.com", "status": "available"}],
            })
            with self.assertRaises(StorageError):
                store.replace_many({
                    "accounts": [{"id": 2, "email": "new@example.com"}],
                    "outlook_emails": [{"email": "missing-id@example.com"}],
                })
            self.assertEqual(store.load("accounts")[0]["id"], 1)
            self.assertEqual(store.load("outlook_emails")[0]["status"], "available")

    def test_runtime_export_rebuilds_legacy_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "runtime.db"
            store = SQLiteRuntimeStore(database)
            store.initialize()
            for kind, records in {
                "accounts": [{"id": 1, "email": "a@example.com", "access_token": "token"}],
                "jobs": [{"id": 2, "status": "success"}],
                "outlook_emails": [],
                "generic_api_emails": [],
                "icloud_emails": [{"id": 3, "email": "i@icloud.com"}],
                "domain_emails": [{"id": 4, "email": "d@example.net"}],
            }.items():
                store.replace_all(kind, records)
            paths = {
                "_OUTLOOK_JSON": root / "outlook.json",
                "_OUTLOOK_TXT": root / "outlook.txt",
                "_GENERIC_API_EMAIL_JSON": root / "api.json",
                "_GENERIC_API_EMAIL_TXT": root / "api.txt",
                "_ICLOUD_EMAIL_JSON": root / "icloud.json",
                "_ACCOUNTS_JSON": root / "accounts.json",
                "_ACCOUNTS_TXT": root / "accounts.txt",
                "_TOKENS_TXT": root / "tokens.txt",
                "_JOBS_JSON": root / "jobs.json",
                "_DOMAIN_EMAIL_JSON": root / "domain.json",
                "_VIEWER_HTML": root / "viewer.html",
            }
            bindings = {name: paths[name] for name in runtime_db._SQLITE_PATH_BINDINGS}
            with patch.multiple(runtime_db, _RUNTIME_DB=database, _SQLITE_STORE=store, **paths), \
                    patch.object(runtime_db, "_SQLITE_PATH_BINDINGS", bindings):
                runtime_db.export_runtime_snapshots()
            self.assertEqual(json.loads(paths["_ACCOUNTS_JSON"].read_text(encoding="utf-8"))[0]["id"], 1)
            self.assertEqual(json.loads(paths["_JOBS_JSON"].read_text(encoding="utf-8"))[0]["id"], 2)
            self.assertEqual(paths["_TOKENS_TXT"].read_text(encoding="utf-8"), "token\n")

    def test_restore_validates_source_and_preserves_previous_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = SQLiteRuntimeStore(root / "runtime.db")
            current.initialize()
            current.replace_all("accounts", [{"id": 1, "email": "old@example.com"}])
            source = SQLiteRuntimeStore(root / "backup.db")
            source.initialize()
            source.replace_all("accounts", [{"id": 2, "email": "new@example.com"}])
            report = _restore(source.path, current.path)
            self.assertTrue(Path(report["previous"]).exists())
            self.assertEqual(SQLiteRuntimeStore(current.path).load("accounts")[0]["id"], 2)


if __name__ == "__main__":
    unittest.main()
