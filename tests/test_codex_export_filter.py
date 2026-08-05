# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from webui.app import create_app


class CodexExportFilterTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
        self.rows = [
            {"filename": "codex-new@example.com.json", "email": "new@example.com", "exported_count": 0},
            {"filename": "codex-used@example.com.json", "email": "used@example.com", "exported_count": 2},
            {"filename": "codex-reset@example.com.json", "email": "reset@example.com", "exported_count": None},
        ]

    @patch("webui.app.db.codex_accounts_summary", return_value={"total": 3, "exported": 1, "pending": 2})
    @patch("webui.app.db.list_codex_accounts")
    def test_pending_filter_runs_before_pagination(self, list_codex_accounts, _summary):
        list_codex_accounts.return_value = self.rows

        response = self.client.get("/api/codex?paged=1&page=1&page_size=1&export_status=pending")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["total"], 2)
        self.assertEqual([item["email"] for item in body["accounts"]], ["new@example.com"])
        self.assertEqual(body["summary"], {"total": 3, "exported": 1, "pending": 2})

    @patch("webui.app.db.codex_accounts_summary", return_value={"total": 3, "exported": 1, "pending": 2})
    @patch("webui.app.db.list_codex_accounts")
    def test_exported_filter_can_be_combined_with_search(self, list_codex_accounts, _summary):
        list_codex_accounts.return_value = self.rows

        response = self.client.get("/api/codex?export_status=exported&q=used")

        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["email"] for item in response.get_json()["accounts"]], ["used@example.com"])

    @patch("webui.app.db.list_codex_accounts", return_value=[])
    def test_invalid_export_status_is_rejected(self, _list_codex_accounts):
        response = self.client.get("/api/codex?export_status=unknown")

        self.assertEqual(response.status_code, 400)
        self.assertIn("export_status 仅支持", response.get_json()["error"])

    def test_account_page_exposes_export_filter(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'id="codexExportFilterV2"', response.data)
        self.assertIn(b'data-codex-export-status="pending"', response.data)


if __name__ == "__main__":
    unittest.main()
