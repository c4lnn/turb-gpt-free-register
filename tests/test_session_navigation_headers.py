# -*- coding: utf-8 -*-
import time
import unittest
from types import SimpleNamespace
from requests.cookies import RequestsCookieJar
from http.cookiejar import Cookie
from unittest.mock import patch

from config.browser import build_browser_environment
from core.openai.transport.session import BrowserSession, HeaderProfile, attach_sentinel_credentials


class SessionNavigationHeaderTests(unittest.TestCase):
    HIGH_ENTROPY_FIELDS = {
        "sec-ch-ua-full-version": "chrome_full_version",
        "sec-ch-ua-full-version-list": "sec_ch_ua_full_version_list",
        "sec-ch-ua-platform-version": "sec_ch_ua_platform_version",
        "sec-ch-ua-arch": "sec_ch_ua_arch",
        "sec-ch-ua-bitness": "sec_ch_ua_bitness",
        "sec-ch-ua-model": "sec_ch_ua_model",
    }
    RUM_FIELDS = {
        "traceparent", "tracestate", "x-access-flow-invocation-id",
        "x-openai-document-navigation-id", "x-datadog-origin",
        "x-datadog-sampling-priority", "x-datadog-trace-id", "x-datadog-parent-id",
    }

    def test_vietnam_exit_uses_matching_locale_and_timezone(self):
        profile = build_browser_environment({
            "country": "VN",
            "timezone": "Asia/Ho_Chi_Minh",
            "city": "Bien Hoa",
        })

        self.assertEqual(profile["navigator_language"], "vi-VN")
        self.assertTrue(profile["accept_language"].startswith("vi-VN,vi;"))
        self.assertEqual(profile["timezone_iana"], "Asia/Ho_Chi_Minh")
        self.assertEqual(profile["timezone_offset_minutes"], 420)

    def test_proxy_country_without_named_profile_is_derived_from_geo(self):
        profile = build_browser_environment({
            "country": "TH",
            "timezone": "Asia/Bangkok",
            "city": "Bangkok",
        })

        self.assertEqual(profile["locale_profile"], "geo:th")
        self.assertEqual(profile["navigator_language"], "th-TH")
        self.assertTrue(profile["accept_language"].startswith("th-TH,th;"))
        self.assertEqual(profile["timezone_iana"], "Asia/Bangkok")
        self.assertEqual(profile["timezone_offset_minutes"], 420)

    def test_unknown_proxy_country_does_not_leak_fixed_local_locale(self):
        profile = build_browser_environment({
            "country": "XX",
            "timezone": "UTC",
        })

        self.assertEqual(profile["locale_profile"], "geo:xx")
        self.assertEqual(profile["navigator_language"], "en-US")
        self.assertEqual(profile["timezone_iana"], "UTC")

    def test_geo_country_name_is_normalized_to_iso_code(self):
        geo = BrowserSession._normalize_geo_response({
            "country": "Japan",
            "country_code": "JP",
            "timezone": "Asia/Tokyo",
        })
        self.assertEqual(geo["country"], "JP")

        geo_without_code = BrowserSession._normalize_geo_response({"country": "Vietnam"})
        self.assertEqual(geo_without_code["country"], "VN")

    def _session_stub(self):
        session = object.__new__(BrowserSession)
        session.browser_profile = {
            "user_agent": "Mozilla/5.0 Test Chrome/146.0.0.0 Safari/537.36",
            "accept_language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
            "send_client_hints": True,
            "sec_ch_ua": '"Google Chrome";v="146", "Chromium";v="146"',
            "sec_ch_ua_mobile": "?0",
            "sec_ch_ua_platform": '"macOS"',
            "chrome_full_version": "146.0.0.0",
            "sec_ch_ua_full_version_list": '"Google Chrome";v="146.0.0.0", "Chromium";v="146.0.0.0"',
            "sec_ch_ua_platform_version": '"15.7.0"',
            "sec_ch_ua_arch": '"arm"',
            "sec_ch_ua_bitness": '"64"',
            "sec_ch_ua_model": '""',
        }
        session.document_navigation_id = "document-for-header-tests"
        session.datadog_origin = "rum"
        session.datadog_trace_id = "11"
        session.datadog_parent_id = "12"
        session.device_id = "device-for-header-tests"
        session.oai_session_id = "session-for-header-tests"
        return session

    def test_cross_site_auth_navigation_uses_native_document_headers(self):
        headers = self._session_stub().get_auth_navigate_headers(
            referer="https://chatgpt.com/"
        )

        self.assertEqual(headers["sec-fetch-site"], "cross-site")
        self.assertEqual(headers["sec-fetch-mode"], "navigate")
        self.assertEqual(headers["sec-fetch-dest"], "document")
        self.assertEqual(headers["sec-fetch-user"], "?1")
        self.assertNotIn("cache-control", headers)
        self.assertEqual(
            headers["accept-language"],
            "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
        )
        self.assertNotIn("origin", headers)
        self.assertFalse(any(key.startswith("x-datadog-") for key in headers))

    def test_auth_navigation_can_omit_user_initiated_header(self):
        headers = self._session_stub().get_auth_navigate_headers(
            referer="https://chatgpt.com/", user_initiated=False,
        )

        self.assertEqual(headers["sec-fetch-site"], "cross-site")
        self.assertNotIn("sec-fetch-user", headers)

    def test_external_oauth_navigation_has_no_fake_referer(self):
        headers = self._session_stub().get_auth_navigate_headers(referer="")

        self.assertEqual(headers["sec-fetch-site"], "none")
        self.assertEqual(headers["sec-fetch-mode"], "navigate")
        self.assertEqual(headers["sec-fetch-dest"], "document")
        self.assertEqual(headers["sec-fetch-user"], "?1")
        self.assertNotIn("referer", headers)

    def test_initial_chatgpt_navigation_matches_address_bar_navigation(self):
        headers = self._session_stub().get_chatgpt_navigate_headers(referer="")

        self.assertEqual(headers["sec-fetch-site"], "none")
        self.assertEqual(headers["sec-fetch-mode"], "navigate")
        self.assertEqual(headers["sec-fetch-dest"], "document")
        self.assertNotIn("referer", headers)
        self.assertNotIn("cache-control", headers)

    def test_browser_session_uses_chrome152_for_both_device_id_modes(self):
        sessions = [
            BrowserSession(proxy="", detect_exit_geo=False, server_device_id=False),
            BrowserSession(proxy="", detect_exit_geo=False, server_device_id=True),
        ]
        try:
            for session in sessions:
                self.assertEqual(session.impersonate, "chrome152")
                self.assertIn("Chrome/152.0.0.0", session.browser_profile["user_agent"])
                headers = session.get_chatgpt_navigate_headers(referer="")
                self.assertIn('v="152"', headers["sec-ch-ua"])
                self.assertEqual(headers["sec-ch-ua-platform"], '"macOS"')
                self.assertEqual(headers["sec-ch-ua-mobile"], "?0")
            self.assertFalse(sessions[0].server_device_id)
            self.assertTrue(sessions[1].server_device_id)
        finally:
            for session in sessions:
                session.close()

    def test_auth_high_entropy_headers_are_opt_in_as_one_complete_set(self):
        session = self._session_stub()
        session.browser_profile.update({
            "chrome_full_version": "173.2.4.6",
            "sec_ch_ua_full_version_list": '"Synthetic Chromium";v="173.2.4.6"',
            "sec_ch_ua_platform_version": '"23.4.5"',
            "sec_ch_ua_arch": '"synthetic-arch"',
            "sec_ch_ua_bitness": '"128"',
            "sec_ch_ua_model": '"Synthetic model"',
        })
        expected = {
            header: session.browser_profile[key]
            for header, key in self.HIGH_ENTROPY_FIELDS.items()
        }
        builders = (
            session.get_auth_headers, session.get_auth_navigate_headers,
            session.get_nextauth_headers,
            lambda **kwargs: session.get_nextauth_csrf_headers("https://chatgpt.com/", **kwargs),
            lambda **kwargs: session.get_nextauth_signin_headers("https://chatgpt.com/", **kwargs),
            session.get_chatgpt_navigate_headers,
            lambda **kwargs: session.get_nextauth_csrf_headers("https://chatgpt.com/", no_cache=True, **kwargs),
            lambda **kwargs: session.get_nextauth_signin_headers("https://chatgpt.com/", no_cache=True, **kwargs),
            lambda **kwargs: session.get_chatgpt_navigate_headers(
                "https://auth.openai.com/", user_initiated=False, **kwargs,
            ),
        )
        for index, build in enumerate(builders):
            with self.subTest(scene=index):
                default = build()
                enabled = build(include_high_entropy=True)
                self.assertTrue(set(expected).isdisjoint(default))
                self.assertEqual({name: enabled[name] for name in expected}, expected)
                self.assertEqual(
                    {name: value for name, value in enabled.items() if name not in expected and name not in self.RUM_FIELDS},
                    {name: value for name, value in default.items() if name not in self.RUM_FIELDS},
                )
                sentinel = session.get_sentinel_headers()
                self.assertTrue(set(expected).isdisjoint(sentinel))
                self.assertTrue(self.RUM_FIELDS.isdisjoint(sentinel))
                self.assertNotIn("openai-sentinel-token", sentinel)
                self.assertNotIn("openai-sentinel-so-token", sentinel)
                self.assertEqual(sentinel["User-Agent"], session.browser_profile["user_agent"])
                self.assertEqual(sentinel["accept-language"], session.browser_profile["accept_language"])
                subsequent = build(include_high_entropy=True)
                self.assertEqual({name: subsequent[name] for name in expected}, expected)
                self.assertIsNot(enabled, subsequent)
        self.assertTrue(set(expected).isdisjoint(session.get_chatgpt_headers()))

    def test_auth_high_entropy_opt_in_rejects_incomplete_or_non_chromium_profile(self):
        session = BrowserSession(proxy="", detect_exit_geo=False)
        try:
            session.browser_profile = {
                "send_client_hints": True,
                "chrome_full_version": "152.0.0.0",
            }
            with self.assertRaisesRegex(ValueError, "缺少高熵 Client Hints"):
                session.get_auth_headers(include_high_entropy=True)

            session.browser_profile = {"send_client_hints": False}
            with self.assertRaisesRegex(ValueError, "仅支持 Chromium"):
                session.get_auth_navigate_headers(include_high_entropy=True)
        finally:
            session.close()

    def test_password_totp_nine_scenes_have_exact_header_contracts(self):
        from config import SENTINEL_SV

        session = self._session_stub()
        profile = HeaderProfile.PASSWORD_TOTP_LOGIN
        referer = "https://chatgpt.com/auth/login_with?login_hint=user%2Btag%40example.test"
        base = {"user-agent", "accept-language", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform"}
        fetch = {"accept", "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest", "referer"}
        high_entropy = set(self.HIGH_ENTROPY_FIELDS)
        cached_fetch = base | fetch | high_entropy | {"cache-control", "pragma"}
        auth_fetch = base | fetch | high_entropy | self.RUM_FIELDS | {"content-type", "origin"}
        document = base | high_entropy | {
            "accept", "referer", "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest",
            "upgrade-insecure-requests",
        }
        sentinel = base | fetch | {"content-type", "origin"}

        with patch("core.openai.transport.session.random.getrandbits", side_effect=[11, 12, 21, 22, 31, 32]), patch(
            "core.openai.transport.session.uuid.uuid4", side_effect=["password-call", "issue-call", "verify-call"]
        ):
            csrf = session.get_nextauth_csrf_headers(referer, header_profile=profile)
            signin = session.get_nextauth_signin_headers(referer, header_profile=profile)
            authorize = session.get_auth_navigate_headers(header_profile=profile)
            password_page = session.get_auth_navigate_headers(header_profile=profile)
            password_challenge = session.get_sentinel_headers(header_profile=profile)
            password = attach_sentinel_credentials(
                session.get_auth_headers("https://auth.openai.com/", header_profile=profile), "password-credential",
            )
            issue = session.get_auth_headers("https://auth.openai.com/", accept="*/*", header_profile=profile)
            mfa_challenge = session.get_sentinel_headers(header_profile=profile)
            verify = attach_sentinel_credentials(
                session.get_auth_headers("https://auth.openai.com/", header_profile=profile), "mfa-credential",
            )

        scenes = [
            (16, csrf, cached_fetch),
            (17, signin, cached_fetch | {"content-type", "origin"}),
            (18, authorize, document),
            (19, password_page, document),
            (34, password_challenge, sentinel),
            (58, password, auth_fetch | {"openai-sentinel-token"}),
            (84, issue, auth_fetch),
            (87, mfa_challenge, sentinel),
            (93, verify, auth_fetch | {"openai-sentinel-token"}),
        ]
        for capture, raw_headers, expected_fields in scenes:
            with self.subTest(capture=capture):
                headers = {name.lower(): value for name, value in raw_headers.items()}
                self.assertEqual(set(headers), expected_fields)
                self.assertEqual(headers["user-agent"], session.browser_profile["user_agent"])
                self.assertEqual(headers["accept-language"], session.browser_profile["accept_language"])
                for header, key in (
                    ("sec-ch-ua", "sec_ch_ua"), ("sec-ch-ua-mobile", "sec_ch_ua_mobile"),
                    ("sec-ch-ua-platform", "sec_ch_ua_platform"),
                ):
                    self.assertEqual(headers[header], session.browser_profile[key])
                if capture not in (34, 87):
                    self.assertEqual(
                        {name: headers[name] for name in self.HIGH_ENTROPY_FIELDS},
                        {name: session.browser_profile[key] for name, key in self.HIGH_ENTROPY_FIELDS.items()},
                    )
                if capture in (18, 19):
                    self.assertTrue(headers["accept"].startswith("text/html,application/xhtml+xml"))
                    self.assertEqual(headers["referer"], "https://chatgpt.com/")
                    self.assertEqual(headers["sec-fetch-site"], "cross-site")
                    self.assertEqual(headers["sec-fetch-mode"], "navigate")
                    self.assertEqual(headers["sec-fetch-dest"], "document")
                    self.assertEqual(headers["upgrade-insecure-requests"], "1")
                else:
                    self.assertEqual(headers["sec-fetch-site"], "same-origin")
                    self.assertEqual(headers["sec-fetch-mode"], "cors")
                    self.assertEqual(headers["sec-fetch-dest"], "empty")

        for headers in (csrf, signin):
            self.assertEqual(headers["accept"], "*/*")
            self.assertEqual(headers["referer"], referer)
            self.assertEqual(headers["cache-control"], "no-cache")
            self.assertEqual(headers["pragma"], "no-cache")
        self.assertEqual(signin["content-type"], "application/x-www-form-urlencoded")
        self.assertEqual(signin["origin"], "https://chatgpt.com")
        for headers in (password_challenge, mfa_challenge):
            self.assertEqual(headers["accept"], "*/*")
            self.assertEqual(headers["content-type"], "text/plain;charset=UTF-8")
            self.assertEqual(headers["origin"], "https://sentinel.openai.com")
            self.assertEqual(headers["referer"], f"https://sentinel.openai.com/backend-api/sentinel/frame.html?sv={SENTINEL_SV}")
        for headers in (password, issue, verify):
            self.assertEqual(headers["content-type"], "application/json")
            self.assertEqual(headers["origin"], "https://auth.openai.com")
            self.assertEqual(headers["referer"], "https://auth.openai.com/")
        self.assertEqual(password["accept"], "application/json")
        self.assertEqual(issue["accept"], "*/*")
        self.assertEqual(verify["accept"], "application/json")
        self.assertEqual(password["openai-sentinel-token"], "password-credential")
        self.assertEqual(verify["openai-sentinel-token"], "mfa-credential")

    def test_standard_nextauth_endpoints_preserve_cache_and_priority_choices(self):
        session = self._session_stub()
        referer = "https://chatgpt.com/auth/login_with?login_hint=registration%40example.test"
        ordinary = session.get_nextauth_csrf_headers(referer)
        self.assertEqual(ordinary, session.get_nextauth_headers(referer))
        self.assertEqual(ordinary["content-type"], "application/json")
        for no_cache in (False, True):
            csrf = session.get_nextauth_csrf_headers(referer, no_cache=no_cache)
            signin = session.get_nextauth_signin_headers(referer, no_cache=no_cache)
            for headers in (csrf, signin):
                self.assertEqual(headers["priority"], "u=1, i")
                self.assertEqual(headers["referer"], referer)
                self.assertTrue(set(self.HIGH_ENTROPY_FIELDS).isdisjoint(headers))
                self.assertTrue(self.RUM_FIELDS.isdisjoint(headers))
                if no_cache:
                    self.assertEqual(headers["cache-control"], "no-cache")
                    self.assertEqual(headers["pragma"], "no-cache")
                else:
                    self.assertNotIn("cache-control", headers)
                    self.assertNotIn("pragma", headers)
            self.assertNotIn("origin", csrf)
            self.assertEqual("content-type" in csrf, not no_cache)
            self.assertEqual(signin["content-type"], "application/x-www-form-urlencoded")
            self.assertEqual(signin["origin"], "https://chatgpt.com")

    def test_nextauth_fetch_retains_empty_referer_without_adding_origin(self):
        headers = self._session_stub().get_nextauth_headers(referer="")

        self.assertEqual(headers["referer"], "")
        self.assertNotIn("origin", headers)

    def test_standard_auth_preserves_explicit_high_entropy_and_accept_choices(self):
        session = self._session_stub()
        referer = "https://auth.openai.com/"
        fetch = session.get_auth_headers(referer, include_high_entropy=True, accept="*/*")
        navigation = session.get_auth_navigate_headers(referer, include_high_entropy=True)
        self.assertEqual(fetch["accept"], "*/*")
        self.assertEqual(fetch["priority"], "u=1, i")
        self.assertEqual(fetch["referer"], referer)
        self.assertEqual(navigation["priority"], "u=0, i")
        self.assertEqual(navigation["sec-fetch-user"], "?1")
        self.assertEqual(navigation["sec-fetch-site"], "same-origin")
        self.assertEqual(navigation["referer"], referer)
        for headers in (fetch, navigation):
            self.assertEqual(
                {name: headers[name] for name in self.HIGH_ENTROPY_FIELDS},
                {name: session.browser_profile[key] for name, key in self.HIGH_ENTROPY_FIELDS.items()},
            )

    def test_high_entropy_validation_rejects_each_missing_or_empty_profile_value(self):
        session = self._session_stub()
        complete = dict(session.browser_profile)
        builders = (
            lambda: session.get_nextauth_csrf_headers("https://chatgpt.com/", header_profile=HeaderProfile.PASSWORD_TOTP_LOGIN),
            lambda: session.get_nextauth_signin_headers("https://chatgpt.com/", header_profile=HeaderProfile.PASSWORD_TOTP_LOGIN),
            lambda: session.get_auth_headers(include_high_entropy=True),
            lambda: session.get_auth_navigate_headers(header_profile=HeaderProfile.PASSWORD_TOTP_LOGIN),
            lambda: session.get_nextauth_headers(include_high_entropy=True),
            lambda: session.get_nextauth_csrf_headers("https://chatgpt.com/", include_high_entropy=True),
            lambda: session.get_nextauth_signin_headers("https://chatgpt.com/", include_high_entropy=True),
            lambda: session.get_chatgpt_navigate_headers(include_high_entropy=True),
        )
        for header, key in self.HIGH_ENTROPY_FIELDS.items():
            for invalid in (None, "", " ", 0):
                session.browser_profile = {**complete, key: invalid}
                for index, build in enumerate(builders):
                    with self.subTest(header=header, invalid=invalid, scene=index):
                        with self.assertRaisesRegex(ValueError, header):
                            build()
            session.browser_profile = dict(complete)
            del session.browser_profile[key]
            for index, build in enumerate(builders):
                with self.subTest(missing=key, scene=index):
                    with self.assertRaisesRegex(ValueError, header):
                        build()
        session.browser_profile = complete
        for build in builders:
            self.assertEqual(build()["sec-ch-ua-model"], '""')

    def test_standard_fetch_preserves_base_profile_and_per_field_fallbacks(self):
        session = self._session_stub()
        session.browser_profile = {
            "user_agent": "Synthetic session UA",
            "sec_ch_ua_mobile": "?1",
        }
        with patch.multiple(
            "core.openai.transport.session",
            USER_AGENT="Synthetic fallback UA",
            ACCEPT_LANGUAGE="en-GB,en;q=0.9",
            SEC_CH_UA='"Fallback";v="1"',
            SEC_CH_UA_MOBILE="?0",
            SEC_CH_UA_PLATFORM='"Fallback OS"',
        ):
            headers = session.get_nextauth_headers()
            self.assertEqual(headers["User-Agent"], "Synthetic session UA")
            self.assertEqual(headers["accept-language"], "en-GB,en;q=0.9")
            self.assertEqual(headers["sec-ch-ua"], '"Fallback";v="1"')
            self.assertEqual(headers["sec-ch-ua-mobile"], "?1")
            self.assertEqual(headers["sec-ch-ua-platform"], '"Fallback OS"')

            session.browser_profile = {}
            self.assertEqual(session.get_nextauth_headers()["User-Agent"], "Synthetic fallback UA")

    def test_non_chromium_scenes_do_not_fabricate_client_hints(self):
        session = self._session_stub()
        session.browser_profile = {
            "user_agent": "Synthetic Safari", "accept_language": "en-GB,en;q=0.9", "send_client_hints": False,
        }
        for headers in (
            session.get_nextauth_csrf_headers("https://chatgpt.com/"),
            session.get_nextauth_signin_headers("https://chatgpt.com/"),
            session.get_auth_headers(), session.get_auth_navigate_headers(),
            session.get_sentinel_headers(header_profile=HeaderProfile.PASSWORD_TOTP_LOGIN),
            session.get_chatgpt_headers(), session.get_chatgpt_navigate_headers(),
        ):
            self.assertEqual(headers["User-Agent"], "Synthetic Safari")
            self.assertEqual(headers["accept-language"], "en-GB,en;q=0.9")
            self.assertFalse(any(name.lower().startswith("sec-ch-") for name in headers))
        for build in (
            lambda: session.get_nextauth_csrf_headers("https://chatgpt.com/", header_profile=HeaderProfile.PASSWORD_TOTP_LOGIN),
            lambda: session.get_nextauth_signin_headers("https://chatgpt.com/", header_profile=HeaderProfile.PASSWORD_TOTP_LOGIN),
            lambda: session.get_auth_headers(header_profile=HeaderProfile.PASSWORD_TOTP_LOGIN),
            lambda: session.get_auth_navigate_headers(include_high_entropy=True),
            lambda: session.get_nextauth_headers(include_high_entropy=True),
            lambda: session.get_nextauth_csrf_headers("https://chatgpt.com/", include_high_entropy=True),
            lambda: session.get_nextauth_signin_headers("https://chatgpt.com/", include_high_entropy=True),
            lambda: session.get_chatgpt_navigate_headers(include_high_entropy=True),
        ):
            with self.assertRaisesRegex(ValueError, "仅支持 Chromium"):
                build()

    def test_each_scene_returns_independent_dictionaries_without_interleaved_pollution(self):
        session = self._session_stub()
        target = HeaderProfile.PASSWORD_TOTP_LOGIN
        builders = (
            lambda: session.get_nextauth_headers(),
            lambda: session.get_nextauth_csrf_headers("https://chatgpt.com/", header_profile=target),
            lambda: session.get_nextauth_signin_headers("https://chatgpt.com/", header_profile=target),
            lambda: session.get_auth_headers(header_profile=target),
            lambda: session.get_auth_navigate_headers(header_profile=target),
            lambda: session.get_sentinel_headers(header_profile=target),
            lambda: session.get_chatgpt_headers(),
            lambda: session.get_chatgpt_navigate_headers(),
            lambda: session.get_nextauth_headers(include_high_entropy=True),
            lambda: session.get_nextauth_csrf_headers("https://chatgpt.com/", include_high_entropy=True),
            lambda: session.get_nextauth_signin_headers("https://chatgpt.com/", include_high_entropy=True),
            lambda: session.get_chatgpt_navigate_headers(include_high_entropy=True),
        )
        for index, build in enumerate(builders):
            with self.subTest(scene=index):
                first, second = build(), build()
                self.assertIsNot(first, second)
                snapshot = dict(second)
                self.assertIs(attach_sentinel_credentials(first, "validated-main", "validated-so"), first)
                first["accept"] = "changed-for-one-request"
                session.get_auth_headers()
                session.get_auth_navigate_headers()
                session.get_nextauth_csrf_headers("https://chatgpt.com/")
                session.get_sentinel_headers()
                self.assertEqual(second, snapshot)
        for headers in (
            session.get_auth_headers(), session.get_nextauth_headers(), session.get_sentinel_headers(),
            session.get_chatgpt_headers(), session.get_auth_navigate_headers(),
        ):
            self.assertIn("priority", headers)
            self.assertTrue(set(self.HIGH_ENTROPY_FIELDS).isdisjoint(headers))
            self.assertNotIn("openai-sentinel-token", headers)
            self.assertNotIn("openai-sentinel-so-token", headers)
        self.assertEqual(session.get_auth_navigate_headers()["sec-fetch-user"], "?1")

    def test_auth_rum_ids_are_request_scoped_and_navigation_id_is_stable(self):
        session = self._session_stub()
        profile = HeaderProfile.PASSWORD_TOTP_LOGIN
        with patch("core.openai.transport.session.random.getrandbits", side_effect=[101, 102, 201, 202, 301, 302]), patch(
            "core.openai.transport.session.uuid.uuid4", side_effect=["inv-password", "inv-issue", "inv-verify"]
        ):
            requests = [
                session.get_auth_headers(header_profile=profile),
                session.get_auth_headers(accept="*/*", header_profile=profile),
                session.get_auth_headers(header_profile=profile),
            ]
        for headers, trace, parent, invocation in zip(
            requests, (101, 201, 301), (102, 202, 302), ("inv-password", "inv-issue", "inv-verify"),
        ):
            self.assertEqual(headers["x-datadog-trace-id"], str(trace))
            self.assertEqual(headers["x-datadog-parent-id"], str(parent))
            self.assertEqual(headers["traceparent"], f"00-{trace:032x}-{parent:016x}-01")
            self.assertEqual(headers["tracestate"], "dd=s:1;o:rum")
            self.assertEqual(headers["x-datadog-origin"], "rum")
            self.assertEqual(headers["x-datadog-sampling-priority"], "1")
            self.assertEqual(headers["x-access-flow-invocation-id"], invocation)
            self.assertEqual(headers["x-openai-document-navigation-id"], "document-for-header-tests")
        self.assertEqual(session.document_navigation_id, "document-for-header-tests")
        for headers in (
            session.get_nextauth_csrf_headers("https://chatgpt.com/", header_profile=profile),
            session.get_auth_navigate_headers(header_profile=profile),
            session.get_sentinel_headers(header_profile=profile),
        ):
            self.assertTrue(self.RUM_FIELDS.isdisjoint(headers))

    def test_credential_attachment_only_adds_supplied_validated_tokens(self):
        for sentinel_header in (None, "", "validated-main"):
            for so_header in (None, "", "validated-so"):
                with self.subTest(sentinel_header=sentinel_header, so_header=so_header):
                    headers = {"accept": "application/json"}
                    result = attach_sentinel_credentials(headers, sentinel_header, so_header)
                    self.assertIs(result, headers)
                    expected = {"accept": "application/json"}
                    if sentinel_header:
                        expected["openai-sentinel-token"] = sentinel_header
                    if so_header:
                        expected["openai-sentinel-so-token"] = so_header
                    self.assertEqual(headers, expected)

    def test_navigation_headers_keep_navigation_only_fields(self):
        # 关闭传输层默认头后，导航头必须由构造函数显式提供，否则导航报文会缺这两项。
        session = BrowserSession(proxy="", detect_exit_geo=False)
        try:
            auth_headers = session.get_auth_navigate_headers(referer="https://chatgpt.com/")
            chat_headers = session.get_chatgpt_navigate_headers(referer="https://chatgpt.com/")
        finally:
            session.close()
        for headers in (auth_headers, chat_headers):
            self.assertEqual(headers["upgrade-insecure-requests"], "1")
            self.assertEqual(headers["sec-fetch-user"], "?1")
            self.assertEqual(headers["sec-fetch-dest"], "document")
            self.assertEqual(headers["sec-fetch-mode"], "navigate")

    def test_xhr_header_sets_omit_navigation_only_fields(self):
        # 同一会话的 XHR 头集合不得携带导航专属字段。
        session = BrowserSession(proxy="", detect_exit_geo=False)
        try:
            xhr_headers = [
                session.get_nextauth_headers(),
                session.get_sentinel_headers(),
            ]
        finally:
            session.close()
        for headers in xhr_headers:
            self.assertNotIn("upgrade-insecure-requests", headers)
            self.assertNotIn("sec-fetch-user", headers)

    def test_browser_sessions_are_independent_without_seed(self):
        first = BrowserSession(proxy="", detect_exit_geo=False, device_id="device-for-test")
        second = BrowserSession(proxy="", detect_exit_geo=False, device_id="device-for-test")
        try:
            self.assertNotEqual(first.oai_session_id, second.oai_session_id)
            self.assertNotEqual(first.oai_session_id, first.device_id)
        finally:
            first.close()
            second.close()

    def test_explicit_seed_reuses_fingerprint_across_sessions(self):
        first = BrowserSession(proxy="", detect_exit_geo=False, fingerprint_seed="registration:user@example.com")
        second = BrowserSession(proxy="", detect_exit_geo=False, fingerprint_seed="registration:user@example.com")
        try:
            self.assertEqual(first.device_id, second.device_id)
            self.assertEqual(first.oai_session_id, second.oai_session_id)
            self.assertEqual(first.auth_session_logging_id, second.auth_session_logging_id)
        finally:
            first.close()
            second.close()

    def test_cookie_export_keeps_cf_cookie_and_adds_device_context(self):
        session = BrowserSession(proxy="", detect_exit_geo=False, device_id="device-cookie-test")
        try:
            session.session.cookies.set("__cf_bm", "cf-value", domain="chatgpt.com", path="/")
            header = session.chatgpt_cookie_header_with_device()
            self.assertIn("__cf_bm=cf-value", header)
            self.assertIn("oai-did=device-cookie-test", header)
            self.assertIn(f"oai-locale={session.navigator_language()}", header)
        finally:
            session.close()

    # ---- 服务端设备初始化策略与遗留复用隔离 ----

    def _new_session(self, **kwargs):
        return BrowserSession(proxy="", detect_exit_geo=False, **kwargs)

    def _add_jar_cookie(self, session, name="oai-did", value="X", domain=".chatgpt.com",
                        path="/", expires=None):
        if not isinstance(session.session.cookies, SimpleNamespace):
            # wreq 的 jar 是只读快照；这些用例隔离验证接受规则，不写入快照。
            session.session.cookies = SimpleNamespace(jar=RequestsCookieJar())
        cookie = Cookie(
            version=0,
            name=name,
            value=value,
            port=None,
            port_specified=False,
            domain=domain,
            domain_specified=bool(domain),
            domain_initial_dot=(domain or "").startswith("."),
            path=path,
            path_specified=bool(path),
            secure=True,
            expires=expires,
            discard=(expires is None),
            comment=None,
            comment_url=None,
            rest={},
            rfc2109=False,
        )
        session.session.cookies.jar.set_cookie(cookie)

    def test_legacy_default_is_not_server_mode_and_keeps_explicit_device(self):
        session = self._new_session(device_id="explicit-device")
        try:
            self.assertFalse(session.server_device_id)
            self.assertEqual(session.device_id, "explicit-device")
        finally:
            session.close()

    def test_server_mode_pends_device_id_until_preflight(self):
        session = self._new_session(server_device_id=True)
        try:
            self.assertTrue(session.server_device_id)
            self.assertEqual(session.device_id, "")
            # 其它 ID/SID 仍按正常规则生成，不因服务端设备模式而清空。
            self.assertTrue(session.oai_session_id)
            self.assertTrue(session.auth_session_logging_id)
            self.assertTrue(session.sentinel_sid)
            self.assertNotEqual(session.sentinel_sid, session.oai_session_id)
        finally:
            session.close()

    def test_server_mode_with_seed_pends_device_but_keeps_seeded_identity(self):
        seed = "registration:user@example.com"
        first = self._new_session(fingerprint_seed=seed, server_device_id=True)
        second = self._new_session(fingerprint_seed=seed, server_device_id=True)
        try:
            self.assertEqual(first.device_id, "")
            self.assertEqual(second.device_id, "")
            # 非设备 ID 仍由 seed 派生，跨会话稳定。
            self.assertEqual(first.oai_session_id, second.oai_session_id)
            self.assertEqual(first.sentinel_sid, second.sentinel_sid)
            self.assertEqual(first.auth_session_logging_id, second.auth_session_logging_id)
        finally:
            first.close()
            second.close()

    def test_server_mode_does_not_synthesize_device_cookie_on_export(self):
        session = self._new_session(server_device_id=True)
        try:
            session.session.cookies.set("__cf_bm", "cf-value", domain="chatgpt.com", path="/")
            header = session.chatgpt_cookie_header_with_device()
            self.assertIn("__cf_bm=cf-value", header)
            self.assertNotIn("oai-did=", header)
            self.assertNotIn("oai-locale=", header)
        finally:
            session.close()

    def test_server_mode_seed_oai_cookies_is_noop(self):
        session = self._new_session(server_device_id=True)
        try:
            session.seed_oai_cookies()
            header = session.chatgpt_cookie_header_with_device()
            self.assertNotIn("oai-did=", header)
            self.assertNotIn("oai-locale=", header)
        finally:
            session.close()

    # ---- get_server_device_id 有效域/路径/过期/歧义解析 ----

    def test_server_device_id_accepts_host_only_and_chatgpt_domain_cookie(self):
        host_only = self._new_session(server_device_id=True)
        try:
            self._add_jar_cookie(host_only, value="host-only-did", domain="chatgpt.com")
            self.assertEqual(host_only.get_server_device_id(), "host-only-did")
        finally:
            host_only.close()

        domain_cookie = self._new_session(server_device_id=True)
        try:
            self._add_jar_cookie(domain_cookie, value="subdomain-did", domain=".chatgpt.com")
            self.assertEqual(domain_cookie.get_server_device_id(), "subdomain-did")
        finally:
            domain_cookie.close()

    def test_server_device_id_ignores_wrong_domains_and_public_suffix(self):
        for domain, label in (
            ("auth.openai.com", "auth"),
            ("sentinel.openai.com", "sentinel"),
            (".openai.com", "unrelated-parent"),
            (".com", "public-suffix"),
        ):
            session = self._new_session(server_device_id=True)
            try:
                self._add_jar_cookie(session, value="wrong-did", domain=domain)
                self.assertEqual(session.get_server_device_id(), "", msg=label)
            finally:
                session.close()

    def test_server_device_id_ignores_empty_domain_and_empty_value(self):
        empty_domain = self._new_session(server_device_id=True)
        try:
            self._add_jar_cookie(empty_domain, value="no-domain-did", domain="")
            self.assertEqual(empty_domain.get_server_device_id(), "")
        finally:
            empty_domain.close()

        empty_value = self._new_session(server_device_id=True)
        try:
            self._add_jar_cookie(empty_value, value="", domain=".chatgpt.com")
            self.assertEqual(empty_value.get_server_device_id(), "")
        finally:
            empty_value.close()

    def test_server_device_id_ignores_expired_and_wrong_path(self):
        expired = self._new_session(server_device_id=True)
        try:
            self._add_jar_cookie(expired, value="expired-did", domain=".chatgpt.com",
                                 expires=int(time.time()) - 100)
            self.assertEqual(expired.get_server_device_id(), "")
        finally:
            expired.close()

        wrong_path = self._new_session(server_device_id=True)
        try:
            self._add_jar_cookie(wrong_path, value="path-did", domain=".chatgpt.com", path="/api")
            self.assertEqual(wrong_path.get_server_device_id(), "")
        finally:
            wrong_path.close()

    def test_server_device_id_rejects_ambiguous_cookies_even_same_value(self):
        diff_value = self._new_session(server_device_id=True)
        try:
            self._add_jar_cookie(diff_value, value="X", domain=".chatgpt.com")
            self._add_jar_cookie(diff_value, value="Y", domain="chatgpt.com")
            self.assertEqual(diff_value.get_server_device_id(), "")
        finally:
            diff_value.close()

        same_value = self._new_session(server_device_id=True)
        try:
            self._add_jar_cookie(same_value, value="X", domain=".chatgpt.com")
            self._add_jar_cookie(same_value, value="X", domain="chatgpt.com")
            self.assertEqual(same_value.get_server_device_id(), "")
        finally:
            same_value.close()

    def test_server_device_id_empty_when_no_cookie(self):
        session = self._new_session(server_device_id=True)
        try:
            self.assertEqual(session.get_server_device_id(), "")
        finally:
            session.close()

    # ---- reset_preflight_transport 行为 ----

    def test_reset_preserves_identity_and_isolates_cookies(self):
        session = self._new_session(server_device_id=True)
        try:
            session.device_id = "server-bound-did"
            session.session.cookies.set("oai-did", "server-bound-did", domain="chatgpt.com", path="/")
            session.session.cookies.set("__cf_bm", "cf-value", domain="chatgpt.com", path="/")
            session.blocked_until = time.time() + 900
            session.blocked_reason = "HTTP 403"

            profile_before = dict(session.browser_profile)
            impersonate_before = session.impersonate
            sid_before = session.sentinel_sid
            oai_before = session.oai_session_id

            session.reset_preflight_transport()

            self.assertEqual(session.device_id, "server-bound-did")
            self.assertEqual(session.sentinel_sid, sid_before)
            self.assertEqual(session.oai_session_id, oai_before)
            self.assertEqual(session.impersonate, impersonate_before)
            self.assertEqual(session.browser_profile, profile_before)
            # 新 Jar 无 Cookie：失败尝试的 cookie 不进入下一尝试。
            self.assertEqual(session.get_server_device_id(), "")
            self.assertEqual(session.blocked_until, 0.0)
            self.assertEqual(session.blocked_reason, "")
        finally:
            session.close()

    def test_reset_closes_replaced_session_and_relay(self):
        class CloseRecorder:
            def __init__(self):
                self.closed = False
                self.proxies = None
                self.timeout = None

            def close(self):
                self.closed = True

        session = self._new_session(server_device_id=True)
        try:
            old_session = CloseRecorder()
            old_relay = CloseRecorder()
            session.session = old_session
            session._proxy_pool_relay = old_relay

            session.reset_preflight_transport()

            self.assertTrue(old_session.closed)
            self.assertTrue(old_relay.closed)
            self.assertIsNot(session.session, old_session)
            self.assertIsNone(session._proxy_pool_relay)
        finally:
            session.close()

    def test_reset_rotates_pool_route_when_candidate_available(self):
        session = self._new_session(server_device_id=True)
        try:
            with patch("config.proxy.PROXY_POOL",
                       ["socks5://127.0.0.1:10001", "socks5://127.0.0.1:10002"]):
                session._proxy_pool_selected = True
                session.proxy = "socks5://127.0.0.1:10001"
                session.proxy_target = session.proxy

                session.reset_preflight_transport()

                self.assertEqual(session.proxy, "socks5://127.0.0.1:10002")
                self.assertEqual(session.proxy_target, "socks5://127.0.0.1:10002")
        finally:
            session.close()

    def test_reset_keeps_single_pool_proxy_and_never_falls_back_to_direct(self):
        session = self._new_session(server_device_id=True)
        try:
            with patch("config.proxy.PROXY_POOL", ["socks5://127.0.0.1:10001"]):
                session._proxy_pool_selected = True
                session.proxy = "socks5://127.0.0.1:10001"
                session.proxy_target = session.proxy

                session.reset_preflight_transport()

                self.assertEqual(session.proxy, "socks5://127.0.0.1:10001")
                self.assertEqual(session.proxy_target, "socks5://127.0.0.1:10001")
        finally:
            session.close()

    def test_reset_keeps_explicit_proxy_route(self):
        session = BrowserSession(proxy="socks5://127.0.0.1:10009", detect_exit_geo=False,
                                 server_device_id=True)
        try:
            session.reset_preflight_transport()
            self.assertEqual(session.proxy, "socks5://127.0.0.1:10009")
        finally:
            session.close()

    def test_unseeded_legacy_sessions_keep_independent_devices(self):
        with self._new_session() as first, self._new_session() as second:
            self.assertNotEqual(first.device_id, second.device_id)
            self.assertNotEqual(first.device_id, first.sentinel_sid)
            self.assertTrue(first.device_id)

    def test_failed_transport_replacement_closes_new_relay_and_keeps_old_route(self):
        class Relay:
            closed = False

            def close(self):
                self.closed = True

        with self._new_session(server_device_id=True) as session:
            old_transport = session.session
            session._proxy_pool_selected = True
            session.proxy = session.proxy_target = "socks5://127.0.0.1:10001"
            relay = Relay()
            with patch.object(session, "_next_pool_proxy", return_value="socks5://127.0.0.1:10002"), patch(
                "core.infra.proxy_chain.open_proxy_pool_proxy", return_value=("http://127.0.0.1:20000", relay)
            ), patch.object(session, "_open_raw_session", side_effect=RuntimeError("factory failed")):
                with self.assertRaises(RuntimeError):
                    session.reset_preflight_transport()
            self.assertTrue(relay.closed)
            self.assertIs(session.session, old_transport)
            self.assertEqual(session.proxy, "socks5://127.0.0.1:10001")


if __name__ == "__main__":
    unittest.main()
