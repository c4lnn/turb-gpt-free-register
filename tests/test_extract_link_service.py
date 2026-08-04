# -*- coding: utf-8 -*-
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from core import extract_link_service as service
from core import masi_cdk_pool as pool
from core.extract_link_providers import ProviderError


class FakeLegacyProvider:
    def __init__(self, events):
        self.events = events
        self.closed = False

    def create_job(self, **kwargs):
        return {"job_id": "legacy-job", "cdk_remaining": 3}

    def iter_events(self, **kwargs):
        yield from self.events

    def close(self):
        self.closed = True


class FakeMasiProvider:
    def __init__(self, *, quotas=None, jobs=None):
        self.quotas = list(quotas or [])
        self.jobs = list(jobs or [])
        self.created_with = []
        self.canceled_with = []

    def query_quota(self, *, cdk):
        value = self.quotas.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def create_job(self, *, cdk, access_token):
        self.created_with.append((cdk, access_token))
        return {"job_id": "masi-job", "status": "queued"}

    def get_job(self, *, cdk, job_id):
        value = self.jobs.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def cancel_job(self, *, cdk, job_id):
        self.canceled_with.append((cdk, job_id))
        return {"ok": True}


class ExtractLinkServiceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(pool, "_POOL_PATH", Path(self.tempdir.name) / "pool.json")
        self.path_patch.start()
        pool.reset_runtime_leases()

    def tearDown(self):
        pool.reset_runtime_leases()
        self.path_patch.stop()
        self.tempdir.cleanup()

    def test_legacy_sse_log_and_result_are_preserved(self):
        adapter = FakeLegacyProvider([
            ("log", {"message": "working"}),
            ("result", {"result": {"long_url": "https://pay.test"}}),
        ])
        route = {"base_url": "https://legacy", "cdk": "CDK", "request_timeout": 30, "wait_timeout": 180, "link_type": "pix"}
        with patch.object(service, "_legacy_provider", return_value=adapter), \
             patch.object(service.db, "update_account_extract") as update:
            result = service._run_legacy(account_id=1, access_token="AT", route=route)
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["long_url"], "https://pay.test")
        self.assertTrue(adapter.closed)
        self.assertTrue(any(call.args[1].get("message") == "working" for call in update.call_args_list))

    def test_legacy_sse_error_event_fails(self):
        adapter = FakeLegacyProvider([("error", {"error": {"message": "vendor failed"}})])
        route = {"base_url": "https://legacy", "cdk": "CDK", "request_timeout": 30, "wait_timeout": 180, "link_type": "pix"}
        with patch.object(service, "_legacy_provider", return_value=adapter), patch.object(service.db, "update_account_extract"):
            with self.assertRaisesRegex(RuntimeError, "vendor failed"):
                service._run_legacy(account_id=1, access_token="AT", route=route)
        self.assertTrue(adapter.closed)

    def test_legacy_sse_done_or_disconnect_without_result_fails(self):
        route = {"base_url": "https://legacy", "cdk": "CDK", "request_timeout": 30, "wait_timeout": 180, "link_type": "pix"}
        for events in [[("done", {})], []]:
            with self.subTest(events=events):
                adapter = FakeLegacyProvider(events)
                with patch.object(service, "_legacy_provider", return_value=adapter), patch.object(service.db, "update_account_extract"):
                    with self.assertRaisesRegex(RuntimeError, "未返回 result"):
                        service._run_legacy(account_id=1, access_token="AT", route=route)
                self.assertTrue(adapter.closed)

    def test_masi_selector_skips_busy_and_uses_next_cdk(self):
        pool.import_cdks("CDK-A\nCDK-B")
        adapter = FakeMasiProvider(quotas=[
            {"total_uses": 10, "remaining_uses": 2, "pending_uses": 2, "available_uses": 0},
            {"total_uses": 10, "remaining_uses": 8, "pending_uses": 1, "available_uses": 7},
        ])
        job, lease = service._select_masi_cdk_and_create(adapter, access_token="AT")
        self.assertEqual(job["job_id"], "masi-job")
        self.assertEqual(adapter.created_with, [("CDK-B", "AT")])
        pool.release_lease(lease["id"])

    def test_masi_selector_fails_immediately_when_all_cdks_are_disabled(self):
        cdk_id = pool.import_cdks("CDK-A")["added"][0]["id"]
        pool.set_enablement(enabled=False, ids=[cdk_id])
        adapter = FakeMasiProvider()
        with self.assertRaisesRegex(service.CdkPoolDisabled, "没有已启用"):
            service._select_masi_cdk_and_create(adapter, access_token="AT")
        self.assertEqual(adapter.quotas, [])

    def test_enqueue_rejects_masi_before_claim_when_no_enabled_cdk(self):
        route = {"provider": "masi", "link_type": "kakao_pay", "update_mode": "poll"}
        with patch.object(service, "resolve_route", return_value=route), \
             patch.object(service.db, "claim_account_extract") as claim:
            with self.assertRaisesRegex(service.CdkPoolDisabled, "没有已启用"):
                service.enqueue_account_extract(account_id=1, email="masked@example.com", access_token="AT")
        claim.assert_not_called()

    def test_quota_query_retries_then_succeeds(self):
        adapter = FakeMasiProvider(quotas=[
            ProviderError("temporary", retryable=True),
            ProviderError("temporary", retryable=True),
            {"total_uses": 10, "remaining_uses": 8, "pending_uses": 1, "available_uses": 7},
        ])
        with patch.object(service, "_runtime_setting", side_effect=lambda key, default=None: 3 if key == "MASI_CDK_QUERY_MAX_ATTEMPTS" else 0), \
             patch.object(service.time, "sleep"):
            quota = service._query_masi_quota_with_retry(adapter, cdk="CDK")
        self.assertEqual(quota["available_uses"], 7)

    def test_poll_reaches_completed_and_does_not_cancel(self):
        adapter = FakeMasiProvider(jobs=[
            {"status": "queued"},
            {"status": "running"},
            {"status": "completed", "output": {"long_url": "https://pay.test"}},
        ])
        route = {"wait_timeout": 180}
        with patch.object(service.db, "update_account_extract"), patch.object(service.time, "sleep"):
            result = service._poll_masi_job(account_id=1, adapter=adapter, cdk="CDK", job_id="job", route=route)
        self.assertEqual(result["status"], "success")
        self.assertEqual(adapter.canceled_with, [])

    def test_disabled_bound_cdk_remains_available_for_existing_job_poll(self):
        cdk_id = pool.import_cdks("CDK-A")["added"][0]["id"]
        pool.set_enablement(enabled=False, ids=[cdk_id])
        bound = pool.get_secret(cdk_id)
        adapter = FakeMasiProvider(jobs=[
            {"status": "completed", "output": {"long_url": "https://pay.test"}},
        ])
        with patch.object(service.db, "update_account_extract"):
            result = service._poll_masi_job(
                account_id=1, adapter=adapter, cdk=bound["cdk"], job_id="bound-job", route={"wait_timeout": 180},
            )
        self.assertTrue(result["ok"])
        self.assertFalse(pool.list_cdks()[0]["enabled"])

    def test_poll_maps_canceled_terminal_state(self):
        adapter = FakeMasiProvider(jobs=[{"status": "canceled"}])
        with patch.object(service.db, "update_account_extract"):
            result = service._poll_masi_job(account_id=1, adapter=adapter, cdk="CDK", job_id="job", route={"wait_timeout": 180})
        self.assertEqual(result["status"], "canceled")

    def test_poll_rejects_unknown_state(self):
        adapter = FakeMasiProvider(jobs=[{"status": "mystery"}])
        with self.assertRaisesRegex(RuntimeError, "未知状态"):
            service._poll_masi_job(account_id=1, adapter=adapter, cdk="CDK", job_id="job", route={"wait_timeout": 180})

    def test_poll_retries_transient_errors_and_resets_counter(self):
        adapter = FakeMasiProvider(jobs=[
            ProviderError("rate limited", status_code=429, retryable=True),
            {"status": "running"},
            ProviderError("upstream", status_code=502, retryable=True),
            {"status": "completed", "output": {"long_url": "https://pay.test"}},
        ])
        with patch.object(service.db, "update_account_extract"), patch.object(service.time, "sleep"):
            result = service._poll_masi_job(account_id=1, adapter=adapter, cdk="CDK", job_id="job", route={"wait_timeout": 180})
        self.assertEqual(result["status"], "success")

    def test_poll_stops_on_non_retryable_4xx(self):
        adapter = FakeMasiProvider(jobs=[ProviderError("unauthorized", status_code=401, retryable=False)])
        with self.assertRaises(ProviderError), patch.object(service.db, "update_account_extract"):
            service._poll_masi_job(account_id=1, adapter=adapter, cdk="CDK", job_id="job", route={"wait_timeout": 180})

    def test_poll_stops_at_consecutive_error_limit(self):
        adapter = FakeMasiProvider(jobs=[
            ProviderError("upstream", status_code=502, retryable=True),
            ProviderError("upstream", status_code=502, retryable=True),
            ProviderError("upstream", status_code=502, retryable=True),
        ])
        with patch.object(service.db, "update_account_extract"), patch.object(service.time, "sleep"), \
             patch.object(service, "_runtime_setting", side_effect=lambda key, default=None: 3 if key == "EXTRACT_LINK_POLL_MAX_ERRORS" else default):
            with self.assertRaisesRegex(RuntimeError, "连续查询失败 3 次"):
                service._poll_masi_job(account_id=1, adapter=adapter, cdk="CDK", job_id="job", route={"wait_timeout": 180})

    def test_poll_maps_failed_terminal_state(self):
        adapter = FakeMasiProvider(jobs=[{"status": "failed", "error": {"message": "remote failed"}}])
        with patch.object(service.db, "update_account_extract"):
            with self.assertRaisesRegex(service.MasiJobFailed, "remote failed"):
                service._poll_masi_job(account_id=1, adapter=adapter, cdk="CDK", job_id="job", route={"wait_timeout": 180})

    def test_masi_resubmits_failed_jobs_up_to_three_total_attempts(self):
        pool.import_cdks("CDK-A")
        quota = {"total_uses": 10, "remaining_uses": 10, "pending_uses": 0, "available_uses": 10}
        adapter = FakeMasiProvider(
            quotas=[quota, quota, quota],
            jobs=[
                {"status": "failed", "error": {"message": "first failed"}},
                {"status": "failed", "error": {"message": "second failed"}},
                {"status": "completed", "output": {"long_url": "https://pay.test"}},
            ],
        )
        route = {"provider": "masi", "link_type": "kakao_pay", "update_mode": "poll", "wait_timeout": 180}
        with patch.object(service, "_masi_provider", return_value=adapter), \
             patch.object(adapter, "close", create=True), \
             patch.object(service.db, "update_account_extract"), \
             patch.object(service.time, "sleep"):
            result = service._run_masi(account_id=1, access_token="AT", route=route)
        self.assertEqual(result["result"]["long_url"], "https://pay.test")
        self.assertEqual(adapter.created_with, [("CDK-A", "AT")] * 3)

    def test_masi_resubmit_reselects_and_skips_newly_disabled_cdk(self):
        imported = pool.import_cdks("CDK-A\nCDK-B")
        first_id = imported["added"][0]["id"]
        quota = {"total_uses": 10, "remaining_uses": 10, "pending_uses": 0, "available_uses": 10}

        class DisableAfterFailureProvider(FakeMasiProvider):
            def get_job(self, *, cdk, job_id):
                value = super().get_job(cdk=cdk, job_id=job_id)
                if value.get("status") == "failed":
                    pool.set_enablement(enabled=False, ids=[first_id])
                return value

        adapter = DisableAfterFailureProvider(
            quotas=[quota, quota],
            jobs=[
                {"status": "failed", "error": {"message": "first failed"}},
                {"status": "completed", "output": {"long_url": "https://pay.test"}},
            ],
        )
        route = {"provider": "masi", "link_type": "kakao_pay", "update_mode": "poll", "wait_timeout": 180}
        with patch.object(service, "_masi_provider", return_value=adapter), \
             patch.object(adapter, "close", create=True), \
             patch.object(service.db, "update_account_extract"), \
             patch.object(service.time, "sleep"):
            result = service._run_masi(account_id=1, access_token="AT", route=route)
        self.assertTrue(result["ok"])
        self.assertEqual(adapter.created_with, [("CDK-A", "AT"), ("CDK-B", "AT")])

    def test_masi_stops_after_three_failed_jobs(self):
        pool.import_cdks("CDK-A")
        quota = {"total_uses": 10, "remaining_uses": 10, "pending_uses": 0, "available_uses": 10}
        adapter = FakeMasiProvider(
            quotas=[quota, quota, quota],
            jobs=[
                {"status": "failed", "error": {"message": "first failed"}},
                {"status": "failed", "error": {"message": "second failed"}},
                {"status": "failed", "error": {"message": "third failed"}},
            ],
        )
        route = {"provider": "masi", "link_type": "kakao_pay", "update_mode": "poll", "wait_timeout": 180}
        with patch.object(service, "_masi_provider", return_value=adapter), \
             patch.object(adapter, "close", create=True), \
             patch.object(service.db, "update_account_extract"), \
             patch.object(service.time, "sleep"):
            with self.assertRaisesRegex(service.MasiJobFailed, "third failed"):
                service._run_masi(account_id=1, access_token="AT", route=route)
        self.assertEqual(adapter.created_with, [("CDK-A", "AT")] * 3)

    def test_masi_does_not_resubmit_when_job_creation_is_uncertain(self):
        pool.import_cdks("CDK-A")
        quota = {"total_uses": 10, "remaining_uses": 10, "pending_uses": 0, "available_uses": 10}

        class UncertainCreateProvider(FakeMasiProvider):
            def create_job(self, *, cdk, access_token):
                self.created_with.append((cdk, access_token))
                raise ProviderError("connection reset", retryable=True)

        adapter = UncertainCreateProvider(quotas=[quota])
        route = {"provider": "masi", "link_type": "kakao_pay", "update_mode": "poll", "wait_timeout": 180}
        with patch.object(service, "_masi_provider", return_value=adapter), \
             patch.object(adapter, "close", create=True), \
             patch.object(service.db, "update_account_extract"):
            with self.assertRaisesRegex(ProviderError, "connection reset"):
                service._run_masi(account_id=1, access_token="AT", route=route)
        self.assertEqual(adapter.created_with, [("CDK-A", "AT")])

    def test_poll_timeout_preserves_job_for_resume(self):
        adapter = FakeMasiProvider(jobs=[])
        with patch.object(service.time, "monotonic", side_effect=[0, 2]), \
             patch.object(service, "_runtime_setting", side_effect=lambda key, default=None: default):
            with self.assertRaises(TimeoutError):
                service._poll_masi_job(account_id=1, adapter=adapter, cdk="CDK", job_id="job", route={"wait_timeout": 1})
        self.assertEqual(adapter.canceled_with, [])

    def test_resume_existing_masi_job_polls_without_create(self):
        adapter = FakeMasiProvider(jobs=[
            {"status": "completed", "output": {"long_url": "https://pay.test"}},
        ])
        route = {"provider": "masi", "link_type": "kakao_pay", "update_mode": "poll", "wait_timeout": 180}
        with patch.object(service, "_masi_provider", return_value=adapter), \
             patch.object(adapter, "close", create=True), \
             patch.object(service.db, "mark_account_extract_running", return_value=True), \
             patch.object(service.db, "update_account_extract") as update, \
             patch.object(service._QUEUE_SLOTS, "release"):
            result = service._run_existing_masi_extract(
                account_id=1,
                email="masked@example.com",
                job_id="existing-job",
                cdk="CDK-A",
                route=route,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(adapter.created_with, [])
        self.assertEqual(update.call_args.args[1]["job_id"], "existing-job")

    def test_selector_keeps_single_cdk_leased_until_create_returns(self):
        pool.import_cdks("CDK-A")
        entered = threading.Event()
        release = threading.Event()

        class BlockingProvider(FakeMasiProvider):
            def query_quota(self, *, cdk):
                return {"total_uses": 1, "remaining_uses": 1, "pending_uses": 0, "available_uses": 1}

            def create_job(self, *, cdk, access_token):
                self.created_with.append((cdk, access_token))
                entered.set()
                release.wait(2)
                return {"job_id": access_token, "status": "queued"}

        adapter = BlockingProvider()
        results = []

        def select():
            results.append(service._select_masi_cdk_and_create(adapter, access_token="AT-1"))

        worker = threading.Thread(target=select)
        worker.start()
        self.assertTrue(entered.wait(1))
        self.assertEqual(pool.leased_count(), 1)
        self.assertIsNone(pool.lease_next())
        release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(adapter.created_with, [("CDK-A", "AT-1")])
        _, lease = results[0]
        pool.release_lease(lease["id"])

    def test_success_requires_long_url(self):
        with self.assertRaisesRegex(RuntimeError, "long_url"):
            service._normalize_success_result({})
        self.assertEqual(service._normalize_success_result({"copy_paste": "https://pay.test"})["long_url"], "https://pay.test")

    def test_error_sanitizer_removes_credentials(self):
        text = service._sanitize_error("failed https://x.test?a=1&cdk=SECRET token=TOKEN", "SECRET", "TOKEN")
        self.assertNotIn("SECRET", text)
        self.assertNotIn("TOKEN", text)

    def test_full_masi_run_persists_binding_before_poll(self):
        imported = pool.import_cdks("CDK-A\nCDK-B")
        cdk_id = imported["added"][0]["id"]
        adapter = FakeMasiProvider(
            quotas=[{"total_uses": 10, "remaining_uses": 8, "pending_uses": 1, "available_uses": 7}],
            jobs=[{"status": "completed", "output": {"long_url": "https://pay.test"}}],
        )
        route = {"provider": "masi", "link_type": "kakao_pay", "update_mode": "poll", "wait_timeout": 180}
        updates = []
        with patch.object(service, "_masi_provider", return_value=adapter), \
             patch.object(adapter, "close", create=True), \
             patch.object(service.db, "update_account_extract", side_effect=lambda account_id, data: updates.append(dict(data)) or True):
            result = service._run_masi(account_id=1, access_token="AT", route=route)
        binding = next(data for data in updates if data.get("job_id") == "masi-job")
        self.assertEqual(binding["cdk_id"], cdk_id)
        self.assertNotIn("cdk", binding)
        self.assertEqual(result["result"]["long_url"], "https://pay.test")
        self.assertEqual(pool.list_cdks(pool=pool.POOL_SELECTABLE)[0]["id"], cdk_id)

    def test_bulk_refresh_snapshots_proxy_once(self):
        route = {"provider": "masi", "link_type": "kakao_pay", "update_mode": "poll", "proxy": "http://proxy.test:8080"}
        seen_routes = []

        def refresh(cdk_id, *, route=None, session=None):
            seen_routes.append(route)
            return {"ok": True, "item": {"id": cdk_id, "pool": "selectable", "moved": False}}

        with patch.object(service, "resolve_route", return_value=route) as resolve, \
             patch.object(service, "refresh_masi_cdk", side_effect=refresh):
            result = service.refresh_masi_cdks(ids=["one", "two"])
        self.assertEqual(result["success_count"], 2)
        resolve.assert_called_once_with(link_type="kakao_pay", provider="masi", update_mode="poll")
        self.assertEqual(len(seen_routes), 2)
        self.assertTrue(all(item is route for item in seen_routes))


if __name__ == "__main__":
    unittest.main()
