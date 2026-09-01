# -*- coding: utf-8 -*-
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db
from webui.app import _compact_account_for_list


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

    def test_each_primary_state_is_wrapped_with_status_meta(self):
        self.assertIn("return wrap(`<span class=\"pill status-running\"", self.renderer)
        self.assertIn("return wrap(`<span class=\"pill status-failed\"", self.renderer)
        self.assertIn("return wrap(`<span class=\"pill ${cls}\"", self.renderer)
        self.assertIn("${main}${_planStatusMeta(r)}", self.renderer)


if __name__ == "__main__":
    unittest.main()
