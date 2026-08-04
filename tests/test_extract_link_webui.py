# -*- coding: utf-8 -*-
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import masi_cdk_pool as pool
from webui.app import create_app


class ExtractLinkWebUiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(pool, "_POOL_PATH", Path(self.tempdir.name) / "pool.json")
        self.path_patch.start()
        self.accounts_path = Path(self.tempdir.name) / "accounts.json"
        self.accounts_path.write_text("[]", encoding="utf-8")
        self.accounts_patch = patch("core.db._ACCOUNTS_JSON", self.accounts_path)
        self.accounts_patch.start()
        pool.reset_runtime_leases()
        self.app = create_app(auth_code="test-auth")
        self.client = self.app.test_client()
        self.headers = {"X-Auth-Code": "test-auth"}

    def tearDown(self):
        pool.reset_runtime_leases()
        self.accounts_patch.stop()
        self.path_patch.stop()
        self.tempdir.cleanup()

    def test_cdk_list_is_masked(self):
        pool.import_cdks("KSCAN-AAAA-BBBB-1234")
        response = self.client.get("/api/extract-link/cdks?provider=masi&pool=selectable", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        item = response.get_json()["items"][0]
        self.assertNotIn("cdk", item)
        self.assertNotIn("KSCAN-AAAA-BBBB-1234", response.get_data(as_text=True))
        self.assertIn("****", item["masked_cdk"])
        self.assertIs(item["enabled"], True)
        self.assertIsNotNone(item["created_at"])

    def test_cdk_enablement_supports_ids_and_pool_scopes(self):
        imported = pool.import_cdks("CDK-A\nCDK-B\nCDK-C")
        ids = [row["id"] for row in imported["added"]]
        page = self.client.post(
            "/api/extract-link/cdks/enablement",
            json={"scope": "ids", "ids": ids[:2], "enabled": False},
            headers=self.headers,
        )
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.get_json()["changed_count"], 2)
        summary = self.client.get("/api/extract-link/cdks?pool=selectable", headers=self.headers).get_json()["summary"]
        self.assertEqual((summary["enabled_selectable_count"], summary["disabled_selectable_count"]), (1, 2))
        all_rows = self.client.post(
            "/api/extract-link/cdks/enablement",
            json={"scope": "pool", "pool": "selectable", "enabled": True},
            headers=self.headers,
        )
        self.assertEqual(all_rows.status_code, 200)
        self.assertEqual((all_rows.get_json()["matched_count"], all_rows.get_json()["changed_count"]), (3, 2))

    def test_cdk_enablement_validates_scope_and_boolean(self):
        cases = [
            {"scope": "ids", "ids": ["one"], "enabled": "false"},
            {"scope": "ids", "ids": [], "enabled": False},
            {"scope": "pool", "pool": "invalid", "enabled": False},
            {"scope": "pool", "pool": "selectable", "ids": ["one"], "enabled": False},
            {"scope": "unknown", "enabled": False},
            {"scope": "ids", "ids": [str(index) for index in range(101)], "enabled": False},
            {"scope": "ids", "ids": [{}], "enabled": False},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                response = self.client.post("/api/extract-link/cdks/enablement", json=payload, headers=self.headers)
                self.assertEqual(response.status_code, 400)

    def test_cdk_list_is_paginated_with_global_positions(self):
        pool.import_cdks("\n".join(f"CDK-{index:02d}" for index in range(1, 13)))
        response = self.client.get(
            "/api/extract-link/cdks?provider=masi&pool=selectable&page=2&page_size=5", headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual((body["page"], body["page_size"], body["total"], body["total_pages"]), (2, 5, 12, 3))
        self.assertEqual([item["position"] for item in body["items"]], [6, 7, 8, 9, 10])

    def test_cdk_list_rejects_invalid_pagination(self):
        for query in ("page=0", "page_size=0", "page_size=101", "page=abc"):
            response = self.client.get(f"/api/extract-link/cdks?{query}", headers=self.headers)
            self.assertEqual(response.status_code, 400, query)

    def test_import_reports_added_duplicate_and_refresh_summary(self):
        refresh = {"total": 1, "success_count": 1, "failed_count": 0, "moved_to_exhausted": 0, "moved_to_selectable": 0, "results": []}
        with patch("core.extract_link_service.refresh_masi_cdks", return_value=refresh):
            first = self.client.post("/api/extract-link/cdks/import", json={"text": "CDK-A", "refresh_quota": True}, headers=self.headers)
            second = self.client.post("/api/extract-link/cdks/import", json={"text": "CDK-A", "refresh_quota": True}, headers=self.headers)
        self.assertEqual(first.get_json()["added_count"], 1)
        self.assertEqual(second.get_json()["duplicate_count"], 1)

    def test_import_does_not_refresh_quota_by_default(self):
        with patch("core.extract_link_service.refresh_masi_cdks") as refresh:
            response = self.client.post("/api/extract-link/cdks/import", json={"text": "CDK-A\nCDK-B"}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertFalse(body["refresh_requested"])
        self.assertEqual(body["added_count"], 2)
        self.assertEqual(body["refresh"]["total"], 0)
        refresh.assert_not_called()

    def test_duplicate_import_without_refresh_preserves_quota_and_pool(self):
        cdk_id = pool.import_cdks("CDK-A")["added"][0]["id"]
        pool.update_quota(cdk_id, {"total_uses": 1, "remaining_uses": 0, "pending_uses": 0, "available_uses": 0})
        with patch("core.extract_link_service.refresh_masi_cdks") as refresh:
            response = self.client.post("/api/extract-link/cdks/import", json={"text": "CDK-A"}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        item = pool.list_cdks()[0]
        self.assertEqual(item["pool"], "exhausted")
        self.assertEqual(item["remaining_uses"], 0)
        refresh.assert_not_called()

    def test_import_rejects_non_boolean_refresh_option(self):
        response = self.client.post(
            "/api/extract-link/cdks/import", json={"text": "CDK-A", "refresh_quota": "false"}, headers=self.headers,
        )
        self.assertEqual(response.status_code, 400)

    def test_delete_rejects_leased_cdk(self):
        cdk_id = pool.import_cdks("CDK-A")["added"][0]["id"]
        pool.lease_by_id(cdk_id)
        response = self.client.delete(f"/api/extract-link/cdks/{cdk_id}", headers=self.headers)
        self.assertEqual(response.status_code, 409)
        pool.release_lease(cdk_id)

    def test_delete_rejects_cdk_bound_to_active_job(self):
        cdk_id = pool.import_cdks("CDK-A")["added"][0]["id"]
        self.accounts_path.write_text(json.dumps([{
            "id": 1,
            "extract_link_status": "running",
            "extract_link_cdk_id": cdk_id,
        }]), encoding="utf-8")
        response = self.client.delete(f"/api/extract-link/cdks/{cdk_id}", headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(pool.list_cdks()[0]["id"], cdk_id)

    def test_frontend_only_references_local_extract_status_and_cdk_apis(self):
        template = (Path(__file__).parents[1] / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("/v1/kakao/jobs", template)
        self.assertIn("/api/accounts/plan-check-status", template)
        self.assertIn("/api/extract-link/cdks", template)
        self.assertIn('id="masiCdkRefreshAfterImport" type="checkbox"', template)
        self.assertIn("refresh_quota:refreshQuota", template)
        self.assertIn('id="masiCdkPagination"', template)
        self.assertIn("page_size=${pageSize}", template)

    def test_resume_endpoint_only_enqueues_existing_job_poll(self):
        self.accounts_path.write_text(json.dumps([{
            "id": 1,
            "email": "test@example.invalid",
            "extract_link_status": "failed",
            "extract_link_provider": "masi",
            "extract_link_job_id": "existing-job",
            "extract_link_cdk_id": "existing-cdk",
            "extract_link_error": "TimeoutError: Masi Job 等待超时",
        }]), encoding="utf-8")
        accepted = {"accepted": True, "busy": False, "job_id": "existing-job"}
        with patch("core.extract_link_service.enqueue_existing_masi_job_poll", return_value=accepted) as enqueue:
            response = self.client.post(
                "/api/accounts/extract-link-resume",
                json={"account_id": 1},
                headers=self.headers,
            )
        self.assertEqual(response.status_code, 202)
        enqueue.assert_called_once_with(account_id=1, email="test@example.invalid")

    def test_account_list_marks_bound_failed_masi_job_resumable(self):
        self.accounts_path.write_text(json.dumps([{
            "id": 1,
            "email": "test@example.invalid",
            "extract_link_status": "failed",
            "extract_link_provider": "masi",
            "extract_link_job_id": "existing-job",
            "extract_link_cdk_id": "existing-cdk",
            "extract_link_error": "TimeoutError: Masi Job 等待超时",
        }]), encoding="utf-8")
        response = self.client.get("/api/accounts?paged=1&page=1&page_size=20", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        item = response.get_json()["items"][0]
        self.assertTrue(item["extract_link_resumable"])
        self.assertNotIn("extract_link_cdk_id", item)

    def test_account_list_does_not_offer_resume_for_business_failure(self):
        self.accounts_path.write_text(json.dumps([{
            "id": 1,
            "email": "test@example.invalid",
            "extract_link_status": "failed",
            "extract_link_provider": "masi",
            "extract_link_job_id": "failed-job",
            "extract_link_cdk_id": "existing-cdk",
            "extract_link_error": "MasiJobFailed: Kakao 提炼失败",
        }]), encoding="utf-8")
        response = self.client.get("/api/accounts?paged=1&page=1&page_size=20", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["items"][0]["extract_link_resumable"])

    def test_templates_offer_resume_without_vendor_job_api(self):
        template_dir = Path(__file__).parents[1] / "webui" / "templates"
        for name in ("index.html", "index_legacy.html"):
            with self.subTest(template=name):
                template = (template_dir / name).read_text(encoding="utf-8")
                self.assertIn('data-extract-link-resume="${esc(r.id)}"', template)
                self.assertIn("/api/accounts/extract-link-resume", template)
                self.assertNotIn("/v1/kakao/jobs", template)

    def test_cdk_template_has_enablement_controls_and_exact_page_ids(self):
        template = (Path(__file__).parents[1] / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="masiCdkBulkAction"', template)
        self.assertIn('id="btnApplyMasiCdkBulk"', template)
        self.assertIn('data-masi-cdk-enabled="${esc(item.id)}"', template)
        self.assertIn("MASI_CDK_PAGE_ITEMS.map(item => item.id)", template)
        self.assertIn("/api/extract-link/cdks/enablement", template)
        self.assertIn("scope:'pool', pool:MASI_CDK_ACTIVE_POOL", template)
        self.assertIn("确定禁用${MASI_CDK_ACTIVE_POOL", template)
        self.assertIn('<th class="time imported">导入时间</th><th class="time checked">最近查询</th>', template)
        self.assertIn('data-label="导入时间">${esc(item.created_at || \'—\')}</td>', template)
        self.assertIn('colspan="11"', template)
        self.assertIn('min-height: 20px !important; max-height: 20px !important;', template)
        self.assertIn("enabled_available_uses", template)

    def test_extract_success_label_is_the_copy_control(self):
        template_dir = Path(__file__).parents[1] / "webui" / "templates"
        for name in ("index.html", "index_legacy.html"):
            with self.subTest(template=name):
                template = (template_dir / name).read_text(encoding="utf-8")
                self.assertNotIn("cbtn('复制提链'", template)
                self.assertIn("pill status-success extract-success-copy", template)
                self.assertIn('title="${esc(successTitle)}"', template)

    def test_extract_status_uses_fixed_short_labels_and_hover_details(self):
        template_dir = Path(__file__).parents[1] / "webui" / "templates"
        for name in ("index.html", "index_legacy.html"):
            with self.subTest(template=name):
                template = (template_dir / name).read_text(encoding="utf-8")
                start = template.index("function _extractLinkProgressLabel")
                end = template.index("function _extractLinkAction", start)
                renderer = template[start:end]
                self.assertIn("extract-status-label", renderer)
                self.assertIn("_extractLinkProgressLabel(msg)", renderer)
                self.assertIn('title="${esc(reason)}"', renderer)
                self.assertNotIn('<div class="extract-link-error"', renderer)
                self.assertNotIn("${esc(reason)}</div>", renderer)

    def test_account_templates_include_codex_success_filter(self):
        template_dir = Path(__file__).parents[1] / "webui" / "templates"
        for name in ("index.html", "index_legacy.html"):
            with self.subTest(template=name):
                template = (template_dir / name).read_text(encoding="utf-8")
                self.assertIn("SHOW_CODEX_SUCCESS_ONLY", template)
                self.assertIn("codex_status=${encodeURIComponent(codexStatus)}", template)
                self.assertIn("Codex已通过", template)

    def test_capabilities_endpoint(self):
        response = self.client.get("/api/extract-link/capabilities", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["providers"]["masi"]["update_modes"], ["poll"])

    def test_config_rejects_invalid_extract_route_before_write(self):
        response = self.client.post(
            "/api/config",
            json={"updates": {"EXTRACT_LINK_PROVIDER": "masi", "EXTRACT_LINK_TYPE": "pix", "EXTRACT_LINK_UPDATE_MODE": "sse"}},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("组合不受支持", response.get_json()["error"])

    def test_config_rejects_invalid_proxy_without_echoing_credentials(self):
        proxy = "ftp://user:secret-password@proxy.test:21/path"
        response = self.client.post(
            "/api/config", json={"updates": {"EXTRACT_LINK_PROXY": proxy}}, headers=self.headers,
        )
        self.assertEqual(response.status_code, 400)
        body = response.get_data(as_text=True)
        self.assertNotIn(proxy, body)
        self.assertNotIn("secret-password", body)

    def test_config_read_does_not_return_proxy_credentials(self):
        proxy = "http://user:secret-password@127.0.0.1:7816"
        with patch.dict(os.environ, {"EXTRACT_LINK_PROXY": proxy}):
            response = self.client.get("/api/config", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertNotIn(proxy, body)
        self.assertNotIn("secret-password", body)
        field = next(item for item in response.get_json() if item["key"] == "EXTRACT_LINK_PROXY")
        self.assertEqual(field["value"], "")
        self.assertTrue(field["configured"])


if __name__ == "__main__":
    unittest.main()
