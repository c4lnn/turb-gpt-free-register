# -*- coding: utf-8 -*-
"""注册预检门禁：真实 BrowserSession/CookieJar，只有底层网络响应受控。

契约：validate_registration_geo 只消费 trace dict（loc/ip），不发网络；
网络/HTTP/407/解析分类在 network_preflight；冻结画像跨 HTTP 与 SDK 一致，
手动国家覆盖已知实际国家，未知探测国家即使手动已选也不可救活。
"""
from contextlib import contextmanager
import unittest
from unittest.mock import Mock, patch

from curl_cffi.requests import Cookies
from config import browser as browser_cfg
from core import openai_auth
from core.registration_geo import RegistrationGeoError
from core.registration_service import StopRequested
from core.session import BrowserSession


_TRACE_JP = "loc=JP\nip=1.2.3.4\n"
_TRACE_VN = "loc=VN\nip=1.2.3.4\n"


class _Response:
    def __init__(self, status=200, text=""):
        self.status_code = status
        self.text = text
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Transport:
    """受控 raw 传输：只服务 trace 与 csrf，其余返回 200 空响应。"""

    def __init__(self, trace, warmup):
        self.cookies = Cookies()
        self.trace = trace
        self.warmup = warmup
        self.closed = False
        self.proxies = {}
        self.trace_calls = 0
        self.warmup_calls = 0

    def get(self, url, **kwargs):
        if url.endswith("/cdn-cgi/trace"):
            self.trace_calls += 1
            return self.trace(self)
        if url.endswith("/api/auth/csrf"):
            self.warmup_calls += 1
            return self.warmup(self)
        return _Response()

    def close(self):
        self.closed = True


@contextmanager
def _session(warmup, *, trace=None, server=True, proxy="", profile=None):
    """真实 BrowserSession，替换为受控传输；预检重试会重建传输。"""
    session = BrowserSession(proxy=proxy, detect_exit_geo=False,
                             server_device_id=server, device_id="legacy-device",
                             browser_profile=profile)
    session.session.close()
    transports = []

    def create():
        raw = _Transport(trace or (lambda _: _Response(text=_TRACE_JP)), warmup)
        transports.append(raw)
        return raw

    session.session = create()
    with patch.object(session, "_open_raw_session", side_effect=create), patch.object(
        openai_auth, "_interruptible_sleep"
    ), patch.object(openai_auth._protocol_cfg, "OPENAI_REGISTRATION_WARMUP_ATTEMPTS", 3):
        try:
            yield session, transports
        finally:
            session.close()


@contextmanager
def _geo_session(profile=None, *, server=True):
    """纯 validate_registration_geo 测试：真实 BrowserSession + 计数传输，不发网络。"""
    session = BrowserSession(proxy="", detect_exit_geo=False,
                             server_device_id=server, device_id="legacy-device",
                             browser_profile=profile)
    session.session.close()
    spy = _Transport(lambda _: _Response(text=_TRACE_JP), lambda _: _Response())
    session.session = spy
    try:
        yield session, spy
    finally:
        session.close()


def _cookie(raw, value="server-device", domain="chatgpt.com"):
    raw.cookies.set("oai-did", value, domain=domain, path="/")
    return _Response()


