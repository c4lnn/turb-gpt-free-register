# -*- coding: utf-8 -*-
import unittest
from pathlib import Path
from unittest.mock import patch

from webui.app import create_app


TEMPLATE_PATH = Path(__file__).parents[1] / "webui" / "templates" / "index.html"


class AccountCopyFormatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = TEMPLATE_PATH.read_text(encoding="utf-8")

    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    def test_single_endpoint_supports_three_formats_and_normalizes_totp(self):
        account = {
            "id": 1,
            "email": "one@example.com",
            "access_token": "at-one",
            "totp_secret": " jbs wy3dpehp k3pxp== ",
        }
        expected = {
            "access_token": ("at-one", False, ""),
            "email_access_token": ("one@example.com----at-one", False, ""),
            "email_access_token_totp": (
                "one@example.com----at-one----JBSWY3DPEHPK3PXP",
                False,
                "",
            ),
        }

        for field, (value, fallback, fallback_reason) in expected.items():
            with patch("webui.app.db.get_account", return_value=account):
                response = self.client.get(f"/api/accounts/1/secret?field={field}")

            self.assertEqual(response.status_code, 200)
            body = response.get_json()
            self.assertEqual(body["value"], value)
            self.assertEqual(body["fallback"], fallback)
            self.assertEqual(body["fallback_reason"], fallback_reason)
            self.assertFalse(body["skipped"])
            self.assertEqual(body["skip_reason"], "")
            self.assertEqual(response.headers["Cache-Control"], "no-store, max-age=0")
            self.assertEqual(response.headers["Pragma"], "no-cache")

    def test_outlook_account_uses_standard_fields_only(self):
        account = {
            "id": 2,
            "email": "outlook@example.com",
            "access_token": "at-outlook",
            "totp_secret": "OUTLOOKSECRET",
            "password": "outlook-password",
            "client_id": "outlook-client-id",
            "refresh_token": "outlook-refresh-token",
            "original_email_line": "outlook@example.com----outlook-password----outlook-client-id----outlook-refresh-token",
            "copy_line": "outlook@example.com----outlook-password----outlook-client-id----outlook-refresh-token----at-outlook",
        }

        with patch("webui.app.db.get_account", return_value=account):
            response = self.client.get(
                "/api/accounts/2/secret?field=email_access_token_totp"
            )

        self.assertEqual(response.status_code, 200)
        value = response.get_json()["value"]
        self.assertEqual(value, "outlook@example.com----at-outlook----OUTLOOKSECRET")
        for forbidden in (
            account["password"],
            account["client_id"],
            account["refresh_token"],
            account["original_email_line"],
            account["copy_line"],
        ):
            self.assertNotIn(forbidden, value)

        with patch("webui.app.db.get_account", return_value=account):
            response = self.client.get("/api/accounts/2/secret?field=access_token")
        self.assertEqual(response.get_json()["value"], "at-outlook")

    def test_totp_format_falls_back_and_missing_fields_are_skipped(self):
        accounts = {
            1: {"id": 1, "email": "no-totp@example.com", "access_token": "at-one"},
            2: {"id": 2, "email": "no-at@example.com", "access_token": ""},
            3: {"id": 3, "email": "", "access_token": "at-three"},
        }

        with patch("webui.app.db.get_account", side_effect=accounts.get):
            fallback = self.client.get(
                "/api/accounts/1/secret?field=email_access_token_totp"
            )
        self.assertEqual(fallback.status_code, 200)
        self.assertEqual(fallback.get_json()["value"], "no-totp@example.com----at-one")
        self.assertTrue(fallback.get_json()["fallback"])
        self.assertEqual(fallback.get_json()["fallback_reason"], "missing_totp_secret")

        with patch("webui.app.db.get_account", side_effect=accounts.get):
            no_at = self.client.get(
                "/api/accounts/2/secret?field=email_access_token_totp"
            )
            no_email = self.client.get(
                "/api/accounts/3/secret?field=email_access_token_totp"
            )
        for response, reason in ((no_at, "missing_access_token"), (no_email, "missing_email")):
            body = response.get_json()
            self.assertEqual(response.status_code, 200)
            self.assertEqual(body["value"], "")
            self.assertTrue(body["skipped"])
            self.assertEqual(body["skip_reason"], reason)

    def test_bulk_preserves_order_deduplicates_and_reports_fallbacks_and_skips(self):
        accounts = {
            1: {"id": 1, "email": "one@example.com", "access_token": "at-one", "totp_secret": "ONE"},
            2: {"id": 2, "email": "two@example.com", "access_token": "at-two"},
            3: {"id": 3, "email": "three@example.com", "access_token": ""},
            4: {"id": 4, "email": "", "access_token": "at-four"},
        }

        with patch("webui.app.db.get_account", side_effect=accounts.get) as get_account:
            response = self.client.post(
                "/api/accounts/secret-bulk",
                json={
                    "account_ids": [2, 1, 2, 3, 4, "bad", 404],
                    "field": "email_access_token_totp",
                },
            )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual([item["id"] for item in body["values"]], [2, 1])
        self.assertEqual(
            [item["value"] for item in body["values"]],
            ["two@example.com----at-two", "one@example.com----at-one----ONE"],
        )
        self.assertTrue(body["values"][0]["fallback"])
        self.assertEqual(body["fallback_count"], 1)
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["skipped_count"], 4)
        self.assertEqual(
            [(item["id"], item["reason"]) for item in body["skipped"]],
            [
                (3, "missing_access_token"),
                (4, "missing_email"),
                ("bad", "ID 非法"),
                (404, "账号不存在"),
            ],
        )
        self.assertEqual([call.args[0] for call in get_account.call_args_list], [2, 1, 3, 4, 404])
        self.assertEqual(response.headers["Cache-Control"], "no-store, max-age=0")

    def test_single_and_bulk_use_the_same_formatter_result(self):
        account = {
            "id": 1,
            "email": "same@example.com",
            "access_token": "at-same",
        }

        for field in ("access_token", "email_access_token", "email_access_token_totp"):
            with patch("webui.app.db.get_account", return_value=account):
                single = self.client.get(f"/api/accounts/1/secret?field={field}").get_json()
            with patch("webui.app.db.get_account", return_value=account):
                bulk = self.client.post(
                    "/api/accounts/secret-bulk",
                    json={"account_ids": [1], "field": field},
                ).get_json()

            item = bulk["values"][0]
            self.assertEqual(single["value"], item["value"])
            self.assertEqual(single["fallback"], bool(item.get("fallback")))
            self.assertEqual(single["fallback_reason"], item.get("fallback_reason", ""))
            self.assertEqual(single["skipped"], False)
            self.assertEqual(bulk["skipped"], [])

    @patch("webui.app.db.list_accounts_page")
    def test_account_list_does_not_expose_copy_secrets(self, list_accounts_page):
        list_accounts_page.return_value = {
            "items": [{
                "id": 1,
                "email": "one@example.com",
                "access_token": "list-at-secret",
                "totp_secret": "list-totp-secret",
                "copy_line": "one@example.com----list-at-secret----list-totp-secret",
            }],
            "total": 1,
        }

        response = self.client.get("/api/accounts?paged=1&page=1&page_size=20")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        encoded = repr(body)
        for secret in ("list-at-secret", "list-totp-secret"):
            self.assertNotIn(secret, encoded)
        item = body["items"][0]
        self.assertNotIn("access_token", item)
        self.assertNotIn("totp_secret", item)
        self.assertNotIn("copy_line", item)

    def test_sensitive_error_responses_are_also_no_store_and_not_logged(self):
        with patch("webui.app.db.get_account", return_value={"id": 1, "email": "one@example.com"}), \
             patch("webui.app.logger") as logger:
            response = self.client.get("/api/accounts/1/secret?field=unknown")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.headers["Cache-Control"], "no-store, max-age=0")
        self.assertEqual(response.headers["Pragma"], "no-cache")
        self.assertNotIn("unknown", " ".join(str(call) for call in logger.mock_calls))

    def test_account_template_unifies_format_driven_copy_and_download(self):
        for option in (
            '<option value="at" selected>AT</option>',
            '<option value="email_at">邮箱----AT</option>',
            '<option value="email_at_totp">邮箱----AT----2FA</option>',
        ):
            self.assertIn(option, self.template)

        menu_start = self.template.index("function _accountsV2MoreMenu")
        menu_end = self.template.index("function closeAccountsV2MoreMenus", menu_start)
        account_menu = self.template[menu_start:menu_end]
        self.assertIn('data-account-copy-format="true"', account_menu)
        self.assertNotIn('data-account-copy-secret="copy_line"', account_menu)
        self.assertNotIn("copy_line", account_menu)

        format_start = self.template.index("function accountAtExportFormat")
        format_end = self.template.index("async function fetchAccountAtExport", format_start)
        format_block = self.template[format_start:format_end]
        self.assertIn("email_access_token_totp", format_block)
        self.assertIn("email_at_totp", format_block)

        click_start = self.template.index("const copySecretBtn =")
        click_end = self.template.index("const planBtn =", click_start)
        click_block = self.template[click_start:click_end]
        self.assertIn("data-account-copy-format", click_block)
        self.assertIn("accountAtExportFormat()", click_block)
        self.assertIn("fetchOneAccountSecret(id, field)", click_block)
        self.assertIn("accountAtExportSkipReason", click_block)

        for function_name in (
            "async function copySelectedAccountLines()",
            "async function downloadSelectedAccountTxt()",
            "async function copyCurrentPageTokens()",
        ):
            start = self.template.index(function_name)
            end = self.template.find("\nfunction ", start + 1)
            if end < 0:
                end = len(self.template)
            self.assertIn("fetchAccountAtExport(ids)", self.template[start:end])

        self.assertIn("OUTLOOK.map(r=>r.copy_line || r.email)", self.template)
        self.assertIn("cbtn('复制整行', r.account_copy_line, 'good')", self.template)

    def test_download_uses_same_lines_and_metadata_summary_as_bulk_copy(self):
        bulk_start = self.template.index("async function copySelectedAccountLines()")
        download_start = self.template.index("async function downloadSelectedAccountTxt()")
        current_page_start = self.template.index("async function copyCurrentPageTokens()")
        self.assertIn("result.lines.join('\\n')", self.template[bulk_start:download_start])
        self.assertIn("result.lines.join('\\n')", self.template[download_start:current_page_start])
        self.assertIn("accountAtExportResultMessage('已下载', result)", self.template[download_start:current_page_start])
        helper_start = self.template.index("async function fetchAccountAtExport")
        helper_end = self.template.index("async function copySelectedAccountLines", helper_start)
        helper_block = self.template[helper_start:helper_end]
        self.assertIn("fallbackCount", helper_block)
        self.assertIn("skippedNoAt", helper_block)


if __name__ == "__main__":
    unittest.main()
