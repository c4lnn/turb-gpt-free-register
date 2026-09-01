import unittest
import json
import tempfile
import os
from pathlib import Path
from unittest.mock import patch

from core import db
from core.sqlite_store import SQLiteRuntimeStore
from flask import Flask
from webui.app import _account_status_filter_args
from core.account_status_contracts import (
    build_account_status_contract,
    classify_checkout_session_type,
    classify_plan_category,
    extract_link_capabilities,
    normalize_codex_auth_status,
    normalize_extract_link_status,
    normalize_plan_query_status,
    plan_capabilities,
    checkout_capabilities,
    normalize_checkout_query_status,
)
from webui.app import _compact_account_for_list


class AccountStatusContractTests(unittest.TestCase):
    def test_plan_categories_are_stable_and_require_known_trial_flag(self):
        base = {"current_plan_type": "free", "trial_eligibility_known": True}
        self.assertEqual(classify_plan_category({**base, "plus_trial_eligible": True}), "free_trial_eligible")
        self.assertEqual(classify_plan_category({**base, "plus_trial_eligible": False}), "free_no_trial")
        self.assertEqual(classify_plan_category({"current_plan_type": "plus"}), "paid")
        self.assertEqual(classify_plan_category({"current_plan_type": "free"}), "unknown")
        self.assertEqual(classify_plan_category({"current_plan_type": "new_plan"}), "unknown")

    def test_plan_query_states_are_separate_from_category(self):
        self.assertEqual(normalize_plan_query_status("queued"), "queued")
        self.assertEqual(normalize_plan_query_status("running"), "running")
        self.assertEqual(normalize_plan_query_status("failed"), "failed")
        self.assertEqual(normalize_plan_query_status("future_state"), "unknown")
        self.assertTrue(plan_capabilities("free_no_trial", "running")["is_checking"])
        self.assertFalse(plan_capabilities("unknown", "failed")["is_eligible"])
        self.assertEqual(classify_plan_category({
            "current_plan_type": "free", "trial_eligibility_known": True,
            "plus_trial_eligible": False, "plan_check_status": "failed",
        }), "unknown")
        self.assertEqual(classify_plan_category({
            "current_plan_type": "free", "trial_eligibility_known": True,
            "plus_trial_eligible": False, "plan_check_status": "failed",
            "plan_last_success_at": "2026-08-01T00:00:00",
        }), "free_no_trial")

    def test_checkout_query_state_is_separate_from_session_type(self):
        self.assertEqual(normalize_checkout_query_status("running"), "running")
        self.assertEqual(normalize_checkout_query_status("future_state"), "unknown")
        self.assertTrue(checkout_capabilities("queued")["is_checking"])

    def test_codex_legacy_operation_values_do_not_become_auth_facts(self):
        self.assertEqual(normalize_codex_auth_status("retrying"), "unknown")
        self.assertEqual(normalize_codex_auth_status("deactivated"), "unknown")
        contract = build_account_status_contract({"codex_status": "retrying"})
        self.assertEqual(contract["codex_auth_status"], "unknown")
        self.assertEqual(contract["codex_operation_status"], "running")

    def test_extract_legacy_cancel_aliases_and_unknown_are_safe(self):
        self.assertEqual(normalize_extract_link_status("cancelled"), "canceled")
        self.assertEqual(normalize_extract_link_status("stopped"), "canceled")
        self.assertEqual(normalize_extract_link_status("future_state"), "unknown")
        self.assertFalse(extract_link_capabilities("future_state")["can_retry"])

    def test_extract_contract_normalizes_cancelled_write_result(self):
        self.assertEqual(normalize_extract_link_status("cancelled"), "canceled")

    def test_checkout_session_types_use_one_classifier(self):
        self.assertEqual(classify_checkout_session_type("oaics_123"), "oaics")
        self.assertEqual(classify_checkout_session_type("cs_live_123"), "cs_live")
        self.assertEqual(classify_checkout_session_type("cs_test_123"), "other_cs")
        self.assertEqual(classify_checkout_session_type(""), "unknown")

    def test_contract_contains_capabilities_and_no_input_secrets(self):
        row = {
            "current_plan_type": "free",
            "trial_eligibility_known": True,
            "plus_trial_eligible": False,
            "access_token": "secret",
            "password": "secret",
        }
        result = build_account_status_contract(row)
        self.assertEqual(result["plan_category_code"], "free_no_trial")
        self.assertIn("can_start", result["plan_capabilities"])
        self.assertNotIn("access_token", result)
        self.assertNotIn("password", result)

    def test_account_list_dto_excludes_credentials_and_raw_checkout_payload(self):
        result = _compact_account_for_list({
            "id": 1,
            "email": "fixture@example.invalid",
            "access_token": "token-fixture",
            "password": "password-fixture",
            "totp_secret": "totp-fixture",
            "checkout_session_id": "cs_live_fixture",
            "checkout_check_result_json": '{"secret":"fixture"}',
            "current_plan_type": "free",
            "trial_eligibility_known": True,
            "plus_trial_eligible": False,
        })
        for key in (
            "access_token", "password", "totp_secret", "checkout_session_id",
            "checkout_check_result_json",
        ):
            self.assertNotIn(key, result)
        self.assertEqual(result["plan_category_code"], "free_no_trial")

    def test_codex_operation_recovery_does_not_change_authorization_fact(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "accounts.json"
            path.write_text(json.dumps([{
                "id": 1,
                "email": "fixture@example.invalid",
                "codex_status": "retrying",
                "codex_auth_status": "failed",
                "codex_operation_status": "running",
            }]), encoding="utf-8")
            with patch.object(db, "_ACCOUNTS_JSON", path):
                self.assertEqual(db.recover_interrupted_codex_operations(), 1)
            row = json.loads(path.read_text(encoding="utf-8"))[0]
            self.assertEqual(row["codex_auth_status"], "failed")
            self.assertEqual(row["codex_operation_status"], "failed")
            self.assertEqual(row["codex_status"], "retrying")

    def test_live_check_capabilities_keep_deactivated_separate(self):
        contract = build_account_status_contract({
            "codex_status": "success",
            "live_check_status": "deactivated",
        })
        self.assertEqual(contract["codex_auth_status"], "success")
        self.assertFalse(contract["live_check_capabilities"]["account_available"])
        self.assertTrue(contract["live_check_capabilities"]["is_terminal"])

    def test_codex_operation_write_does_not_overwrite_authorization_legacy_field(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "accounts.json"
            path.write_text(json.dumps([{
                "id": 1, "email": "fixture@example.invalid", "codex_status": "failed",
            }]), encoding="utf-8")
            with patch.object(db, "_ACCOUNTS_JSON", path):
                self.assertTrue(db.update_account_codex_status("fixture@example.invalid", "retrying"))
            row = json.loads(path.read_text(encoding="utf-8"))[0]
            self.assertEqual(row["codex_status"], "failed")
            self.assertEqual(row["codex_operation_status"], "running")

    def test_account_status_migration_is_idempotent_and_preserves_legacy_raw(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "accounts.json"
            path.write_text(json.dumps([{
                "id": 1, "email": "fixture@example.invalid", "codex_status": "retrying",
            }]), encoding="utf-8")
            with patch.object(db, "_ACCOUNTS_JSON", path):
                first = db.migrate_account_status_contracts()
                second = db.migrate_account_status_contracts()
            row = json.loads(path.read_text(encoding="utf-8"))[0]
            self.assertEqual(first["changed"], 1)
            self.assertEqual(second["changed"], 0)
            self.assertEqual(row["codex_status_legacy_raw"], "retrying")
            self.assertEqual(row["codex_auth_status"], "unknown")
            self.assertEqual(row["codex_operation_status"], "running")

    def test_account_status_migration_uses_same_contract_for_sqlite(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = SQLiteRuntimeStore(Path(tempdir) / "runtime.db")
            store.initialize()
            store.replace_all("accounts", [{
                "id": 1, "email": "sqlite@example.invalid", "codex_status": "stopped",
            }])
            with patch.dict(os.environ, {"RUNTIME_STORAGE_BACKEND": "sqlite"}), patch.object(db, "_SQLITE_STORE", store):
                first = db.migrate_account_status_contracts()
                second = db.migrate_account_status_contracts()
            row = store.load("accounts")[0]
            self.assertEqual(first["changed"], 1)
            self.assertEqual(second["changed"], 0)
            self.assertEqual(row["codex_auth_status"], "unknown")
            self.assertEqual(row["codex_operation_status"], "canceled")

    def test_invalid_legacy_codex_filter_is_rejected(self):
        app = Flask(__name__)
        with app.test_request_context("/?codex_status=future_state"):
            _, error = _account_status_filter_args()
        self.assertEqual(error, "codex_status 非法")

    def test_explicit_status_filter_dimensions_are_preserved(self):
        app = Flask(__name__)
        with app.test_request_context(
            "/?plan_category=paid&codex_auth_status=failed&codex_operation_status=success&live_check_status=live"
        ):
            filters, error = _account_status_filter_args()
        self.assertIsNone(error)
        self.assertEqual(filters["plan_filter"], "paid")
        self.assertEqual(filters["codex_auth_status_filter"], "failed")
        self.assertEqual(filters["codex_operation_status_filter"], "success")
        self.assertEqual(filters["live_check_status_filter"], "live")
        self.assertEqual(filters["codex_status_filter"], "")

    def test_legacy_and_explicit_status_filters_cannot_be_mixed(self):
        app = Flask(__name__)
        with app.test_request_context("/?codex_status=retrying&codex_operation_status=running"):
            _, error = _account_status_filter_args()
        self.assertEqual(error, "codex_status 不能与新状态筛选参数同时提交")

    def test_frontend_uses_central_labels_machine_codes_and_capabilities(self):
        template = (Path(__file__).parents[1] / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn("const ACCOUNT_STATUS_LABELS", template)
        self.assertIn("return ACCOUNT_STATUS_LABELS[domain]", template)
        self.assertIn("plan_category_code", template)
        self.assertIn("r.codex_capabilities || {}", template)
        self.assertIn("r.plan_capabilities?.is_checking", template)
        self.assertIn("r.checkout_capabilities?.is_checking", template)
        self.assertIn("plan_category=${encodeURIComponent(planCategory)}", template)
        self.assertIn("codex_auth_status=${encodeURIComponent(codexAuthStatus)}", template)
        self.assertIn("return ACCOUNT_STATUS_LABELS[domain]?.[String(code || '').toLowerCase()] || '未知';", template)
        self.assertNotIn("acc.codex_status !== 'retrying'", template)
        self.assertNotIn("(a.codex_status || '') === 'retrying'", template)
        self.assertNotIn("(a.codex_status || '') === 'deactivated'", template)
        self.assertNotIn("plan !== 'free' || !acc.plus_trial_eligible", template)
        self.assertNotIn("a.current_plan_type || a.plan_type", template)
        self.assertNotIn("r.plan_result_class ===", template)
        self.assertNotIn("['oaics', 'cs_live', 'other_cs', 'unknown'].includes", template)
        self.assertNotIn("codex_status=${encodeURIComponent(codexStatus)}", template)
        self.assertIn("status === 'success' && type !== 'unknown'", template)


if __name__ == "__main__":
    unittest.main()
