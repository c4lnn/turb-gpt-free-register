# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from config import checkout_session as checkout_cfg
from core import db, plan_check_service
from webui import app as web_app


class CheckoutSessionWebUiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.accounts_path = Path(self.tempdir.name) / "accounts.json"
        self.accounts_path.write_text(json.dumps([{
            "id": 7,
            "email": "ui@example.invalid",
            "access_token": "at-ui-secret",
            "plan_type": "free",
        }]), encoding="utf-8")
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(db, "_ACCOUNTS_JSON", self.accounts_path))
        self.stack.enter_context(patch.object(db, "_render_static_viewer"))
        for name in (
            "recover_interrupted_plan_checks",
            "recover_interrupted_checkout_sessions",
            "recover_interrupted_extract_links",
            "recover_interrupted_live_checks",
            "recover_interrupted_codex_agents",
        ):
            self.stack.enter_context(patch.object(web_app.db, name, return_value=0))
        self.enqueue = self.stack.enter_context(
            patch.object(web_app.checkout_session_service, "enqueue_checkout_session_check", return_value={
                "accepted": True,
                "busy": False,
                "account_id": 7,
                "status": "queued",
                "trigger": "manual",
            })
        )
        self.client = web_app.create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    def tearDown(self):
        self.stack.close()
        self.tempdir.cleanup()

    def test_manual_api_reads_token_server_side_and_returns_accepted_without_secret(self):
        response = self.client.post(
            "/api/accounts/check-checkout-session",
            json={"account_id": 7, "access_token": "attacker-supplied"},
            headers={"X-Auth-Code": "test-auth"},
        )
        self.assertEqual(response.status_code, 202)
        body = response.get_json()
        self.assertTrue(body["ok"])
        encoded = json.dumps(body, ensure_ascii=False)
        self.assertNotIn("at-ui-secret", encoded)
        self.assertNotIn("attacker-supplied", encoded)
        self.assertNotIn("proxy", body)
        self.enqueue.assert_called_once()
        self.assertEqual(self.enqueue.call_args.kwargs["access_token"], "at-ui-secret")

    def test_busy_is_409_and_missing_token_is_400(self):
        self.enqueue.return_value = {"accepted": False, "busy": True, "error": "正在检测"}
        response = self.client.post(
            "/api/accounts/check-checkout-session",
            json={"account_id": 7},
            headers={"X-Auth-Code": "test-auth"},
        )
        self.assertEqual(response.status_code, 409)

        row = json.loads(self.accounts_path.read_text(encoding="utf-8"))[0]
        row["access_token"] = ""
        self.accounts_path.write_text(json.dumps([row]), encoding="utf-8")
        response = self.client.post(
            "/api/accounts/check-checkout-session",
            json={"account_id": 7},
            headers={"X-Auth-Code": "test-auth"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.enqueue.call_count, 1)

    def test_bulk_api_deduplicates_reads_tokens_server_side_and_reports_skips(self):
        rows = json.loads(self.accounts_path.read_text(encoding="utf-8"))
        rows.extend([
            {"id": 8, "email": "second@example.invalid", "access_token": "at-second"},
            {"id": 9, "email": "missing@example.invalid", "access_token": ""},
        ])
        self.accounts_path.write_text(json.dumps(rows), encoding="utf-8")

        def enqueue(**kwargs):
            return {
                "accepted": True,
                "busy": False,
                "account_id": kwargs["account_id"],
                "email": kwargs["email"],
                "status": "queued",
                "trigger": kwargs["trigger"],
            }

        self.enqueue.side_effect = enqueue
        response = self.client.post(
            "/api/accounts/check-checkout-session-bulk",
            json={"account_ids": [7, 8, 7, 9, 404, "bad"]},
            headers={"X-Auth-Code": "test-auth"},
        )

        self.assertEqual(response.status_code, 202)
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["started_count"], 2)
        self.assertEqual(body["skipped_count"], 3)
        self.assertEqual(self.enqueue.call_count, 2)
        self.assertEqual(
            [call.kwargs["access_token"] for call in self.enqueue.call_args_list],
            ["at-ui-secret", "at-second"],
        )
        self.assertTrue(all(call.kwargs["trigger"] == "manual_bulk" for call in self.enqueue.call_args_list))
        encoded = json.dumps(body, ensure_ascii=False)
        self.assertNotIn("at-ui-secret", encoded)
        self.assertNotIn("at-second", encoded)

    def test_bulk_api_rejects_empty_or_oversized_ids(self):
        empty = self.client.post(
            "/api/accounts/check-checkout-session-bulk",
            json={"account_ids": []},
            headers={"X-Auth-Code": "test-auth"},
        )
        self.assertEqual(empty.status_code, 400)

        oversized = self.client.post(
            "/api/accounts/check-checkout-session-bulk",
            json={"account_ids": list(range(501))},
            headers={"X-Auth-Code": "test-auth"},
        )
        self.assertEqual(oversized.status_code, 400)

    def test_legacy_account_list_is_compact_and_hides_checkout_secrets(self):
        rows = json.loads(self.accounts_path.read_text(encoding="utf-8"))
        rows[0].update({
            "checkout_session_id": "oaics_fixture_full_id",
            "checkout_check_result_json": json.dumps({
                "client_secret": "client_secret_fixture",
                "customer_session": "customer_session_fixture",
            }),
            "checkout_check_proxy_used": "http://proxy-user:proxy-pass@proxy.example:8080",
        })
        self.accounts_path.write_text(json.dumps(rows), encoding="utf-8")

        response = self.client.get(
            "/api/accounts?limit=20",
            headers={"X-Auth-Code": "test-auth"},
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        encoded = json.dumps(body, ensure_ascii=False)
        self.assertNotIn("access_token", body[0])
        self.assertNotIn("checkout_session_id", encoded)
        self.assertNotIn("at-ui-secret", encoded)
        self.assertNotIn("oaics_fixture_full_id", encoded)
        self.assertNotIn("client_secret_fixture", encoded)
        self.assertNotIn("proxy-user", encoded)

        status_response = self.client.get(
            "/api/accounts/plan-check-status",
            headers={"X-Auth-Code": "test-auth"},
        )
        status_encoded = json.dumps(status_response.get_json(), ensure_ascii=False)
        self.assertNotIn("at-ui-secret", status_encoded)
        self.assertNotIn("oaics_fixture_full_id", status_encoded)


class CheckoutAutoTriggerTests(unittest.TestCase):
    def test_only_final_registration_eligibility_enqueues_once(self):
        eligible = {
            "ok": True,
            "current_plan_type": "free",
            "plus_trial_eligible": True,
            "plus_trial_campaign_id": "plus-1-month-free",
            "plus_trial_discount_percentage": 100,
        }
        with patch.object(checkout_cfg, "CHECKOUT_SESSION_AUTO_CHECK", True), patch(
            "core.checkout_session_service.enqueue_checkout_session_check",
            return_value={"accepted": True},
        ) as enqueue:
            plan_check_service._maybe_enqueue_checkout_session(
                account_id=1,
                email="auto@example.invalid",
                access_token="at-secret",
                trigger="registration_auto",
                result=eligible,
            )
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.kwargs["trigger"], "registration_auto")

    def test_manual_or_incomplete_eligibility_never_enqueues(self):
        incomplete = {
            "ok": True,
            "current_plan_type": "free",
            "plus_trial_eligible": True,
            "plus_trial_campaign_id": "plus-1-month-free",
        }
        with patch.object(checkout_cfg, "CHECKOUT_SESSION_AUTO_CHECK", True), patch(
            "core.checkout_session_service.enqueue_checkout_session_check"
        ) as enqueue:
            for trigger, result in (("manual", incomplete), ("registration_auto", incomplete), ("manual_bulk", {
                **incomplete,
                "plus_trial_discount_percentage": 100,
            })):
                plan_check_service._maybe_enqueue_checkout_session(
                    account_id=1,
                    email="auto@example.invalid",
                    access_token="at-secret",
                    trigger=trigger,
                    result=result,
                )
        enqueue.assert_not_called()

    def test_auto_enqueue_exception_log_does_not_include_secret_text(self):
        eligible = {
            "ok": True,
            "current_plan_type": "free",
            "plus_trial_eligible": True,
            "plus_trial_campaign_id": "plus-1-month-free",
            "plus_trial_discount_percentage": 100,
        }
        with patch.object(checkout_cfg, "CHECKOUT_SESSION_AUTO_CHECK", True), patch(
            "core.checkout_session_service.enqueue_checkout_session_check",
            side_effect=RuntimeError("at-secret oaics_full_sensitive_id http://proxy-user:proxy-pass@example.invalid"),
        ), self.assertLogs("core.plan_check_service", level="WARNING") as captured:
            plan_check_service._maybe_enqueue_checkout_session(
                account_id=1,
                email="auto@example.invalid",
                access_token="at-secret",
                trigger="registration_auto",
                result=eligible,
            )
        output = "\n".join(captured.output)
        self.assertNotIn("at-secret", output)
        self.assertNotIn("oaics_full_sensitive_id", output)
        self.assertNotIn("proxy-user", output)


if __name__ == "__main__":
    unittest.main()
