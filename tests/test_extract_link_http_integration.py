# -*- coding: utf-8 -*-
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import requests

from core import extract_link_service as service
from core import masi_cdk_pool as pool
from core.extract_link_providers import LegacyExtractProvider, MasiKakaoProvider


class _MockExtractHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    calls = []
    masi_poll_count = 0

    def log_message(self, *_args):
        return

    def _json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        type(self).calls.append(("GET", self.path, self.headers.get("X-CDK"), None))
        if self.path == "http://extract.invalid/v1/cdk/status":
            self._json(200, {"ok": True, "cdk": {"total_uses": 2, "remaining_uses": 2, "pending_uses": 0, "available_uses": 2}})
            return
        if self.path.startswith("/api/jobs/legacy-job/events"):
            body = (
                'event: log\ndata: {"message":"legacy running"}\n\n'
                'event: result\ndata: {"result":{"long_url":"https://pay.invalid/legacy"}}\n\n'
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/v1/cdk/status":
            cdk = self.headers.get("X-CDK")
            quota = (
                {"total_uses": 1, "remaining_uses": 0, "pending_uses": 0, "available_uses": 0}
                if cdk == "CDK-EXHAUSTED"
                else {"total_uses": 10, "remaining_uses": 8, "pending_uses": 1, "available_uses": 7}
            )
            self._json(200, {"ok": True, "cdk": quota})
            return
        if self.path == "/v1/kakao/jobs/masi-job":
            type(self).masi_poll_count += 1
            status = "running" if type(self).masi_poll_count == 1 else "completed"
            job = {"job_id": "masi-job", "status": status}
            if status == "completed":
                job["output"] = {"long_url": "https://pay.invalid/masi", "vendor_field": "kept"}
            self._json(200, {"ok": True, "job": job})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        body = self._body()
        type(self).calls.append(("POST", self.path, self.headers.get("X-CDK"), body))
        if self.path == "/api/extract":
            self._json(200, {"job_id": "legacy-job", "status": "queued", "cdk_remaining": 3})
            return
        if self.path == "/v1/kakao/jobs":
            self._json(200, {"ok": True, "job": {"job_id": "masi-job", "status": "queued"}})
            return
        if self.path == "/v1/kakao/jobs/masi-job/cancel":
            self._json(200, {"ok": True})
            return
        self._json(404, {"error": "not found"})


class ExtractLinkHttpIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _MockExtractHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)

    def setUp(self):
        _MockExtractHandler.calls = []
        _MockExtractHandler.masi_poll_count = 0
        self.tempdir = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(pool, "_POOL_PATH", Path(self.tempdir.name) / "pool.json")
        self.path_patch.start()
        pool.reset_runtime_leases()

    def tearDown(self):
        pool.reset_runtime_leases()
        self.path_patch.stop()
        self.tempdir.cleanup()

    def test_legacy_sse_end_to_end_against_local_http(self):
        adapter = LegacyExtractProvider(
            base_url=self.base_url,
            cdk="LEGACY-CDK",
            timeout=2,
            event_timeout=2,
            session=requests.Session(),
        )
        route = {"base_url": self.base_url, "cdk": "LEGACY-CDK", "request_timeout": 2, "wait_timeout": 2, "link_type": "pix"}
        with patch.object(service, "_legacy_provider", return_value=adapter), patch.object(service.db, "update_account_extract"):
            result = service._run_legacy(account_id=1, access_token="FAKE-AT", route=route)
        self.assertEqual(result["result"]["long_url"], "https://pay.invalid/legacy")
        create = next(call for call in _MockExtractHandler.calls if call[1] == "/api/extract")
        self.assertEqual(create[3], {"link_type": "pix", "cdk": "LEGACY-CDK", "token": "FAKE-AT"})
        self.assertTrue(any("/events?cdk=LEGACY-CDK" in call[1] for call in _MockExtractHandler.calls))

    def test_masi_multi_cdk_poll_end_to_end_against_local_http(self):
        imported = pool.import_cdks("CDK-EXHAUSTED\nCDK-USABLE")
        exhausted_id, usable_id = [item["id"] for item in imported["added"]]
        adapter = MasiKakaoProvider(base_url=self.base_url, timeout=2, session=requests.Session())
        route = {"base_url": self.base_url, "request_timeout": 2, "wait_timeout": 5, "provider": "masi", "link_type": "kakao_pay", "update_mode": "poll"}
        updates = []
        with patch.object(service, "_masi_provider", return_value=adapter), \
             patch.object(service, "_runtime_setting", side_effect=lambda key, default=None: 0.01 if key == "EXTRACT_LINK_POLL_INTERVAL" else default), \
             patch.object(service.db, "update_account_extract", side_effect=lambda _id, data: updates.append(dict(data)) or True):
            result = service._run_masi(account_id=7, access_token="FAKE-AT", route=route)

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["result"]["vendor_field"], "kept")
        pools = {item["id"]: item["pool"] for item in pool.list_cdks()}
        self.assertEqual(pools[exhausted_id], "exhausted")
        self.assertEqual(pools[usable_id], "selectable")
        binding = next(item for item in updates if item.get("job_id") == "masi-job")
        self.assertEqual(binding["cdk_id"], usable_id)
        self.assertEqual(binding["provider"], "masi")
        create = next(call for call in _MockExtractHandler.calls if call[1] == "/v1/kakao/jobs")
        polls = [call for call in _MockExtractHandler.calls if call[1] == "/v1/kakao/jobs/masi-job"]
        self.assertEqual(create[2], "CDK-USABLE")
        self.assertEqual(create[3], {"access_token": "FAKE-AT"})
        self.assertEqual(len(polls), 2)
        self.assertTrue(all(call[2] == "CDK-USABLE" for call in polls))
        self.assertFalse(any(call[1].endswith("/cancel") for call in _MockExtractHandler.calls))

    def test_explicit_proxy_turns_failed_direct_path_into_success(self):
        direct = MasiKakaoProvider(base_url="http://extract.invalid", timeout=1)
        try:
            with self.assertRaises(Exception):
                direct.query_quota(cdk="FAKE-CDK")
        finally:
            direct.close()

        proxied = MasiKakaoProvider(base_url="http://extract.invalid", timeout=2, proxy=self.base_url)
        try:
            quota = proxied.query_quota(cdk="FAKE-CDK")
        finally:
            proxied.close()
        self.assertEqual(quota["available_uses"], 2)
        proxy_call = next(call for call in _MockExtractHandler.calls if call[1] == "http://extract.invalid/v1/cdk/status")
        self.assertEqual(proxy_call[2], "FAKE-CDK")


if __name__ == "__main__":
    unittest.main()
