# -*- coding: utf-8 -*-
import unittest

from core.mailcom_protocol import (
    is_invalid_token_challenge,
    is_invalid_token_response,
    parse_bearer_challenge,
    redact_headers,
    redact_mapping,
)


class _Response:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


class MailComProtocolTests(unittest.TestCase):
    def test_invalid_token_requires_401_and_structured_bearer_error(self):
        value = 'Bearer error="invalid_token", error_description="Provided token is not active"'
        self.assertEqual(parse_bearer_challenge(value)["error"], "invalid_token")
        self.assertTrue(is_invalid_token_challenge(value))
        self.assertTrue(is_invalid_token_response(_Response(401, {"WWW-Authenticate": value})))
        self.assertFalse(is_invalid_token_response(_Response(401, {})))
        self.assertFalse(is_invalid_token_response(_Response(403, {"WWW-Authenticate": value})))
        self.assertFalse(is_invalid_token_response(_Response(401, {"WWW-Authenticate": 'Basic realm="mail"'})))

    def test_redaction_never_keeps_credentials_or_body(self):
        headers = redact_headers({
            "Authorization": "Bearer secret-at",
            "Cookie": "sid=secret",
            "WWW-Authenticate": 'Bearer error="invalid_token", error_description="details"',
        })
        self.assertEqual(headers["Authorization"], "[redacted]")
        self.assertEqual(headers["Cookie"], "[redacted]")
        self.assertEqual(headers["WWW-Authenticate"], "Bearer error=invalid_token")
        payload = redact_mapping({"password": "pw", "mail_access_token": "at", "body": "mail body"})
        self.assertEqual(payload, {"password": "[redacted]", "mail_access_token": "[redacted]", "body": "[redacted]"})


if __name__ == "__main__":
    unittest.main()
