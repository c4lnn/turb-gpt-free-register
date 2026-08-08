# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db


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
                result={"ok": False, "error": "network error"},
            ))
        row = self._row()
        self.assertEqual(row["plan_check_status"], "failed")
        self.assertEqual(row["plan_check_updated_at"], "2026-08-07T03:00:00")

        row["plan_check_status"] = "running"
        self.accounts_path.write_text(json.dumps([row]), encoding="utf-8")
        with patch.object(db, "_now", return_value="2026-08-07T03:00:01"):
            self.assertEqual(db.recover_interrupted_plan_checks(), 1)
        recovered = self._row()
        self.assertEqual(recovered["plan_check_status"], "failed")
        self.assertEqual(recovered["plan_check_updated_at"], "2026-08-07T03:00:01")

    def test_status_snapshot_returns_timestamp_and_revision_tracks_it(self):
        row = self._row()
        row.update({
            "plan_check_status": "success",
            "plan_check_ok": True,
            "plan_check_updated_at": "2026-08-07T04:00:00",
        })
        self.accounts_path.write_text(json.dumps([row]), encoding="utf-8")

        first = db.list_account_plan_check_statuses()
        self.assertEqual(first["items"][0]["plan_check_updated_at"], "2026-08-07T04:00:00")

        row["plan_check_updated_at"] = "2026-08-07T04:00:01"
        self.accounts_path.write_text(json.dumps([row]), encoding="utf-8")
        second = db.list_account_plan_check_statuses()
        self.assertNotEqual(first["revision"], second["revision"])


class AccountPlanCellTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = (
            Path(__file__).parents[1] / "webui" / "templates" / "index.html"
        ).read_text(encoding="utf-8")
        start = cls.template.index("function _planStatusMeta")
        end = cls.template.index("function _planAction", start)
        cls.renderer = cls.template[start:end]

    def test_success_label_uses_trial_eligibility_for_free_and_currency_for_paid(self):
        self.assertIn(
            "lower === 'free' ? (r.plus_trial_eligible ? '资格' : '') : r.billing_currency",
            self.renderer,
        )
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
        self.assertIn("if (!label || !updated) return '';", self.renderer)

    def test_each_primary_state_is_wrapped_with_status_meta(self):
        self.assertIn("return wrap(`<span class=\"pill status-running\"", self.renderer)
        self.assertIn("return wrap(`<span class=\"pill status-failed\"", self.renderer)
        self.assertIn("return wrap(`<span class=\"pill ${cls}\"", self.renderer)
        self.assertIn("${main}${_planStatusMeta(r)}", self.renderer)


if __name__ == "__main__":
    unittest.main()
