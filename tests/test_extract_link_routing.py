# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import extract_link_service as service


class ExtractLinkRoutingTests(unittest.TestCase):
    def test_default_route_preserves_legacy_sse(self):
        values = {
            "EXTRACT_LINK_PROVIDER": "legacy",
            "EXTRACT_LINK_TYPE": "pix",
            "EXTRACT_LINK_UPDATE_MODE": "sse",
            "EXTRACT_LINK_API_BASE": "https://legacy.test",
            "EXTRACT_LINK_CDK": "secret-cdk",
            "EXTRACT_LINK_REQUEST_TIMEOUT": 30,
            "EXTRACT_LINK_EVENT_TIMEOUT": 180,
        }
        with patch.object(service, "_runtime_setting", side_effect=lambda key, default=None: values.get(key, default)):
            route = service.resolve_route()
        self.assertEqual(route["provider"], "legacy")
        self.assertEqual(route["update_mode"], "sse")
        self.assertEqual(route["link_type"], "pix")

    def test_masi_kakao_poll_is_supported(self):
        with patch.object(service, "_runtime_setting", side_effect=lambda key, default=None: default):
            route = service.resolve_route(provider="masi", link_type="kakao_pay", update_mode="poll")
        self.assertEqual(route["base_url"], "https://masi.cc.cd")
        self.assertNotIn("cdk", route)

    def test_invalid_combination_is_rejected_without_credentials(self):
        with self.assertRaisesRegex(ValueError, "provider=masi") as caught:
            service.resolve_route(provider="masi", link_type="pix", update_mode="sse")
        self.assertNotIn("CDK-SECRET", str(caught.exception))

    def test_capabilities_are_serializable(self):
        caps = service.provider_capabilities()
        self.assertEqual(caps["legacy"]["update_modes"], ["sse"])
        self.assertEqual(caps["masi"]["link_types"], ["kakao_pay"])

    def test_proxy_supports_empty_and_expected_schemes(self):
        self.assertEqual(service.validate_extract_proxy(""), "")
        for value in (
            "http://127.0.0.1:7816",
            "https://proxy.test:8443",
            "socks5://user:pass@proxy.test:1080",
            "socks5h://proxy.test:1080",
        ):
            with self.subTest(value=value):
                self.assertEqual(service.validate_extract_proxy(value), value)

    def test_invalid_proxy_error_does_not_echo_credentials(self):
        secret = "user:secret-password"
        with self.assertRaises(ValueError) as caught:
            service.validate_extract_proxy(f"ftp://{secret}@proxy.test:21/path?token=secret")
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn("secret-password", str(caught.exception))

    def test_route_snapshots_proxy_for_running_task(self):
        values = {
            "MASI_KAKAO_API_BASE": "https://masi.test",
            "EXTRACT_LINK_PROXY": "http://proxy-one.test:8080",
        }
        with patch.object(service, "_runtime_setting", side_effect=lambda key, default=None: values.get(key, default)):
            first = service.resolve_route(provider="masi", link_type="kakao_pay", update_mode="poll")
            values["EXTRACT_LINK_PROXY"] = "http://proxy-two.test:8080"
            second = service.resolve_route(provider="masi", link_type="kakao_pay", update_mode="poll")
        self.assertEqual(first["proxy"], "http://proxy-one.test:8080")
        self.assertEqual(second["proxy"], "http://proxy-two.test:8080")


if __name__ == "__main__":
    unittest.main()
