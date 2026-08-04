# -*- coding: utf-8 -*-
import unittest

from core.extract_link_providers import LegacyExtractProvider, MasiKakaoProvider, ProviderError


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self.payload = payload if payload is not None else {}
        self.text = text
        self.closed = False

    def json(self):
        return self.payload

    def close(self):
        self.closed = True

    def iter_lines(self):
        return iter([b"event: done", b"data: {}", b""])


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)


class ExtractLinkProviderTests(unittest.TestCase):
    def test_masi_contract_uses_header_and_documented_paths(self):
        session = FakeSession([
            FakeResponse(payload={"cdk": {"total_uses": 10, "remaining_uses": 8, "pending_uses": 1, "available_uses": 7}}),
            FakeResponse(payload={"ok": True, "job": {"job_id": "job-1", "status": "queued"}}),
            FakeResponse(payload={"ok": True, "job": {"job_id": "job-1", "status": "completed", "output": {"long_url": "https://pay.test"}}}),
            FakeResponse(payload={"ok": True}),
        ])
        provider = MasiKakaoProvider(base_url="https://masi.test", timeout=30, session=session)

        quota = provider.query_quota(cdk="CDK-1")
        created = provider.create_job(cdk="CDK-1", access_token="AT-1")
        job = provider.get_job(cdk="CDK-1", job_id="job-1")
        provider.cancel_job(cdk="CDK-1", job_id="job-1")

        self.assertEqual(quota["available_uses"], 7)
        self.assertEqual(created["job_id"], "job-1")
        self.assertEqual(job["status"], "completed")
        self.assertEqual([call[1] for call in session.calls], [
            "https://masi.test/v1/cdk/status",
            "https://masi.test/v1/kakao/jobs",
            "https://masi.test/v1/kakao/jobs/job-1",
            "https://masi.test/v1/kakao/jobs/job-1/cancel",
        ])
        self.assertTrue(all(call[2]["headers"]["X-CDK"] == "CDK-1" for call in session.calls))
        self.assertEqual(session.calls[1][2]["json"], {"access_token": "AT-1"})
        self.assertEqual(session.calls[3][2]["json"], {})

    def test_masi_quota_requires_all_integer_fields(self):
        session = FakeSession([FakeResponse(payload={"cdk": {"total_uses": 10}})])
        provider = MasiKakaoProvider(base_url="https://masi.test", timeout=30, session=session)
        with self.assertRaisesRegex(ProviderError, "remaining_uses"):
            provider.query_quota(cdk="CDK-1")

    def test_legacy_create_contract_is_preserved(self):
        session = FakeSession([FakeResponse(payload={"job_id": "legacy-job"})])
        provider = LegacyExtractProvider(
            base_url="https://legacy.test", cdk="OLD-CDK", timeout=30, event_timeout=180, session=session,
        )
        job = provider.create_job(access_token="AT", link_type="pix")
        self.assertEqual(job["job_id"], "legacy-job")
        self.assertEqual(session.calls[0][1], "https://legacy.test/api/extract")
        self.assertEqual(session.calls[0][2]["json"], {"link_type": "pix", "cdk": "OLD-CDK", "token": "AT"})

    def test_error_response_redacts_request_credentials_and_closes_response(self):
        secret_cdk = "KSCAN-SECRET-CDK"
        secret_at = "SECRET-ACCESS-TOKEN"
        response = FakeResponse(status_code=400, payload={"error": f"bad {secret_cdk} {secret_at}"})
        session = FakeSession([response])
        provider = MasiKakaoProvider(base_url="https://masi.test", timeout=5, session=session)

        with self.assertRaises(ProviderError) as caught:
            provider.create_job(cdk=secret_cdk, access_token=secret_at)

        self.assertNotIn(secret_cdk, str(caught.exception))
        self.assertNotIn(secret_at, str(caught.exception))
        self.assertTrue(response.closed)

    def test_proxy_is_used_for_all_masi_requests(self):
        proxy = "http://user:pass@proxy.test:8080"
        session = FakeSession([
            FakeResponse(payload={"cdk": {"total_uses": 1, "remaining_uses": 1, "pending_uses": 0, "available_uses": 1}}),
            FakeResponse(payload={"job": {"job_id": "job", "status": "queued"}}),
            FakeResponse(payload={"job": {"job_id": "job", "status": "running"}}),
            FakeResponse(payload={"ok": True}),
        ])
        provider = MasiKakaoProvider(base_url="https://masi.test", timeout=5, proxy=proxy, session=session)
        provider.query_quota(cdk="CDK")
        provider.create_job(cdk="CDK", access_token="AT")
        provider.get_job(cdk="CDK", job_id="job")
        provider.cancel_job(cdk="CDK", job_id="job")
        expected = {"http": proxy, "https": proxy}
        self.assertTrue(all(call[2].get("proxies") == expected for call in session.calls))

    def test_proxy_is_used_for_legacy_json_and_sse_requests(self):
        proxy = "socks5h://proxy.test:1080"
        session = FakeSession([
            FakeResponse(payload={"remaining_uses": 3}),
            FakeResponse(payload={"job_id": "job"}),
            FakeResponse(payload={}),
        ])
        provider = LegacyExtractProvider(
            base_url="https://legacy.test", cdk="CDK", timeout=5, event_timeout=5, proxy=proxy, session=session,
        )
        provider.query_quota()
        provider.create_job(access_token="AT", link_type="pix")
        list(provider.iter_events(job_id="job"))
        expected = {"http": proxy, "https": proxy}
        self.assertTrue(all(call[2].get("proxies") == expected for call in session.calls))

    def test_error_response_redacts_proxy_credentials(self):
        proxy = "http://user:secret-password@proxy.test:8080"
        session = FakeSession([FakeResponse(status_code=502, payload={"error": f"failed via {proxy}"})])
        provider = MasiKakaoProvider(base_url="https://masi.test", timeout=5, proxy=proxy, session=session)
        with self.assertRaises(ProviderError) as caught:
            provider.query_quota(cdk="CDK")
        self.assertNotIn(proxy, str(caught.exception))
        self.assertNotIn("secret-password", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
