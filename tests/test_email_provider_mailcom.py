# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import email as email_config
from core import email_provider
from core.mailcom_client import MailComAccount


class MailComEmailProviderRoutingTests(unittest.TestCase):
    def test_source_contract_and_pick(self):
        self.assertIn("mailcom", email_provider.parse_email_sources("mailcom,outlook"))
        with patch("core.mailcom_provider.pick_account", return_value=MailComAccount(email="a@mail.com")) as pick:
            with patch.object(email_provider, "parse_email_sources", return_value=["mailcom"]):
                self.assertEqual(email_provider.acquire_email(), "a@mail.com")
        pick.assert_called_once()

    @patch("core.mailcom_provider.fetch_latest_otp", return_value="123456")
    @patch.object(email_provider, "resolve_email_source", return_value="mailcom")
    def test_wait_for_otp_routes_after_ts_and_options(self, resolve, fetch):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True):
            result = email_provider.wait_for_otp(
                "a@mail.com", after_ts=123.0, max_wait=9, poll_interval=2, settle_seconds=1
            )
        self.assertEqual(result, "123456")
        fetch.assert_called_once_with("a@mail.com", after_ts=123.0, max_wait=9, poll_interval=2, settle_seconds=1)

    @patch("core.mailcom_provider.release_account")
    @patch.object(email_provider, "resolve_email_source", return_value="mailcom")
    def test_release_and_unconsumed_release_are_source_isolated(self, resolve, release):
        self.assertEqual(email_provider.release_email("a@mail.com", status="disabled", note="bad"), "mailcom")
        release.assert_called_once_with("a@mail.com", status="disabled", note="bad")
        with patch("core.db.release_unconsumed_mailcom_email", return_value=True) as unconsumed:
            self.assertTrue(email_provider.release_email_if_unconsumed("a@mail.com", note="unused"))
        unconsumed.assert_called_once_with("a@mail.com", note="unused")

    def test_mailcom_alias_create_failure_does_not_fall_back_to_next_source(self):
        with (
            patch.object(email_provider, "parse_email_sources", return_value=["mailcom", "outlook"]),
            patch.object(email_provider, "_pick_from_source", side_effect=RuntimeError("alias_capacity_full")) as pick,
        ):
            with self.assertRaisesRegex(RuntimeError, "停止来源回退"):
                email_provider.acquire_email()
        self.assertEqual(pick.call_count, 1)


if __name__ == "__main__":
    unittest.main()