class RegistrationPreflightTests(unittest.TestCase):
    def test_success_binds_server_cookie_not_explicit_constructor_value(self):
        with _session(_cookie) as (session, _):
            self.assertEqual(session.device_id, "")
            openai_auth.network_preflight(session)
            self.assertEqual(session.device_id, "server-device")
            self.assertEqual(session.get_server_device_id(), session.device_id)

    def test_missing_wrong_domain_and_ambiguous_cookie_exhaust_without_identity(self):
        def ambiguous(raw):
            _cookie(raw, "X", "chatgpt.com")
            return _cookie(raw, "Y", ".chatgpt.com")

        for warmup in (lambda _: _Response(), lambda r: _cookie(r, domain="auth.openai.com"), ambiguous):
            with self.subTest(warmup=warmup), _session(warmup) as (session, transports):
                with self.assertRaises(RuntimeError):
                    openai_auth.network_preflight(session)
                self.assertEqual(session.device_id, "")
                self.assertEqual(len(transports), 3)
                self.assertTrue(all(raw.closed for raw in transports[:-1]))

    def test_exception_after_receiving_valid_cookie_can_pass(self):
        def warmup(raw):
            _cookie(raw)
            raise ConnectionError("response interrupted after headers")

        with _session(warmup) as (session, _):
            openai_auth.network_preflight(session)
            self.assertEqual(session.device_id, "server-device")

    def test_error_status_with_valid_cookie_clears_circuit_for_auth(self):
        def warmup(raw):
            _cookie(raw)
            return _Response(status=403)

        with _session(warmup) as (session, _):
            openai_auth.network_preflight(session)
            self.assertEqual(session.device_id, "server-device")
            # 后续真实会话 get 不应被 warmup 的熔断阻止。
            self.assertEqual(session.get("https://chatgpt.com/api/auth/providers").status_code, 200)

    def test_empty_expired_or_nonroot_cookie_does_not_initialize_device(self):
        from requests.cookies import create_cookie

        for attributes in ({"value": ""}, {"expires": 1}, {"path": "/api"}):
            def invalid_cookie(raw):
                options = {"name": "oai-did", "value": "invalid-device", "domain": "chatgpt.com", "path": "/"}
                options.update(attributes)
                raw.cookies.jar.set_cookie(create_cookie(**options))
                return _Response()

            with self.subTest(attributes=attributes), _session(invalid_cookie) as (session, _):
                with self.assertRaises(RuntimeError):
                    openai_auth.network_preflight(session)
                self.assertEqual(session.device_id, "")

    def test_missing_registration_backend_cannot_fall_back_to_legacy_transport(self):
        with patch("core.protocol_profiles.validate_protocol_profile", side_effect=ValueError("backend unavailable")), patch(
            "core.session.Session", side_effect=AssertionError("legacy backend must not open")
        ):
            with self.assertRaisesRegex(ValueError, "backend unavailable"):
                BrowserSession(proxy="", detect_exit_geo=False, server_device_id=True)

    def test_initial_trace_non_200_stops_before_warmup(self):
        def forbidden(_):
            self.fail("trace HTTP failure must not send csrf")

        with _session(forbidden, trace=lambda _: _Response(status=403)) as (session, transports):
            with self.assertRaises(RegistrationGeoError) as ctx:
                openai_auth.network_preflight(session)
            self.assertEqual(ctx.exception.category, "geo_service")
            self.assertEqual(ctx.exception.stage, "detect")
            self.assertEqual(session.device_id, "")
            self.assertEqual(transports[0].warmup_calls, 0)

    def test_initial_trace_407_stops_before_warmup(self):
        def forbidden(_):
            self.fail("trace 407 must not send csrf")

        with _session(forbidden, trace=lambda _: _Response(status=407)) as (session, transports):
            with self.assertRaises(RegistrationGeoError) as ctx:
                openai_auth.network_preflight(session)
            self.assertEqual(ctx.exception.category, "proxy")
            self.assertEqual(ctx.exception.stage, "detect")
            self.assertEqual(transports[0].warmup_calls, 0)

    def test_initial_trace_network_failure_stops_before_warmup(self):
        def forbidden(_):
            self.fail("trace network failure must not send csrf")

        def down(_):
            raise ConnectionError("network down")

        with _session(forbidden, trace=down) as (session, transports):
            with self.assertRaises(RegistrationGeoError) as ctx:
                openai_auth.network_preflight(session)
            self.assertEqual(ctx.exception.category, "geo_service")
            self.assertEqual(ctx.exception.stage, "detect")
            self.assertEqual(session.device_id, "")
            self.assertEqual(transports[0].warmup_calls, 0)

    def test_initial_trace_parse_failure_stops_before_warmup(self):
        def forbidden(_):
            self.fail("trace parse failure must not send csrf")

        with _session(forbidden, trace=lambda _: _Response(text="no key-value pairs")) as (session, transports):
            with self.assertRaises(RegistrationGeoError) as ctx:
                openai_auth.network_preflight(session)
            self.assertEqual(ctx.exception.category, "geo_parse")
            self.assertEqual(ctx.exception.stage, "detect")
            self.assertEqual(transports[0].warmup_calls, 0)

    def test_initial_trace_invalid_ip_stops_before_warmup(self):
        def forbidden(_):
            self.fail("invalid IP must not send csrf")

        with _session(forbidden, trace=lambda _: _Response(text="loc=JP\nip=not-an-ip\n")) as (session, transports):
            with self.assertRaises(RegistrationGeoError) as ctx:
                openai_auth.network_preflight(session)
            self.assertEqual(ctx.exception.category, "geo_incomplete")
            self.assertEqual(ctx.exception.stage, "validate")
            self.assertEqual(transports[0].warmup_calls, 0)

    def test_initial_trace_unknown_country_with_manual_jp_stops_before_warmup(self):
        def forbidden(_):
            self.fail("unknown country must not send csrf even with manual JP")

        profile = {"geo": {"country": "JP"}}
        with _session(forbidden, trace=lambda _: _Response(text="loc=XX\nip=1.2.3.4\n"),
                      profile=profile) as (session, transports):
            with self.assertRaises(RegistrationGeoError) as ctx:
                openai_auth.network_preflight(session)
            self.assertEqual(ctx.exception.category, "geo_incomplete")
            self.assertEqual(ctx.exception.stage, "validate")
            self.assertEqual(session.device_id, "")
            self.assertEqual(transports[0].warmup_calls, 0)

    def test_initial_trace_failure_does_not_trust_geo_cache(self):
        from core import session as session_mod
        cache_key = "__direct__"
        session_mod._GEO_CACHE[cache_key] = {"country": "JP", "ip": "1.2.3.4"}

        def forbidden(_):
            self.fail("geo cache must not rescue a failed trace")

        def down(_):
            raise ConnectionError("network down")

        try:
            with _session(forbidden, trace=down) as (session, transports):
                with self.assertRaises(RegistrationGeoError) as ctx:
                    openai_auth.network_preflight(session)
                self.assertEqual(ctx.exception.category, "geo_service")
                self.assertEqual(transports[0].warmup_calls, 0)
        finally:
            session_mod._GEO_CACHE.pop(cache_key, None)

    def test_warmup_403_without_valid_device_stops(self):
        with _session(lambda _: _Response(status=403)) as (session, transports):
            with self.assertRaises(RuntimeError):
                openai_auth.network_preflight(session)
            self.assertEqual(session.device_id, "")
            self.assertEqual(len(transports), 3)

    def test_retry_uses_clean_cookie_jar_preserves_profile_and_explicit_route(self):
        calls = 0

        def warmup(raw):
            nonlocal calls
            calls += 1
            if calls == 1:
                raw.cookies.set("failed-cookie", "discard-me", domain="chatgpt.com", path="/")
                return _Response()
            self.assertFalse(any(c.name == "failed-cookie" for c in raw.cookies.jar))
            return _cookie(raw, "second-attempt")

        with _session(warmup, proxy="socks5://127.0.0.1:10009") as (session, transports):
            # 先触发一次门禁冻结，得到跨重试应保持不变的画像快照。
            session.validate_registration_geo({"loc": "JP", "ip": "1.2.3.4"})
            profile = dict(session.browser_profile)
            openai_auth.network_preflight(session)
            self.assertEqual(session.device_id, "second-attempt")
            self.assertEqual(session.browser_profile, profile)
            self.assertEqual(session.proxy, "socks5://127.0.0.1:10009")
            self.assertEqual(session.session.proxies["https"], session.proxy)
            self.assertTrue(transports[0].closed)
            self.assertIs(session.session, transports[-1])
            self.assertEqual(session.exit_geo_observation_scope, "previous_transport_trace")
            self.assertEqual(session.exit_geo["country"], "JP")
            self.assertEqual(session.get("https://auth.openai.com/").status_code, 200)


    def test_stop_propagates_without_binding_received_cookie(self):
        def stopped(raw):
            _cookie(raw)
            raise StopRequested("stopped")

        with _session(stopped) as (session, _):
            with self.assertRaises(StopRequested):
                openai_auth.network_preflight(session)
            self.assertEqual(session.device_id, "")

    def test_stop_before_network_leaves_uninitialized_identity(self):
        with _session(_cookie) as (session, _), patch.object(
            openai_auth, "_check_stop_requested", side_effect=StopRequested("stopped")
        ):
            with self.assertRaises(StopRequested):
                openai_auth.network_preflight(session)
            self.assertEqual(session.get_server_device_id(), "")
            self.assertEqual(session.device_id, "")

    def test_legacy_preflight_keeps_explicit_identity_and_cookie_exports(self):
        with _session(_cookie, server=False) as (session, _):
            openai_auth.network_preflight(session)
            self.assertEqual(session.device_id, "legacy-device")
            self.assertEqual(session.get_server_device_id(), "legacy-device")
            self.assertIn("oai-did=legacy-device", session.auth_cookie_header())
            self.assertIn("oai-locale=", session.auth_cookie_header())


