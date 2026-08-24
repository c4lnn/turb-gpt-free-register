# -*- coding: utf-8 -*-
import unittest
from pathlib import Path
from unittest.mock import patch

from webui.app import create_app


def _filtered(rows):
    def list_rows(status=None, limit=500):
        matched = [row for row in rows if not status or row.get("status") == status]
        return matched[:limit]

    return list_rows


class EmailPoolFilterApiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.db.list_icloud_email_pool")
    def test_query_matches_email_only_and_ignores_case(self, list_icloud):
        list_icloud.return_value = [
            {
                "email": "Target.User@example.invalid",
                "status": "available",
                "note": "ordinary",
            },
            {
                "email": "other@example.invalid",
                "status": "disabled",
                "note": "target.user only appears outside email",
                "copy_line": "TARGET.USER",
            },
        ]

        response = self.client.get("/api/outlook?source=icloud&q=target.user")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["email"] for item in response.get_json()],
            ["Target.User@example.invalid"],
        )

    @patch("webui.app.db.list_icloud_email_pool")
    def test_status_text_does_not_match_non_email_fields(self, list_icloud):
        list_icloud.return_value = [
            {"email": "one@example.invalid", "status": "available", "note": "可用"},
            {"email": "two@example.invalid", "status": "disabled", "note": "已停用"},
        ]

        response = self.client.get("/api/outlook?source=icloud&q=可用")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), [])

    @patch("webui.app.db.list_icloud_email_pool")
    @patch("webui.app.db.list_domain_email_pool")
    @patch("webui.app.db.list_generic_api_email_pool")
    @patch("webui.app.db.list_outlook_pool")
    def test_source_status_query_and_pagination_are_combined(
        self,
        list_outlook,
        list_generic,
        list_domain,
        list_icloud,
    ):
        list_outlook.side_effect = _filtered([
            {"email": "match-outlook@example.invalid", "status": "disabled", "created_at": "2026-08-04"},
            {"email": "available-outlook@example.invalid", "status": "available", "created_at": "2026-08-05"},
        ])
        list_generic.side_effect = _filtered([
            {"email": "match-generic@example.invalid", "status": "disabled", "created_at": "2026-08-03"},
        ])
        list_domain.side_effect = _filtered([
            {"email": "other-domain@example.invalid", "status": "disabled", "created_at": "2026-08-02"},
        ])
        list_icloud.side_effect = _filtered([
            {"email": "match-icloud@example.invalid", "status": "used", "created_at": "2026-08-01"},
        ])

        response = self.client.get(
            "/api/outlook?source=all&status=disabled&q=match-&paged=1&page=1&page_size=1"
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["total"], 2)
        self.assertEqual(body["page_size"], 1)
        self.assertEqual(body["items"][0]["email"], "match-outlook@example.invalid")
        self.assertEqual(body["items"][0]["source"], "outlook")
        for mocked in (list_outlook, list_generic, list_domain, list_icloud):
            mocked.assert_called_once_with(status="disabled", limit=1_000_000)


class EmailPoolFilterTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = (
            Path(__file__).parents[1] / "webui" / "templates" / "index.html"
        ).read_text(encoding="utf-8")

    def test_status_filter_exposes_all_supported_values(self):
        self.assertIn('id="poolStatusV2Wrap"', self.template)
        self.assertIn('id="poolStatusV2Btn"', self.template)
        for value, label in (
            ("all", "全部状态"),
            ("available", "可用"),
            ("registering", "注册中"),
            ("used", "已用"),
            ("failed", "失败"),
            ("disabled", "已停用"),
        ):
            self.assertIn(f'data-value="{value}" role="option">{label}</button>', self.template)

    def test_search_and_request_use_separate_query_parameters(self):
        self.assertIn('placeholder="搜索邮箱"', self.template)
        self.assertNotIn('placeholder="搜索邮箱、状态…"', self.template)
        self.assertIn("const status = getPoolStatus();", self.template)
        self.assertIn("&status=${encodeURIComponent(status)}&q=${encodeURIComponent(q)}`", self.template)

    def test_filter_changes_reset_expected_state(self):
        self.assertIn("function onPoolStatusChange() {", self.template)
        self.assertIn("PAGERS.outlook.page = 1;\n  OUTLOOK_SELECTED.clear();\n  loadOutlook();", self.template)
        self.assertIn("PAGERS.outlook.page = 1; loadOutlook();", self.template)


if __name__ == "__main__":
    unittest.main()
