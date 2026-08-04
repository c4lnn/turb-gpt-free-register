# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db


class ExtractLinkDbTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.accounts_path = Path(self.tempdir.name) / "accounts.json"
        self.accounts_path.write_text(json.dumps([{
            "id": 1,
            "email": "test@example.invalid",
            "extract_link_status": "success",
            "extract_link_job_id": "old-job",
            "extract_link_cdk_id": "old-cdk",
            "extract_link_cdk_fingerprint": "old-fingerprint",
            "extract_link_long_url": "https://pay.invalid/old",
        }]), encoding="utf-8")
        self.path_patch = patch.object(db, "_ACCOUNTS_JSON", self.accounts_path)
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.tempdir.cleanup()

    def _row(self):
        return json.loads(self.accounts_path.read_text(encoding="utf-8"))[0]

    def test_claim_snapshots_route_and_clears_previous_job_binding(self):
        self.assertTrue(db.claim_account_extract(1, link_type="kakao_pay", provider="masi", update_mode="poll"))
        row = self._row()
        self.assertEqual((row["extract_link_type"], row["extract_link_provider"], row["extract_link_update_mode"]), ("kakao_pay", "masi", "poll"))
        self.assertIsNone(row["extract_link_job_id"])
        self.assertIsNone(row["extract_link_cdk_id"])
        self.assertIsNone(row["extract_link_long_url"])

    def test_canceled_is_terminal_and_preserves_binding(self):
        db.claim_account_extract(1, link_type="kakao_pay", provider="masi", update_mode="poll")
        db.update_account_extract(1, {"status": "running", "job_id": "job", "cdk_id": "cdk", "cdk_fingerprint": "fingerprint"})
        db.update_account_extract(1, {"status": "canceled", "error": "已取消"})
        row = self._row()
        self.assertEqual(row["extract_link_status"], "canceled")
        self.assertTrue(row["extract_link_completed_at"])
        self.assertEqual(row["extract_link_job_id"], "job")
        self.assertEqual(row["extract_link_cdk_fingerprint"], "fingerprint")

    def test_resume_claim_preserves_existing_job_and_cdk(self):
        row = self._row()
        row.update({
            "extract_link_status": "failed",
            "extract_link_provider": "masi",
            "extract_link_job_id": "existing-job",
            "extract_link_cdk_id": "existing-cdk",
            "extract_link_cdk_fingerprint": "existing-fingerprint",
            "extract_link_error": "TimeoutError: Masi Job 等待超时",
        })
        self.accounts_path.write_text(json.dumps([row]), encoding="utf-8")
        self.assertTrue(db.claim_account_extract_resume(1))
        resumed = self._row()
        self.assertEqual(resumed["extract_link_status"], "queued")
        self.assertEqual(resumed["extract_link_job_id"], "existing-job")
        self.assertEqual(resumed["extract_link_cdk_id"], "existing-cdk")
        self.assertEqual(resumed["extract_link_cdk_fingerprint"], "existing-fingerprint")
        self.assertIsNone(resumed["extract_link_error"])

    def test_resume_claim_rejects_definite_business_failure(self):
        row = self._row()
        row.update({
            "extract_link_status": "failed",
            "extract_link_provider": "masi",
            "extract_link_job_id": "failed-job",
            "extract_link_cdk_id": "existing-cdk",
            "extract_link_error": "MasiJobFailed: Kakao 提炼失败",
        })
        self.accounts_path.write_text(json.dumps([row]), encoding="utf-8")
        self.assertFalse(db.account_extract_resumable(row))
        self.assertFalse(db.claim_account_extract_resume(1))
        self.assertEqual(self._row()["extract_link_status"], "failed")

    def test_restart_recovery_preserves_diagnostic_fields(self):
        db.claim_account_extract(1, link_type="kakao_pay", provider="masi", update_mode="poll")
        db.update_account_extract(1, {"status": "running", "job_id": "job", "cdk_id": "cdk", "cdk_fingerprint": "fingerprint"})
        self.assertEqual(db.recover_interrupted_extract_links(), 1)
        row = self._row()
        self.assertEqual(row["extract_link_status"], "failed")
        self.assertEqual(row["extract_link_provider"], "masi")
        self.assertEqual(row["extract_link_update_mode"], "poll")
        self.assertEqual(row["extract_link_job_id"], "job")
        self.assertEqual(row["extract_link_cdk_fingerprint"], "fingerprint")


if __name__ == "__main__":
    unittest.main()
