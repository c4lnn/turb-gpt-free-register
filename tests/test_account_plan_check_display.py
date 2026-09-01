# -*- coding: utf-8 -*-
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from core import db
from core.sqlite_store import SQLiteRuntimeStore
from webui.app import _compact_account_for_list
from webui import app as web_app


class AccountPlanCheckDbTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.accounts_path = Path(self.tempdir.name) / "accounts.json"
        self.accounts_path.write_text(json.dumps([{
            "id": 1,
            "email": "plan@example.invalid",
            "plan_type": "plus",
            "current_plan_type": "plus",
            "updated_at": "2026-08-07T00:00:00",
        }]), encoding="utf-8")
        self.path_patch = patch.object(db, "_ACCOUNTS_JSON", self.accounts_path)
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.tempdir.cleanup()

    def _row(self):
        return json.loads(self.accounts_path.read_text(encoding="utf-8"))[0]

    def test_plan_check_updated_at_tracks_each_lifecycle_state_only(self):
        with patch.object(db, "_now", return_value="2026-08-07T01:00:00"):
            self.assertTrue(db.claim_account_plan_check(acc_id=1))
        self.assertEqual(self._row()["plan_check_updated_at"], "2026-08-07T01:00:00")

        with patch.object(db, "_now", return_value="2026-08-07T01:00:01"):
            self.assertTrue(db.mark_account_plan_check_running(1))
        self.assertEqual(self._row()["plan_check_updated_at"], "2026-08-07T01:00:01")

        with patch.object(db, "_now", return_value="2026-08-07T01:00:02"):
            self.assertTrue(db.update_account_plan_check(acc_id=1, result={
                "ok": True,
                "checked_at": "2026-08-07T01:00:02",
                "current_plan_type": "plus",
                "billing_currency": "USD",
            }))
        self.assertEqual(self._row()["plan_check_updated_at"], "2026-08-07T01:00:02")

        with patch.object(db, "_now", return_value="2026-08-07T02:00:00"):
            self.assertTrue(db.update_account_note(1, "unrelated"))
        self.assertEqual(self._row()["plan_check_updated_at"], "2026-08-07T01:00:02")

    def test_failed_and_recovered_checks_refresh_plan_timestamp(self):
        with patch.object(db, "_now", return_value="2026-08-07T03:00:00"):
            self.assertTrue(db.update_account_plan_check(
                acc_id=1,
                result={
                    "ok": False,
                    "error": "network error",
                    "plan_check_error_kind": "network_connection",
                },
            ))
        row = self._row()
        self.assertEqual(row["plan_check_status"], "failed")
        self.assertEqual(row["plan_check_error_kind"], "network_connection")
        self.assertEqual(row["plan_check_updated_at"], "2026-08-07T03:00:00")

        row["plan_check_status"] = "running"
        self.accounts_path.write_text(json.dumps([row]), encoding="utf-8")
        with patch.object(db, "_now", return_value="2026-08-07T03:00:01"):
            self.assertEqual(db.recover_interrupted_plan_checks(), 1)
        recovered = self._row()
        self.assertEqual(recovered["plan_check_status"], "failed")
        self.assertIsNone(recovered["plan_check_error_kind"])
        self.assertEqual(recovered["plan_check_updated_at"], "2026-08-07T03:00:01")

    def test_local_expiry_result_is_persisted_without_fabricated_http_status(self):
        row = self._row()
        row.update({
            "current_plan_type": "plus",
            "plan_last_success_at": "2026-08-06T23:00:00",
            "plan_check_status": "success",
            "plan_check_ok": True,
        })
        self.accounts_path.write_text(json.dumps([row]), encoding="utf-8")

        with patch.object(db, "_now", return_value="2026-08-07T06:00:00"):
            self.assertTrue(db.update_account_plan_check(acc_id=1, result={
                "ok": False,
                "checked_at": "2026-08-07T06:00:00",
                "http_status": None,
                "plan_check_error_kind": "local_token_expired",
                "error": "本地AT已失效，请手动查活刷新",
                "token_expired": True,
                "token_expires_at": "2026-08-07T05:59:00Z",
                "needs_live_check": True,
            }))

        persisted = self._row()
        self.assertEqual(persisted["plan_check_status"], "failed")
        self.assertFalse(persisted["plan_check_ok"])
        self.assertEqual(persisted["plan_check_error_kind"], "local_token_expired")
        self.assertIsNone(persisted["plan_check_http_status"])
        self.assertEqual(persisted["plan_check_error"], "本地AT已失效，请手动查活刷新")
        self.assertTrue(persisted["token_expired"])
        self.assertEqual(persisted["token_expires_at"], "2026-08-07T05:59:00Z")
        self.assertTrue(persisted["needs_live_check"])
        self.assertEqual(json.loads(persisted["plan_check_result_json"])["plan_check_error_kind"], "local_token_expired")
        self.assertEqual(persisted["current_plan_type"], "plus")
        self.assertEqual(persisted["plan_last_success_at"], "2026-08-06T23:00:00")

    def test_historical_local_expiry_compatibility_is_read_only_and_401_wins(self):
        row = self._row()
        row.update({
            "plan_check_status": "failed",
            "plan_check_ok": False,
            "plan_check_error": "AT已过期/失效，请手动查活刷新",
            "plan_check_http_status": None,
            "plan_check_updated_at": "2026-08-07T07:00:00",
        })
        self.accounts_path.write_text(json.dumps([row]), encoding="utf-8")
        before = self.accounts_path.read_text(encoding="utf-8")

        decorated = db.get_account(1)
        snapshot = db.list_account_plan_check_statuses()

        self.assertEqual(decorated["plan_check_error_kind"], "local_token_expired")
        self.assertEqual(snapshot["items"][0]["plan_check_error_kind"], "local_token_expired")
        self.assertEqual(self.accounts_path.read_text(encoding="utf-8"), before)

        row["plan_check_http_status"] = 401
        self.accounts_path.write_text(json.dumps([row]), encoding="utf-8")
        before_401 = self.accounts_path.read_text(encoding="utf-8")
        decorated_401 = db.get_account(1)
        snapshot_401 = db.list_account_plan_check_statuses()
        self.assertEqual(decorated_401["plan_check_error_kind"], "http_4xx")
        self.assertEqual(snapshot_401["items"][0]["plan_check_error_kind"], "http_4xx")
        self.assertEqual(snapshot_401["items"][0]["plan_check_http_status"], 401)
        self.assertEqual(self.accounts_path.read_text(encoding="utf-8"), before_401)

        row["plan_check_http_status"] = None
        row["plan_check_error"] = "请求失败，请稍后重试"
        self.accounts_path.write_text(json.dumps([row]), encoding="utf-8")
        unknown = db.get_account(1)
        self.assertNotIn("plan_check_error_kind", unknown)

    def test_sqlite_historical_read_preserves_payload_and_status_contract(self):
        with tempfile.TemporaryDirectory() as tempdir:
            sqlite_path = Path(tempdir) / "runtime.db"
            store = SQLiteRuntimeStore(sqlite_path)
            store.initialize()
            row = {
                "id": 1,
                "email": "sqlite-plan@example.invalid",
                "current_plan_type": "plus",
                "plan_last_success_at": "2026-08-06T23:00:00",
                "plan_check_status": "failed",
                "plan_check_ok": False,
                "plan_check_error": "AT已过期/失效，请手动查活刷新",
                "plan_check_http_status": None,
                "plan_check_updated_at": "2026-08-07T08:00:00",
            }
            store.replace_all("accounts", [row])
            before = store.load("accounts")

            with patch.dict(os.environ, {"RUNTIME_STORAGE_BACKEND": "sqlite"}), \
                    patch.dict(db._SQLITE_PATH_BINDINGS, {"_ACCOUNTS_JSON": db._ACCOUNTS_JSON}), \
                    patch.object(db, "_RUNTIME_DB", sqlite_path), \
                    patch.object(db, "_SQLITE_STORE", store):
                account = db.get_account(1)
                snapshot = db.list_account_plan_check_statuses()
                after = store.load("accounts")

            self.assertEqual(account["plan_check_error_kind"], "local_token_expired")
            self.assertEqual(snapshot["items"][0]["plan_check_error_kind"], "local_token_expired")
            self.assertEqual(snapshot["items"][0]["plan_query_status"], "failed")
            self.assertEqual(snapshot["items"][0]["plan_category_code"], "paid")
            self.assertEqual(after, before)

    def test_account_apis_return_consistent_plan_diagnostics_without_credentials(self):
        self.accounts_path.write_text(json.dumps([
            {
                "id": 1,
                "email": "local-expired@example.invalid",
                "access_token": "fixture-local-token",
                "password": "fixture-local-password",
                "plan_check_status": "failed",
                "plan_check_ok": False,
                "plan_check_error": "本地AT已失效，请手动查活刷新",
                "plan_check_error_kind": "local_token_expired",
                "plan_check_http_status": None,
                "token_expired": True,
                "token_expires_at": "2026-08-07T05:59:00Z",
                "needs_live_check": True,
                "plan_check_updated_at": "2026-08-07T09:00:00",
            },
            {
                "id": 2,
                "email": "http-401@example.invalid",
                "access_token": "fixture-http-token",
                "password": "fixture-http-password",
                "plan_check_status": "failed",
                "plan_check_ok": False,
                "plan_check_error": "AT已失效，请手动查活刷新",
                "plan_check_http_status": 401,
                "plan_check_updated_at": "2026-08-07T09:01:00",
            },
        ]), encoding="utf-8")

        with ExitStack() as stack:
            stack.enter_context(patch.object(web_app.db, "_render_static_viewer"))
            for name in (
                "recover_interrupted_plan_checks",
                "recover_interrupted_checkout_sessions",
                "recover_interrupted_extract_links",
                "recover_interrupted_live_checks",
            ):
                stack.enter_context(patch.object(web_app.db, name, return_value=0))
            app = web_app.create_app(auth_code="test-auth")
            client = app.test_client()
            headers = {"X-Auth-Code": "test-auth"}
            account_response = client.get("/api/accounts?archived=all", headers=headers)
            snapshot_response = client.get(
                "/api/accounts/plan-check-status?archived=all", headers=headers
            )
            paged_responses = []
            for page in (1, 2):
                paged_account = client.get(
                    f"/api/accounts?archived=all&paged=1&page={page}&page_size=1",
                    headers=headers,
                )
                paged_snapshot = client.get(
                    f"/api/accounts/plan-check-status?archived=all&page={page}&page_size=1",
                    headers=headers,
                )
                paged_responses.append((paged_account, paged_snapshot))

        self.assertEqual(account_response.status_code, 200)
        self.assertEqual(snapshot_response.status_code, 200)
        accounts = {item["id"]: item for item in account_response.get_json()}
        snapshot = {item["id"]: item for item in snapshot_response.get_json()["items"]}
        for account_id in (1, 2):
            for key in ("plan_check_error", "plan_check_error_kind", "plan_check_http_status"):
                self.assertEqual(accounts[account_id].get(key), snapshot[account_id].get(key))
        self.assertEqual(accounts[1]["plan_check_error_kind"], "local_token_expired")
        self.assertIsNone(accounts[1].get("plan_check_http_status"))
        self.assertEqual(accounts[2]["plan_check_error_kind"], "http_4xx")
        self.assertEqual(accounts[2]["plan_check_http_status"], 401)
        for page, (paged_account_response, paged_snapshot_response) in enumerate(paged_responses, start=1):
            self.assertEqual(paged_account_response.status_code, 200)
            self.assertEqual(paged_snapshot_response.status_code, 200)
            paged_account = paged_account_response.get_json()
            paged_snapshot = paged_snapshot_response.get_json()
            self.assertEqual(paged_account["page"], page)
            self.assertEqual(paged_snapshot["page"], page)
            account_item = paged_account["items"][0]
            snapshot_item = paged_snapshot["items"][0]
            self.assertEqual(account_item["id"], snapshot_item["id"])
            for key in ("plan_check_error", "plan_check_error_kind", "plan_check_http_status"):
                self.assertEqual(account_item.get(key), snapshot_item.get(key))
        response_text = account_response.get_data(as_text=True) + snapshot_response.get_data(as_text=True)
        self.assertNotIn("fixture-local-token", response_text)
        self.assertNotIn("fixture-http-token", response_text)
        self.assertNotIn("fixture-local-password", response_text)
        self.assertNotIn("fixture-http-password", response_text)

    def test_requeue_clears_previous_error_kind(self):
        db.update_account_plan_check(
            acc_id=1,
            result={
                "ok": False,
                "error": "HTTP 503",
                "http_status": 503,
                "plan_check_error_kind": "http_5xx",
            },
        )
        self.assertEqual(self._row()["plan_check_error_kind"], "http_5xx")

        with patch.object(db, "_now", return_value="2026-08-07T03:01:00"):
            self.assertTrue(db.claim_account_plan_check(acc_id=1))
        queued = self._row()
        self.assertEqual(queued["plan_check_status"], "queued")
        self.assertIsNone(queued["plan_check_error_kind"])

        with patch.object(db, "_now", return_value="2026-08-07T03:01:01"):
            self.assertTrue(db.mark_account_plan_check_running(1))
        running = self._row()
        self.assertEqual(running["plan_check_status"], "running")
        self.assertIsNone(running["plan_check_error_kind"])

    def test_status_snapshot_returns_timestamp_and_revision_tracks_it(self):
        row = self._row()
        row.update({
            "plan_check_status": "failed",
            "plan_check_ok": False,
            "plan_check_error": "HTTP 503",
            "plan_check_http_status": 503,
            "plan_check_updated_at": "2026-08-07T04:00:00",
        })
        self.accounts_path.write_text(json.dumps([row]), encoding="utf-8")

        first = db.list_account_plan_check_statuses()
        self.assertEqual(first["items"][0]["plan_check_error_kind"], "http_5xx")
        self.assertEqual(first["items"][0]["plan_check_http_status"], 503)
        self.assertEqual(db.list_accounts()[0]["plan_check_error_kind"], "http_5xx")
        self.assertEqual(
            _compact_account_for_list(db.list_accounts()[0])["plan_check_error_kind"],
            "http_5xx",
        )
        self.assertEqual(
            _compact_account_for_list(db.list_accounts()[0])["plan_check_http_status"],
            503,
        )
        self.assertEqual(first["items"][0]["plan_check_updated_at"], "2026-08-07T04:00:00")

        row["plan_check_updated_at"] = "2026-08-07T04:00:01"
        row["plan_check_error"] = "ConnectionError: refused"
        row["plan_check_http_status"] = None
        self.accounts_path.write_text(json.dumps([row]), encoding="utf-8")
        second = db.list_account_plan_check_statuses()
        self.assertNotEqual(first["revision"], second["revision"])
        self.assertEqual(second["items"][0]["plan_check_error_kind"], "network_connection")

    def test_historical_timeout_and_response_errors_are_classified_without_writing(self):
        row = self._row()
        row.update({
            "plan_check_status": "failed",
            "plan_check_ok": False,
            "plan_check_error": "ReadTimeout: request timed out",
            "plan_check_updated_at": "2026-08-07T05:00:00",
        })
        self.accounts_path.write_text(json.dumps([row]), encoding="utf-8")
        snapshot = db.list_account_plan_check_statuses()
        self.assertEqual(snapshot["items"][0]["plan_check_error_kind"], "network_timeout")
        persisted = self._row()
        self.assertNotIn("plan_check_error_kind", persisted)

        row["plan_check_error"] = "ValueError: 响应缺少 accounts 对象"
        self.accounts_path.write_text(json.dumps([row]), encoding="utf-8")
        self.assertEqual(
            db.list_account_plan_check_statuses()["items"][0]["plan_check_error_kind"],
            "response_format",
        )


class AccountPlanCellTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = (
            Path(__file__).parents[1] / "webui" / "templates" / "index.html"
        ).read_text(encoding="utf-8")
        start = cls.template.index("const ACCOUNT_STATUS_LABELS")
        end = cls.template.index("function _planAction", start)
        cls.renderer = cls.template[start:end]
        cls.node = shutil.which("node")

    def _render_plan_cells(self, rows):
        if not self.node:
            self.skipTest("需要 Node.js 执行内嵌的套餐渲染函数")
        script = """
function esc(value) {
  return String(value ?? '').replace(/[&<>\\\"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '\"': '&quot;', "'": '&#39;'
  }[ch]));
}
function _fmtPlanTime(value) { return String(value ?? ''); }
__RENDERER__
function _fmtPlanTime(value) { return String(value ?? ''); }
const rows = __ROWS__;
process.stdout.write(JSON.stringify(rows.map(_planCell)));
""".replace("__RENDERER__", self.renderer).replace(
            "__ROWS__", json.dumps(rows, ensure_ascii=False)
        )
        result = subprocess.run(
            [self.node, "-e", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_success_label_uses_trial_eligibility_for_free_and_currency_for_paid(self):
        self.assertIn("const category = String(r.plan_category_code || '').toLowerCase();", self.renderer)
        self.assertNotIn("r.plus_trial_eligible === false", self.renderer)
        self.assertIn("无 Plus 试用资格", self.renderer)
        self.assertIn("[plan, planSuffix].filter(Boolean).join('|')", self.renderer)
        self.assertNotIn("parts.join('/')", self.renderer)
        self.assertIn("计费周期: ${billing}", self.renderer)
        self.assertIn("折扣: ${discount}", self.renderer)

    def test_status_meta_covers_all_states_and_reuses_subtext_style(self):
        for status, label in (
            ("queued", "查询排队中"),
            ("running", "查询中"),
            ("success", "查询成功"),
            ("failed", "查询失败"),
        ):
            self.assertIn(f"{status}: '{label}'", self.renderer)
        self.assertIn('r.plan_check_updated_at', self.renderer)
        self.assertIn('class="acc-v2-sub"', self.renderer)
        self.assertIn("if (!updated) return '';", self.renderer)

    def test_failed_status_uses_short_visible_summary_and_keeps_tooltip_detail(self):
        for label in (
            "网络请求超时",
            "网络连接失败",
            "HTTP 4xx",
            "HTTP 5xx",
            "响应异常",
        ):
            self.assertIn(label, self.template)
        self.assertIn("const summary = code === 'failed' ? _planErrorSummary(r) : '';", self.renderer)
        self.assertIn("const text = summary ? `查询失败：${summary}|${time}` : detailText;", self.renderer)
        self.assertIn("const status = Number(r.plan_check_http_status);", self.renderer)
        self.assertIn("if (inRange) return `HTTP ${status}`;", self.renderer)
        self.assertIn('title="${esc(detailText)}"', self.renderer)
        self.assertIn('title="${[esc(reason), lastTitle].filter(Boolean).join(\'；\')}"', self.renderer)
        self.assertNotIn("网络错误 · 请求超时", self.template)
        self.assertNotIn("网络错误 · 连接失败", self.template)
        self.assertNotIn("响应异常 · 数据格式", self.template)

    def test_failed_summary_renders_specific_http_status_and_updates_without_reload(self):
        base = {
            "plan_check_status": "failed",
            "plan_query_status": "failed",
            "plan_check_error_kind": "http_4xx",
            "plan_check_updated_at": "2026-08-28T12:34:00",
            "plan_check_error": "HTTP response body with sensitive details",
            "plan_check_network_route": "proxy",
            "plan_check_proxy_used": "http://proxy-user:proxy-pass@example.invalid:8080",
            "current_plan_type": "plus",
        }
        rows = [
            {**base, "plan_check_http_status": 401},
            {**base, "plan_check_http_status": 402},
            {**base, "plan_check_error_kind": "http_5xx", "plan_check_http_status": 503},
        ]
        rendered = self._render_plan_cells(rows)

        for html, status in zip(rendered, (401, 402, 503)):
            self.assertIn(f"查询失败：HTTP {status}|2026-08-28T12:34:00", html)
            self.assertNotIn("HTTP 4xx|2026-08-28T12:34:00", html)
            self.assertNotIn("HTTP 5xx|2026-08-28T12:34:00", html)
        self.assertNotEqual(rendered[0], rendered[1])
        self.assertNotEqual(rendered[1], rendered[2])
        self.assertIn("HTTP response body with sensitive details", rendered[0])
        self.assertNotIn("proxy-user:proxy-pass@example.invalid:8080", rendered[0])
        visible_subtext = rendered[0].split('<div class="acc-v2-sub"', 1)[1].split('</div>', 1)[0]
        self.assertNotIn("HTTP response body with sensitive details", visible_subtext)
        self.assertNotIn("proxy-user:proxy-pass@example.invalid:8080", visible_subtext)

    def test_failed_summary_distinguishes_local_expiry_and_unknown_history(self):
        base = {
            "plan_check_status": "failed",
            "plan_query_status": "failed",
            "plan_check_updated_at": "2026-08-28T13:00:00",
            "plan_check_error": "AT已失效，请手动查活刷新",
            "current_plan_type": "plus",
            "plan_category_code": "paid",
            "plan_last_success_at": "2026-08-27T13:00:00",
        }
        rows = [
            {**base, "plan_check_error_kind": "local_token_expired", "plan_check_http_status": None},
            {**base, "plan_check_error_kind": "http_4xx", "plan_check_http_status": 401},
            {**base, "plan_check_error_kind": "http_5xx", "plan_check_http_status": 503},
            {**base, "plan_check_error_kind": "future_error", "plan_check_http_status": None},
        ]
        rendered = self._render_plan_cells(rows)

        self.assertIn("查询失败：本地AT已失效|2026-08-28T13:00:00", rendered[0])
        self.assertNotIn("HTTP 401|2026-08-28T13:00:00", rendered[0])
        self.assertIn("查询失败：HTTP 401|2026-08-28T13:00:00", rendered[1])
        self.assertNotIn("本地AT已失效|2026-08-28T13:00:00", rendered[1])
        self.assertIn("查询失败：HTTP 503|2026-08-28T13:00:00", rendered[2])
        self.assertIn("查询失败|2026-08-28T13:00:00", rendered[3])
        self.assertNotIn("HTTP 401", rendered[3])
        self.assertIn("上次: plus/付费套餐", rendered[0])
        self.assertIn("AT已失效，请手动查活刷新", rendered[0])
        self.assertIn("if (kind === 'local_token_expired') return '本地AT已失效';", self.renderer)
        self.assertNotIn("r.token_expired", self.renderer)

    def test_each_primary_state_is_wrapped_with_status_meta(self):
        self.assertIn("return wrap(`<span class=\"pill status-running\"", self.renderer)
        self.assertIn("return wrap(`<span class=\"pill status-failed\"", self.renderer)
        self.assertIn("return wrap(`<span class=\"pill ${cls}\"", self.renderer)
        self.assertIn("${main}${_planStatusMeta(r)}", self.renderer)


if __name__ == "__main__":
    unittest.main()
