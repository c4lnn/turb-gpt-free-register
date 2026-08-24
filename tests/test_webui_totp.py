# -*- coding: utf-8 -*-
import json
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import pyotp

from webui.app import create_app


SECRET = "JBSWY3DPEHPK3PXP"
TEMPLATE_PATH = Path(__file__).parents[1] / "webui" / "templates" / "index.html"


class WebUiTotpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = TEMPLATE_PATH.read_text(encoding="utf-8")
        start = cls.template.index("function _twofaCell")
        end = cls.template.index("function _accountsV2MoreMenu", start)
        cls.twofa_renderer = cls.template[start:end]
        cls.node = shutil.which("node")

    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    def _render_twofa_cells(self, rows):
        if not self.node:
            self.skipTest("需要 Node.js 执行内嵌的 2FA 渲染函数")
        script = """
function esc(value) {
  return String(value ?? '').replace(/[&<>\\\"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '\\"': '&quot;', "'": '&#39;'
  }[ch]));
}
__RENDERER__
const rows = __ROWS__;
process.stdout.write(JSON.stringify(rows.map(_twofaCell)));
""".replace("__RENDERER__", self.twofa_renderer).replace(
            "__ROWS__", json.dumps(rows, ensure_ascii=False)
        )
        result = subprocess.run(
            [self.node, "-e", script],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_returns_current_code_without_exposing_secret(self):
        timestamp = 1_700_000_001.2
        with patch(
            "webui.app.db.get_account",
            return_value={"id": 1, "email": "one@example.com", "totp_secret": SECRET},
        ), patch("webui.app.time.time", return_value=timestamp):
            response = self.client.get("/api/accounts/1/totp-code")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["code"], pyotp.TOTP(SECRET).at(timestamp))
        self.assertRegex(body["code"], r"^\d{6}$")
        self.assertEqual(body["period_seconds"], 30)
        self.assertEqual(body["remaining_seconds"], 30 - (int(timestamp) % 30))
        self.assertNotIn("totp_secret", body)
        self.assertNotIn(SECRET, repr(body))
        self.assertEqual(response.headers["Cache-Control"], "no-store, max-age=0")

    @patch("webui.app.db.get_account", return_value={"id": 1, "email": "one@example.com"})
    def test_requires_saved_secret(self, _get_account):
        response = self.client.get("/api/accounts/1/totp-code")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "该账号没有已保存的 2FA Secret")

    @patch(
        "webui.app.db.get_account",
        return_value={"id": 1, "email": "one@example.com", "totp_secret": "INVALID!"},
    )
    def test_rejects_invalid_secret(self, _get_account):
        response = self.client.get("/api/accounts/1/totp-code")

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.get_json()["error"], "该账号保存的 2FA Secret 无效")

    def test_endpoint_requires_webui_authorization(self):
        client = create_app(auth_code="test-auth").test_client()

        response = client.get("/api/accounts/1/totp-code")

        self.assertEqual(response.status_code, 401)

    def test_template_contains_one_unified_totp_control(self):
        self.assertEqual(self.template.count("data-account-totp-code"), 2)
        self.assertIn("/totp-code`)", self.template)
        self.assertIn(">已启用</button>", self.twofa_renderer)
        self.assertNotIn(">未取码</button>", self.twofa_renderer)
        self.assertNotIn("data-account-totp-output", self.template)
        self.assertIn("twofa-code-btn.is-code", self.template)
        self.assertIn("width: 100px; min-width: 100px", self.template)
        self.assertIn("width: 82px; min-width: 82px", self.template)

    def test_twofa_renderer_keeps_status_matrix_without_local_secret(self):
        rows = [
            {"id": 1, "totp_enabled": True, "twofa_status": "enabled"},
            {
                "id": 2,
                "totp_enabled": False,
                "twofa_status": "already_enabled",
                "twofa_stage": "reauth",
                "twofa_error_code": "already_enabled",
            },
            {
                "id": 3,
                "totp_enabled": False,
                "twofa_status": "failed",
                "twofa_stage": "activate",
                "twofa_error_code": "totp_activate_failed",
            },
            {"id": 4, "totp_enabled": False, "twofa_status": "disabled"},
            {"id": 5, "totp_enabled": False, "twofa_status": ""},
            {"id": 6, "totp_enabled": False, "twofa_status": "enabled"},
        ]

        rendered = self._render_twofa_cells(rows)

        self.assertEqual(rendered[0].count('data-account-totp-code="1"'), 1)
        self.assertIn(">已启用</button>", rendered[0])
        for html, label in zip(rendered[1:], ("已存在", "失败", "未启用", "未设置", "已启用")):
            self.assertIn(label, html)
            self.assertNotIn("data-account-totp-code", html)
        self.assertIn("远端已启用 TOTP，但本地未保存 Secret", rendered[1])
        self.assertIn("reauth · already_enabled", rendered[1])
        self.assertIn("activate · totp_activate_failed", rendered[2])

    def test_totp_click_contract_supports_refresh_and_failure_recovery(self):
        start = self.template.index("const totpButton =")
        end = self.template.index("const moreToggle =", start)
        click_handler = self.template[start:end]

        for fragment in (
            "const previousLabel = totpButton.textContent || '已启用'",
            "const previousTitle = totpButton.title",
            "totpButton.disabled = true",
            "totpButton.setAttribute('aria-busy', 'true')",
            "totpButton.textContent = '生成中'",
            "/totp-code`)",
            "result.code",
            "result.remaining_seconds",
            "totpButton.classList.add('is-code')",
            "totpButton.classList.toggle('is-code', wasCode)",
            "totpButton.disabled = false",
            "totpButton.setAttribute('aria-busy', 'false')",
        ):
            self.assertIn(fragment, click_handler)
        self.assertEqual(click_handler.count("/totp-code`"), 1)


if __name__ == "__main__":
    unittest.main()
