# -*- coding: utf-8 -*-
import unittest
from unittest.mock import Mock, patch

from config import email as email_config
from core.mail import email_provider
from core.mail.email_transport import (
    configure_curl_session,
    direct_email_requests,
    direct_email_requests_enabled,
    requests_request,
    requests_session,
)


class DirectEmailTransportTests(unittest.TestCase):
    def test_requests_transport_ignores_environment_proxies_only_in_direct_scope(self):
        fake_session = Mock()
        fake_session.trust_env = True
        fake_session.__enter__ = Mock(return_value=fake_session)
        fake_session.__exit__ = Mock(return_value=False)
        with patch("requests.Session", return_value=fake_session) as session_factory:
            outside = requests_session()
            self.assertTrue(outside.trust_env)
            with direct_email_requests():
                direct = requests_session()
                self.assertFalse(direct.trust_env)
                requests_request("GET", "https://mail.example.test/otp")
                self.assertTrue(direct_email_requests_enabled())
            self.assertFalse(direct_email_requests_enabled())
        self.assertEqual(session_factory.call_count, 3)
        fake_session.request.assert_called_once_with("GET", "https://mail.example.test/otp")

    def test_curl_provider_session_ignores_environment_proxies_in_direct_scope(self):
        session = Mock(trust_env=True)
        with direct_email_requests():
            self.assertIs(configure_curl_session(session), session)
        self.assertFalse(session.trust_env)

    def test_all_automatic_otp_provider_dispatches_run_in_direct_scope(self):
        providers = {
            "outlook": "core.mail.outlook_client",
            "cloudflare": "core.mail.cf_temp_mail_client",
            "cloudflare_domain": "core.mail.qqmail_client",
            "icloud": "core.mail.icloud_client",
            "generic_api": "core.mail.generic_api_mail_client",
            "remail": "core.mail.remail_client",
        }
        for source, module in providers.items():
            with self.subTest(source=source), patch.object(
                email_config, "USE_EMAIL_SERVICE", True,
            ), patch.object(
                email_provider, "_registered_email_source", return_value=None,
            ), patch.object(
                email_provider, "resolve_email_source", return_value=source,
            ), patch(
                f"{module}.fetch_latest_otp",
                side_effect=lambda *_args, **_kwargs: (
                    "654321" if direct_email_requests_enabled() else self.fail("provider ran outside direct scope")
                ),
            ) as fetch:
                self.assertEqual(
                    email_provider.wait_for_otp("user@example.test", after_ts=123, direct=True),
                    "654321",
                )
                fetch.assert_called_once()

    def test_registration_email_acquisition_uses_direct_scope(self):
        with patch.object(
            email_provider, "parse_email_sources", return_value=["outlook"],
        ), patch.object(
            email_provider, "_pick_from_source",
            side_effect=lambda _source: (
                "direct@example.test" if direct_email_requests_enabled()
                else self.fail("email allocation ran outside direct scope")
            ),
        ) as pick:
            self.assertEqual(email_provider.acquire_email(direct=True), "direct@example.test")
        pick.assert_called_once_with("outlook")

    def test_browser_registration_email_acquisition_requests_direct_route(self):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_provider, "acquire_email", return_value="direct@example.test",
        ) as acquire:
            self.assertEqual(email_provider.acquire_email_after_input(), "direct@example.test")
        acquire.assert_called_once_with(direct=True)


if __name__ == "__main__":
    unittest.main()
