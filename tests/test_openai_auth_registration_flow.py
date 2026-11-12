# -*- coding: utf-8 -*-
import json
import uuid
import unittest
from unittest.mock import patch

from config import browser as browser_config
import core.openai.auth.openai_auth as openai_auth
import core.openai.codex.codex_oauth as codex_oauth
from core.openai.transport.session import BrowserSession


class _Response:
    def __init__(self, *, status_code=200, url="", payload=None, text=""):
        self.status_code = status_code
        self.url = url
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _ObservedResponse(_Response):
    def __init__(self, session, observed_ids, **kwargs):
        super().__init__(**kwargs)
        self._session = session
        self._observed_ids = observed_ids

    def raise_for_status(self):
        self._observed_ids.append(self._session.document_navigation_id)
        super().raise_for_status()


class _ProtocolSession:
    def __init__(self):
        self.device_id = "device-id"
        self.document_navigation_id = "document-1"
        self.browser_profile = {"user_agent": "Test UA", "navigator_language": "ja-JP"}
        self.posts = []
        self.gets = []
        self.get_navigation_ids = []

    def get_auth_headers(self, referer=""):
        return {
            "accept": "application/json",
            "content-type": "application/json",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "origin": "https://auth.openai.com",
            "referer": referer,
            "x-access-flow-invocation-id": str(uuid.uuid4()),
            "x-openai-document-navigation-id": self.document_navigation_id,
        }

    def get_auth_navigate_headers(self, referer="", **_kwargs):
        return {"referer": referer}

    def get_chatgpt_headers(self, referer=""):
        return {"referer": referer}

    def navigator_language(self):
        return "en-US"

    def get_sentinel_headers(self):
        return {"content-type": "text/plain;charset=UTF-8"}

    def auth_cookie_header(self):
        return "oai-did=device-id"

    def rotate_document_navigation_id(self):
        old = self.document_navigation_id
        self.document_navigation_id = old + "-next"
        return self.document_navigation_id

    def post(self, url, *, headers, data=None, allow_redirects=False):
        self.posts.append((url, headers, data, allow_redirects))
        if url.endswith("/sentinel/req"):
            return _Response(payload={"token": "challenge", "persona": "p"})
        return _Response(payload={"continue_url": "/api/accounts/email-otp/send"})

    def get(self, url, *, headers, allow_redirects=True):
        self.gets.append((url, headers, allow_redirects))
        self.get_navigation_ids.append(self.document_navigation_id)
        return _Response(url="https://auth.openai.com/email-verification")

