import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db
from core.mailcom_alias_domains import MailComAliasDomainError
from core.sqlite_store import SQLiteRuntimeStore
from webui import app as web_app


class MailComDomainManagementTests(unittest.TestCase):
    def test_json_state_defaults_persists_and_rejects_unknown_domain(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "domains.json"
            with patch.dict(os.environ, {"RUNTIME_STORAGE_BACKEND": "json"}), patch.object(
                db, "_MAILCOM_ALIAS_DOMAIN_STATE_JSON", state
            ):
                self.assertEqual(db.mailcom_alias_domain_summary(), {"total": 138, "enabled": 138, "disabled": 0})
                db.set_mailcom_alias_domain_enabled("MAIL.COM", False)
                self.assertEqual(db.mailcom_alias_domain_summary()["enabled"], 137)
                self.assertFalse(next(row for row in db.list_mailcom_alias_domains() if row["domain"] == "mail.com")["enabled"])
                with self.assertRaises(KeyError):
                    db.set_mailcom_alias_domain_enabled("unknown.invalid", True)
                with self.assertRaises(ValueError):
                    db.set_mailcom_alias_domain_enabled("mail.com", "false")
                self.assertEqual(db.set_all_mailcom_alias_domains_enabled(False), {"total": 138, "enabled": 0, "disabled": 138})
                self.assertEqual(db.set_all_mailcom_alias_domains_enabled(True)["enabled"], 138)

    def test_sqlite_state_is_idempotent_and_preserves_disabled_value(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime.db"
            store = SQLiteRuntimeStore(runtime)
            store.initialize()
            with patch.dict(os.environ, {"RUNTIME_STORAGE_BACKEND": "sqlite"}), patch.object(
                db, "_RUNTIME_DB", runtime
            ), patch.object(db, "_SQLITE_STORE", store):
                db.set_mailcom_alias_domain_enabled("mail.com", False)
                self.assertEqual(len(db.list_mailcom_alias_domains()), 138)
                self.assertFalse(next(row for row in db.list_mailcom_alias_domains() if row["domain"] == "mail.com")["enabled"])

    def test_api_returns_redacted_domain_state_and_validates_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"RUNTIME_STORAGE_BACKEND": "json"}), patch.object(
                web_app.db, "_MAILCOM_ALIAS_DOMAIN_STATE_JSON", Path(directory) / "domains.json"
            ):
                client = web_app.create_app(auth_code="domain-test").test_client()
                headers = {"X-Auth-Code": "domain-test"}
                response = client.get("/api/mailcom/domains", headers=headers)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(response.get_json()["items"]), 138)
                response = client.patch("/api/mailcom/domains/mail.com", json={"enabled": False}, headers=headers)
                self.assertEqual(response.status_code, 200)
                self.assertFalse(response.get_json()["item"]["enabled"])
                self.assertEqual(
                    client.patch("/api/mailcom/domains/unknown.invalid", json={"enabled": True}, headers=headers).status_code,
                    404,
                )
                self.assertEqual(
                    client.patch("/api/mailcom/domains/mail.com", json={"enabled": "false"}, headers=headers).status_code,
                    400,
                )
                response = client.post("/api/mailcom/domains/bulk-status", json={"enabled": False}, headers=headers)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json()["summary"]["enabled"], 0)
                response = client.post("/api/mailcom/domains/bulk-status", json={"enabled": "false"}, headers=headers)
                self.assertEqual(response.status_code, 400)

    def test_empty_enabled_set_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "domains.json"
            with patch.dict(os.environ, {"RUNTIME_STORAGE_BACKEND": "json"}), patch.object(
                db, "_MAILCOM_ALIAS_DOMAIN_STATE_JSON", state
            ):
                rows = db.list_mailcom_alias_domains()
                for row in rows:
                    db.set_mailcom_alias_domain_enabled(row["domain"], False)
                with self.assertRaises(MailComAliasDomainError):
                    db.get_enabled_mailcom_alias_domains()


if __name__ == "__main__":
    unittest.main()
