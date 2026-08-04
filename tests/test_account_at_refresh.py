# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from core import account_export, codex_retry_service, db, otp_operation_lock
from core import registration_service as service
from core import roxy_registration
from webui.app import create_app


def session_payload(email="user@example.com", token="new-at"):
    return {
        "accessToken": token,
        "user": {"id": "user-new", "name": "New Name", "email": email, "mfa": True},
        "account": {"id": "account-new", "planType": "free", "structure": "personal"},
        "expires": "2026-09-01T00:00:00.000Z",
    }


class SessionNormalizerTests(unittest.TestCase):
    def test_normalizes_registration_fields(self):
        result = account_export.normalize_chatgpt_session(
            session_payload(), expected_email="USER@example.com"
        )
        self.assertEqual(result["access_token"], "new-at")
        self.assertEqual(result["user_id"], "user-new")
        self.assertEqual(result["user_name"], "New Name")
        self.assertEqual(result["plan_type"], "free")
        self.assertEqual(result["expires_at"], "2026-09-01T00:00:00.000Z")
        self.assertEqual(result["extra"]["account"]["id"], "account-new")

    def test_rejects_missing_fields_and_email_mismatch(self):
        for key in ("accessToken", "user", "account", "expires"):
            payload = session_payload()
            payload.pop(key)
            with self.subTest(key=key), self.assertRaises(ValueError):
                account_export.normalize_chatgpt_session(payload, expected_email="user@example.com")
        with self.assertRaisesRegex(ValueError, "邮箱与目标账户不一致"):
            account_export.normalize_chatgpt_session(
                session_payload(email="other@example.com"), expected_email="user@example.com"
            )

    @patch("core.account_export._append_batch_archive", return_value=Path("archive"))
    @patch("core.db.insert_account", return_value=7)
    @patch("core.plan_check_service.enqueue_account_plan_check", return_value={"accepted": True})
    def test_registration_save_uses_same_normalized_mapping(self, _plan, insert_account, _archive):
        payload = session_payload()
        account_export.save_account_data(
            email="user@example.com",
            access_token=payload["accessToken"],
            extra={"user": payload["user"], "account": payload["account"], "expires": payload["expires"]},
        )
        kwargs = insert_account.call_args.kwargs
        self.assertEqual(kwargs["access_token"], "new-at")
        self.assertEqual(kwargs["user_id"], "user-new")
        self.assertEqual(kwargs["plan_type"], "free")
        self.assertEqual(kwargs["expires_at"], payload["expires"])


class AccountSessionDbTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.paths = {
            "_ACCOUNTS_JSON": root / "accounts.json",
            "_LEGACY_ACCOUNTS_JSON": root / "legacy-accounts.json",
            "_OUTLOOK_JSON": root / "outlook.json",
            "_LEGACY_OUTLOOK_JSON": root / "legacy-outlook.json",
            "_GENERIC_API_EMAIL_JSON": root / "generic.json",
            "_DOMAIN_EMAIL_JSON": root / "domain.json",
            "_ICLOUD_EMAIL_JSON": root / "icloud.json",
            "_ACCOUNTS_TXT": root / "accounts.txt",
            "_TOKENS_TXT": root / "tokens.txt",
            "_OUTLOOK_TXT": root / "outlook.txt",
            "_GENERIC_API_EMAIL_TXT": root / "generic.txt",
            "_VIEWER_HTML": root / "viewer.html",
            "_JOBS_JSON": root / "jobs.json",
            "_LEGACY_JOBS_JSON": root / "legacy-jobs.json",
            "_LOG_DIR": root / "logs",
        }
        self.patchers = [patch.object(db, name, value) for name, value in self.paths.items()]
        for item in self.patchers:
            item.start()
        self.paths["_ACCOUNTS_JSON"].write_text(json.dumps([{
            "id": 1,
            "email": "user@example.com",
            "access_token": "old-at",
            "totp_secret": "totp-old",
            "note": "keep-note",
            "archived": True,
            "codex_status": "success",
            "extract_link_status": "success",
            "extra_json": json.dumps({
                "registration_password": "saved-password",
                "codex": {"status": "success"},
                "historical": "keep",
            }),
        }]), encoding="utf-8")
        self.paths["_OUTLOOK_JSON"].write_text(json.dumps([{
            "id": 1,
            "email": "user@example.com",
            "password": "mail-password",
            "client_id": "client",
            "refresh_token": "mail-refresh",
            "status": "used",
        }]), encoding="utf-8")
        for key in ("_GENERIC_API_EMAIL_JSON", "_DOMAIN_EMAIL_JSON", "_ICLOUD_EMAIL_JSON", "_JOBS_JSON"):
            self.paths[key].write_text("[]", encoding="utf-8")

    def tearDown(self):
        otp_operation_lock.clear()
        for item in reversed(self.patchers):
            item.stop()
        self.tempdir.cleanup()

    def _account(self):
        return json.loads(self.paths["_ACCOUNTS_JSON"].read_text(encoding="utf-8"))[0]

    def test_atomic_update_preserves_business_fields_and_syncs_pool(self):
        normalized = account_export.normalize_chatgpt_session(
            session_payload(), expected_email="user@example.com"
        )
        db.update_account_session(
            1,
            expected_email="user@example.com",
            normalized=normalized,
            auth_method="password",
            roxybrowser={"profile_id": "profile-1"},
        )
        row = self._account()
        extra = json.loads(row["extra_json"])
        self.assertEqual(row["access_token"], "new-at")
        self.assertEqual(row["note"], "keep-note")
        self.assertTrue(row["archived"])
        self.assertEqual(row["totp_secret"], "totp-old")
        self.assertEqual(row["codex_status"], "success")
        self.assertEqual(row["extract_link_status"], "success")
        self.assertEqual(extra["registration_password"], "saved-password")
        self.assertEqual(extra["historical"], "keep")
        self.assertEqual(extra["user"]["id"], "user-new")
        self.assertEqual(extra["roxybrowser"]["profile_id"], "profile-1")
        pool = json.loads(self.paths["_OUTLOOK_JSON"].read_text(encoding="utf-8"))[0]
        self.assertEqual(pool["access_token"], "new-at")
        self.assertEqual(pool["status"], "used")

    def test_invalid_password_is_removed_only_on_successful_commit(self):
        normalized = account_export.normalize_chatgpt_session(session_payload(), expected_email="user@example.com")
        db.update_account_session(
            1,
            expected_email="user@example.com",
            normalized=normalized,
            auth_method="otp",
            invalidate_registration_password=True,
        )
        self.assertNotIn("registration_password", json.loads(self._account()["extra_json"]))

    def test_validation_failure_does_not_change_old_token(self):
        with self.assertRaises(ValueError):
            account_export.normalize_chatgpt_session(
                session_payload(email="other@example.com"), expected_email="user@example.com"
            )
        self.assertEqual(self._account()["access_token"], "old-at")

    def test_claim_and_restart_recovery(self):
        job = db.create_job("outlook", job_type="at_refresh", email="user@example.com", account_id=1)
        self.assertTrue(db.claim_account_at_refresh(1, job["id"]))
        self.assertFalse(db.claim_account_at_refresh(1, job["id"] + 1))
        db.update_account_at_refresh_status(1, "running", job_id=job["id"])
        self.assertEqual(db.recover_interrupted_at_refresh_jobs(), 1)
        self.assertEqual(db.recover_interrupted_at_refreshes(), 1)
        self.assertEqual(self._account()["at_refresh_status"], "failed")

    def test_worker_updates_full_session_without_registration_side_effects(self):
        payload = session_payload()
        normalized = account_export.normalize_chatgpt_session(payload, expected_email="user@example.com")
        job = db.create_job("outlook", job_type="at_refresh", email="user@example.com", account_id=1)
        self.assertTrue(db.claim_account_at_refresh(1, job["id"]))
        self.assertTrue(otp_operation_lock.reserve("user@example.com", "at_refresh"))
        result = {
            "success": True,
            "normalized": normalized,
            "auth_method": "otp",
            "password_invalid": False,
            "roxybrowser": {"profile_id": "profile-worker"},
        }
        with patch("core.roxy_registration.run_roxy_at_refresh", return_value=result) as run, patch(
            "core.plan_check_service.enqueue_account_plan_check", return_value={"accepted": True}
        ) as enqueue:
            service._run_at_refresh_job(job["id"], job["log_file"], "user@example.com", 1)
        row = self._account()
        self.assertEqual(row["access_token"], "new-at")
        self.assertEqual(row["user_id"], "user-new")
        self.assertEqual(row["at_refresh_status"], "success")
        self.assertEqual(db.get_job(job["id"])["status"], "success")
        run.assert_called_once_with("user@example.com", registration_password="saved-password")
        enqueue.assert_called_once_with(
            account_id=1, email="user@example.com", access_token="new-at", trigger="at_refresh"
        )
        self.assertIsNone(otp_operation_lock.owner("user@example.com"))
        log_text = Path(job["log_file"]).read_text(encoding="utf-8")
        self.assertNotIn("new-at", log_text)
        self.assertNotIn("saved-password", log_text)

    def test_worker_failure_preserves_old_token_and_releases_owner(self):
        job = db.create_job("outlook", job_type="at_refresh", email="user@example.com", account_id=1)
        self.assertTrue(db.claim_account_at_refresh(1, job["id"]))
        self.assertTrue(otp_operation_lock.reserve("user@example.com", "at_refresh"))
        with patch("core.roxy_registration.run_roxy_at_refresh", side_effect=RuntimeError("network failed")):
            service._run_at_refresh_job(job["id"], job["log_file"], "user@example.com", 1)
        row = self._account()
        self.assertEqual(row["access_token"], "old-at")
        self.assertEqual(row["at_refresh_status"], "failed")
        self.assertEqual(db.get_job(job["id"])["status"], "failed")
        self.assertIsNone(otp_operation_lock.owner("user@example.com"))

    def test_submit_creates_bound_background_job(self):
        executor = MagicMock()
        with patch.object(service, "get_executor", return_value=executor):
            result = service.submit_account_at_refresh(1, workers=2)
        self.assertTrue(result["ok"])
        job = result["job"]
        self.assertEqual(job["job_type"], "at_refresh")
        self.assertEqual(job["account_id"], 1)
        self.assertEqual(job["email"], "user@example.com")
        executor.submit.assert_called_once()
        self.assertEqual(otp_operation_lock.owner("user@example.com"), "at_refresh")

    def test_submit_rejects_codex_retrying_account(self):
        rows = json.loads(self.paths["_ACCOUNTS_JSON"].read_text(encoding="utf-8"))
        rows[0]["codex_status"] = "retrying"
        self.paths["_ACCOUNTS_JSON"].write_text(json.dumps(rows), encoding="utf-8")
        result = service.submit_account_at_refresh(1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 409)
        self.assertIn("Codex", result["error"])