class OpenAIRegistrationFlowTests(unittest.TestCase):


    def test_navigate_create_account_password_switches_from_passwordless_page(self):
        class Session(_ProtocolSession):
            def get(self, url, *, headers, allow_redirects):
                self.gets.append((url, headers, allow_redirects))
                return _Response(url="https://auth.openai.com/create-account/password")

        session = Session()
        final_url = openai_auth.navigate_create_account_password(
            session, "https://auth.openai.com/email-verification"
        )

        url, headers, redirects = session.gets[-1]
        self.assertEqual(url, "https://auth.openai.com/create-account/password")
        self.assertEqual(headers["referer"], "https://auth.openai.com/email-verification")
        self.assertEqual(headers["sec-fetch-site"], "same-origin")
        self.assertEqual(headers["sec-fetch-user"], "?1")
        self.assertTrue(redirects)
        self.assertEqual(final_url, "https://auth.openai.com/create-account/password")
        self.assertEqual(session.document_navigation_id, "document-1-next")

    def test_email_otp_send_accepts_relative_url_and_rotates_document_id(self):
        session = _ProtocolSession()

        final_url = openai_auth.navigate_email_otp_send(
            session, "/api/accounts/email-otp/send"
        )

        url, headers, redirects = session.gets[-1]
        self.assertEqual(url, "https://auth.openai.com/api/accounts/email-otp/send")
        self.assertEqual(headers["referer"], "https://auth.openai.com/create-account/password")
        self.assertEqual(headers["sec-fetch-site"], "same-origin")
        self.assertEqual(headers["sec-fetch-user"], "?1")
        self.assertNotIn("x-openai-document-navigation-id", headers)
        self.assertTrue(redirects)
        self.assertEqual(final_url, "https://auth.openai.com/email-verification")
        self.assertEqual(session.get_navigation_ids, ["document-1-next"])
        self.assertEqual(session.document_navigation_id, "document-1-next")

    def test_email_otp_send_retry_rotates_before_each_navigation(self):
        class FailOnceSession(_ProtocolSession):
            def get(self, url, *, headers, allow_redirects=True):
                self.gets.append((url, headers, allow_redirects))
                self.get_navigation_ids.append(self.document_navigation_id)
                if len(self.gets) == 1:
                    return _Response(status_code=503, url="https://auth.openai.com/email-verification")
                return _Response(url="https://auth.openai.com/email-verification")

        session = FailOnceSession()
        with self.assertRaises(RuntimeError):
            openai_auth.navigate_email_otp_send(session)
        openai_auth.navigate_email_otp_send(session)

        self.assertEqual(
            session.get_navigation_ids,
            ["document-1-next", "document-1-next-next"],
        )
        self.assertTrue(all(
            "x-openai-document-navigation-id" not in headers
            for _url, headers, _redirects in session.gets
        ))

    def test_validate_and_create_account_reuse_otp_page_navigation_id(self):
        session = _ProtocolSession()
        openai_auth.navigate_email_otp_send(session)
        navigation_id = session.document_navigation_id

        openai_auth.validate_email_otp(session, "123456")
        validate_headers = session.posts[-1][1]
        openai_auth.create_account(session, "Test User", "1990-01-01", "sentinel")
        create_headers = session.posts[-1][1]

        self.assertEqual(validate_headers["x-openai-document-navigation-id"], navigation_id)
        self.assertEqual(validate_headers["referer"], "https://auth.openai.com/email-verification")
        self.assertEqual(create_headers["x-openai-document-navigation-id"], navigation_id)

    def test_registration_auth_json_posts_use_root_referer_and_preserve_payloads(self):
        session = _ProtocolSession()
        openai_auth.register_user(
            session, "user@example.test", "password-placeholder", "sentinel-placeholder",
        )
        openai_auth.validate_email_otp(
            session, "123456", "sentinel-placeholder", referer="https://auth.openai.com/",
        )
        openai_auth.create_account(
            session, "Test User", "1990-01-01", "sentinel-placeholder",
        )

        expected = [
            ("https://auth.openai.com/api/accounts/user/register", {
                "password": "password-placeholder", "username": "user@example.test",
            }),
            ("https://auth.openai.com/api/accounts/email-otp/validate", {"code": "123456"}),
            ("https://auth.openai.com/api/accounts/create_account", {
                "name": "Test User", "birthdate": "1990-01-01",
            }),
        ]
        self.assertEqual([post[0] for post in session.posts], [item[0] for item in expected])
        for (url, headers, body, _redirects), (expected_url, expected_body) in zip(session.posts, expected):
            with self.subTest(endpoint=expected_url.rsplit("/", 1)[-1]):
                self.assertEqual(url, expected_url)
                self.assertEqual(headers["referer"], "https://auth.openai.com/")
                self.assertEqual(headers["origin"], "https://auth.openai.com")
                self.assertEqual(headers["content-type"], "application/json")
                self.assertEqual(headers["x-openai-document-navigation-id"], "document-1")
                self.assertEqual(json.loads(body), expected_body)


    def test_send_email_otp_rotates_before_navigation(self):
        session = _ProtocolSession()

        openai_auth.send_email_otp(session)

        url, headers, _redirects = session.gets[-1]
        self.assertEqual(url, "https://auth.openai.com/api/accounts/email-otp/send")
        self.assertEqual(session.get_navigation_ids, ["document-1-next"])
        self.assertNotIn("x-openai-document-navigation-id", headers)





    def test_challenge_network_failure_closes_sdk_without_exposing_credentials(self):
        class SDK:
            closed = False

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.closed = True

            def requirements(self):
                return "request-proof"

            def solve(self, _challenge):
                raise AssertionError("a failed HTTP challenge must not be solved")

        class Session(_ProtocolSession):
            def post(self, _url, *, headers, data):
                raise ConnectionError("proxy credential must-not-leak")

        sdk = SDK()
        with patch.object(openai_auth, "SentinelSDK", return_value=sdk), patch.object(
            openai_auth, "_proxy_retry_config", return_value=(1, 0)
        ):
            with self.assertRaises(openai_auth.SentinelRequestError) as raised:
                openai_auth.request_sentinel_token(Session(), "username_password_create")
        self.assertTrue(sdk.closed)
        self.assertEqual(raised.exception.category, "sentinel_network_failed")
        self.assertNotIn("must-not-leak", str(raised.exception))

    def _solved_response(self, session, flow, *, so_required=False, include_so=False):
        result = {
            "token": "current-challenge",
            "so": {"required": so_required},
            "_sentinel_flow": flow,
            "_sentinel_device_id": session.device_id,
            "_sentinel_header": json.dumps({
                "p": "proof", "c": "current-challenge",
                "id": session.device_id, "flow": flow,
            }),
            "_sentinel_so_header": None,
        }
        if include_so:
            result["_sentinel_so_header"] = json.dumps({
                "so": "observer-proof", "c": "current-challenge",
                "id": session.device_id, "flow": flow,
            })
        return result

    def test_result_from_another_flow_cannot_be_consumed(self):
        session = _ProtocolSession()
        result = self._solved_response(session, "username_password_create")
        with self.assertRaises(openai_auth.SentinelRequestError):
            openai_auth.build_sentinel_header(session, result, "oauth_create_account")

    def test_result_from_another_device_cannot_be_consumed(self):
        session = _ProtocolSession()
        result = self._solved_response(session, "username_password_create")
        session.device_id = "other-device"
        with self.assertRaises(openai_auth.SentinelRequestError):
            openai_auth.build_sentinel_header(session, result, "username_password_create")

    def test_proof_from_another_challenge_cannot_be_consumed(self):
        session = _ProtocolSession()
        result = self._solved_response(session, "username_password_create")
        result["token"] = "next-challenge"
        with self.assertRaises(openai_auth.SentinelRequestError):
            openai_auth.build_sentinel_header(session, result, "username_password_create")

    def test_empty_so_is_only_accepted_when_current_challenge_does_not_require_it(self):
        session = _ProtocolSession()
        result = self._solved_response(session, "username_password_create")
        _header, so = openai_auth.build_sentinel_header(session, result, "username_password_create")
        self.assertIsNone(so)
        result["so"]["required"] = True
        with self.assertRaises(openai_auth.SentinelRequestError):
            openai_auth.build_sentinel_header(session, result, "username_password_create")

    def test_browser_session_keeps_document_id_until_explicit_navigation(self):
        session = object.__new__(BrowserSession)
        session.document_navigation_id = "document-a"
        session.datadog_trace_id = "1"
        session.datadog_parent_id = "2"
        session.datadog_origin = "rum"

        first = session._attach_auth_rum_headers({})
        second = session._attach_auth_rum_headers({})
        self.assertEqual(first["x-openai-document-navigation-id"], "document-a")
        self.assertEqual(second["x-openai-document-navigation-id"], "document-a")
        self.assertNotEqual(first["x-access-flow-invocation-id"], second["x-access-flow-invocation-id"])
        self.assertNotEqual(first["x-datadog-trace-id"], second["x-datadog-trace-id"])
        self.assertNotEqual(first["x-datadog-parent-id"], second["x-datadog-parent-id"])

        rotated = session.rotate_document_navigation_id()
        third = session._attach_auth_rum_headers({})
        self.assertNotEqual(rotated, "document-a")
        self.assertEqual(third["x-openai-document-navigation-id"], rotated)

    # ── 协议分流 / 已有账号登录 ─────────────────────────────────────────

    def _state_session(self, landing_url, page=None):
        session = _ProtocolSession()
        session.registration_authorize_state = {
            "landing_url": landing_url,
            "page": page or {},
        }
        return session

    def test_get_auth_continue_url_top_level_and_page_fallback(self):
        self.assertEqual(
            openai_auth.get_auth_continue_url({"continue_url": "https://chatgpt.com/cb"}),
            "https://chatgpt.com/cb",
        )
        self.assertEqual(
            openai_auth.get_auth_continue_url({"page": {"external_url": "https://auth.openai.com/next"}}),
            "https://auth.openai.com/next",
        )
        self.assertEqual(openai_auth.get_auth_continue_url({}), "")
        self.assertEqual(openai_auth.get_auth_continue_url(None), "")

    def test_get_auth_step_variants(self):
        self.assertEqual(openai_auth.get_auth_step({"page": {"type": "mfa_challenge"}}), "totp")
        self.assertEqual(openai_auth.get_auth_step({"page": {"type": "email_otp_verification"}}), "otp")
        self.assertEqual(openai_auth.get_auth_step({"page": {"type": "login_password"}}), "password")
        self.assertEqual(openai_auth.get_auth_step({"page": {"type": "about_you"}}), "profile")
        self.assertEqual(openai_auth.get_auth_step({"page": {"type": "external_url"}}), "callback")
        self.assertEqual(
            openai_auth.get_auth_step({"continue_url": "https://auth.openai.com/email-verification"}),
            "otp",
        )
        self.assertEqual(
            openai_auth.get_auth_step({"continue_url": "https://auth.openai.com/authorize/continue?code=x"}),
            "callback",
        )
        self.assertEqual(openai_auth.get_auth_step({}), "unknown")

    def test_get_auth_step_conflict_is_unknown(self):
        self.assertEqual(
            openai_auth.get_auth_step({
                "page": {"type": "mfa_challenge"},
                "continue_url": "https://auth.openai.com/email-verification",
            }),
            "unknown",
        )

    def test_get_auth_step_evil_host_is_unknown(self):
        # 已知 page.type 也不能让非允许域 continue_url 过关。
        self.assertEqual(
            openai_auth.get_auth_step({
                "page": {"type": "external_url"},
                "continue_url": "https://evil.example/cb?code=x",
            }),
            "unknown",
        )

    def test_registration_state_new_password(self):
        session = self._state_session("https://auth.openai.com/create-account/password")
        state = openai_auth.get_registration_state(session, session.registration_authorize_state["landing_url"])
        self.assertEqual(state["kind"], "new_password")
        self.assertEqual(state["step"], "password")

    def test_registration_state_existing_password(self):
        session = self._state_session("https://auth.openai.com/log-in/password")
        state = openai_auth.get_registration_state(session, session.registration_authorize_state["landing_url"])
        self.assertEqual(state["kind"], "existing")
        self.assertEqual(state["step"], "password")

    def test_registration_state_log_in_generic_is_existing_otp(self):
        session = self._state_session("https://auth.openai.com/log-in")
        state = openai_auth.get_registration_state(session, session.registration_authorize_state["landing_url"])
        self.assertEqual(state["kind"], "existing")
        self.assertEqual(state["step"], "otp")

    def test_registration_state_email_verification_signup_is_new_password(self):
        session = self._state_session("https://auth.openai.com/email-verification")
        state = openai_auth.get_registration_state(
            session, session.registration_authorize_state["landing_url"], intent="signup",
        )
        self.assertEqual(state["kind"], "new_password")
        self.assertEqual(state["step"], "password")

    def test_registration_state_email_verification_default_signup_is_new_password(self):
        session = self._state_session("https://auth.openai.com/email-verification")
        state = openai_auth.get_registration_state(session, session.registration_authorize_state["landing_url"])
        self.assertEqual(state["kind"], "new_password")
        self.assertEqual(state["step"], "password")


    def test_registration_state_email_verification_login_is_existing_otp(self):
        session = self._state_session("https://auth.openai.com/email-verification")
        state = openai_auth.get_registration_state(
            session, session.registration_authorize_state["landing_url"], intent="login",
        )
        self.assertEqual(state["kind"], "existing")
        self.assertEqual(state["step"], "otp")
        self.assertTrue(state["otp_delivery_required"])

    def test_probe_omits_so_when_required_for_registration_and_reauth(self):
        class ProbeSession(_ProtocolSession):
            def post(self, url, *, headers, data=None, allow_redirects=False):
                super().post(url, headers=headers, data=data, allow_redirects=allow_redirects)
                return _Response(payload={
                    "page": {"type": "login_password"},
                    "continue_url": "https://auth.openai.com/log-in/password",
                })

        for strict in (False, True):
            for so_case in ("missing", "valid", "invalid"):
                with self.subTest(strict=strict, so_case=so_case):
                    session = ProbeSession()
                    proof = self._solved_response(
                        session, "authorize_continue", so_required=True,
                        include_so=so_case == "valid",
                    )
                    if so_case == "invalid":
                        proof["_sentinel_so_header"] = "invalid-so"
                    context = {}
                    if not strict:
                        context = {
                            "referer": "https://auth.openai.com/",
                            "screen_hint": "signup",
                        }
                    with patch.object(openai_auth, "request_sentinel_token", return_value=proof):
                        state = openai_auth.probe_registration_state(
                            session, "user@example.test", strict=strict, **context,
                        )
                    self.assertEqual(state["step"], "password")
                    self.assertEqual(len(session.posts), 1)
                    url, headers, body, _ = session.posts[0]
                    self.assertEqual(url, "https://auth.openai.com/api/accounts/authorize/continue")
                    expected_body = {
                        "username": {"kind": "email", "value": "user@example.test"},
                    }
                    if not strict:
                        expected_body["screen_hint"] = "signup"
                    self.assertEqual(json.loads(body), expected_body)
                    expected_referer = (
                        "https://auth.openai.com/" if not strict
                        else "https://auth.openai.com/create-account"
                    )
                    self.assertEqual(headers["referer"], expected_referer)
                    self.assertEqual(json.loads(headers["openai-sentinel-token"]),
                                     json.loads(proof["_sentinel_header"]))
                    self.assertNotIn("openai-sentinel-so-token", headers)

    def test_probe_rejects_invalid_main_credentials_before_request(self):
        for defect in ("missing", "flow", "device", "challenge", "proof"):
            with self.subTest(defect=defect):
                session = _ProtocolSession()
                proof = self._solved_response(session, "authorize_continue", so_required=True)
                token = json.loads(proof["_sentinel_header"])
                if defect == "missing":
                    proof["_sentinel_header"] = None
                else:
                    field = {"flow": "flow", "device": "id", "challenge": "c", "proof": "p"}[defect]
                    token[field] = "" if defect == "proof" else "different"
                    proof["_sentinel_header"] = json.dumps(token)
                with patch.object(openai_auth, "request_sentinel_token", return_value=proof):
                    with self.assertRaises(openai_auth.SentinelRequestError):
                        openai_auth.probe_registration_state(session, "user@example.test")
                self.assertEqual(session.posts, [])

    def test_codex_otp_keeps_required_so_validation_for_shared_flow(self):
        for so_case in ("missing", "mismatched", "valid"):
            with self.subTest(so_case=so_case):
                session = _ProtocolSession()
                proof = self._solved_response(
                    session, "authorize_continue", so_required=True,
                    include_so=so_case != "missing",
                )
                if so_case == "mismatched":
                    so = json.loads(proof["_sentinel_so_header"])
                    so["c"] = "previous-challenge"
                    proof["_sentinel_so_header"] = json.dumps(so)
                with patch.object(codex_oauth, "request_sentinel_token", return_value=proof):
                    if so_case == "valid":
                        codex_oauth._submit_email_otp(session, "123456")
                    else:
                        with self.assertRaises(openai_auth.SentinelRequestError):
                            codex_oauth._submit_email_otp(session, "123456")
                if so_case == "valid":
                    self.assertEqual(len(session.posts), 1)
                    url, headers, _, _ = session.posts[0]
                    self.assertEqual(url, "https://auth.openai.com/api/accounts/email-otp/validate")
                    self.assertEqual(headers["openai-sentinel-so-token"], proof["_sentinel_so_header"])
                else:
                    self.assertEqual(session.posts, [])

    def test_probe_registration_state_missing_otp_mode_is_existing(self):
        session = _ProtocolSession()
        response = _Response(payload={"page": {"type": "email_otp_verification", "payload": {}}, "continue_url": "/email-verification"})
        with patch.object(openai_auth, "request_sentinel_token", return_value={}), \
                patch.object(openai_auth, "build_sentinel_header", return_value=("sentinel", None)), \
                patch.object(session, "post", return_value=response):
            state = openai_auth.probe_registration_state(session, "user@example.com")
        self.assertEqual(state["kind"], "existing")
        self.assertEqual(state["step"], "otp")
        self.assertTrue(state["otp_delivery_required"])

    def test_probe_registration_state_unknown_page_type_is_existing_otp(self):
        session = _ProtocolSession()
        response = _Response(payload={"page": {"type": "unrecognized_page"}})
        with patch.object(openai_auth, "request_sentinel_token", return_value={}), \
                patch.object(openai_auth, "build_sentinel_header", return_value=("sentinel", None)), \
                patch.object(session, "post", return_value=response):
            state = openai_auth.probe_registration_state(session, "user@example.com")
        self.assertEqual(state["kind"], "existing")
        self.assertEqual(state["step"], "otp")
        self.assertTrue(state["otp_delivery_required"])

    def test_probe_registration_state_create_password_continue_url_is_new(self):
        session = _ProtocolSession()
        response = _Response(payload={"page": {}, "continue_url": "/create-account/password"})
        with patch.object(openai_auth, "request_sentinel_token", return_value={}), \
                patch.object(openai_auth, "build_sentinel_header", return_value=("sentinel", None)), \
                patch.object(session, "post", return_value=response):
            state = openai_auth.probe_registration_state(session, "user@example.com")
        self.assertEqual(state["kind"], "new_password")
        self.assertEqual(state["step"], "password")
        self.assertFalse(state["otp_delivery_required"])

    def test_probe_registration_state_malformed_json_falls_back_to_new(self):
        class MalformedResponse(_Response):
            def json(self):
                raise ValueError("invalid json")

        session = _ProtocolSession()
        with patch.object(openai_auth, "request_sentinel_token", return_value={}), \
                patch.object(openai_auth, "build_sentinel_header", return_value=("sentinel", None)), \
                patch.object(session, "post", return_value=MalformedResponse()):
            state = openai_auth.probe_registration_state(session, "user@example.com")
        self.assertEqual(state["kind"], "new_password")
        self.assertEqual(state["classification_source"], "probe_parse_fallback")

    def test_probe_registration_state_passwordless_login_reuses_challenge(self):
        session = _ProtocolSession()
        response = _Response(payload={
            "page": {"type": "email_otp_verification", "payload": {
                "email_verification_mode": "passwordless_login",
            }},
            "continue_url": "/email-verification",
        })
        with patch.object(openai_auth, "request_sentinel_token", return_value={}), \
                patch.object(openai_auth, "build_sentinel_header", return_value=("sentinel", None)), \
                patch.object(session, "post", return_value=response):
            state = openai_auth.probe_registration_state(session, "user@example.com")
        self.assertEqual(state["kind"], "existing")
        self.assertFalse(state["otp_delivery_required"])


    def test_probe_registration_state_posts_email_once_and_classifies_response(self):
        session = _ProtocolSession()
        response = _Response(payload={
            "page": {"type": "email_otp_verification", "payload": {"email_verification_mode": "passwordless_signup"}},
            "continue_url": "/email-verification",
        })
        with patch.object(openai_auth, "request_sentinel_token", return_value={"token": "proof"}), \
                patch.object(openai_auth, "build_sentinel_header", return_value=("sentinel", None)), \
                patch.object(session, "post", return_value=response) as post:
            state = openai_auth.probe_registration_state(
                session, "user@example.com",
                referer="https://auth.openai.com/",
                screen_hint="signup",
            )

        self.assertEqual(state["kind"], "new_passwordless")
        self.assertFalse(state["otp_delivery_required"])
        post.assert_called_once()
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://auth.openai.com/api/accounts/authorize/continue")
        self.assertEqual(json.loads(kwargs["data"]), {
            "username": {"kind": "email", "value": "user@example.com"},
            "screen_hint": "signup",
        })
        self.assertEqual(kwargs["headers"]["referer"], "https://auth.openai.com/")
        self.assertEqual(kwargs["headers"]["openai-sentinel-token"], "sentinel")

    def test_probe_registration_state_fails_on_http_error(self):
        session = _ProtocolSession()
        with patch.object(openai_auth, "request_sentinel_token", return_value={"token": "proof"}), \
                patch.object(openai_auth, "build_sentinel_header", return_value=("sentinel", None)), \
                patch.object(session, "post", return_value=_Response(status_code=403)):
            with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
                openai_auth.probe_registration_state(session, "user@example.com")

    def test_registration_state_create_account_generic_is_new_password(self):
        session = self._state_session("https://auth.openai.com/create-account")
        state = openai_auth.get_registration_state(session, session.registration_authorize_state["landing_url"], intent="signup")
        self.assertEqual(state["kind"], "new_password")

    def test_registration_state_about_you_is_new_password(self):
        session = self._state_session("https://auth.openai.com/about-you")
        state = openai_auth.get_registration_state(session, session.registration_authorize_state["landing_url"], intent="signup")
        self.assertEqual(state["kind"], "new_password")

    def test_registration_state_evil_host_path_follows_oss_substring_rule(self):
        session = self._state_session("https://evil.example/create-account/password")
        state = openai_auth.get_registration_state(session, session.registration_authorize_state["landing_url"], intent="signup")
        self.assertEqual(state["kind"], "new_password")
        self.assertEqual(state["step"], "password")

    def test_registration_state_known_landing_wins_over_passive_page_conflict(self):
        session = self._state_session(
            "https://auth.openai.com/create-account/password",
            page={"type": "login_password"},
        )
        state = openai_auth.get_registration_state(session, session.registration_authorize_state["landing_url"], intent="signup")
        self.assertEqual(state["kind"], "new_password")
        self.assertEqual(state["step"], "password")
    def test_registration_state_ignores_passive_page_for_unknown_landing(self):
        session = self._state_session(
            "https://auth.openai.com/unclassified",
            page={"type": "email_otp_verification", "payload": {"email_verification_mode": "passwordless_signup"}},
        )
        state = openai_auth.get_registration_state(session, session.registration_authorize_state["landing_url"], intent="signup")
        self.assertEqual(state["kind"], "unknown")

    def test_verify_login_password_success(self):
        session = _ProtocolSession()
        with patch.object(openai_auth, "request_sentinel_token", return_value={}) as sentinel, \
             patch.object(openai_auth, "build_sentinel_header", return_value=("hdr", None)) as build:
            result = openai_auth.verify_login_password(session, "pw")
        url, headers, data, redirects = session.posts[-1]
        self.assertEqual(url, "https://auth.openai.com/api/accounts/password/verify")
        self.assertEqual(json.loads(data), {"password": "pw"})
        self.assertFalse(redirects)
        self.assertEqual(headers["openai-sentinel-token"], "hdr")
        self.assertEqual(headers["referer"], "https://auth.openai.com/")
        self.assertEqual(headers["accept"], "application/json")
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(headers["origin"], "https://auth.openai.com")
        sentinel.assert_called_once_with(session, "password_verify")
        build.assert_called_once_with(session, {}, "password_verify")
        self.assertEqual(result["continue_url"], "/api/accounts/email-otp/send")

    def test_verify_login_password_explicit_rejection(self):
        class Session(_ProtocolSession):
            def post(self, url, *, headers, data=None, allow_redirects=False):
                return _Response(
                    status_code=401,
                    payload={"error": {"code": "invalid_password"}},
                    text='{"error":{"code":"invalid_password"}}',
                )

        session = Session()
        with patch.object(openai_auth, "request_sentinel_token", return_value={}), \
             patch.object(openai_auth, "build_sentinel_header", return_value=("hdr", None)):
            with self.assertRaises(openai_auth.PasswordRejectedError) as ctx:
                openai_auth.verify_login_password(session, "pw")
        self.assertEqual(ctx.exception.result, {"error": {"code": "invalid_password"}})

    def test_verify_login_password_429_is_not_rejection(self):
        class Session(_ProtocolSession):
            def post(self, url, *, headers, data=None, allow_redirects=False):
                return _Response(status_code=429, payload={}, text="rate limited")

        session = Session()
        with patch.object(openai_auth, "request_sentinel_token", return_value={}), \
             patch.object(openai_auth, "build_sentinel_header", return_value=("hdr", None)):
            with self.assertRaises(RuntimeError) as ctx:
                openai_auth.verify_login_password(session, "pw")
        self.assertNotIsInstance(ctx.exception, openai_auth.PasswordRejectedError)


    def test_verify_login_password_dead_account_codes_are_not_password_rejections(self):
        class Session(_ProtocolSession):
            def post(self, url, *, headers, data=None, allow_redirects=False):
                return _Response(
                    status_code=403,
                    payload={"error": {"code": self.error_code}},
                )

        for code in ("account_deactivated", "account_deleted", "account_banned"):
            with self.subTest(code=code):
                session = Session()
                session.error_code = code
                with patch.object(openai_auth, "request_sentinel_token", return_value={}), \
                     patch.object(openai_auth, "build_sentinel_header", return_value=("hdr", None)):
                    with self.assertRaises(openai_auth.AccountUnusableError) as ctx:
                        openai_auth.verify_login_password(session, "pw")
                self.assertEqual(ctx.exception.error_code, code)
                self.assertNotIsInstance(ctx.exception, openai_auth.PasswordRejectedError)
    def test_register_user_registration_disallowed_raises(self):
        class Session(_ProtocolSession):
            def post(self, url, *, headers, data=None, allow_redirects=False):
                return _Response(
                    status_code=409,
                    payload={"error": {"code": "registration_disallowed"}},
                    text='{"error":{"code":"registration_disallowed"}}',
                )

        session = Session()
        with self.assertRaises(openai_auth.PasswordRegistrationRejectedError) as ctx:
            openai_auth.register_user(session, "a@b.c", "pw", "hdr", None)
        self.assertEqual(ctx.exception.result["error"]["code"], "registration_disallowed")

    def test_register_user_invalid_state_is_not_registration_rejection(self):
        class Session(_ProtocolSession):
            def post(self, url, *, headers, data=None, allow_redirects=False):
                return _Response(
                    status_code=409,
                    payload={"error": {"code": "invalid_state"}},
                    text='{"error":{"code":"invalid_state"}}',
                )

        session = Session()
        with self.assertRaises(RuntimeError) as ctx:
            openai_auth.register_user(session, "a@b.c", "pw", "hdr", None)
        self.assertNotIsInstance(ctx.exception, openai_auth.PasswordRegistrationRejectedError)

    def test_request_login_otp_resend_posts_resend(self):
        session = _ProtocolSession()
        result = openai_auth.request_login_otp(
            session, {"page": {"type": "email_otp_verification"}}, resend=True
        )
        url, _headers, _data, _redirects = session.posts[-1]
        self.assertEqual(url, "https://auth.openai.com/api/accounts/email-otp/resend")
        self.assertEqual(result["continue_url"], "https://auth.openai.com/email-verification")
        self.assertEqual(session.get_navigation_ids, [])
        self.assertEqual(session.document_navigation_id, "document-1")

    def test_request_login_otp_follows_send_path(self):
        session = _ProtocolSession()
        result = openai_auth.request_login_otp(
            session,
            {"continue_url": "https://auth.openai.com/api/accounts/email-otp/send"},
            resend=False,
        )
        url, headers, _redirects = session.gets[-1]
        self.assertEqual(url, "https://auth.openai.com/api/accounts/email-otp/send")
        self.assertEqual(session.get_navigation_ids, ["document-1-next"])
        self.assertNotIn("x-openai-document-navigation-id", headers)
        self.assertEqual(result["continue_url"], "https://auth.openai.com/email-verification")

    def test_request_login_otp_follows_relative_send_path(self):
        session = _ProtocolSession()
        result = openai_auth.request_login_otp(
            session,
            {"continue_url": "/api/accounts/email-otp/send"},
            resend=False,
        )
        url, headers, _redirects = session.gets[-1]
        self.assertEqual(url, "https://auth.openai.com/api/accounts/email-otp/send")
        self.assertEqual(session.get_navigation_ids, ["document-1-next"])
        self.assertNotIn("x-openai-document-navigation-id", headers)
        self.assertEqual(result["continue_url"], "https://auth.openai.com/email-verification")
    def test_request_login_otp_unknown_step_fails_without_request(self):
        session = _ProtocolSession()
        with self.assertRaises(RuntimeError) as ctx:
            openai_auth.request_login_otp(session, {"page": {"type": "about_you"}}, resend=False)
        self.assertNotIn("https://", str(ctx.exception))
        self.assertEqual(session.posts, [])
        self.assertEqual(session.gets, [])

    def test_request_login_otp_evil_send_path_not_followed(self):
        session = _ProtocolSession()
        with self.assertRaises(RuntimeError):
            openai_auth.request_login_otp(
                session,
                {"continue_url": "https://evil.example/api/accounts/email-otp/send"},
                resend=False,
            )
        self.assertEqual(session.posts, [])
        self.assertEqual(session.gets, [])

    def test_verify_login_totp_missing_factor_id(self):
        session = _ProtocolSession()
        with self.assertRaisesRegex(RuntimeError, "factor_id"):
            openai_auth.verify_login_totp(session, {"page": {"type": "mfa_challenge"}}, "secret")

    def test_verify_login_totp_missing_secret(self):
        session = _ProtocolSession()
        with self.assertRaisesRegex(RuntimeError, "totp_secret"):
            openai_auth.verify_login_totp(
                session,
                {"page": {"type": "mfa_challenge", "payload": {"factor_id": "f1"}}},
                "",
            )

    def test_verify_login_totp_success(self):
        session = _ProtocolSession()
        proof = self._solved_response(session, "verify_mfa", include_so=True)
        with patch.object(openai_auth, "request_sentinel_token", return_value=proof) as sentinel, \
             patch.object(openai_auth, "_totp_code", return_value="123456"):
            openai_auth.verify_login_totp(
                session,
                {"page": {"type": "mfa_challenge", "payload": {"factor_id": "f1"}}},
                "JBSWY3DPEHPK3PXP",
            )
        sentinel.assert_called_once_with(session, "verify_mfa")
        self.assertEqual(
            [post[0] for post in session.posts],
            [
                "https://auth.openai.com/api/accounts/mfa/issue_challenge",
                "https://auth.openai.com/api/accounts/mfa/verify",
            ],
        )
        issue_headers, issue_data = session.posts[0][1:3]
        verify_headers, verify_data = session.posts[1][1:3]
        self.assertEqual(json.loads(issue_data), {
            "id": "f1", "type": "totp", "force_fresh_challenge": False,
        })
        self.assertEqual(json.loads(verify_data), {"id": "f1", "type": "totp", "code": "123456"})
        self.assertNotIn("openai-sentinel-token", issue_headers)
        self.assertNotIn("openai-sentinel-so-token", issue_headers)
        self.assertEqual(issue_headers["accept"], "*/*")
        self.assertEqual(verify_headers["accept"], "application/json")
        self.assertEqual(verify_headers["openai-sentinel-token"], proof["_sentinel_header"])
        self.assertNotIn("openai-sentinel-so-token", verify_headers)
        self.assertEqual(issue_headers["referer"], "https://auth.openai.com/")
        self.assertEqual(verify_headers["referer"], "https://auth.openai.com/")
        self.assertEqual(issue_headers["x-openai-document-navigation-id"], "document-1")
        self.assertEqual(verify_headers["x-openai-document-navigation-id"], "document-1")
        self.assertNotEqual(
            issue_headers["x-access-flow-invocation-id"],
            verify_headers["x-access-flow-invocation-id"],
        )


    def test_password_to_totp_trace_is_ordered_and_secret_free(self):
        events = []
        factor_ids = []

        class TraceSession(_ProtocolSession):
            def post(self, url, *, headers, data=None, allow_redirects=False):
                body = json.loads(data or "{}")
                endpoint = url.split("/api/accounts/", 1)[-1]
                events.append({
                    "kind": "request",
                    "endpoint": endpoint,
                    "body_keys": sorted(body),
                    "accept": headers.get("accept"),
                    "content_type": headers.get("content-type"),
                    "origin": headers.get("origin"),
                    "referer": headers.get("referer"),
                    "fetch": [headers.get(key) for key in (
                        "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest",
                    )],
                    "has_sentinel": "openai-sentinel-token" in headers,
                    "has_sentinel_so": "openai-sentinel-so-token" in headers,
                    "invocation_id": headers.get("x-access-flow-invocation-id"),
                    "navigation_id": headers.get("x-openai-document-navigation-id"),
                })
                if endpoint == "password/verify":
                    return _Response(payload={
                        "page": {"type": "mfa_challenge", "payload": {"factor_id": "factor-placeholder"}},
                        "continue_url": "/mfa-challenge/factor-placeholder",
                    })
                if endpoint == "mfa/issue_challenge":
                    factor_ids.append(body["id"])
                    return _Response(payload={})
                if endpoint == "mfa/verify":
                    factor_ids.append(body["id"])
                    return _Response(payload={
                        "continue_url": "https://chatgpt.com/api/auth/callback/openai",
                    })
                raise AssertionError(f"unexpected Auth endpoint: {endpoint}")

        session = TraceSession()

        def request_sentinel(current_session, flow):
            events.append({"kind": "sentinel", "flow": flow})
            return self._solved_response(current_session, flow, include_so=True)

        with patch.object(openai_auth, "request_sentinel_token", side_effect=request_sentinel), \
             patch.object(openai_auth, "_totp_code", return_value="totp-code-marker"):
            state = openai_auth.verify_login_password(session, "password-marker")
            result = openai_auth.verify_login_totp(session, state, "totp-secret-marker")

        self.assertEqual(
            [(event["kind"], event.get("flow") or event.get("endpoint")) for event in events],
            [
                ("sentinel", "password_verify"),
                ("request", "password/verify"),
                ("request", "mfa/issue_challenge"),
                ("sentinel", "verify_mfa"),
                ("request", "mfa/verify"),
            ],
        )
        requests = [event for event in events if event["kind"] == "request"]
        self.assertEqual(
            [event["body_keys"] for event in requests],
            [["password"], ["force_fresh_challenge", "id", "type"], ["code", "id", "type"]],
        )
        self.assertEqual(factor_ids, ["factor-placeholder", "factor-placeholder"])
        self.assertEqual([event["accept"] for event in requests], [
            "application/json", "*/*", "application/json",
        ])
        self.assertTrue(all(event["content_type"] == "application/json" for event in requests))
        self.assertTrue(all(event["origin"] == "https://auth.openai.com" for event in requests))
        self.assertTrue(all(event["referer"] == "https://auth.openai.com/" for event in requests))
        self.assertTrue(all(event["fetch"] == ["same-origin", "cors", "empty"] for event in requests))
        self.assertEqual([event["has_sentinel"] for event in requests], [True, False, True])
        self.assertEqual([event["has_sentinel_so"] for event in requests], [False, False, False])
        self.assertEqual({event["navigation_id"] for event in requests}, {"document-1"})
        invocation_ids = [event["invocation_id"] for event in requests]
        self.assertEqual(len(set(invocation_ids)), 3)
        trace = json.dumps(events, sort_keys=True)
        for secret in (
            "password-marker", "totp-secret-marker", "totp-code-marker",
            "proof", "observer-proof", "current-challenge", "cookie-marker",
        ):
            self.assertNotIn(secret, trace)
        self.assertEqual(result["continue_url"], "https://chatgpt.com/api/auth/callback/openai")

    def test_mfa_verify_sends_so_only_when_current_challenge_requires_it(self):
        session = _ProtocolSession()
        proof = self._solved_response(
            session, "verify_mfa", so_required=True, include_so=True,
        )
        with patch.object(openai_auth, "request_sentinel_token", return_value=proof):
            openai_auth._mfa_verify(session, "factor-placeholder", "totp-placeholder")
        headers = session.posts[-1][1]
        main_token = json.loads(headers["openai-sentinel-token"])
        so_token = json.loads(headers["openai-sentinel-so-token"])
        self.assertEqual(main_token["flow"], "verify_mfa")
        self.assertEqual(main_token["c"], "current-challenge")
        self.assertEqual(so_token["flow"], "verify_mfa")
        self.assertEqual(so_token["c"], "current-challenge")

    def test_mfa_sentinel_failure_stops_without_old_token_fallback(self):
        session = _ProtocolSession()
        flows = []
        error = openai_auth.SentinelRequestError("challenge unavailable", "sentinel_network_failed")

        def fail_sentinel(_session, flow):
            flows.append(flow)
            raise error

        with patch.object(openai_auth, "request_sentinel_token", side_effect=fail_sentinel), \
             patch.object(openai_auth, "_totp_code", return_value="123456"):
            with self.assertRaises(openai_auth.SentinelRequestError) as raised:
                openai_auth.verify_login_totp(
                    session,
                    {"page": {"type": "mfa_challenge", "payload": {"factor_id": "f1"}}},
                    "totp-secret-placeholder",
                )
        self.assertIs(raised.exception, error)
        self.assertEqual(flows, ["verify_mfa"])
        self.assertEqual(
            [post[0] for post in session.posts],
            ["https://auth.openai.com/api/accounts/mfa/issue_challenge"],
        )

    def test_mfa_dead_account_errors_are_classified_at_issue_and_verify(self):
        class Session(_ProtocolSession):
            def __init__(self, responses):
                super().__init__()
                self.responses = iter(responses)

            def post(self, url, *, headers, data=None, allow_redirects=False):
                self.posts.append((url, headers, data, allow_redirects))
                return next(self.responses)

        session = Session([_Response(
            status_code=403, payload={"error": {"code": "account_deleted"}},
        )])
        with patch.object(openai_auth, "request_sentinel_token") as sentinel, \
             patch.object(openai_auth, "_totp_code") as totp:
            with self.assertRaises(openai_auth.AccountUnusableError) as issue_error:
                openai_auth.verify_login_totp(
                    session,
                    {"page": {"type": "mfa_challenge", "payload": {"factor_id": "f1"}}},
                    "secret",
                )
        self.assertEqual(issue_error.exception.error_code, "account_deleted")
        sentinel.assert_not_called()
        totp.assert_not_called()
        self.assertEqual(len(session.posts), 1)

        session = Session([
            _Response(payload={}),
            _Response(status_code=403, payload={"error": {"code": "account_banned"}}),
        ])
        proof = self._solved_response(session, "verify_mfa")
        with patch.object(openai_auth, "request_sentinel_token", return_value=proof), \
             patch.object(openai_auth, "_totp_code", return_value="123456"):
            with self.assertRaises(openai_auth.AccountUnusableError) as verify_error:
                openai_auth.verify_login_totp(
                    session,
                    {"page": {"type": "mfa_challenge", "payload": {"factor_id": "f1"}}},
                    "secret",
                )
        self.assertEqual(verify_error.exception.error_code, "account_banned")
        self.assertEqual(len(session.posts), 2)
        self.assertTrue(session.posts[-1][1].get("openai-sentinel-token"))

    def test_mfa_unknown_http_and_network_errors_are_not_dead_account(self):
        class Session(_ProtocolSession):
            def __init__(self, response):
                super().__init__()
                self.response = response

            def post(self, url, *, headers, data=None, allow_redirects=False):
                self.posts.append((url, headers, data, allow_redirects))
                if url.endswith("/mfa/issue_challenge"):
                    return _Response(payload={})
                if isinstance(self.response, BaseException):
                    raise self.response
                return self.response

        for response, expected in (
            (_Response(status_code=400, payload={"error": {"code": "invalid_totp"}}), RuntimeError),
            (ConnectionError("connection reset"), ConnectionError),
        ):
            with self.subTest(error=type(response).__name__):
                session = Session(response)
                proof = self._solved_response(session, "verify_mfa")
                with patch.object(openai_auth, "request_sentinel_token", return_value=proof), \
                     patch.object(openai_auth, "_totp_code", return_value="123456"):
                    with self.assertRaises(expected) as raised:
                        openai_auth.verify_login_totp(
                            session,
                            {"page": {"type": "mfa_challenge", "payload": {"factor_id": "f1"}}},
                            "secret",
                        )
                self.assertNotIsInstance(raised.exception, openai_auth.AccountUnusableError)
    def test_get_mfa_enabled_accepts_only_booleans(self):
        class Session(_ProtocolSession):
            def __init__(self, payload):
                super().__init__()
                self._payload = payload

            def get(self, url, *, headers, allow_redirects=True):
                return _Response(payload=self._payload)

        self.assertTrue(openai_auth.get_mfa_enabled(Session({"mfa_enabled": True}), "tok"))
        self.assertFalse(openai_auth.get_mfa_enabled(Session({"mfa_enabled": False}), "tok"))
        with self.assertRaises(RuntimeError):
            openai_auth.get_mfa_enabled(Session({"mfa_enabled": "false"}), "tok")
        with self.assertRaises(RuntimeError):
            openai_auth.get_mfa_enabled(Session({}), "tok")

    def test_get_mfa_enabled_uses_each_session_locale_profile(self):
        class Session(BrowserSession):
            def __init__(self, profile):
                self.browser_profile = profile
                self.device_id = "device-placeholder"
                self.client_build_number = "build-placeholder"
                self.client_version = "version-placeholder"
                self.oai_session_id = "session-placeholder"
                self.datadog_origin = "rum"
                self.datadog_trace_id = "1"
                self.datadog_parent_id = "2"
                self.response = _Response(payload={"mfa_enabled": True})
                self.request_headers = None

            def get(self, url, *, headers, allow_redirects=True):
                self.request_headers = dict(headers)
                return self.response

        with patch.object(browser_config, "AUTO_BROWSER_LOCALE_FROM_IP", True):
            profiles = [
                browser_config.build_browser_environment(
                    {"country": "BR", "timezone": "America/Sao_Paulo"}, base_profile={},
                ),
                browser_config.build_browser_environment(
                    {"country": "VN", "timezone": "Asia/Ho_Chi_Minh"}, base_profile={},
                ),
            ]
        with patch.object(browser_config, "AUTO_BROWSER_LOCALE_FROM_IP", False), \
             patch.object(browser_config, "BROWSER_LOCALE_PROFILE", "jp"):
            profiles.append(browser_config.build_browser_environment({}, base_profile={}))

        sessions = [Session(profile) for profile in profiles]
        for session, profile in zip(sessions, profiles):
            with self.subTest(locale=profile["navigator_language"]):
                self.assertTrue(openai_auth.get_mfa_enabled(session, "access-token-placeholder"))
                self.assertEqual(
                    session.request_headers["oai-language"], profile["navigator_language"],
                )
                self.assertEqual(
                    session.request_headers["accept-language"], profile["accept_language"],
                )
                self.assertEqual(profile["navigator_languages"][0], profile["navigator_language"])
                self.assertTrue(profile["accept_language"].startswith(profile["navigator_language"]))
                self.assertTrue(profile["timezone_iana"])

        self.assertEqual(
            [session.request_headers["oai-language"] for session in sessions],
            [profile["navigator_language"] for profile in profiles],
        )
        self.assertEqual(
            len({session.request_headers["oai-language"] for session in sessions}),
            len(sessions),
        )

    def test_registration_follow_authorize_omits_user_header_and_auto_follows(self):
        class Session(_ProtocolSession):
            def get_auth_navigate_headers(self, referer="", user_initiated=True, **_kwargs):
                headers = {"referer": referer}
                if user_initiated:
                    headers["sec-fetch-user"] = "?1"
                return headers

            def get(self, url, *, headers, allow_redirects=True):
                self.gets.append((url, dict(headers), allow_redirects))
                self.get_navigation_ids.append(self.document_navigation_id)
                return _Response(url="https://auth.openai.com/email-verification")

        session = Session()
        with patch.object(openai_auth, "_proxy_retry_config", return_value=(1, 0)):
            final_url = openai_auth.follow_authorize(
                session,
                "https://auth.openai.com/oauth/authorize?state=test",
                user_initiated=False,
            )

        self.assertEqual(final_url, "https://auth.openai.com/email-verification")
        self.assertEqual(len(session.gets), 1)
        url, headers, allow_redirects = session.gets[0]
        self.assertEqual(url, "https://auth.openai.com/oauth/authorize?state=test")
        self.assertNotIn("sec-fetch-user", headers)
        self.assertTrue(allow_redirects)

    def test_follow_authorize_default_keeps_user_initiated_header(self):
        class Session(_ProtocolSession):
            def get_auth_navigate_headers(self, referer="", user_initiated=True, **_kwargs):
                headers = {"referer": referer}
                if user_initiated:
                    headers["sec-fetch-user"] = "?1"
                return headers

        session = Session()
        with patch.object(openai_auth, "_proxy_retry_config", return_value=(1, 0)):
            openai_auth.follow_authorize(
                session, "https://auth.openai.com/oauth/authorize?state=test",
            )

        self.assertEqual(len(session.gets), 1)
        self.assertTrue(session.gets[0][2])
        self.assertEqual(session.gets[0][1]["sec-fetch-user"], "?1")

    def _probe_registration_navigation_id(self, session):
        with patch.object(
            openai_auth, "request_sentinel_token", return_value={"token": "challenge"}
        ), patch.object(
            openai_auth, "build_sentinel_header", return_value=("sentinel-header", None)
        ):
            openai_auth.probe_registration_state(session, "user@example.com")
        return session.posts[-1][1]["x-openai-document-navigation-id"]

    def test_follow_authorize_rotates_before_status_check_and_probe_uses_new_id(self):
        class Session(_ProtocolSession):
            def __init__(self):
                super().__init__()
                self.status_ids = []

            def get(self, url, *, headers, allow_redirects=True):
                self.get_navigation_ids.append(self.document_navigation_id)
                return _ObservedResponse(
                    self, self.status_ids,
                    url="https://auth.openai.com/create-account/password",
                )

        session = Session()
        with patch.object(openai_auth, "_proxy_retry_config", return_value=(1, 0)):
            final_url = openai_auth.follow_authorize(
                session, "https://auth.openai.com/api/accounts/authorize?x=1"
            )

        probe_navigation_id = self._probe_registration_navigation_id(session)

        self.assertEqual(final_url, "https://auth.openai.com/create-account/password")
        self.assertEqual(session.registration_authorize_state["landing_url"], final_url)
        self.assertEqual(session.registration_authorize_state["page"], {})
        self.assertEqual(session.get_navigation_ids, ["document-1"])
        self.assertEqual(session.status_ids, ["document-1-next"])
        self.assertEqual(probe_navigation_id, "document-1-next")

    def test_follow_authorize_http_error_rotates_before_retry(self):
        class Session(_ProtocolSession):
            def __init__(self):
                super().__init__()
                self.status_ids = []

            def get(self, url, *, headers, allow_redirects=True):
                self.get_navigation_ids.append(self.document_navigation_id)
                status_code = 403 if len(self.get_navigation_ids) == 1 else 200
                return _ObservedResponse(
                    self, self.status_ids, status_code=status_code,
                    url="https://auth.openai.com/create-account/password",
                )

        session = Session()
        with patch.object(openai_auth, "_proxy_retry_config", return_value=(2, 0)):
            openai_auth.follow_authorize(
                session, "https://auth.openai.com/api/accounts/authorize?x=1"
            )

        probe_navigation_id = self._probe_registration_navigation_id(session)

        self.assertEqual(session.get_navigation_ids, ["document-1", "document-1-next"])
        self.assertEqual(
            session.status_ids, ["document-1-next", "document-1-next-next"]
        )
        self.assertEqual(probe_navigation_id, "document-1-next-next")

    def test_follow_authorize_transport_exception_does_not_rotate_before_retry(self):
        class Session(_ProtocolSession):
            def __init__(self):
                super().__init__()
                self.status_ids = []

            def get(self, url, *, headers, allow_redirects=True):
                self.get_navigation_ids.append(self.document_navigation_id)
                if len(self.get_navigation_ids) == 1:
                    raise ConnectionError("connection reset")
                return _ObservedResponse(
                    self, self.status_ids,
                    url="https://auth.openai.com/create-account/password",
                )

        session = Session()
        with patch.object(openai_auth, "_proxy_retry_config", return_value=(2, 0)):
            openai_auth.follow_authorize(
                session, "https://auth.openai.com/api/accounts/authorize?x=1"
            )

        probe_navigation_id = self._probe_registration_navigation_id(session)

        self.assertEqual(session.get_navigation_ids, ["document-1", "document-1"])
        self.assertEqual(session.status_ids, ["document-1-next"])
        self.assertEqual(probe_navigation_id, "document-1-next")


if __name__ == "__main__":
    unittest.main()
