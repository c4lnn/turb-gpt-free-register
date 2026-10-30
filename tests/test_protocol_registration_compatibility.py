# -*- coding: utf-8 -*-
import json
import unittest
from unittest.mock import patch

from core import openai_auth


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




class AuthContextAndBoundaryTests(unittest.TestCase):
    def test_auth_logs_do_not_include_otp_or_token_values(self):
        session = _AuthApiSession("nav-real")
        with self.assertLogs("core.openai_auth", level="INFO") as captured:
            openai_auth.validate_email_otp(session, "123456", "token-secret-placeholder")
        output = "\n".join(captured.output)
        self.assertNotIn("123456", output)
        self.assertNotIn("token-secret-placeholder", output)


    def test_password_creation_and_submission_do_not_log_the_password(self):
        session = _AuthApiSession()
        with patch("config.register.REGISTER_PASSWORD", "configured-test-password"), self.assertLogs("core.openai_auth", level="INFO") as captured:
            password = openai_auth.generate_registration_password()
            openai_auth.register_user(session, "user@example.com", password, "proof-placeholder")
        self.assertNotIn(password, "\n".join(captured.output))

    def test_otp_failure_does_not_expose_response_credentials(self):
        session = _AuthApiSession()
        response = _Response(400, {"error": {"message": "invalid OTP response-secret-marker"}})
        with patch.object(session, "post", return_value=response), self.assertLogs(
            "core.openai_auth", level="INFO"
        ) as captured:
            with self.assertRaises(openai_auth.EmailOtpInvalidError) as raised:
                openai_auth.validate_email_otp(session, "123456")
        self.assertNotIn("response-secret-marker", str(raised.exception))
        self.assertNotIn("response-secret-marker", "\n".join(captured.output))

    def test_otp_success_does_not_log_oauth_continuation_credentials(self):
        session = _AuthApiSession()
        response = _Response(payload={"page": {"type": "external_url"},
            "continue_url": "https://chatgpt.com/api/auth/callback/openai?code=private-code-marker"})
        with patch.object(session, "post", return_value=response), self.assertLogs(
            "core.openai_auth", level="INFO"
        ) as captured:
            result = openai_auth.validate_email_otp(session, "123456")
        self.assertEqual(result["continue_url"], response.payload["continue_url"])
        self.assertNotIn("private-code-marker", "\n".join(captured.output))

    def _solved_response(self, session):
        flow = "username_password_create"
        return {"token": "current-challenge", "_sentinel_flow": flow,
            "_sentinel_device_id": session.device_id, "_sentinel_so_header": None,
            "_sentinel_header": json.dumps({"p": "current-proof", "c": "current-challenge",
                "id": session.device_id, "flow": flow})}

    def test_so_is_optional_only_when_the_current_challenge_does_not_require_it(self):
        session = _AuthApiSession()
        response = self._solved_response(session)
        _, so = openai_auth.build_sentinel_header(session, response, "username_password_create")
        self.assertIsNone(so)
        response["so"] = {"required": True}
        with self.assertRaises(openai_auth.SentinelRequestError):
            openai_auth.build_sentinel_header(session, response, "username_password_create")

    def test_proofs_from_another_flow_device_or_challenge_are_rejected(self):
        session = _AuthApiSession()
        for field, value in (("flow", "oauth_create_account"),
                             ("id", "another-task-device"), ("c", "previous-challenge")):
            with self.subTest(field=field):
                response = self._solved_response(session)
                token = json.loads(response["_sentinel_header"])
                token[field] = value
                response["_sentinel_header"] = json.dumps(token)
                with self.assertRaises(openai_auth.SentinelRequestError):
                    openai_auth.build_sentinel_header(session, response, "username_password_create")

    def test_so_from_a_previous_challenge_cannot_fill_a_current_requirement(self):
        session = _AuthApiSession()
        response = self._solved_response(session)
        response["so"] = {"required": True}
        response["_sentinel_so_header"] = json.dumps({"so": "previous-observer-proof",
            "c": "previous-challenge", "id": session.device_id,
            "flow": "username_password_create"})
        with self.assertRaises(openai_auth.SentinelRequestError):
            openai_auth.build_sentinel_header(session, response, "username_password_create")

    def test_proof_is_only_mandatory_when_the_current_challenge_requires_pow(self):
        session = _AuthApiSession()
        for required in (False, True):
            with self.subTest(pow_required=required):
                response = self._solved_response(session)
                response["proofofwork"] = {"required": required}
                token = json.loads(response["_sentinel_header"])
                token["p"] = None
                response["_sentinel_header"] = json.dumps(token)
                if required:
                    with self.assertRaises(openai_auth.SentinelRequestError):
                        openai_auth.build_sentinel_header(session, response, "username_password_create")
                else:
                    header, _ = openai_auth.build_sentinel_header(session, response, "username_password_create")
                    self.assertEqual(json.loads(header)["c"], response["token"])

    def test_optional_proof_does_not_allow_a_missing_main_token(self):
        session = _AuthApiSession()
        for header in (None, "", "{}"):
            with self.subTest(header=header):
                response = self._solved_response(session)
                response.update(proofofwork={"required": False}, _sentinel_header=header)
                with self.assertRaises(openai_auth.SentinelRequestError):
                    openai_auth.build_sentinel_header(session, response, "username_password_create")

if __name__ == "__main__":
    unittest.main()
