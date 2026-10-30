# -*- coding: utf-8 -*-
import time
import unittest
from types import SimpleNamespace
from requests.cookies import RequestsCookieJar
from http.cookiejar import Cookie
from unittest.mock import patch
from config import browser

from config.browser import build_browser_environment
from core.session import BrowserSession


class SessionNavigationHeaderTests(unittest.TestCase):
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
        }
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

    def test_chrome146_client_hints_match_curl_impersonation(self):
        # 固定此用例的协议版本，不能受开发机 Web/.env 当前选择影响。
        with patch.multiple(browser, PROTOCOL_HTTP_CLIENT="curl_cffi", CURL_CFFI_IMPERSONATE="chrome146"):
            session = BrowserSession(proxy="", detect_exit_geo=False)
        try:
            headers = session.get_chatgpt_navigate_headers(referer="")
            self.assertEqual(
                headers["sec-ch-ua"],
                '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
            )
            self.assertEqual(headers["sec-ch-ua-platform"], '"macOS"')
            self.assertEqual(headers["sec-ch-ua-mobile"], "?0")
        finally:
            session.close()

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
                "core.proxy_chain.open_proxy_pool_proxy", return_value=("http://127.0.0.1:20000", relay)
            ), patch.object(session, "_open_raw_session", side_effect=RuntimeError("factory failed")):
                with self.assertRaises(RuntimeError):
                    session.reset_preflight_transport()
            self.assertTrue(relay.closed)
            self.assertIs(session.session, old_transport)
            self.assertEqual(session.proxy, "socks5://127.0.0.1:10001")


if __name__ == "__main__":
    unittest.main()