class RegistrationGeoGateTests(unittest.TestCase):
    def test_none_rejects_geo_incomplete_without_network(self):
        with _geo_session() as (session, spy):
            with self.assertRaises(RegistrationGeoError) as ctx:
                session.validate_registration_geo(None)
            self.assertEqual(ctx.exception.category, "geo_incomplete")
            self.assertEqual(ctx.exception.stage, "validate")
            self.assertFalse(session._registration_geo_frozen)
            self.assertEqual(spy.trace_calls, 0)
            self.assertEqual(spy.warmup_calls, 0)

    def test_freezes_auto_profile_and_records_actual_country(self):
        with _geo_session() as (session, _):
            session.validate_registration_geo({"loc": "JP", "ip": "1.2.3.4"})
            self.assertTrue(session._registration_geo_frozen)
            self.assertEqual(session.exit_geo["country"], "JP")
            self.assertEqual(session.exit_geo["ip"], "1.2.3.4")
            self.assertEqual(session.exit_geo["sources"], ["trace"])
            self.assertNotIn("timezone", session.exit_geo)
            self.assertEqual(session.browser_profile["profile_country"], "JP")
            self.assertEqual(session.browser_profile["locale_source"], "auto")
            self.assertEqual(session.browser_profile["geo"], session.exit_geo)
            self.assertEqual(session.browser_profile["navigator_language"], "ja-JP")
            self.assertEqual(session.browser_profile["timezone_iana"], "Asia/Tokyo")
            self.assertEqual(session.browser_profile["timezone_offset_minutes"], 540)

    def test_auto_vn_profile_and_constructor_default_is_not_manual(self):
        with _geo_session() as (session, _):
            session.validate_registration_geo({"loc": "VN", "ip": "1.2.3.4"})
            # 构造阶段默认 jp 画像不算手动，自动模式取实际国家 VN。
            self.assertEqual(session.exit_geo["country"], "VN")
            self.assertEqual(session.browser_profile["profile_country"], "VN")
            self.assertEqual(session.browser_profile["locale_source"], "auto")
            self.assertEqual(session.browser_profile["navigator_language"], "vi-VN")
            self.assertEqual(session.browser_profile["timezone_iana"], "Asia/Ho_Chi_Minh")

    def test_manual_jp_overrides_known_vn_without_rewriting_actual(self):
        profile = {"geo": {"country": "JP"}}
        with _geo_session(profile=profile) as (session, _):
            session.validate_registration_geo({"loc": "VN", "ip": "1.2.3.4"})
            self.assertEqual(session.exit_geo["country"], "VN")
            self.assertEqual(session.exit_geo["ip"], "1.2.3.4")
            self.assertEqual(session.browser_profile["profile_country"], "JP")
            self.assertEqual(session.browser_profile["locale_source"], "manual")
            self.assertEqual(session.browser_profile["navigator_language"], "ja-JP")
            self.assertEqual(session.browser_profile["timezone_iana"], "Asia/Tokyo")
            self.assertEqual(session.browser_profile["geo"]["country"], "VN")

    def test_manual_via_geo_country_code(self):
        profile = {"geo": {"country_code": "JP"}}
        with _geo_session(profile=profile) as (session, _):
            session.validate_registration_geo({"loc": "VN", "ip": "1.2.3.4"})
            self.assertEqual(session.browser_profile["profile_country"], "JP")
            self.assertEqual(session.browser_profile["locale_source"], "manual")

    def test_manual_via_locale_profile(self):
        profile = {"locale_profile": "jp"}
        with _geo_session(profile=profile) as (session, _):
            session.validate_registration_geo({"loc": "VN", "ip": "1.2.3.4"})
            self.assertEqual(session.browser_profile["profile_country"], "JP")
            self.assertEqual(session.browser_profile["locale_source"], "manual")
            self.assertEqual(session.browser_profile["navigator_language"], "ja-JP")

    def test_unknown_country_with_manual_jp_rejects(self):
        profile = {"geo": {"country": "JP"}}
        with _geo_session(profile=profile) as (session, _):
            with self.assertRaises(RegistrationGeoError) as ctx:
                session.validate_registration_geo({"loc": "XX", "ip": "1.2.3.4"})
            self.assertEqual(ctx.exception.category, "geo_incomplete")
            self.assertFalse(session._registration_geo_frozen)

    def test_unknown_manual_country_rejects(self):
        profile = {"geo": {"country": "ZZ"}}
        with _geo_session(profile=profile) as (session, _):
            with self.assertRaises(RegistrationGeoError) as ctx:
                session.validate_registration_geo({"loc": "JP", "ip": "1.2.3.4"})
            self.assertEqual(ctx.exception.category, "geo_incomplete")
            self.assertFalse(session._registration_geo_frozen)

    def test_manual_empty_country_rejects_not_silent_auto(self):
        profile = {"geo": {"country": ""}}
        with _geo_session(profile=profile) as (session, _):
            with self.assertRaises(RegistrationGeoError) as ctx:
                session.validate_registration_geo({"loc": "JP", "ip": "1.2.3.4"})
            self.assertEqual(ctx.exception.category, "geo_incomplete")

    def test_manual_bad_timezone_rejects(self):
        profile = {"geo": {"country": "JP"}, "timezone_iana": "Europe/London"}
        with _geo_session(profile=profile) as (session, _):
            with self.assertRaises(RegistrationGeoError) as ctx:
                session.validate_registration_geo({"loc": "JP", "ip": "1.2.3.4"})
            self.assertEqual(ctx.exception.category, "geo_mismatch")

    def test_manual_bad_language_rejects(self):
        profile = {
            "geo": {"country": "JP"},
            "navigator_language": "en-US",
            "navigator_languages": ["en-US", "en"],
            "accept_language": "en-US,en;q=0.9",
            "timezone_iana": "Asia/Tokyo",
        }
        with _geo_session(profile=profile) as (session, _):
            with self.assertRaises(RegistrationGeoError) as ctx:
                session.validate_registration_geo({"loc": "JP", "ip": "1.2.3.4"})
            self.assertEqual(ctx.exception.category, "geo_mismatch")

    def test_manual_incomplete_locale_rejects(self):
        profile = {"geo": {"country": "JP"}, "navigator_language": "ja-JP"}
        with _geo_session(profile=profile) as (session, _):
            with self.assertRaises(RegistrationGeoError) as ctx:
                session.validate_registration_geo({"loc": "JP", "ip": "1.2.3.4"})
            self.assertEqual(ctx.exception.category, "geo_incomplete")

    def test_auto_disabled_still_detects_actual_country(self):
        with _geo_session() as (session, _), patch.object(browser_cfg, "AUTO_BROWSER_LOCALE_FROM_IP", False):
            session.validate_registration_geo({"loc": "VN", "ip": "1.2.3.4"})
            # 关闭自动画像仍探测实际国家，但画像取自 BROWSER_LOCALE_PROFILE (jp)。
            self.assertEqual(session.exit_geo["country"], "VN")
            self.assertEqual(session.browser_profile["profile_country"], "JP")
            self.assertEqual(session.browser_profile["locale_source"], "manual")
            self.assertEqual(session.browser_profile["navigator_language"], "ja-JP")

    def test_same_country_ip_rotation_keeps_frozen_locale(self):
        with _geo_session() as (session, _):
            session.validate_registration_geo({"loc": "JP", "ip": "1.2.3.4"})
            language = session.browser_profile["navigator_language"]
            session.validate_registration_geo({"loc": "JP", "ip": "1.2.3.5"})
            self.assertEqual(session.exit_geo["ip"], "1.2.3.5")
            self.assertEqual(session.exit_geo["country"], "JP")
            self.assertEqual(session.browser_profile["navigator_language"], language)
            self.assertEqual(session.browser_profile["profile_country"], "JP")

    def test_subsequent_known_country_keeps_frozen_locale(self):
        with _geo_session() as (session, _):
            session.validate_registration_geo({"loc": "JP", "ip": "1.2.3.4"})
            session.validate_registration_geo({"loc": "VN", "ip": "1.2.3.5"})
            self.assertEqual(session.exit_geo["country"], "VN")
            self.assertEqual(session.browser_profile["navigator_language"], "ja-JP")
            self.assertEqual(session.browser_profile["profile_country"], "JP")
            self.assertEqual(session.browser_profile["locale_source"], "auto")

    def test_subsequent_unknown_country_rejects(self):
        with _geo_session() as (session, _):
            session.validate_registration_geo({"loc": "JP", "ip": "1.2.3.4"})
            with self.assertRaises(RegistrationGeoError) as ctx:
                session.validate_registration_geo({"loc": "XX", "ip": "1.2.3.5"})
            self.assertEqual(ctx.exception.category, "geo_incomplete")
            self.assertEqual(session.browser_profile["navigator_language"], "ja-JP")

    def test_missing_or_invalid_exit_ip_does_not_freeze_profile(self):
        for trace in ({"loc": "JP"}, {"loc": "JP", "ip": ""}, {"loc": "JP", "ip": "not-an-ip"}):
            with self.subTest(trace=trace), _geo_session() as (session, _):
                with self.assertRaises(RegistrationGeoError) as raised:
                    session.validate_registration_geo(trace)
                self.assertEqual(raised.exception.category, "geo_incomplete")
                self.assertFalse(session._registration_geo_frozen)

    def test_constructor_failure_releases_transport_and_owned_proxy_relay(self):
        raw = _Transport(lambda _: _Response(), lambda _: _Response())
        relay = Mock()
        with patch("core.session.pick_proxy", return_value="http://127.0.0.1:9"), patch(
            "core.proxy_chain.open_proxy_pool_proxy", return_value=("http://127.0.0.1:9", relay)
        ), patch(
            "core.wreq_session.WreqSession", return_value=raw
        ), patch(
            "core.session.validate_browser_profile", side_effect=RuntimeError("synthetic profile failure")
        ):
            with self.assertRaisesRegex(RuntimeError, "synthetic profile failure"):
                BrowserSession(proxy=None, detect_exit_geo=False, server_device_id=True)
        self.assertTrue(raw.closed)
        relay.close.assert_called_once_with()

    def test_independent_task_is_not_gated(self):
        with _geo_session(server=False) as (session, spy):
            # 非注册会话不触发地域门禁，不探测也不抛错。
            session.validate_registration_geo(None)
            self.assertEqual(spy.trace_calls, 0)


if __name__ == "__main__":
    unittest.main()
