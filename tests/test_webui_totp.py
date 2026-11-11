# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

import pyotp

from webui.app import create_app

SECRET = "JBSWY3DPEHPK3PXP"


class WebUiTotpTests(unittest.TestCase):

    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"


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



if __name__ == "__main__":
    unittest.main()
