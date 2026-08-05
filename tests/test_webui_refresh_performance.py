import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from core import db
from core import registration_service as service
from webui import app as web_app


class WebUiRefreshPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.recovery_patches = ExitStack()
        for name in (
            "recover_interrupted_plan_checks",
            "recover_interrupted_extract_links",
            "recover_interrupted_codex_agents",
            "recover_interrupted_at_refresh_jobs",
            "recover_interrupted_at_refreshes",
        ):
            self.recovery_patches.enter_context(
                patch.object(web_app.db, name, return_value=0)
            )
        self.client = web_app.create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    def tearDown(self):
        self.recovery_patches.close()

    @staticmethod
    def _jobs():
        return [
            {
                "id": 5,
                "job_type": "registration",
                "status": "failed",
                "account_id": 1,
                "email": "one@example.invalid",
            },
            {
                "id": 4,
                "job_type": "registration",
                "status": "success",
                "account_id": 2,
                "email": "two@example.invalid",
            },
            {
                "id": 3,
                "job_type": "codex_retry",
                "status": "stopped",
                "account_id": 2,
                "email": "two@example.invalid",
            },
            {
                "id": 2,
                "job_type": "registration",
                "status": "cancelled",
                "account_id": None,
                "email": "missing@example.invalid",
            },
        ]

    def test_retry_account_snapshot_is_batched_and_non_sensitive(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "accounts.json"
            path.write_text(
                json.dumps(
                    [
                        {
                            "id": 1,
                            "email": "one@example.invalid",
                            "codex_status": "success",
                            "access_token": "secret-at",
                            "totp_secret": "secret-totp",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            with patch.object(db, "_ACCOUNTS_JSON", path):
                snapshot = db.get_retry_account_snapshot()

        self.assertEqual(snapshot["by_id"][1]["codex_status"], "success")
        self.assertIs(snapshot["by_id"][1], snapshot["by_email"]["one@example.invalid"])
        self.assertNotIn("access_token", snapshot["by_id"][1])
        self.assertNotIn("totp_secret", snapshot["by_id"][1])

    def test_retry_info_uses_snapshot_for_terminal_jobs(self):
        account_success = {"id": 1, "email": "one@example.invalid", "codex_status": "success"}
        account_failed = {"id": 2, "email": "two@example.invalid", "codex_status": "failed"}
        snapshot = {
            "by_id": {1: account_success, 2: account_failed},
            "by_email": {
                "one@example.invalid": account_success,
                "two@example.invalid": account_failed,
            },
        }
        account_lookup = Mock(side_effect=AssertionError("per-job account lookup"))
        with patch.object(service.db, "get_account", account_lookup), patch.object(
            service.db, "get_account_by_email", account_lookup
        ), patch.object(service.db, "get_successful_retry_for_job", return_value=None):
            success_info = service.get_retry_info(
                self._jobs()[0], account_snapshot=snapshot
            )
            failed_info = service.get_retry_info(
                self._jobs()[2], account_snapshot=snapshot
            )
            missing_info = service.get_retry_info(
                self._jobs()[3], account_snapshot=snapshot
            )

        self.assertEqual(success_info["display_status"], "success")
        self.assertFalse(success_info["retryable"])
        self.assertEqual(failed_info["retry_action"], "codex")
        self.assertTrue(failed_info["retryable"])
        self.assertEqual(missing_info["retry_action"], "registration")
        self.assertTrue(missing_info["retryable"])
        account_lookup.assert_not_called()

    def test_retry_info_short_circuits_successful_retry(self):
        account_lookup = Mock(side_effect=AssertionError("account lookup should not run"))
        with patch.object(service.db, "get_successful_retry_for_job", return_value={"id": 99}), patch.object(
            service.db, "get_account", account_lookup
        ), patch.object(service.db, "get_account_by_email", account_lookup):
            info = service.get_retry_info(self._jobs()[0], account_snapshot={"by_id": {}, "by_email": {}})

        self.assertEqual(info["successful_retry_job_id"], 99)
        self.assertFalse(info["retryable"])
        account_lookup.assert_not_called()

    def test_paged_jobs_only_enrich_current_page_and_keep_counts(self):
        rows = self._jobs()
        calls = []

        def fake_retry_info(row, *, account_snapshot=None):
            calls.append((row["id"], account_snapshot))
            return {
                "retryable": row["status"] == "failed",
                "retry_action": "registration" if row["status"] == "failed" else None,
                "retry_label": "retry" if row["status"] == "failed" else None,
                "retry_reason": None,
                "display_status": row["status"],
            }

        with patch.object(web_app.db, "list_jobs", return_value=rows) as list_jobs, patch.object(
            web_app.db,
            "get_retry_account_snapshot",
            return_value={"by_id": {}, "by_email": {}},
        ) as snapshot, patch.object(web_app.svc, "get_retry_info", side_effect=fake_retry_info):
            response = self.client.get(
                "/api/jobs?paged=1&page=1&page_size=1",
                headers={"X-Auth-Code": "test-auth"},
            )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["total"], 4)
        self.assertEqual(body["status_counts"], {"failed": 1, "success": 1, "stopped": 1, "cancelled": 1, "active": 0})
        self.assertEqual([item["id"] for item in body["items"]], [5])
        self.assertEqual([job_id for job_id, _ in calls], [5])
        self.assertIsNotNone(calls[0][1])
        snapshot.assert_called_once_with()
        list_jobs.assert_called_once_with(limit=1_000_000)

    def test_non_paged_jobs_keep_full_response_behavior(self):
        rows = self._jobs()
        calls = []

        def list_jobs(limit):
            return rows[:limit]

        def fake_retry_info(row, *, account_snapshot=None):
            calls.append(row["id"])
            return {
                "retryable": False,
                "retry_action": None,
                "retry_label": None,
                "retry_reason": None,
                "display_status": row["status"],
            }

        with patch.object(web_app.db, "list_jobs", side_effect=list_jobs), patch.object(
            web_app.db,
            "get_retry_account_snapshot",
            return_value={"by_id": {}, "by_email": {}},
        ), patch.object(web_app.svc, "get_retry_info", side_effect=fake_retry_info):
            response = self.client.get(
                "/api/jobs?limit=2",
                headers={"X-Auth-Code": "test-auth"},
            )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertIsInstance(body, list)
        self.assertEqual([item["id"] for item in body], [5, 4])
        self.assertEqual(calls, [5, 4])

    def test_summary_response_fields_remain_compatible(self):
        response = self.client.get(
            "/api/summary",
            headers={"X-Auth-Code": "test-auth"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            {
                "accounts",
                "outlook_total",
                "outlook_available",
                "outlook_used",
                "outlook_failed",
            }.issubset(response.get_json())
        )

    def test_template_contains_refresh_guards_and_no_nested_summary_refresh(self):
        template_path = Path(__file__).parents[1] / "webui" / "templates" / "index.html"
        template = template_path.read_text(encoding="utf-8")
        self.assertIn("let jobsRefreshPromise = null;", template)
        self.assertIn("if (jobsRefreshPromise) return jobsRefreshPromise;", template)
        self.assertIn("let summaryRefreshPromise = null;", template)
        self.assertIn("if (summaryRefreshPromise) return summaryRefreshPromise;", template)

        jobs_start = template.index("let jobsRefreshPromise = null;")
        jobs_end = template.index("let modalScrollY", jobs_start)
        self.assertNotIn("loadSummary();", template[jobs_start:jobs_end])
        self.assertIn("jobsTimer = setInterval", template)
        self.assertIn("setInterval(loadSummary, 5000)", template)


if __name__ == "__main__":
    unittest.main()
