# -*- coding: utf-8 -*-
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import email as email_config
from core import db, email_provider
from webui.app import create_app


def _jwt_for_email(email: str) -> str:
    payload = {
        "https://api.openai.com/profile": {"email": email},
        "https://api.openai.com/auth": {"chatgpt_user_id": "user-test"},
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=")
    return f"header.{encoded}.signature"


class RegisteredICloudImportTests(unittest.TestCase):
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
            "_JOBS_JSON": root / "jobs.json",
            "_LEGACY_JOBS_JSON": root / "legacy-jobs.json",
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
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.tempdir.cleanup()

    def _post_import(self, text: str, *, as_registered: bool = True):
        return self.client.post(
            "/api/outlook/import",
            json={"source": "icloud", "as_registered": as_registered, "text": text},
        )

    def test_plain_icloud_import_keeps_one_email_per_line_format(self):
        response = self._post_import("one@privaterelay.appleid.com\ntwo@icloud.com", as_registered=False)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["inserted"], 2)
        self.assertEqual(response.get_json()["parsed"], 2)
        self.assertEqual(
            [row["status"] for row in db.list_icloud_email_pool(limit=10)],
            ["available", "available"],
        )
        self.assertIsNone(db.get_account_by_email("one@privaterelay.appleid.com"))

    def test_registered_import_accepts_both_delimiters_and_links_pool(self):
        first_token = _jwt_for_email("one@privaterelay.appleid.com")
        second_token = _jwt_for_email("two@icloud.com")
        response = self._post_import(
            f"one@privaterelay.appleid.com----Bearer {first_token}\n"
            f"two@icloud.com===={second_token}"
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual((body["parsed"], body["inserted"], body["skipped"]), (2, 2, 0))
        self.assertNotIn(first_token, response.get_data(as_text=True))
        self.assertNotIn(second_token, response.get_data(as_text=True))

        first_account = db.get_account_by_email("one@privaterelay.appleid.com")
        self.assertEqual(first_account["email_source"], "icloud")
        self.assertEqual(first_account["access_token"], first_token)
        self.assertIsNone(first_account.get("user_name"))
        self.assertIsNone(first_account.get("plan_type"))

        first_pool = db.get_icloud_email_by_email("one@privaterelay.appleid.com")
        self.assertEqual(first_pool["status"], "used")
        self.assertEqual(first_pool["registered_account_id"], first_account["id"])
        self.assertEqual(first_pool["access_token"], first_token)

    def test_malformed_and_claim_mismatch_are_skipped_without_exposing_tokens(self):
        valid_token = _jwt_for_email("valid@icloud.com")
        mismatch_token = _jwt_for_email("other@icloud.com")
        response = self._post_import(
            f"valid@icloud.com----{valid_token}\n"
            "missing-at@icloud.com\n"
            f"wrong@icloud.com----{mismatch_token}\n"
            "extra@icloud.com----token----extra"
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual((body["parsed"], body["inserted"], body["skipped"]), (4, 1, 3))
        self.assertEqual([item["line"] for item in body["errors"]], [2, 3, 4])
        self.assertIn("邮箱不一致", body["errors"][1]["reason"])
        response_text = response.get_data(as_text=True)
        self.assertNotIn(valid_token, response_text)
        self.assertNotIn(mismatch_token, response_text)
        self.assertIsNone(db.get_account_by_email("wrong@icloud.com"))
        self.assertIsNone(db.get_icloud_email_by_email("wrong@icloud.com"))

    def test_unparseable_nonempty_token_is_saved_without_remote_validation_claim(self):
        response = self._post_import("opaque@icloud.com----opaque-access-token")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["inserted"], 1)
        self.assertNotIn("已验证", response.get_data(as_text=True))
        self.assertEqual(db.get_account_by_email("opaque@icloud.com")["access_token"], "opaque-access-token")

    def test_existing_pool_row_is_reused(self):
        existing = [{"id": 17, "email": "reuse@icloud.com", "status": "available", "imported_at": "old"}]
        self.paths["_ICLOUD_EMAIL_JSON"].write_text(json.dumps(existing), encoding="utf-8")
        token = _jwt_for_email("reuse@icloud.com")

        inserted, skipped = db.import_registered_email_accounts(
            [{"email": "reuse@icloud.com", "access_token": token}],
            source="icloud",
        )

        self.assertEqual((inserted, skipped), (1, 0))
        pool = db.get_icloud_email_by_email("reuse@icloud.com")
        self.assertEqual(pool["id"], 17)
        self.assertEqual(pool["status"], "used")
        self.assertEqual(pool["registered_account_id"], db.get_account_by_email("reuse@icloud.com")["id"])

    def test_duplicate_account_does_not_overwrite_existing_data(self):
        first_token = _jwt_for_email("duplicate@icloud.com")
        second_token = "replacement-token"
        db.import_registered_email_accounts(
            [{"email": "duplicate@icloud.com", "access_token": first_token}],
            source="icloud",
        )
        accounts = json.loads(self.paths["_ACCOUNTS_JSON"].read_text(encoding="utf-8"))
        accounts[0]["note"] = "keep-me"
        self.paths["_ACCOUNTS_JSON"].write_text(json.dumps(accounts), encoding="utf-8")

        inserted, skipped = db.import_registered_email_accounts(
            [{"email": "duplicate@icloud.com", "access_token": second_token}],
            source="icloud",
        )

        self.assertEqual((inserted, skipped), (0, 1))
        account = db.get_account_by_email("duplicate@icloud.com")
        self.assertEqual(account["access_token"], first_token)
        self.assertEqual(account["note"], "keep-me")

    def test_claim_mismatch_direct_db_call_creates_no_one_sided_record(self):
        token = _jwt_for_email("claim@icloud.com")

        inserted, skipped = db.import_registered_email_accounts(
            [{"email": "explicit@icloud.com", "access_token": token}],
            source="icloud",
        )

        self.assertEqual((inserted, skipped), (0, 1))
        self.assertEqual(json.loads(self.paths["_ACCOUNTS_JSON"].read_text(encoding="utf-8")), [])
        self.assertEqual(json.loads(self.paths["_ICLOUD_EMAIL_JSON"].read_text(encoding="utf-8")), [])

    def test_import_does_not_create_background_jobs(self):
        token = _jwt_for_email("quiet@icloud.com")

        response = self._post_import(f"quiet@icloud.com----{token}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(self.paths["_JOBS_JSON"].read_text(encoding="utf-8")), [])


class RegisteredICloudOtpRoutingTests(unittest.TestCase):
    @patch.object(db, "get_account_by_email", return_value={"email_source": "icloud"})
    def test_registered_account_source_is_authoritative(self, get_account):
        with patch.object(email_provider, "parse_email_sources", return_value=["outlook"]):
            self.assertEqual(email_provider.resolve_email_source("saved@icloud.com"), "icloud")
        get_account.assert_called_once_with("saved@icloud.com")

    def test_icloud_pool_is_used_when_account_source_is_missing(self):
        with patch.object(db, "get_account_by_email", return_value=None), \
             patch("core.gptmail_client.get_account_context", return_value=None), \
             patch("core.cf_temp_mail_client.get_account_context", return_value=None), \
             patch("core.mailnest_client.get_account_context", return_value=None), \
             patch("core.cloudmail_client.get_account_context", return_value=None), \
             patch.object(db, "get_generic_api_email_by_email", return_value=None), \
             patch.object(db, "get_icloud_email_by_email", return_value={"email": "pool@icloud.com"}), \
             patch.object(email_provider, "parse_email_sources", return_value=["outlook"]):
            self.assertEqual(email_provider.resolve_email_source("pool@icloud.com"), "icloud")

    @patch("core.icloud_client.fetch_latest_otp", return_value="123456")
    @patch("core.email_provider.resolve_email_source", return_value="icloud")
    def test_automatic_otp_uses_icloud_qq_imap_path(self, resolve_source, fetch_otp):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True):
            result = email_provider.wait_for_otp("auto@icloud.com", after_ts=100.0)

        self.assertEqual(result, "123456")
        resolve_source.assert_called_once_with("auto@icloud.com")
        fetch_otp.assert_called_once_with("auto@icloud.com", after_ts=100.0)

    @patch("core.icloud_client.fetch_latest_otp")
    @patch("core.manual_otp.wait_for_manual_otp", return_value="654321")
    def test_manual_otp_mode_does_not_connect_icloud_imap(self, wait_manual, fetch_otp):
        with patch.object(email_config, "USE_EMAIL_SERVICE", False), patch.object(
            email_config, "OTP_MAX_WAIT", 90
        ):
            result = email_provider.wait_for_otp("manual@icloud.com", after_ts=100.0, max_wait=20)

        self.assertEqual(result, "654321")
        wait_manual.assert_called_once_with("manual@icloud.com", timeout=20, job_id=None)
        fetch_otp.assert_not_called()


class RegisteredICloudTemplateTests(unittest.TestCase):
    def test_template_exposes_registered_icloud_format(self):
        root = Path(__file__).resolve().parents[1] / "webui" / "templates"
        html = (root / "index.html").read_text(encoding="utf-8")
        self.assertIn("iCloud 已注册账号：邮箱----AT", html)
        self.assertIn("email----accessToken", html)
        self.assertIn('value="icloud"', html)
        self.assertIn('<input id="importAsRegisteredV2" type="checkbox">', html)
        self.assertIn("const as_registered = registeredEl ? !!registeredEl.checked : false;", html)
        self.assertIn('<th class="col-token">AT</th>', html)
        self.assertNotIn('<th class="col-token">Token</th>', html)


if __name__ == "__main__":
    unittest.main()