class RoxyAtRefreshTests(unittest.TestCase):
    def test_registration_rejects_login_password_but_refresh_allows_it(self):
        with patch.object(roxy_registration, "_type_email_address"), patch.object(
            roxy_registration, "_email_input_value_state", return_value={"inputs": [{"value": "user@example.com"}]}
        ), patch.object(roxy_registration, "_submit_email_step"), patch.object(
            roxy_registration, "_wait_email_submit_next_state", return_value="login_password"
        ), patch.object(roxy_registration.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "已注册/不可用"):
                roxy_registration._submit_email_and_wait_next(MagicMock(), "user@example.com", attempts=1)
            self.assertEqual(
                roxy_registration._submit_email_and_wait_next(
                    MagicMock(), "user@example.com", attempts=1, allow_existing_login=True
                ),
                "login_password",
            )

    def test_password_error_falls_back_to_otp_and_returns_normalized_session(self):
        opened = MagicMock(profile_id="profile-1", created_by_run=True)
        client = MagicMock()
        client.open_profile.return_value = opened
        driver = MagicMock()
        payload = session_payload()
        with patch.object(roxy_registration, "RoxyBrowserClient", return_value=client), patch.object(
            roxy_registration, "_build_driver", return_value=driver
        ), patch.object(roxy_registration, "_center_browser_window"), patch.object(
            roxy_registration, "_clear_roxy_auth_state"
        ), patch.object(roxy_registration, "_maybe_accept"), patch.object(
            roxy_registration, "_check_manual_stop"
        ), patch.object(roxy_registration, "_submit_email_and_wait_next", return_value="login_password"), patch.object(
            roxy_registration, "_submit_existing_login_password", return_value="invalid"
        ), patch.object(roxy_registration, "_switch_to_passwordless_login", return_value="otp"), patch.object(
            roxy_registration, "_complete_email_otp"
        ) as otp, patch.object(roxy_registration, "_page_snapshot", return_value={}), patch.object(
            roxy_registration, "_is_profile_like", return_value=False
        ), patch.object(roxy_registration, "_fetch_chatgpt_session", return_value=payload), patch.object(
            roxy_registration, "human_delay"
        ), patch.object(roxy_registration.time, "sleep"):
            result = roxy_registration.run_roxy_at_refresh(
                "user@example.com", registration_password="wrong-password"
            )
        self.assertTrue(result["success"])
        self.assertTrue(result["password_invalid"])
        self.assertEqual(result["auth_method"], "otp")
        self.assertEqual(result["normalized"]["access_token"], "new-at")
        otp.assert_called_once()
        driver.quit.assert_called_once()
        client.cleanup_profile.assert_called_once_with(opened)

    def test_password_submit_recognizes_success_and_extra_otp(self):
        driver = MagicMock()
        password_input = MagicMock()
        driver.find_elements.return_value = [password_input]
        driver.execute_script.return_value = MagicMock()
        common = [
            patch.object(roxy_registration, "_visible", return_value=True),
            patch.object(roxy_registration, "_type_element"),
            patch.object(roxy_registration, "_native_click"),
            patch.object(roxy_registration, "_check_manual_stop"),
            patch.object(roxy_registration.time, "sleep"),
        ]
        for item in common:
            item.start()
        try:
            with patch.object(roxy_registration, "_has_access_token", return_value=True):
                self.assertEqual(
                    roxy_registration._submit_existing_login_password(driver, "password"), "logged_in"
                )
            with patch.object(roxy_registration, "_has_access_token", return_value=False), patch.object(
                roxy_registration, "_is_email_verification_page", return_value=True
            ):
                self.assertEqual(
                    roxy_registration._submit_existing_login_password(driver, "password"), "otp"
                )
        finally:
            for item in reversed(common):
                item.stop()

    def test_missing_password_uses_passwordless_otp_without_password_submit(self):
        opened = MagicMock(profile_id="profile-2", created_by_run=True)
        client = MagicMock()
        client.open_profile.return_value = opened
        driver = MagicMock()
        payload = session_payload()
        with patch.object(roxy_registration, "RoxyBrowserClient", return_value=client), patch.object(
            roxy_registration, "_build_driver", return_value=driver
        ), patch.object(roxy_registration, "_center_browser_window"), patch.object(
            roxy_registration, "_clear_roxy_auth_state"
        ), patch.object(roxy_registration, "_maybe_accept"), patch.object(
            roxy_registration, "_check_manual_stop"
        ), patch.object(roxy_registration, "_submit_email_and_wait_next", return_value="login_password"), patch.object(
            roxy_registration, "_submit_existing_login_password"
        ) as password_submit, patch.object(
            roxy_registration, "_switch_to_passwordless_login", return_value="otp"
        ) as passwordless, patch.object(roxy_registration, "_complete_email_otp") as otp, patch.object(
            roxy_registration, "_page_snapshot", return_value={}
        ), patch.object(roxy_registration, "_is_profile_like", return_value=False), patch.object(
            roxy_registration, "_fetch_chatgpt_session", return_value=payload
        ), patch.object(roxy_registration, "human_delay"), patch.object(roxy_registration.time, "sleep"):
            result = roxy_registration.run_roxy_at_refresh("user@example.com")
        self.assertEqual(result["auth_method"], "otp")
        password_submit.assert_not_called()
        passwordless.assert_called_once_with(driver)
        otp.assert_called_once()

    def test_non_password_failure_does_not_fall_back(self):
        driver = MagicMock()
        password_input = MagicMock()
        driver.find_elements.return_value = [password_input]
        driver.execute_script.return_value = MagicMock()
        with patch.object(roxy_registration, "_visible", return_value=True), patch.object(
            roxy_registration, "_type_element"
        ), patch.object(roxy_registration, "_native_click"), patch.object(
            roxy_registration, "_check_manual_stop"
        ), patch.object(roxy_registration, "_has_access_token", return_value=False), patch.object(
            roxy_registration, "_is_email_verification_page", return_value=False
        ), patch.object(roxy_registration, "_login_password_explicitly_rejected", return_value=False), patch.object(
            roxy_registration.time, "time", side_effect=[0, 0, 31]
        ), patch.object(roxy_registration.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "页面状态超时"):
                roxy_registration._submit_existing_login_password(driver, "password", timeout=30)


