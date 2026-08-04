# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from webui.app import create_app


class AccountAtExportTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.db.get_account")
    def test_access_token_export_preserves_order_and_reports_skips(self, get_account):
        accounts = {
            1: {"id": 1, "email": "one@example.com", "access_token": "at-one"},
            2: {"id": 2, "email": "two@example.com", "access_token": ""},
        }
        get_account.side_effect = accounts.get

        response = self.client.post(
            "/api/accounts/secret-bulk",
            json={"account_ids": [2, 1, 1, "bad", 404], "field": "access_token"},
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["values"], [
            {"id": 1, "email": "one@example.com", "value": "at-one"},
        ])
        self.assertEqual(body["count"], 1)
        self.assertEqual([item["id"] for item in body["skipped"]], [2, "bad", 404])

    @patch("webui.app.db.get_account")
    def test_email_access_token_export_requires_both_values(self, get_account):
        accounts = {
            1: {"id": 1, "email": "one@example.com", "access_token": "at-one"},
            2: {"id": 2, "email": "", "access_token": "at-two"},
            3: {"id": 3, "email": "three@example.com", "access_token": ""},
        }
        get_account.side_effect = accounts.get

        response = self.client.post(
            "/api/accounts/secret-bulk",
            json={"account_ids": [1, 2, 3], "field": "email_access_token"},
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["values"], [
            {"id": 1, "email": "one@example.com", "value": "one@example.com----at-one"},
        ])
        self.assertEqual(body["count"], 1)
        self.assertEqual([item["id"] for item in body["skipped"]], [2, 3])

    @patch("webui.app.db.get_account", return_value={"id": 1, "email": "one@example.com"})
    def test_export_with_no_valid_value_returns_empty_result(self, _get_account):
        response = self.client.post(
            "/api/accounts/secret-bulk",
            json={"account_ids": [1], "field": "email_access_token"},
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["values"], [])
        self.assertEqual(body["count"], 0)
        self.assertEqual(len(body["skipped"]), 1)

    @patch("webui.app.db.get_account", return_value={"id": 1, "email": "one@example.com", "access_token": "at-one"})
    def test_export_rejects_unknown_format_field(self, _get_account):
        response = self.client.post(
            "/api/accounts/secret-bulk",
            json={"account_ids": [1], "field": "{email}::{access_token}"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("field 仅支持", response.get_json()["error"])

    @patch("webui.app.db.list_accounts_page")
    def test_compact_account_list_does_not_expose_export_values(self, list_accounts_page):
        list_accounts_page.return_value = {
            "items": [{
                "id": 1,
                "email": "one@example.com",
                "access_token": "at-secret",
                "copy_line": "one@example.com----at-secret",
            }],
            "total": 1,
        }

        response = self.client.get("/api/accounts?paged=1&page=1&page_size=20")

        self.assertEqual(response.status_code, 200)
        item = response.get_json()["items"][0]
        self.assertTrue(item["has_access_token"])
        self.assertNotIn("access_token", item)
        self.assertNotIn("email_access_token", item)
        self.assertNotIn("copy_line", item)

    @patch("webui.app.db.list_accounts_page")
    def test_account_list_passes_codex_success_filter_before_pagination(self, list_accounts_page):
        list_accounts_page.return_value = {"items": [], "total": 0}

        response = self.client.get(
            "/api/accounts?paged=1&page=2&page_size=20&codex_status=success"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list_accounts_page.call_args.kwargs["codex_status_filter"], "success")
        self.assertEqual(list_accounts_page.call_args.kwargs["offset"], 20)

    @patch("webui.app.db.list_account_plan_check_statuses")
    def test_account_status_poll_uses_same_codex_filter(self, list_statuses):
        list_statuses.return_value = {"items": [], "total": 0, "revision": "0"}

        response = self.client.get(
            "/api/accounts/plan-check-status?page=1&page_size=20&codex_status=success"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list_statuses.call_args.kwargs["codex_status_filter"], "success")


if __name__ == "__main__":
    unittest.main()
