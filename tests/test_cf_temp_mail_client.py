# -*- coding: utf-8 -*-
import string
import unittest
from unittest.mock import Mock, patch

from core import cf_temp_mail_client as client


class CFTempMailClientTests(unittest.TestCase):
    def setUp(self):
        client._CONTEXT_CACHE.clear()
        client._DOMAIN_COUNTER = 0

    def test_pick_account_requires_api_base(self):
        with patch.object(client._email_cfg, "CLOUDFLARE_API_BASE", "", create=True):
            with self.assertRaisesRegex(client.CFTempMailError, "请填写 CLOUDFLARE_API_BASE"):
                client.pick_account()

    @patch("core.cf_temp_mail_client.requests.request")
    def test_pick_account_anonymous_create(self, request_mock):
        response = Mock(status_code=200)
        response.json.return_value = {
            "address": "abc123@mail.example.com",
            "jwt": "jwt-token-1",
        }
        request_mock.return_value = response

        with patch.object(client._email_cfg, "CLOUDFLARE_API_BASE", "https://mail.example.com", create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_AUTH_MODE", "none", create=True
        ), patch.object(client._email_cfg, "CLOUDFLARE_API_KEY", "", create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_PATH_ACCOUNTS", "/api/new_address", create=True
        ), patch.object(client._email_cfg, "CLOUDFLARE_DEFAULT_DOMAINS", ["mail.example.com"], create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_CUSTOM_AUTH", "", create=True
        ), patch.object(client._email_cfg, "CLOUDFLARE_RANDOM_SUBDOMAIN_ENABLED", False, create=True
        ):
            account = client.pick_account()

        self.assertEqual(account.email, "abc123@mail.example.com")
        self.assertEqual(account.jwt, "jwt-token-1")
        self.assertIs(client.get_account_context(account.email), account)
        args, kwargs = request_mock.call_args
        self.assertEqual(args[0], "POST")
        self.assertEqual(args[1], "https://mail.example.com/api/new_address")
        self.assertEqual(kwargs["json"], {"domain": "mail.example.com"})

    @patch("core.cf_temp_mail_client.requests.request")
    def test_admin_create_uses_name_payload_and_header(self, request_mock):
        response = Mock(status_code=200)
        response.json.return_value = {"address": "u@mail.example.com", "jwt": "jwt-2"}
        request_mock.return_value = response

        with patch.object(client._email_cfg, "CLOUDFLARE_API_BASE", "https://mail.example.com", create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_AUTH_MODE", "x-admin-auth", create=True
        ), patch.object(client._email_cfg, "CLOUDFLARE_API_KEY", "admin-pass", create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_PATH_ACCOUNTS", "/admin/new_address", create=True
        ), patch.object(client._email_cfg, "CLOUDFLARE_DEFAULT_DOMAINS", ["mail.example.com"], create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_CUSTOM_AUTH", "global-pass", create=True
        ), patch.object(client._email_cfg, "CLOUDFLARE_NAME_LENGTH", 10, create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_RANDOM_SUBDOMAIN_ENABLED", False, create=True
        ):
            account = client.pick_account()

        self.assertEqual(account.email, "u@mail.example.com")
        _, kwargs = request_mock.call_args
        self.assertEqual(kwargs["headers"]["x-admin-auth"], "admin-pass")
        self.assertEqual(kwargs["headers"]["x-custom-auth"], "global-pass")
        self.assertEqual(kwargs["json"]["enablePrefix"], True)
        self.assertEqual(kwargs["json"]["domain"], "mail.example.com")
        self.assertIn("name", kwargs["json"])

    @patch("core.cf_temp_mail_client.requests.request")
    def test_disabled_random_subdomain_preserves_domain_rotation(self, request_mock):
        first = Mock(status_code=200)
        first.json.return_value = {"address": "a@one.example.com", "jwt": "jwt-1"}
        second = Mock(status_code=200)
        second.json.return_value = {"address": "b@two.example.com", "jwt": "jwt-2"}
        request_mock.side_effect = [first, second]

        with patch.object(client._email_cfg, "CLOUDFLARE_API_BASE", "https://mail.example.com", create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_AUTH_MODE", "none", create=True
        ), patch.object(client._email_cfg, "CLOUDFLARE_API_KEY", "", create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_PATH_ACCOUNTS", "/api/new_address", create=True
        ), patch.object(client._email_cfg, "CLOUDFLARE_DEFAULT_DOMAINS", ["one.example.com", "two.example.com"], create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_CUSTOM_AUTH", "", create=True
        ), patch.object(client._email_cfg, "CLOUDFLARE_RANDOM_SUBDOMAIN_ENABLED", False, create=True):
            client.create_address()
            client.create_address()

        payloads = [call.kwargs["json"] for call in request_mock.call_args_list]
        self.assertEqual(payloads, [{"domain": "one.example.com"}, {"domain": "two.example.com"}])

    @patch("core.cf_temp_mail_client.secrets.choice", side_effect=list("kqmfax"))
    @patch("core.cf_temp_mail_client.requests.request")
    def test_anonymous_create_uses_random_subdomain(self, request_mock, choice):
        response = Mock(status_code=200)
        response.json.return_value = {"address": "u@kqmfax-mail.example.com", "jwt": "jwt-random"}
        request_mock.return_value = response

        with patch.object(client._email_cfg, "CLOUDFLARE_API_BASE", "https://mail.example.com", create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_AUTH_MODE", "none", create=True
        ), patch.object(client._email_cfg, "CLOUDFLARE_API_KEY", "", create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_PATH_ACCOUNTS", "/api/new_address", create=True
        ), patch.object(client._email_cfg, "CLOUDFLARE_DEFAULT_DOMAINS", ["example.com"], create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_RANDOM_SUBDOMAIN_ENABLED", True, create=True
        ), patch.object(client._email_cfg, "CLOUDFLARE_RANDOM_SUBDOMAIN_LENGTH", 6, create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_RANDOM_SUBDOMAIN_SUFFIX", "mail", create=True):
            account = client.create_address()

        self.assertEqual(account.domain, "kqmfax-mail.example.com")
        self.assertEqual(request_mock.call_args.kwargs["json"], {"domain": "kqmfax-mail.example.com"})
        self.assertEqual(choice.call_count, 6)
        self.assertTrue(all(call.args[0] == string.ascii_lowercase for call in choice.call_args_list))

    @patch("core.cf_temp_mail_client._generate_local", return_value="localname1")
    @patch("core.cf_temp_mail_client.secrets.choice", return_value="q")
    @patch("core.cf_temp_mail_client.requests.request")
    def test_admin_create_changes_only_domain_for_random_subdomain(self, request_mock, choice, generate_local):
        response = Mock(status_code=200)
        response.json.return_value = {"address": "localname1@qqqqqq-mail.example.com", "jwt": "jwt-admin"}
        request_mock.return_value = response

        with patch.object(client._email_cfg, "CLOUDFLARE_API_BASE", "https://mail.example.com", create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_AUTH_MODE", "x-admin-auth", create=True
        ), patch.object(client._email_cfg, "CLOUDFLARE_API_KEY", "admin", create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_PATH_ACCOUNTS", "/admin/new_address", create=True
        ), patch.object(client._email_cfg, "CLOUDFLARE_DEFAULT_DOMAINS", ["example.com"], create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_RANDOM_SUBDOMAIN_ENABLED", True, create=True
        ), patch.object(client._email_cfg, "CLOUDFLARE_RANDOM_SUBDOMAIN_LENGTH", 6, create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_RANDOM_SUBDOMAIN_SUFFIX", "mail", create=True):
            client.create_address()

        self.assertEqual(request_mock.call_args.kwargs["json"], {
            "name": "localname1",
            "enablePrefix": True,
            "domain": "qqqqqq-mail.example.com",
        })
        generate_local.assert_called_once_with()

    def test_random_subdomain_config_validation(self):
        cases = (
            ({"CLOUDFLARE_RANDOM_SUBDOMAIN_LENGTH": 0}, "1-32"),
            ({"CLOUDFLARE_RANDOM_SUBDOMAIN_LENGTH": "six"}, "1-32"),
            ({"CLOUDFLARE_RANDOM_SUBDOMAIN_SUFFIX": ""}, "必须填写固定后缀"),
            ({"CLOUDFLARE_RANDOM_SUBDOMAIN_SUFFIX": "Bad"}, "只允许小写字母"),
            ({"CLOUDFLARE_RANDOM_SUBDOMAIN_SUFFIX": "bad_suffix"}, "只允许小写字母"),
            ({"CLOUDFLARE_RANDOM_SUBDOMAIN_LENGTH": 32, "CLOUDFLARE_RANDOM_SUBDOMAIN_SUFFIX": "a" * 31}, "63"),
        )
        defaults = {
            "CLOUDFLARE_RANDOM_SUBDOMAIN_ENABLED": True,
            "CLOUDFLARE_RANDOM_SUBDOMAIN_LENGTH": 6,
            "CLOUDFLARE_RANDOM_SUBDOMAIN_SUFFIX": "mail",
            "CLOUDFLARE_DEFAULT_DOMAINS": ["example.com"],
        }
        for overrides, expected in cases:
            values = {**defaults, **overrides}
            with self.subTest(overrides=overrides), patch.multiple(client._email_cfg, create=True, **values):
                with self.assertRaisesRegex(client.CFTempMailError, expected):
                    client.validate_random_subdomain_config()

    @patch("core.cf_temp_mail_client.secrets.choice", side_effect=list("abcdefuvwxyz"))
    def test_random_subdomain_is_generated_for_each_create(self, choice):
        with patch.object(client._email_cfg, "CLOUDFLARE_RANDOM_SUBDOMAIN_ENABLED", True, create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_RANDOM_SUBDOMAIN_LENGTH", 6, create=True
        ), patch.object(client._email_cfg, "CLOUDFLARE_RANDOM_SUBDOMAIN_SUFFIX", "mail", create=True):
            first = client._with_random_subdomain("example.com")
            second = client._with_random_subdomain("example.com")

        self.assertEqual(first, "abcdef-mail.example.com")
        self.assertEqual(second, "uvwxyz-mail.example.com")

    @patch("core.cf_temp_mail_client.requests.request")
    def test_worker_domain_rejection_adds_hint_without_retry(self, request_mock):
        response = Mock(status_code=400)
        response.json.return_value = {"error": "domain not allowed"}
        request_mock.return_value = response

        with patch.object(client._email_cfg, "CLOUDFLARE_API_BASE", "https://mail.example.com", create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_AUTH_MODE", "none", create=True
        ), patch.object(client._email_cfg, "CLOUDFLARE_API_KEY", "", create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_PATH_ACCOUNTS", "/api/new_address", create=True
        ), patch.object(client._email_cfg, "CLOUDFLARE_DEFAULT_DOMAINS", ["example.com"], create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_RANDOM_SUBDOMAIN_ENABLED", True, create=True
        ), patch.object(client._email_cfg, "CLOUDFLARE_RANDOM_SUBDOMAIN_LENGTH", 6, create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_RANDOM_SUBDOMAIN_SUFFIX", "mail", create=True):
            with self.assertRaisesRegex(client.CFTempMailError, "ENABLE_CREATE_ADDRESS_SUBDOMAIN_MATCH"):
                client.create_address()

        request_mock.assert_called_once()

    @patch("core.cf_temp_mail_client.time.sleep")
    @patch("core.cf_temp_mail_client.requests.request")
    def test_fetch_latest_otp_reads_only_new_openai_email(self, request_mock, sleep):
        client._CONTEXT_CACHE["fresh@mail.example.com"] = client.CFTempMailAccount(
            email="fresh@mail.example.com",
            jwt="jwt-xyz",
            domain="mail.example.com",
        )

        inbox = Mock(status_code=200)
        inbox.json.return_value = {
            "results": [
                {
                    "id": "old",
                    "timestamp": 100,
                    "address": "fresh@mail.example.com",
                    "from": "noreply@openai.com",
                    "subject": "Code 111111",
                    "text": "Your code is 111111",
                },
                {
                    "id": "new",
                    "timestamp": 250,
                    "address": "fresh@mail.example.com",
                    "from": "noreply@openai.com",
                    "subject": "Code 654321",
                    "text": "Your code is 654321",
                },
            ]
        }
        request_mock.return_value = inbox

        with patch.object(client._email_cfg, "CLOUDFLARE_API_BASE", "https://mail.example.com", create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_PATH_MESSAGES", "/api/mails", create=True
        ), patch.object(client._email_cfg, "CLOUDFLARE_AUTH_MODE", "none", create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_API_KEY", "", create=True
        ), patch.object(client._email_cfg, "CLOUDFLARE_CUSTOM_AUTH", "", create=True):
            code = client.fetch_latest_otp(
                "fresh@mail.example.com",
                after_ts=200,
                max_wait=1,
                poll_interval=1,
                settle_seconds=0,
            )

        self.assertEqual(code, "654321")
        args, kwargs = request_mock.call_args
        self.assertEqual(args[0], "GET")
        self.assertTrue(args[1].endswith("/api/mails"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer jwt-xyz")

    def test_release_clears_context(self):
        client._CONTEXT_CACHE["a@b.com"] = client.CFTempMailAccount(email="a@b.com", jwt="t")
        client.release_account("a@b.com", status="used")
        self.assertIsNone(client.get_account_context("a@b.com"))

    def test_admin_mode_without_key_fails(self):
        with patch.object(client._email_cfg, "CLOUDFLARE_API_BASE", "https://mail.example.com", create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_AUTH_MODE", "x-admin-auth", create=True
        ), patch.object(client._email_cfg, "CLOUDFLARE_API_KEY", "", create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_PATH_ACCOUNTS", "/admin/new_address", create=True
        ):
            with self.assertRaisesRegex(client.CFTempMailError, "CLOUDFLARE_API_KEY"):
                client.pick_account()


    @patch("core.cf_temp_mail_client.requests.request")
    def test_list_messages_sends_limit_offset(self, request_mock):
        response = Mock(status_code=200)
        response.json.return_value = {"results": []}
        request_mock.return_value = response

        with patch.object(client._email_cfg, "CLOUDFLARE_API_BASE", "https://mail.example.com", create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_PATH_MESSAGES", "/api/mails", create=True
        ), patch.object(client._email_cfg, "CLOUDFLARE_AUTH_MODE", "none", create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_API_KEY", "", create=True
        ), patch.object(client._email_cfg, "CLOUDFLARE_CUSTOM_AUTH", "", create=True):
            client.list_messages("jwt-xyz")

        args, kwargs = request_mock.call_args
        self.assertEqual(args[0], "GET")
        self.assertTrue(args[1].endswith("/api/mails"))
        self.assertEqual(kwargs["params"]["limit"], 20)
        self.assertEqual(kwargs["params"]["offset"], 0)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer jwt-xyz")


    def test_created_at_without_tz_is_utc(self):
        from datetime import datetime, timezone
        ts = client._message_timestamp({"created_at": "2026-07-19 12:57:38"})
        expected = datetime(2026, 7, 19, 12, 57, 38, tzinfo=timezone.utc).timestamp()
        self.assertAlmostEqual(ts, expected, places=0)

    def test_otp_from_cloudflare_raw_openai_mail(self):
        raw = (
            "From: ChatGPT <noreply@tm.openai.com>\r\n"
            "Subject: ChatGPT code\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            "\r\n"
            "<html><body><p>Your code</p>\n449759\n</body></html>\r\n"
        )
        item = {
            "id": 77,
            "source": "bounces+x@em7877.tm.openai.com",
            "address": "user@beliefcode.online",
            "raw": raw,
            "created_at": "2026-07-19 12:57:38",
        }
        otp_item = client._otp_item(item)
        from core.otp_utils import looks_like_openai_email, extract_otp
        self.assertTrue(looks_like_openai_email(otp_item))
        self.assertEqual(extract_otp(otp_item), "449759")


if __name__ == "__main__":
    unittest.main()