class AtRefreshServiceAndApiTests(unittest.TestCase):
    def tearDown(self):
        otp_operation_lock.clear()

    def test_codex_reserve_rejects_at_refresh_owner(self):
        self.assertTrue(otp_operation_lock.reserve("user@example.com", "at_refresh"))
        self.assertFalse(codex_retry_service.reserve("user@example.com"))

    @patch("webui.app.db.recover_interrupted_at_refreshes", return_value=0)
    @patch("webui.app.db.recover_interrupted_at_refresh_jobs", return_value=0)
    @patch("webui.app.db.recover_interrupted_codex_agents", return_value=0)
    @patch("webui.app.db.recover_interrupted_extract_links", return_value=0)
    @patch("webui.app.db.recover_interrupted_plan_checks", return_value=0)
    @patch("webui.app.svc.submit_account_at_refresh")
    def test_api_trusts_only_path_account_id(self, submit, *_recover):
        submit.return_value = {
            "ok": True,
            "job": {"id": 12, "job_type": "at_refresh", "account_id": 7, "status": "pending"},
        }
        client = create_app(auth_code="test-auth").test_client()
        response = client.post(
            "/api/accounts/7/refresh-at",
            json={"email": "other@example.com", "access_token": "secret-at", "password": "secret-password"},
            headers={"X-Auth-Code": "test-auth"},
        )
        self.assertEqual(response.status_code, 200)
        submit.assert_called_once_with(7)
        body = response.get_data(as_text=True)
        self.assertNotIn("secret-at", body)
        self.assertNotIn("secret-password", body)

    @patch("webui.app.db.recover_interrupted_at_refreshes", return_value=0)
    @patch("webui.app.db.recover_interrupted_at_refresh_jobs", return_value=0)
    @patch("webui.app.db.recover_interrupted_codex_agents", return_value=0)
    @patch("webui.app.db.recover_interrupted_extract_links", return_value=0)
    @patch("webui.app.db.recover_interrupted_plan_checks", return_value=0)
    @patch("webui.app.svc.submit_account_at_refresh")
    def test_bulk_api_starts_each_unique_account_and_reports_skips(self, submit, *_recover):
        submit.side_effect = [
            {"ok": True, "job": {"id": 21, "job_type": "at_refresh", "account_id": 7, "status": "pending"}},
            {"ok": False, "status": 409, "error": "该账号已有 AT 刷新任务"},
        ]
        client = create_app(auth_code="test-auth").test_client()
        response = client.post(
            "/api/accounts/refresh-at-bulk",
            json={
                "account_ids": [7, 7, "bad", 8],
                "access_token": "untrusted-at",
                "password": "untrusted-password",
            },
            headers={"X-Auth-Code": "test-auth"},
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(submit.call_args_list, [call(7), call(8)])
        payload = response.get_json()
        self.assertEqual(payload["started_count"], 1)
        self.assertEqual(payload["skipped_count"], 2)
        body = response.get_data(as_text=True)
        self.assertNotIn("untrusted-at", body)
        self.assertNotIn("untrusted-password", body)

    def test_templates_expose_at_refresh_without_sensitive_fields(self):
        template_dir = Path(__file__).parents[1] / "webui" / "templates"
        for name in ("index.html", "index_legacy.html"):
            with self.subTest(template=name):
                source = (template_dir / name).read_text(encoding="utf-8")
                self.assertIn("data-at-refresh", source)
                self.assertIn("data-at-refresh-log", source)
                self.assertIn("data-at-refresh-stop", source)
                self.assertIn("/refresh-at", source)
                self.assertIn("refresh-at-bulk", source)
                self.assertIn("批量重新获取 AT", source)
                self.assertNotIn("registration_password", source)


if __name__ == "__main__":
    unittest.main()
