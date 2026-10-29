# -*- coding: utf-8 -*-
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core import openai_auth, sentinel_runner


class _Response:
    def __init__(self, status_code=200, payload=None, url="https://auth.openai.com/"):
        self.status_code = status_code
        self.payload = payload if payload is not None else {}
        self.text = json.dumps(self.payload, ensure_ascii=False)
        self.headers = {}
        self.history = []
        self.url = url

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _AuthApiSession:
    device_id = "device-123"

    def __init__(self, navigation_id="nav-123"):
        self.auth_document_navigation_id = navigation_id
        self.calls = []

    def get_auth_headers(self, referer):
        return {"content-type": "application/json", "referer": referer}

    def attach_auth_document_navigation_header(self, headers):
        if self.auth_document_navigation_id:
            headers["x-openai-document-navigation-id"] = self.auth_document_navigation_id
        return headers

    def post(self, url, headers=None, data=None):
        self.calls.append((url, headers or {}, data or ""))
        payload = {"page": {"type": "about_you"}, "continue_url": "https://auth.openai.com/about-you"}
        return _Response(payload=payload, url=url)


class SentinelResourceTests(unittest.TestCase):
    def test_runner_debug_command_redacts_cookie(self):
        process = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"p": "p", "c": "sentinel-placeholder", "id": "device", "flow": "authorize_continue"}),
            stderr="",
        )
        with patch.object(sentinel_runner, "_ensure_runner_environment"), patch.object(
            sentinel_runner.subprocess, "run", return_value=process
        ):
            with self.assertLogs("core.sentinel_runner", level="DEBUG") as captured:
                sentinel_runner.generate_sentinel_token(
                    {},
                    "authorize_continue",
                    "device",
                    cookie="cookie-secret-placeholder",
                )
        output = "\n".join(captured.output)
        self.assertNotIn("cookie-secret-placeholder", output)
        self.assertIn("<redacted>", output)


class AuthContextAndBoundaryTests(unittest.TestCase):
    def test_auth_logs_do_not_include_otp_or_token_values(self):
        session = _AuthApiSession("nav-real")
        with self.assertLogs("core.openai_auth", level="INFO") as captured:
            openai_auth.validate_email_otp(session, "123456", "token-secret-placeholder")
        output = "\n".join(captured.output)
        self.assertNotIn("123456", output)
        self.assertNotIn("token-secret-placeholder", output)


if __name__ == "__main__":
    unittest.main()
