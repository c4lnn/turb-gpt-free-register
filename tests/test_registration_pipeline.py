# -*- coding: utf-8 -*-
"""注册编排的资料分流、初始化错误及任务资源边界。"""
import os
import tempfile
import json
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch
import requests

from core.openai.codex import codex_oauth
from core.openai.auth import chatgpt_auth, openai_auth
from core.infra import db, job_context
from core.mail import email_provider
from core.link import flow_trigger
from core.openai.register import registration_flow as registration
from core.openai.transport.session import BrowserSession, HeaderProfile
from core.openai.register import registration_service
from config import register as register_config


class _Session(BrowserSession):
    proxy = ""
    device_id = "device-for-test"
    auth_session_logging_id = "logging-for-test"
    browser_profile = {
        "send_client_hints": True,
        "chrome_full_version": "146.0.4321.9",
        "sec_ch_ua_full_version_list": '"Chromium";v="146.0.4321.9"',
        "sec_ch_ua_platform_version": '"15.2.0"',
        "sec_ch_ua_arch": '"arm"', "sec_ch_ua_bitness": '"64"', "sec_ch_ua_model": '""',
    }

    def __init__(self):
        self.closed = False
        self.document_navigation_id = "synthetic-document"
        self.datadog_origin = "rum"

    def close(self):
        self.closed = True


class RegistrationPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.stack = ExitStack()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {"RUNTIME_STORAGE_BACKEND": "json"}))
        for name in (
            "_OUTLOOK_JSON", "_OUTLOOK_TXT", "_GENERIC_API_EMAIL_JSON", "_GENERIC_API_EMAIL_TXT",
            "_ICLOUD_EMAIL_JSON", "_DOMAIN_EMAIL_JSON", "_ACCOUNTS_JSON", "_ACCOUNTS_TXT",
            "_TOKENS_TXT", "_JOBS_JSON", "_VIEWER_HTML", "_EMAIL_POOL_LIFECYCLE_JSON",
            "_LEGACY_ACCOUNTS_JSON", "_LEGACY_OUTLOOK_JSON", "_LEGACY_JOBS_JSON", "_LEGACY_SQLITE",
        ):
            self.stack.enter_context(patch.object(db, name, self.root / name))
        self.stack.enter_context(patch.object(db, "_DATA_DIR", self.root))
        self.stack.enter_context(patch.object(db, "_LOG_DIR", self.root / "logs"))
        self.stack.enter_context(patch.object(register_config, "AUTO_PLAN_CHECK_AFTER_REGISTER", False))

    @contextmanager
    def _pipeline(self, otp_result, kind="new_password", step="password"):
        session = _Session()
        observed = {
            "created": False, "saved": False, "flows": [],
            # 按注入顺序抛错的 Sentinel flow：{flow: [异常, ...]}，用完即空。
            "sentinel_failures": {},
        }

        def request_sentinel(_session, flow):
            observed["flows"].append(flow)
            pending = observed["sentinel_failures"].get(flow)
            if pending:
                raise pending.pop(0)
            return {
                "token": "challenge", "_sentinel_flow": flow,
                "_sentinel_device_id": session.device_id,
                "_sentinel_header": json.dumps({
                    "p": "proof", "c": "challenge",
                    "id": session.device_id, "flow": flow,
                }),
                "_sentinel_so_header": None,
            }


        def register_user(*_args, include_high_entropy=False):
            return {"continue_url": "/api/accounts/email-otp/send"}

        def navigate_email_otp_send(*_args, include_high_entropy=False):
            return None

        def validate_email_otp(*_args, referer="", include_high_entropy=False):
            return otp_result

        def create_account(*_args, include_high_entropy=False):
            observed["created"] = True
            return {"continue_url": "https://chatgpt.com/api/auth/callback/openai"}

        original_save = registration.save_account_data

        def save_account(**kwargs):
            observed["saved"] = True
            kwargs["batch_dir"] = self.root / "batch"
            return original_save(**kwargs)

        with ExitStack() as stack:
            for name, value in {
                "_prepare_persisted_email_registration": False,
                "BrowserSession": session,
                "registration_preflight": None,
                "human_delay": None,
                "get_csrf_token": "csrf",
                "signin_openai": "https://auth.openai.com/authorize",
                "follow_authorize": "https://auth.openai.com/create-account/password",
                "get_registration_state": {"kind": kind, "step": step,
                    "page": {"type": "login_password" if kind == "existing" else "signup_password"},
                    "continue_url": "https://auth.openai.com/log-in/password" if kind == "existing"
                    else "https://auth.openai.com/create-account/password"},
                "navigate_create_account_password": None,
                "generate_registration_password": "Test-only-password!",
                "_finalize_registration_session": ({
                    "user": {"id": "test-user", "email": "user@example.com"},
                    "account": {"planType": "free"}, "expires": "2030-01-01T00:00:00Z",
                }, "test-access"),
                "get_mfa_enabled": False,
                "settle_email_on_failure": False,
            }.items():
                stack.enter_context(patch.object(registration, name, return_value=value))
            stack.enter_context(patch.object(registration, "register_user", side_effect=register_user))
            stack.enter_context(patch.object(registration, "validate_email_otp", side_effect=validate_email_otp))
            stack.enter_context(patch.object(registration, "navigate_email_otp_send", side_effect=navigate_email_otp_send))
            stack.enter_context(patch.object(registration, "request_sentinel_token", side_effect=request_sentinel))
            stack.enter_context(patch.object(registration, "create_account", side_effect=create_account))
            stack.enter_context(patch.object(registration, "save_account_data", side_effect=save_account))
            stack.enter_context(patch.object(registration._twofa_cfg, "ENABLE_2FA", False))
            # 整轮重试的轮间等待不在用例里真等；需要断言等待参数的用例自己再 patch 一次取 Mock。
            stack.enter_context(patch.object(registration, "interruptible_sleep", return_value=None))
            stack.enter_context(patch.multiple(
                registration._protocol_cfg, CHATGPT_ANON_BOOTSTRAP_ENABLED=False,
                CHATGPT_AUTH_BOOTSTRAP_ENABLED=False, SEND_SENTINEL_ON_EMAIL_OTP_VALIDATE=False,
            ))
            stack.enter_context(patch.object(codex_oauth, "run_codex_oauth", return_value={"status": "skipped"}))
            stack.enter_context(patch.object(flow_trigger, "trigger_flow", return_value={"status": "skipped"}))
            stack.enter_context(patch.object(email_provider, "resolve_email_source", return_value="manual"))
            yield session, observed

    @contextmanager
    def _rounds(self, proxies=("socks5h://exit-a:1", "socks5h://exit-b:2"), otp_result=None,
                sentinel_failures=None):
        """在 _pipeline 之上让每轮真正新建会话，并控制每轮抽到的出口。

        _pipeline 把 BrowserSession 换成固定对象；这里再换成工厂，每次构造返回新的 _Session，
        出口仍由真实的 config.proxy.pick_proxy() 抽取，只把 random.choice 换成按轮次取值。
        ``sentinel_failures`` 形如 {flow: [异常, ...]}：指定 flow 的 Sentinel 采集按顺序抛出这些异常，
        用完后恢复为成功；proof 的其余字段仍由 _pipeline 的桩按合法格式生成。
        """
        from config import proxy as proxy_config

        created = []
        # OTP 验证后直接进回调才会成功；需要走到回调之后才失败的用例自行传入。
        if otp_result is None:
            otp_result = {"continue_url": "https://chatgpt.com/api/auth/callback/openai"}
        sentinel_failures = {flow: list(errors) for flow, errors in (sentinel_failures or {}).items()}

        def build_session(proxy=None, **kwargs):
            # proxy=None 表示调用方没有指定出口，由 pick_proxy 从代理池抽；与真实构造函数一致。
            chosen = proxy_config.pick_proxy() if proxy is None else proxy
            session = _Session()
            session.proxy = chosen
            created.append(session)
            return session

        with self._pipeline(otp_result) as (_, observed), \
                patch.object(proxy_config, "PROXY_POOL", list(proxies)), \
                patch.object(proxy_config.random, "choice",
                             side_effect=lambda pool: pool[len(created) % len(pool)]), \
                patch.object(registration, "BrowserSession", side_effect=build_session) as constructor, \
                patch.object(registration, "interruptible_sleep") as sleep:
            observed["sentinel_failures"].update(sentinel_failures)
            yield SimpleNamespace(created=created, constructor=constructor, sleep=sleep,
                                  flows=observed["flows"])

    def test_round_retry_rebuilds_session_with_new_exit_and_succeeds(self):
        # 11.4：第 1 轮正式 CSRF 因传输异常失败，第 2 轮成功。
        with self._rounds() as rounds, patch.object(
            registration, "get_csrf_token",
            side_effect=[requests.ConnectionError("connection reset"), "csrf"],
        ) as csrf:
            result = self._run()

        self.assertTrue(result["success"])
        self.assertEqual(rounds.constructor.call_count, 2)
        self.assertEqual([session.proxy for session in rounds.created],
                         ["socks5h://exit-a:1", "socks5h://exit-b:2"])
        self.assertEqual(csrf.call_count, 2)
        # 第 1 轮失败后等「基础间隔 × 轮次」= 5.0 × 1。
        rounds.sleep.assert_called_once_with(5.0)
        # 成功返回的是第 2 轮的结果，不保留第 1 轮的失败类别。
        self.assertNotIn("failure_category", result["registration"])
        self.assertTrue(all(session.closed for session in rounds.created))

    def test_round_retry_gives_up_after_max_attempts_and_releases_email_once(self):
        # 11.4：三轮都是网络类失败，只在最后一轮处理一次邮箱。
        with self._rounds() as rounds, patch.object(
            registration, "get_csrf_token",
            side_effect=requests.ConnectionError("connection reset"),
        ), patch.object(registration, "settle_email_on_failure", return_value=True) as settle:
            result = self._run()

        self.assertFalse(result["success"])
        self.assertEqual(rounds.constructor.call_count, 3)
        self.assertEqual(result["failure_category"], "network")
        self.assertEqual(settle.call_count, 1)
        settle.assert_called_once()
        self.assertEqual(settle.call_args.kwargs["reusable"], True)
        self.assertEqual(rounds.sleep.call_count, 2)
        self.assertEqual([call.args[0] for call in rounds.sleep.call_args_list], [5.0, 10.0])

    def test_round_retry_stops_when_later_round_fails_with_unknown_category(self):
        # 11.4：第 1 轮网络类、第 2 轮是不带 cf-mitigated 的 403，不再重试。
        plain_forbidden = requests.HTTPError("403 Client Error: Forbidden for url: https://chatgpt.com/api/auth/csrf")
        with self._rounds() as rounds, patch.object(
            registration, "get_csrf_token",
            side_effect=[requests.ConnectionError("connection reset"), plain_forbidden],
        ), patch.object(registration, "settle_email_on_failure", return_value=True) as settle:
            result = self._run()

        self.assertFalse(result["success"])
        self.assertEqual(rounds.constructor.call_count, 2)
        self.assertEqual(result["failure_category"], "unknown")
        self.assertFalse(result["retryable_round"])
        settle.assert_called_once()
        self.assertEqual(settle.call_args.kwargs["reusable"], False)

    def test_round_retry_keeps_same_exit_when_pool_has_one_candidate(self):
        # 11.4：代理池只有一个候选时，重试照常进行，出口不变。
        with self._rounds(proxies=("socks5h://only-exit:1",)) as rounds, patch.object(
            registration, "get_csrf_token",
            side_effect=[requests.ConnectionError("connection reset"), "csrf"],
        ):
            result = self._run()

        self.assertTrue(result["success"])
        self.assertEqual(rounds.constructor.call_count, 2)
        self.assertEqual([session.proxy for session in rounds.created],
                         ["socks5h://only-exit:1", "socks5h://only-exit:1"])

    def test_registration_business_requests_use_profile_hints_through_session(self):
        expected = {
            "sec-ch-ua-full-version": _Session.browser_profile["chrome_full_version"],
            "sec-ch-ua-full-version-list": _Session.browser_profile["sec_ch_ua_full_version_list"],
            "sec-ch-ua-platform-version": _Session.browser_profile["sec_ch_ua_platform_version"],
            "sec-ch-ua-arch": _Session.browser_profile["sec_ch_ua_arch"],
            "sec-ch-ua-bitness": _Session.browser_profile["sec_ch_ua_bitness"],
            "sec-ch-ua-model": _Session.browser_profile["sec_ch_ua_model"],
        }
        from urllib.parse import urlsplit

        def response(url, payload=None, status=200, location=None):
            return SimpleNamespace(
                url=url, status_code=status, headers={"location": location} if location else {},
                json=lambda: payload or {}, text="", raise_for_status=lambda: None,
            )

        def get(url, **kwargs):
            path = urlsplit(url).path
            if path == "/api/auth/csrf":
                return response(url, {"csrfToken": "formal-csrf"})
            if path == "/oauth/authorize":
                return response(url, status=302, location="/email-verification")
            if path == "/api/accounts/email-otp/send":
                return response("https://auth.openai.com/email-verification")
            if path == "/api/auth/callback/openai":
                return response(url, status=302, location="/")
            if path == "/api/auth/session":
                return response(url, {
                    "accessToken": "test-access",
                    "user": {"id": "test-user", "email": "user@example.com"},
                    "account": {"id": "test-account", "planType": "free"},
                    "expires": "2030-01-01T00:00:00Z",
                })
            self.assertIn(path, ("/", "/email-verification"))
            return response(url)

        def post(url, **kwargs):
            path = urlsplit(url).path
            payload = {
                "/api/auth/signin/openai": {"url": "https://auth.openai.com/oauth/authorize?state=test"},
                "/api/accounts/user/register": {"continue_url": "/api/accounts/email-otp/send"},
                "/api/accounts/email-otp/validate": {"page": {"type": "about_you"}, "continue_url": "/about-you"},
                "/api/accounts/create_account": {"continue_url": "https://chatgpt.com/api/auth/callback/openai"},
            }
            return response(url, payload[path])

        real_steps = {
            "get_csrf_token": chatgpt_auth.get_csrf_token, "signin_openai": chatgpt_auth.signin_openai,
            "follow_authorize": openai_auth.follow_authorize, "register_user": openai_auth.register_user,
            "navigate_email_otp_send": openai_auth.navigate_email_otp_send,
            "validate_email_otp": openai_auth.validate_email_otp, "create_account": openai_auth.create_account,
            "_finalize_registration_session": registration._finalize_registration_session,
        }
        for kind in ("new_passwordless", "new_password"):
            with self.subTest(kind=kind), self._pipeline({}, kind=kind) as (session, _), patch.multiple(
                registration, **real_steps,
            ), patch.object(registration, "wait_for_otp", return_value="123456"), patch.object(
                session, "get", side_effect=get,
            ) as gets, patch.object(session, "post", side_effect=post) as posts:
                result = self._run()
            self.assertTrue(result["success"], msg=result)
            self.assertEqual(len(gets.call_args_list), 7)
            self.assertEqual(len(posts.call_args_list), 4)
            for call in gets.call_args_list + posts.call_args_list:
                headers = call.kwargs["headers"]
                self.assertEqual({key: headers.get(key) for key in expected}, expected)

    def test_registration_otp_recovery_keeps_high_entropy_on_resend(self):
        for include in (False, True):
            session = _Session()
            bad = SimpleNamespace(status_code=400, text="invalid otp", json=lambda: {})
            ok = SimpleNamespace(status_code=200, text="", json=lambda: {
                "continue_url": "https://chatgpt.com/api/auth/callback/openai",
            }, raise_for_status=lambda: None)
            with self.subTest(include=include), patch.object(session, "post", side_effect=[bad, ok, ok]) as post, \
                    patch.object(registration, "human_delay"), \
                    patch.object(registration, "wait_for_otp", return_value="654321"), \
                    patch.object(registration._protocol_cfg, "SEND_SENTINEL_ON_EMAIL_OTP_VALIDATE", False):
                result = registration._validate_registration_otp(
                    session, "user@example.com", 0, "123456",
                    {"page": {"type": "email_otp_verification"}}, include_high_entropy=include,
                )
            self.assertIn("continue_url", result)
            self.assertEqual([call.args[0].rsplit("/", 1)[-1] for call in post.call_args_list],
                             ["validate", "resend", "validate"])
            for call in post.call_args_list:
                headers = call.kwargs["headers"]
                self.assertEqual(headers.get("sec-ch-ua-full-version"),
                                 session.browser_profile["chrome_full_version"] if include else None)
                self.assertEqual(headers["priority"], "u=1, i")
                self.assertEqual(headers["sec-fetch-site"], "same-origin")

    def test_direct_callback_does_not_submit_profile_or_request_its_proof(self):
        with self._pipeline({
            "page": {"type": "external_url"},
            "continue_url": "https://chatgpt.com/api/auth/callback/openai",
        }) as (session, observed):
            result = registration.run_registration(
                "user@example.com", "Test User", "1995-01-01", otp_code="123456",
            )
        self.assertTrue(result["success"])
        self.assertTrue(observed["saved"])
        self.assertFalse(observed["created"])
        self.assertTrue(session.closed)
        batch = self.root / "batch"
        self.assertEqual(
            {path.name for path in batch.iterdir()},
            {"注册成功的邮箱.txt", "注册成功的token.txt", "注册成功整行.txt", "注册成功账号.json"},
        )
        self.assertEqual((batch / "注册成功的邮箱.txt").read_text(encoding="utf-8").splitlines(), ["user@example.com"])
        self.assertEqual((batch / "注册成功的token.txt").read_text(encoding="utf-8").splitlines(), ["test-access"])
        archived = json.loads((batch / "注册成功账号.json").read_text(encoding="utf-8"))[0]
        self.assertEqual(archived["email"], "user@example.com")
        self.assertEqual(archived["access_token"], "test-access")

    def test_profile_branch_submits_once_after_validation_without_an_extra_navigation(self):
        with self._pipeline({
            "page": {"type": "about_you"}, "continue_url": "/about-you",
        }) as (session, observed):
            result = registration.run_registration(
                "user@example.com", "Test User", "1995-01-01", otp_code="123456",
            )
        self.assertTrue(result["success"])
        self.assertTrue(observed["created"])
        self.assertTrue(observed["saved"])
        self.assertTrue(session.closed)

    def test_new_registration_callback_headers_do_not_change_existing_account_navigation(self):
        finalize = registration._finalize_registration_session
        session_info = {
            "accessToken": "test-access", "user": {"id": "test-user", "email": "user@example.com"},
            "account": {"id": "test-account", "planType": "free"}, "expires": "2030-01-01T00:00:00Z",
        }
        for kind in ("new_password", "existing"):
            otp_result = ({"page": {"type": "about_you"}, "continue_url": "/about-you"}
                          if kind == "new_password" else
                          {"continue_url": "https://chatgpt.com/api/auth/callback/openai"})
            with self.subTest(kind=kind), self._pipeline(
                otp_result, kind=kind, step="password" if kind == "new_password" else "otp",
            ) as (session, _), patch.object(
                registration, "_finalize_registration_session", finalize,
            ), patch.object(registration, "fetch_session", return_value=session_info) as fetch, patch.object(
                registration, "get_registration_state", return_value={
                    "kind": kind, "step": "password" if kind == "new_password" else "otp",
                    "page": {"type": "signup_password" if kind == "new_password" else "email_otp"},
                    "continue_url": "https://auth.openai.com/create-account/password" if kind == "new_password"
                    else "https://auth.openai.com/email-verification",
                },
            ), patch.object(
                session, "get", return_value=SimpleNamespace(url="https://chatgpt.com/", status_code=200, headers={}),
            ) as navigate:
                result = self._run()

            self.assertTrue(result["success"], msg=result)
            headers = navigate.call_args.kwargs["headers"]
            self.assertEqual(headers["referer"], "https://auth.openai.com/" if kind == "new_password"
                             else "https://auth.openai.com/email-verification")
            self.assertEqual(headers.get("sec-fetch-user"), None if kind == "new_password" else "?1")
            self.assertEqual(headers["sec-fetch-site"], "cross-site")
            fetch.assert_called_once_with(session, include_high_entropy=kind != "existing")
            saved = db.get_account_by_email("user@example.com")
            self.assertEqual(saved["access_token"], session_info["accessToken"])

    def test_sdk_failure_preserves_its_stage_and_prevents_password_submission(self):
        from core.openai.auth.sentinel_runner import SentinelSDKError
        with self._pipeline({}) as (session, observed), patch.object(
            registration, "request_sentinel_token",
            side_effect=SentinelSDKError("SDK collection failed", "sdk_exec_failed", "requirements"),
        ), patch.object(registration, "register_user") as submit:
            result = registration.run_registration(
                "user@example.com", "Test User", "1995-01-01", otp_code="123456",
            )
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "sdk_exec_failed")
        self.assertEqual(result["error_stage"], "requirements")
        submit.assert_not_called()
        self.assertFalse(observed["saved"])
        self.assertTrue(session.closed)

    def test_constructor_failure_reaches_task_error_without_attempting_authentication(self):
        class InitializationFailure(RuntimeError):
            category = "geo_incomplete"
            stage = "validate"

        with patch.object(registration, "_prepare_persisted_email_registration", return_value=False), patch.object(
            registration, "BrowserSession", side_effect=InitializationFailure("地域信息不完整")
        ), patch.object(registration, "settle_email_on_failure", return_value=False), patch.object(
            registration, "signin_openai", side_effect=AssertionError("must not authenticate")
        ):
            result = registration.run_registration("user@example.com", "Test User", "1995-01-01")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "geo_incomplete")
        self.assertEqual(result["error_stage"], "validate")

    def test_unknown_landing_sentinel_probe_failure_fails_without_guessing_account_type(self):
        from core.openai.auth.openai_auth import SentinelRequestError

        error = SentinelRequestError("Sentinel challenge 请求失败（Timeout，HTTP=None）", "sentinel_network_failed")
        with self._pipeline({
            "page": {"type": "external_url"},
            "continue_url": "https://chatgpt.com/api/auth/callback/openai",
        }, kind="unknown") as (session, observed), \
                patch.object(registration, "probe_registration_state", side_effect=error) as probe:
            result = self._run()

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "sentinel_network_failed")
        self.assertEqual(probe.call_count, registration._protocol_cfg.OPENAI_REGISTRATION_ROUND_MAX_ATTEMPTS)
        # 探测阶段的 Sentinel 失败发生在 user/register 之前，按 D12 每轮都重来；三轮都不猜账号类型。
        probe.assert_called_with(
            session, "user@example.com",
            referer="https://auth.openai.com/", screen_hint="signup", include_high_entropy=True,
        )
        self.assertEqual(result["failure_category"], "network")
        self.assertEqual(result["error_stage"], "sentinel_challenge")
        self.assertEqual(result["registration"]["account_kind"], "unknown")
        self.assertEqual(observed["flows"], [])
        self.assertFalse(observed["saved"])
        self.assertTrue(session.closed)

    def test_email_otp_sentinel_failure_stops_before_validation(self):
        from core.openai.auth.openai_auth import SentinelRequestError

        error = SentinelRequestError("Sentinel challenge 请求失败（Timeout，HTTP=None）", "sentinel_network_failed")
        with self._pipeline({
            "page": {"type": "external_url"},
            "continue_url": "https://chatgpt.com/api/auth/callback/openai",
        }) as (session, observed):
            def fail_sentinel(current_session, flow):
                observed["flows"].append(flow)
                if flow == "email_otp_validate":
                    raise error
                return {
                    "token": "challenge", "_sentinel_flow": flow,
                    "_sentinel_device_id": current_session.device_id,
                    "_sentinel_header": json.dumps({
                        "p": "proof", "c": "challenge",
                        "id": current_session.device_id, "flow": flow,
                    }),
                    "_sentinel_so_header": None,
                }

            with patch.object(registration._protocol_cfg, "SEND_SENTINEL_ON_EMAIL_OTP_VALIDATE", True), \
                    patch.object(registration, "request_sentinel_token", side_effect=fail_sentinel), \
                    patch.object(registration, "validate_email_otp") as validate:
                result = self._run()

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "sentinel_network_failed")
        self.assertEqual(result["error_stage"], "sentinel_challenge")
        self.assertEqual(observed["flows"], ["username_password_create", "email_otp_validate"])
        validate.assert_not_called()
        self.assertFalse(observed["saved"])
        self.assertTrue(session.closed)

    def test_profile_sentinel_failure_stops_before_create_account(self):
        from core.openai.auth.openai_auth import SentinelRequestError

        error = SentinelRequestError("Sentinel challenge 响应缺少有效 token", "sentinel_challenge_failed")
        otp_result = {
            "page": {"type": "about_you"},
            "continue_url": "https://auth.openai.com/about-you",
        }

        def request_sentinel(current_session, flow):
            observed["flows"].append(flow)
            if flow == "oauth_create_account":
                raise error
            return {
                "token": "challenge", "_sentinel_flow": flow,
                "_sentinel_device_id": current_session.device_id,
                "_sentinel_header": json.dumps({
                    "p": "proof", "c": "challenge",
                    "id": current_session.device_id, "flow": flow,
                }),
                "_sentinel_so_header": None,
            }

        with self._pipeline(otp_result) as (session, observed), \
                patch.object(registration, "request_sentinel_token", side_effect=request_sentinel), \
                patch.object(registration, "create_account") as create:
            result = self._run()

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "sentinel_challenge_failed")
        self.assertEqual(result["error_stage"], "sentinel_challenge")
        self.assertEqual(observed["flows"], ["username_password_create", "oauth_create_account"])
        create.assert_not_called()
        self.assertFalse(observed["saved"])
        self.assertTrue(session.closed)

    def test_sentinel_failure_task_log_has_safe_reason_stage_and_category(self):
        from core.openai.auth.openai_auth import SentinelRequestError

        secrets = (
            "sentinel-token-fixture", "sentinel-so-fixture", "challenge-fixture",
            "cookie-fixture", "otp-fixture", "device-fixture",
            "proxy-user-fixture:proxy-pass-fixture@proxy.example",
        )
        error = SentinelRequestError(
            "sensitive " + " ".join(secrets), "sentinel_network_failed",
        )
        job = db.create_job(email_source="manual", email="user@example.com")

        with self._pipeline({}, kind="unknown"), \
                patch.object(registration_service, "_prepare_registration_args",
                    return_value=("user@example.com", "Test User", "1995-01-01")), \
                patch.object(registration, "probe_registration_state", side_effect=error):
            registration_service._run_one_job(job["id"], job["log_file"])

        stored_job = db.get_job(job["id"])
        task_log = registration_service.read_job_log(job["id"])
        self.assertEqual(stored_job["status"], "failed")
        self.assertEqual(stored_job["error_code"], "sentinel_network_failed")
        self.assertEqual(stored_job["error_stage"], "sentinel_challenge")
        self.assertIn("Sentinel flow 执行失败", stored_job["error_message"])
        self.assertIn("stage=sentinel_challenge", task_log)
        self.assertIn("error_code=sentinel_network_failed", task_log)
        self.assertIn("reason=Sentinel flow 执行失败", task_log)
        for secret in secrets:
            self.assertNotIn(secret, task_log)
            self.assertNotIn(secret, stored_job["error_message"])

    def test_existing_fallback_sends_otp_before_validation(self):
        events = []

        def send_otp(*_args, **_kwargs):
            events.append("send")

        def validate(*_args, **_kwargs):
            self.assertEqual(events, ["send"])
            return {
                "page": {"type": "external_url"},
                "continue_url": "https://chatgpt.com/api/auth/callback/openai",
            }

        with self._pipeline({}, kind="unknown") as (session, _), \
                patch.object(registration, "probe_registration_state", return_value={
                    "kind": "existing", "step": "otp", "page": {"type": "unknown_page"},
                    "continue_url": "", "classification_source": "authorize_continue",
                    "otp_delivery_required": True,
                }) as probe, \
                patch.object(registration, "send_email_otp", side_effect=send_otp) as send, \
                patch.object(registration, "validate_email_otp", side_effect=validate) as validate_otp, \
                patch.object(registration, "register_user") as register:
            result = self._run()

        self.assertTrue(result["success"])
        probe.assert_called_once_with(
            session, "user@example.com",
            referer="https://auth.openai.com/", screen_hint="signup", include_high_entropy=True,
        )
        send.assert_called_once_with(session, referer="https://auth.openai.com/create-account/password")
        validate_otp.assert_called_once()
        register.assert_not_called()
        self.assertTrue(session.closed)

    def _run(self):
        return registration.run_registration(
            "user@example.com", "Test User", "1995-01-01", otp_code="123456",
        )

    def test_password_is_logged_before_submission_and_survives_later_otp_failure(self):
        def submit(*args, **_kwargs):
            self.assertIn("Test-only-password!", "\n".join(captured.output))
            return {"continue_url": "/api/accounts/email-otp/send"}

        def send(*args, include_high_entropy=False):
            credentials = db.get_account_credentials("user@example.com")
            self.assertEqual(credentials["registration_password"], "Test-only-password!")
            self.assertFalse(credentials["complete"])
            self.assertEqual(db.count_accounts(), 0)
            raise TimeoutError("OTP delivery failed")

        with self._pipeline({}) as (session, _), self.assertLogs(registration.logger, level="INFO") as captured, \
                patch.object(registration, "register_user", side_effect=submit), \
                patch.object(registration, "navigate_email_otp_send", side_effect=send):
            result = self._run()
        self.assertFalse(result["success"])
        self.assertTrue(result["registration"]["password_effective"])
        self.assertEqual(result["registration"]["credential_persistence"], "saved")
        self.assertEqual(result["error_stage"], "otp")
        self.assertFalse(result["registration"]["authenticated"])
        self.assertEqual(db.get_account_credentials("user@example.com")["account_id"], result["account_id"])
        self.assertTrue(session.closed)

    def test_password_persistence_failure_stops_all_later_authentication(self):
        with self._pipeline({}) as (session, _), \
                patch.object(db, "save_registration_password", side_effect=OSError("disk full")), \
                patch.object(registration, "navigate_email_otp_send") as send, \
                patch.object(registration, "validate_email_otp") as validate, \
                patch.object(registration, "setup_2fa") as enroll:
            result = self._run()
        self.assertEqual(result["error_stage"], "credential_persistence")
        self.assertTrue(result["registration"]["password_effective"])
        self.assertEqual(result["registration"]["credential_persistence"], "failed")
        self.assertFalse(result["registration"]["authenticated"])
        send.assert_not_called()
        validate.assert_not_called()
        enroll.assert_not_called()
        self.assertTrue(session.closed)

    def test_passwordless_uncertain_submission_does_not_save_or_fall_back(self):
        with self._pipeline({}, kind="new_passwordless") as (session, _), \
                patch.object(registration, "register_user", side_effect=TimeoutError("connection lost")), \
                patch.object(registration, "request_login_otp") as fallback:
            result = self._run()
        self.assertFalse(result["success"])
        self.assertEqual(result["registration"]["password_state"], "submission_unknown")
        self.assertFalse(result["registration"]["password_effective"])
        self.assertIsNone(db.get_account_by_email("user@example.com"))
        fallback.assert_not_called()
        self.assertTrue(session.closed)

    def test_existing_probe_result_never_submits_password_or_profile(self):
        probe_state = {
            "kind": "existing", "step": "otp", "page": {"type": "email_otp_verification"},
            "continue_url": "https://auth.openai.com/email-verification",
            "classification_source": "authorize_continue", "otp_delivery_required": False,
        }
        with self._pipeline({
            "continue_url": "https://chatgpt.com/api/auth/callback/openai",
        }, kind="unknown", step="unknown") as (session, _), \
                patch.object(registration, "probe_registration_state", return_value=probe_state) as probe, \
                patch.object(registration, "register_user") as password, \
                patch.object(registration, "create_account") as profile:
            result = self._run()
        self.assertTrue(result["success"])
        probe.assert_called_once_with(
            session, "user@example.com",
            referer="https://auth.openai.com/", screen_hint="signup", include_high_entropy=True,
        )
        password.assert_not_called()
        profile.assert_not_called()
        self.assertTrue(session.closed)

    def test_existing_password_login_preserves_original_identity_and_credentials(self):
        original_id = db.insert_account(email="user@example.com", access_token="old-token",
            totp_secret="JBSWY3DPEHPK3PXP", extra={"registration_password": "stored-password", "note": "keep"})
        with self._pipeline({}, kind="existing") as (session, _), \
                patch.object(registration, "verify_login_password", return_value={
                    "continue_url": "https://chatgpt.com/api/auth/callback/openai"}) as verify, \
                patch.object(registration, "register_user") as create:
            result = self._run()
        self.assertTrue(result["success"])
        self.assertEqual(result["account_id"], original_id)
        self.assertEqual(result["registration"]["password_state"], "verified")
        verify.assert_called_once_with(session, "stored-password", header_profile=HeaderProfile.STANDARD)
        row = db.get_account_by_email("user@example.com")
        self.assertEqual(row["access_token"], "test-access")
        self.assertEqual(json.loads(row["extra_json"])["note"], "keep")
        credentials = db.get_account_credentials("user@example.com")
        self.assertEqual(credentials["registration_password"], "stored-password")
        self.assertEqual(credentials["totp_secret"], "JBSWY3DPEHPK3PXP")
        create.assert_not_called()
        self.assertTrue(session.closed)


    def test_existing_password_deactivated_error_skips_otp_and_preserves_account(self):
        original_id = db.insert_account(
            email="user@example.com", access_token="old-token",
            extra={"registration_password": "stored-password"},
        )
        error = registration.AccountUnusableError(
            "account_deactivated", error_code="account_deactivated",
        )
        with self._pipeline({}, kind="existing") as (session, _), \
                patch.object(registration, "verify_login_password", side_effect=error) as verify, \
                patch.object(registration, "request_login_otp") as otp, \
                patch.object(registration, "_finalize_registration_session") as callback, \
                patch.object(registration, "settle_email_on_failure") as release:
            result = self._run()

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "account_deactivated")
        self.assertEqual(result["error_stage"], "password")
        verify.assert_called_once_with(session, "stored-password", header_profile=HeaderProfile.STANDARD)
        otp.assert_not_called()
        callback.assert_not_called()
        release.assert_not_called()
        self.assertEqual(db.get_account_by_email("user@example.com")["id"], original_id)
        self.assertEqual(db.get_account_by_email("user@example.com")["access_token"], "old-token")

    def test_existing_mfa_deactivated_error_skips_callback_and_preserves_account(self):
        original_id = db.insert_account(
            email="user@example.com", access_token="old-token", totp_secret="secret-placeholder",
            extra={"registration_password": "stored-password"},
        )
        error = registration.AccountUnusableError(
            "account_deactivated", error_code="account_deactivated",
        )
        mfa_state = {
            "page": {"type": "mfa_challenge", "payload": {"factor_id": "factor-placeholder"}},
            "continue_url": "https://auth.openai.com/mfa-challenge/factor-placeholder",
        }
        with self._pipeline({}, kind="existing") as (session, _), \
                patch.object(registration, "verify_login_password", return_value=mfa_state), \
                patch.object(registration, "verify_login_totp", side_effect=error) as verify, \
                patch.object(registration, "request_login_otp") as otp, \
                patch.object(registration, "_finalize_registration_session") as callback, \
                patch.object(registration, "settle_email_on_failure") as release:
            result = self._run()

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "account_deactivated")
        self.assertEqual(result["error_stage"], "totp")
        verify.assert_called_once_with(
            session, mfa_state, "secret-placeholder", header_profile=HeaderProfile.STANDARD,
        )
        otp.assert_not_called()
        callback.assert_not_called()
        release.assert_not_called()
        self.assertEqual(db.get_account_by_email("user@example.com")["id"], original_id)
        self.assertEqual(db.get_account_by_email("user@example.com")["access_token"], "old-token")
    def test_existing_otp_can_create_local_record_without_inventing_password(self):
        with self._pipeline({"continue_url": "https://chatgpt.com/api/auth/callback/openai"},
                kind="existing", step="otp") as (session, _), \
                patch.object(registration, "get_registration_state", return_value={
                    "kind": "existing", "step": "otp", "page": {"type": "email_otp"},
                    "continue_url": "https://auth.openai.com/email-verification"}), \
                patch.object(registration, "verify_login_password") as password:
            result = self._run()
        self.assertTrue(result["success"])
        self.assertTrue(result["registration"]["authenticated"])
        self.assertFalse(db.get_account_credentials("user@example.com")["registration_password"])
        password.assert_not_called()
        self.assertTrue(session.closed)

    def test_existing_profile_transition_is_rejected_without_modification(self):
        db.insert_account(email="user@example.com", access_token="old-token",
            extra={"registration_password": "stored-password"})
        with self._pipeline({}, kind="existing") as (session, _), \
                patch.object(registration, "verify_login_password", return_value={
                    "page": {"type": "about_you"}, "continue_url": "/about-you"}), \
                patch.object(registration, "create_account") as create, \
                patch.object(registration, "settle_email_on_failure") as release:
            result = self._run()
        self.assertEqual(result["error_code"], "existing_profile_unexpected")
        self.assertEqual(db.get_account_by_email("user@example.com")["access_token"], "old-token")
        create.assert_not_called()
        release.assert_not_called()
        self.assertTrue(session.closed)

    def test_callback_failure_is_not_reported_as_authenticated(self):
        with self._pipeline({"continue_url": "https://chatgpt.com/api/auth/callback/openai"}) as (_, _), \
                patch.object(registration, "_finalize_registration_session", side_effect=TimeoutError("no session")):
            result = self._run()
        self.assertFalse(result["success"])
        self.assertFalse(result["registration"]["authenticated"])
        self.assertEqual(result["registration"]["credential_persistence"], "saved")
        self.assertFalse(db.get_account_credentials("user@example.com")["complete"])

    def test_codex_failure_does_not_affect_saved_registration(self):
        with self._pipeline({"continue_url": "https://chatgpt.com/api/auth/callback/openai"}), \
                patch.object(codex_oauth, "run_codex_oauth", return_value={"ok": False, "status": "failed"}) as codex:
            result = self._run()
        self.assertTrue(result["success"])
        self.assertTrue(result["registration"]["authenticated"])
        self.assertTrue(result["registration"]["account_saved"])
        self.assertEqual(result["codex"]["status"], "not_started")
        codex.assert_not_called()
        self.assertTrue(db.get_account_credentials("user@example.com")["complete"])

    def test_twofa_only_enrolls_when_server_disabled_and_config_enabled(self):
        def enroll_inline(_session, _token, *, on_pending_secret):
            secret = "JBSWY3DPEHPK3PXP"
            on_pending_secret(secret)
            return secret

        for server_state, enabled, expected in (
            (True, True, "failed"), (False, False, "disabled_by_config"),
            (False, True, "enabled"), (None, True, "enabled"),
        ):
            with self.subTest(server_state=server_state, config=enabled), \
                    self._pipeline({"continue_url": "https://chatgpt.com/api/auth/callback/openai"}), \
                    patch.object(registration._twofa_cfg, "ENABLE_2FA", enabled), \
                    patch.object(registration, "get_mfa_enabled", return_value=server_state,
                        side_effect=RuntimeError("unknown state") if server_state is None else None) as mfa_query, \
                    patch.object(registration, "setup_2fa_inline", side_effect=enroll_inline) as enroll:
                result = self._run()
            if server_state is True:
                self.assertFalse(result["success"])
            else:
                self.assertTrue(result["success"])
            self.assertEqual(result["registration"]["twofa_status"], expected)
            if enabled:
                mfa_query.assert_called_once()
            else:
                mfa_query.assert_not_called()
    def test_existing_login_failure_or_stop_does_not_poison_claimed_email(self):
        for outcome in ("failure", "stop", "exception"):
            with self.subTest(outcome=outcome):
                address = f"{outcome}@example.com"
                db.import_outlook_accounts([{"email": address, "password": "mail-password",
                    "client_id": "client", "refresh_token": "refresh"}])
                job = db.create_job(email_source="outlook", email=address)
                db.claim_next_outlook(job_id=job["id"])

                def fail_login(*args, **kwargs):
                    if outcome == "stop":
                        job_context.request_stop(job["id"])
                        raise job_context.StopRequested("stopped during existing login")
                    raise TimeoutError("/log-in/password OTP timeout")

                def raise_after_classification(*args, **kwargs):
                    job_context.record_progress({"account_kind": "existing"})
                    raise RuntimeError("/log-in/password authentication failed")

                with self._pipeline({}, kind="existing", step="otp"), \
                        patch.object(registration, "get_registration_state", return_value={
                            "kind": "existing", "step": "otp", "page": {"type": "email_otp"},
                            "continue_url": "https://auth.openai.com/email-verification"}), \
                        patch.object(registration_service, "_prepare_registration_args",
                            return_value=(address, "Test User", "1995-01-01")), \
                        patch.object(registration, "wait_for_otp", return_value="123456"), \
                        patch.object(registration._email_cfg, "USE_EMAIL_SERVICE", True), \
                        patch.object(registration, "validate_email_otp", side_effect=fail_login), \
                        patch.object(registration, "settle_email_on_failure",
                            wraps=email_provider.settle_email_on_failure) as settle_in_flow, \
                        patch.object(email_provider, "settle_email_on_failure",
                            wraps=email_provider.settle_email_on_failure) as settle_in_provider:
                    if outcome == "exception":
                        with patch.object(registration_service, "run_registration", side_effect=raise_after_classification):
                            registration_service._run_one_job(job["id"], str(self.root / f"{outcome}.log"))
                    else:
                        registration_service._run_one_job(job["id"], str(self.root / f"{outcome}.log"))
                stored_job = db.get_job(job["id"])
                self.assertEqual(stored_job["status"], "stopped" if outcome == "stop" else "failed")
                self.assertEqual(stored_job["registration"]["account_kind"], "existing")
                self.assertEqual(db.get_outlook_by_email(address)["status"], "registering")
                self.assertIsNone(db.get_account_by_email(address))
                # 两处都未调用：registration_flow 是单轮内部的收口，email_provider 是任务层
                # _release_unconsumed_job_email 实际导入的那一个。
                settle_in_flow.assert_not_called()
                settle_in_provider.assert_not_called()

    def test_passwordless_rejection_can_finish_without_saving_candidate(self):
        rejection = registration.PasswordRegistrationRejectedError("password setting rejected", {
            "page": {"type": "email_otp"}, "continue_url": "/email-verification",
        })
        with self._pipeline({"continue_url": "https://chatgpt.com/api/auth/callback/openai"},
                kind="new_passwordless"), \
                patch.object(registration, "register_user", side_effect=rejection), \
                patch.object(registration, "request_login_otp", return_value=rejection.result):
            result = self._run()
        self.assertTrue(result["success"])
        self.assertEqual(result["registration"]["password_state"], "rejected")
        self.assertFalse(result["registration"]["password_effective"])
        self.assertFalse(db.get_account_credentials("user@example.com")["registration_password"])

    def test_passwordless_success_discards_code_from_previous_challenge(self):
        with self._pipeline({"continue_url": "https://chatgpt.com/api/auth/callback/openai"},
                kind="new_passwordless"), \
                patch.object(registration._email_cfg, "USE_EMAIL_SERVICE", True), \
                patch.object(registration, "wait_for_otp", return_value="678901"), \
                patch.object(registration, "validate_email_otp", return_value={
                    "continue_url": "https://chatgpt.com/api/auth/callback/openai"}) as validate:
            result = self._run()
        self.assertTrue(result["success"])
        self.assertEqual(validate.call_args.args[1], "678901")
        self.assertEqual(db.get_account_credentials("user@example.com")["registration_password"],
            "Test-only-password!")


    def test_registration_uses_inline_totp_and_promotes_persisted_secret(self):
        def bind_inline(session, token, *, on_pending_secret):
            self.assertEqual(session.device_id, "device-for-test")
            self.assertEqual(token, "test-access")
            on_pending_secret("fast-secret")
            return "fast-secret"

        with self._pipeline({"continue_url": "https://chatgpt.com/api/auth/callback/openai"}) as (session, _), \
                patch.object(registration._twofa_cfg, "ENABLE_2FA", True), \
                patch.object(registration, "setup_2fa_inline", side_effect=bind_inline) as fast:
            result = self._run()
        self.assertTrue(result["success"])
        fast.assert_called_once()
        credentials = db.get_account_credentials("user@example.com")
        self.assertEqual(credentials["totp_secret"], "fast-secret")
        self.assertIsNone(credentials["pending_tfa_secret"])
        self.assertTrue(session.closed)

    def test_registration_totp_fallback_uses_independent_session_and_same_proxy(self):
        reauth_session = _Session()
        reauth_session.proxy = "http://proxy.example:8080"

        def bind_slow(session, email, *, on_pending_secret, **_kwargs):
            self.assertIs(session, reauth_session)
            self.assertEqual(email, "user@example.com")
            on_pending_secret("slow-secret")
            return "slow-secret"

        with self._pipeline({"continue_url": "https://chatgpt.com/api/auth/callback/openai"}) as (session, _), \
                patch.object(registration._twofa_cfg, "ENABLE_2FA", True), \
                patch.object(registration, "BrowserSession", side_effect=[session, reauth_session]) as session_factory, \
                patch.object(registration, "setup_2fa_inline", side_effect=RuntimeError("fast path rejected")), \
                patch.object(registration, "setup_2fa", side_effect=bind_slow):
            session.proxy = reauth_session.proxy
            result = self._run()
        self.assertTrue(result["success"])
        self.assertEqual(session_factory.call_args_list[1].kwargs["proxy"], reauth_session.proxy)
        self.assertTrue(reauth_session.closed)
        credentials = db.get_account_credentials("user@example.com")
        self.assertEqual(credentials["totp_secret"], "slow-secret")
        self.assertIsNone(credentials["pending_tfa_secret"])
    def test_pending_secret_persistence_failure_stops_before_slow_path(self):
        session = _Session()
        with patch.object(
            registration, "setup_2fa_inline",
            side_effect=lambda _session, _token, *, on_pending_secret: on_pending_secret("pending-secret"),
        ), patch.object(
            db, "save_pending_totp_secret", side_effect=db.CredentialPersistenceError("disk full"),
        ), patch.object(registration, "setup_2fa") as slow_path, patch.object(
            registration, "BrowserSession",
        ) as session_factory:
            with self.assertRaises(db.CredentialPersistenceError):
                registration._setup_registration_totp(
                    session, "user@example.com", "access-token", "registration-password",
                )
        slow_path.assert_not_called()
        session_factory.assert_not_called()
    def test_activation_failure_recovers_pending_secret_without_reenrolling(self):
        session = _Session()
        with patch.object(
            registration, "setup_2fa_inline",
            side_effect=lambda _session, _token, *, on_pending_secret: (
                on_pending_secret("pending-after-timeout") or (_ for _ in ()).throw(TimeoutError("activation response lost"))
            ),
        ), patch.object(
            registration, "setup_2fa",
            side_effect=lambda _session, _email, *, on_already_enabled, **_kwargs: on_already_enabled(),
        ) as slow_path, patch.object(
            registration, "BrowserSession", return_value=_Session(),
        ):
            secret = registration._setup_registration_totp(
                session, "recovery@example.test", "access-token", "login-password",
            )
        self.assertEqual(secret, "pending-after-timeout")
        slow_path.assert_called_once()
        credentials = db.get_account_credentials("recovery@example.test")
        self.assertEqual(credentials["totp_secret"], "pending-after-timeout")
        self.assertIsNone(credentials["pending_tfa_secret"])
    def test_totp_slow_path_without_openai_password_fails_explicitly(self):
        session = _Session()
        with patch.object(
            registration, "setup_2fa_inline", side_effect=RuntimeError("recent_auth_required"),
        ), patch.object(registration, "setup_2fa") as slow_path, patch.object(
            registration, "BrowserSession",
        ) as session_factory:
            with self.assertRaisesRegex(RuntimeError, "需要已保存的 OpenAI 登录密码"):
                registration._setup_registration_totp(session, "user@example.test", "token", None)
        slow_path.assert_not_called()
        session_factory.assert_not_called()


    def test_rejected_password_is_not_retried_or_overwritten_after_otp_login(self):
        account_id = db.insert_account(email="user@example.com", access_token="old-token",
            extra={"registration_password": "rejected-old-password"})
        rejection = registration.PasswordRejectedError("incorrect password", {
            "page": {"type": "email_otp"}, "continue_url": "/email-verification",
        })
        with self._pipeline({"continue_url": "https://chatgpt.com/api/auth/callback/openai"}, kind="existing"), \
                patch.object(registration, "verify_login_password", side_effect=rejection) as password, \
                patch.object(registration, "request_login_otp", return_value=rejection.result):
            result = self._run()
        self.assertTrue(result["success"])
        self.assertEqual(result["account_id"], account_id)
        self.assertEqual(result["registration"]["password_state"], "rejected")
        self.assertFalse(result["registration"]["password_effective"])
        self.assertEqual(password.call_count, 1)
        self.assertEqual(db.get_account_credentials("user@example.com")["registration_password"],
            "rejected-old-password")

    def test_otp_to_totp_requires_saved_factor_secret_before_final_session(self):
        factor = {"page": {"type": "mfa_challenge", "payload": {"factor_id": "factor-test"}}}
        for has_secret in (False, True):
            with self.subTest(has_secret=has_secret):
                if has_secret:
                    db.insert_account(email="user@example.com", access_token="old-token",
                        totp_secret="JBSWY3DPEHPK3PXP")
                issued = []

                def post(url, **kwargs):
                    payload = json.loads(kwargs["data"])
                    self.assertEqual(payload["id"], "factor-test")
                    if url.endswith("/issue_challenge"):
                        issued.append("challenge")
                        reply = {}
                    else:
                        self.assertEqual(issued, ["challenge"])
                        self.assertEqual(payload["code"], "678901")
                        issued.append("verified")
                        reply = {"continue_url": "https://chatgpt.com/api/auth/callback/openai"}
                    return SimpleNamespace(
                        status_code=200, json=lambda: reply, raise_for_status=lambda: None,
                    )

                sentinel_flows = []

                def sentinel_response(current_session, flow, *, header_profile=HeaderProfile.STANDARD):
                    self.assertIs(header_profile, HeaderProfile.STANDARD)
                    sentinel_flows.append(flow)
                    return {
                        "token": "sentinel-challenge",
                        "so": {"required": False},
                        "_sentinel_flow": flow,
                        "_sentinel_device_id": current_session.device_id,
                        "_sentinel_header": json.dumps({
                            "p": "sentinel-proof", "c": "sentinel-challenge",
                            "id": current_session.device_id, "flow": flow,
                        }),
                        "_sentinel_so_header": None,
                    }

                with self._pipeline(factor, kind="existing", step="otp") as (session, _), \
                        patch.object(registration, "get_registration_state", return_value={
                            "kind": "existing", "page": {"type": "email_otp_verification"},
                            "continue_url": "https://auth.openai.com/email-verification"}), \
                        patch.object(session, "get_auth_headers", create=True, return_value={}), \
                        patch.object(session, "post", create=True, side_effect=post), \
                        patch("core.openai.auth.openai_auth.request_sentinel_token", side_effect=sentinel_response), \
                        patch("core.openai.auth.openai_auth._totp_code", return_value="678901"):
                    result = self._run()
                self.assertEqual(result["success"], has_secret)
                self.assertEqual(result["registration"]["authenticated"], has_secret)
                self.assertEqual(sentinel_flows, ["verify_mfa"] if has_secret else [])
                self.assertTrue(session.closed)

    def test_write_exception_with_confirmed_readback_does_not_lose_password(self):
        original_save = db.save_registration_password

        def partial_write(*args, **kwargs):
            original_save(*args, **kwargs)
            raise OSError("failure after authoritative commit")

        with self._pipeline({"continue_url": "https://chatgpt.com/api/auth/callback/openai"}), \
                patch.object(db, "save_registration_password", side_effect=partial_write):
            result = self._run()
        self.assertTrue(result["success"])
        self.assertEqual(result["registration"]["credential_persistence"], "saved")
        self.assertEqual(db.get_account_credentials("user@example.com")["registration_password"],
            "Test-only-password!")

    def test_unreadable_partial_write_stops_without_claiming_not_saved(self):
        original_save = db.save_registration_password
        original_read = db.get_account_credentials
        committed = False

        def partial_write(*args, **kwargs):
            nonlocal committed
            original_save(*args, **kwargs)
            committed = True
            raise OSError("failure after commit")

        def read_credentials(email):
            if committed:
                raise OSError("readback unavailable")
            return original_read(email)

        with self._pipeline({}) as (session, _), \
                patch.object(db, "save_registration_password", side_effect=partial_write), \
                patch.object(db, "get_account_credentials", side_effect=read_credentials), \
                patch.object(registration, "navigate_email_otp_send") as send:
            result = self._run()
        self.assertEqual(result["error_stage"], "credential_persistence")
        self.assertTrue(result["registration"]["password_effective"])
        self.assertEqual(result["registration"]["credential_persistence"], "unknown")
        self.assertFalse(db.get_account_credentials("user@example.com")["complete"])
        send.assert_not_called()
        self.assertTrue(session.closed)

    # ---- 整轮重试的边界（D12）：哪些失败不重试，邮箱各怎么处理 ----

    def _assert_no_retry(self, rounds, settle, category, reusable, constructed=1):
        self.assertFalse(self.result["success"])
        self.assertEqual(rounds.constructor.call_count, constructed)
        self.assertEqual(rounds.sleep.call_count, 0)
        self.assertEqual(self.result["failure_category"], category)
        self.assertFalse(self.result["retryable_round"])
        settle.assert_called_once()
        self.assertEqual(settle.call_args.kwargs["reusable"], reusable)

    def test_no_round_retry_after_register_user_transport_failure(self):
        # 11.5：user/register 已发出、结果未知（password_state == submission_unknown），不能重来。
        with self._rounds() as rounds, patch.object(
            registration, "register_user",
            side_effect=requests.ConnectionError("connection reset"),
        ), patch.object(registration, "settle_email_on_failure", return_value=True) as settle:
            self.result = self._run()
        self._assert_no_retry(rounds, settle, "network", True)
        self.assertEqual(self.result["registration"]["password_state"], "submission_unknown")

    def test_no_round_retry_after_register_user_plain_http_error(self):
        # 11.5：同一位置的 HTTPError 不带 cf-mitigated，类别 unknown。
        with self._rounds() as rounds, patch.object(
            registration, "register_user",
            side_effect=requests.HTTPError("500 Server Error for url: https://auth.openai.com/api/accounts/user/register"),
        ), patch.object(registration, "settle_email_on_failure", return_value=True) as settle:
            self.result = self._run()
        self._assert_no_retry(rounds, settle, "unknown", False)
        self.assertEqual(self.result["registration"]["password_state"], "submission_unknown")

    def test_no_round_retry_for_existing_account_network_failure(self):
        # 11.5：已有账号登录失败只影响任务，邮箱不动。
        # 本地存有登录密码才会走 verify_login_password；没有密码会改走 OTP，量不到这里要的失败。
        db.insert_account(email="user@example.com", access_token="old-token",
            extra={"registration_password": "stored-password"})
        with self._rounds() as rounds, patch.object(
            registration, "get_registration_state",
            return_value={"kind": "existing", "step": "password",
                "page": {"type": "login_password"},
                "continue_url": "https://auth.openai.com/log-in/password"},
        ), patch.object(
            registration, "verify_login_password",
            side_effect=requests.ConnectionError("connection reset"),
        ) as verify, patch.object(
            registration, "settle_email_on_failure", return_value=True,
        ) as settle:
            self.result = self._run()
        self.assertFalse(self.result["success"])
        verify.assert_called_once()
        self.assertEqual(rounds.constructor.call_count, 1)
        self.assertEqual(rounds.sleep.call_count, 0)
        self.assertEqual(self.result["failure_category"], "network")
        self.assertEqual(self.result["registration"]["account_kind"], "existing")
        self.assertFalse(self.result["retryable_round"])
        settle.assert_not_called()

    def test_no_round_retry_for_registration_failure(self):
        # 11.5：_RegistrationFailure 是流程自己判定的终止，类别 unknown。
        with self._rounds() as rounds, patch.object(
            registration, "get_registration_state",
            return_value={"kind": "conflicting", "step": "password"},
        ), patch.object(registration, "settle_email_on_failure", return_value=True) as settle:
            self.result = self._run()
        self._assert_no_retry(rounds, settle, "unknown", False)
        self.assertEqual(self.result["error_code"], "account_state_unknown")

    def test_no_round_retry_when_user_stops_inside_round(self):
        # 11.5：StopRequested 落进同一个 except，分类得 unknown，邮箱按失败处理。
        with self._rounds() as rounds, patch.object(
            registration, "get_csrf_token", side_effect=job_context.StopRequested("stopped by user"),
        ), patch.object(registration, "settle_email_on_failure", return_value=True) as settle:
            self.result = self._run()
        self._assert_no_retry(rounds, settle, "unknown", False)

    def test_cf_challenge_403_retries_in_session_before_switching_round(self):
        # 11.6：正式 CSRF 的同会话重试先于换轮。第 1 轮 3 次都是 CF 403，第 2 轮成功。
        challenge = requests.Response()
        challenge.status_code = 403
        challenge.headers["cf-mitigated"] = "challenge"
        challenge._content = b""
        challenge.url = "https://chatgpt.com/api/auth/csrf"
        ok = requests.Response()
        ok.status_code = 200
        ok._content = json.dumps({"csrfToken": "csrf"}).encode("utf-8")
        ok.url = "https://chatgpt.com/api/auth/csrf"
        responses = [challenge, challenge, challenge, ok]
        gets = []

        class CountingSession(_Session):
            def get(self, url, **kwargs):
                gets.append(self)
                return responses[len(gets) - 1]

        with self._rounds() as rounds, patch.object(
            registration, "BrowserSession", side_effect=lambda **kwargs: CountingSession(),
        ) as constructor, patch.object(
            registration, "get_csrf_token", chatgpt_auth.get_csrf_token,
        ), patch.object(job_context, "interruptible_sleep") as csrf_sleep:
            self.result = self._run()

        self.assertTrue(self.result["success"])
        self.assertEqual(constructor.call_count, 2)
        # 第 1 轮 3 次（同会话重试耗尽）+ 第 2 轮 1 次。
        self.assertEqual(len(gets), 4)
        self.assertEqual(len({id(session) for session in gets[:3]}), 1, "第 1 轮的三次请求应在同一会话内")
        self.assertIsNot(gets[0], gets[3], "第 2 轮应使用新会话")
        csrf_sleep.assert_has_calls([call(5.0), call(10.0)])
        rounds.sleep.assert_called_once_with(5.0)

    def test_sentinel_failure_before_register_user_retries_whole_round(self):
        # 11.6：username_password_create 的 Sentinel 失败发生在 user/register 之前，换出口重来。
        from core.openai.auth.openai_auth import SentinelRequestError

        failure = SentinelRequestError("Sentinel challenge 请求失败（Timeout，HTTP=None）", "sentinel_network_failed")
        with self._rounds(sentinel_failures={"username_password_create": [failure]}) as rounds:
            self.result = self._run()
        self.assertTrue(self.result["success"])
        self.assertEqual(rounds.constructor.call_count, 2)
        self.assertEqual(rounds.flows, ["username_password_create", "username_password_create"])
        self.assertNotIn("failure_category", self.result["registration"])

    def test_sentinel_failure_after_register_user_does_not_retry_round(self):
        # 11.6：oauth_create_account 在 user/register 成功之后，不重试，邮箱回池。
        from core.openai.auth.openai_auth import SentinelRequestError

        failure = SentinelRequestError("Sentinel challenge 请求失败（Timeout，HTTP=None）", "sentinel_network_failed")
        with self._rounds(
            sentinel_failures={"oauth_create_account": [failure]},
            # OTP 验证后进入资料页，才会走到 oauth_create_account 的 Sentinel 采集。
            otp_result={"page": {"type": "about_you"}, "continue_url": "https://auth.openai.com/about-you"},
        ) as rounds, patch.object(
            registration, "settle_email_on_failure", return_value=True,
        ) as settle:
            self.result = self._run()
        self._assert_no_retry(rounds, settle, "network", True)
        self.assertEqual(rounds.flows, ["username_password_create", "oauth_create_account"])
        self.assertEqual(self.result["registration"]["password_state"], "confirmed")
        self.assertEqual(self.result["registration"]["credential_persistence"], "saved")

    def test_email_otp_sentinel_failure_propagates_without_otp_retry(self):
        # 11.7：OTP 验证阶段的 Sentinel 失败直接抛出，不被循环内的 EmailOtpInvalidError 重试吞掉。
        from core.openai.auth.openai_auth import SentinelRequestError

        failure = SentinelRequestError("Sentinel challenge 请求失败（Timeout，HTTP=None）", "sentinel_network_failed")
        with self._rounds(
            sentinel_failures={"email_otp_validate": [failure]},
            otp_result={"continue_url": "https://chatgpt.com/api/auth/callback/openai"},
        ) as rounds, patch.object(
            registration._protocol_cfg, "SEND_SENTINEL_ON_EMAIL_OTP_VALIDATE", True,
        ), patch.object(registration, "validate_email_otp") as validate, patch.object(
            registration, "request_login_otp",
        ) as resend, patch.object(registration, "settle_email_on_failure", return_value=True) as settle:
            self.result = self._run()
        self._assert_no_retry(rounds, settle, "network", True)
        self.assertEqual(rounds.flows[-1], "email_otp_validate")
        self.assertEqual(self.result["registration"]["auth_step"], "otp")
        self.assertEqual(self.result["error_code"], "sentinel_network_failed")
        validate.assert_not_called()
        resend.assert_not_called()

    def test_email_otp_invalid_code_still_retries_after_resend(self):
        # 11.7 对照：现有的 OTP 重发重试不受整轮重试改动的影响。
        from core.openai.auth.openai_auth import EmailOtpInvalidError

        with self._rounds() as rounds, patch.object(
            registration, "validate_email_otp",
            side_effect=[EmailOtpInvalidError("invalid code"),
                         {"continue_url": "https://chatgpt.com/api/auth/callback/openai"}],
        ) as validate, patch.object(
            registration, "request_login_otp", return_value={"continue_url": "/api/accounts/email-otp/validate"},
        ) as resend, patch.object(
            registration, "wait_for_otp", return_value="654321",
        ) as wait, patch.object(registration, "settle_email_on_failure") as settle:
            self.result = self._run()
        self.assertTrue(self.result["success"])
        self.assertEqual(rounds.constructor.call_count, 1)
        self.assertEqual(validate.call_count, 2)
        self.assertEqual(resend.call_count, 1)
        # 重发之后取的是新验证码，不是入参带进来的旧验证码。
        self.assertEqual(validate.call_args_list[1].args[1], "654321")
        wait.assert_called_once()
        settle.assert_not_called()

    def test_no_round_retry_for_transport_failure_after_register_user_succeeds(self):
        # 11.8：user/register 成功返回之后的传输异常，与「请求已发出结果未知」是不同状态。
        with self._rounds() as rounds, patch.object(
            registration, "navigate_email_otp_send",
            side_effect=requests.ConnectionError("connection reset"),
        ), patch.object(registration, "settle_email_on_failure", return_value=True) as settle:
            self.result = self._run()
        self._assert_no_retry(rounds, settle, "network", True)
        self.assertEqual(self.result["registration"]["password_state"], "confirmed")
        self.assertEqual(self.result["registration"]["credential_persistence"], "saved")

    def test_stop_during_round_wait_returns_previous_round_without_new_round(self):
        # 11.3：轮次之间被停止时不开始新的一轮，把上一轮的结果交给调用方；邮箱未被处理。
        with self._rounds() as rounds, patch.object(
            registration, "get_csrf_token", side_effect=requests.ConnectionError("connection reset"),
        ), patch.object(registration, "settle_email_on_failure") as settle:
            rounds.sleep.side_effect = job_context.StopRequested("stopped between rounds")
            self.result = self._run()
        self.assertFalse(self.result["success"])
        self.assertEqual(rounds.constructor.call_count, 1)
        self.assertEqual(self.result["failure_category"], "network")
        self.assertTrue(self.result["retryable_round"])
        settle.assert_not_called()

    def test_stop_before_next_round_returns_previous_round_without_new_round(self):
        # 11.3：等待结束后、下一轮开始前发现停止请求，同样不开始新的一轮。
        with self._rounds() as rounds, patch.object(
            registration, "get_csrf_token", side_effect=requests.ConnectionError("connection reset"),
        ), patch.object(registration, "settle_email_on_failure") as settle, patch.object(
            registration, "check_stop_requested",
            side_effect=job_context.StopRequested("stopped before next round"),
        ) as check:
            self.result = self._run()
        self.assertFalse(self.result["success"])
        self.assertEqual(rounds.constructor.call_count, 1)
        self.assertEqual(check.call_count, 1)
        settle.assert_not_called()

    # ---- 任务层收口（12.1b / 12.6 / 12.7）：_run_one_job 与停止路径对真实池行的处理 ----

    def _claimed_job(self, address):
        """把 address 导入空池，建任务并用它的 id 领取：池行为 registering 且归属该任务。"""
        db._save_outlook([])
        db.import_outlook_accounts([{
            "email": address, "password": "pw", "client_id": "client", "refresh_token": "refresh",
        }])
        job = db.create_job(email_source="outlook", email=address)
        db.claim_next_outlook(job_id=job["id"])
        row = db.get_outlook_by_email(address)
        self.assertEqual(row["status"], "registering", "构造前提：邮箱应已被该任务领取")
        self.assertEqual(row["registration_job_id"], job["id"], "构造前提：池行应归属该任务")
        self.assertEqual(row["network_release_count"], 0, "构造前提：新领取的邮箱尚未回池过")
        return job

    def _assert_email_failed_without_release(self, address):
        """池行被标 failed，且回池计数仍为 0（停止路径不回池、不计次）。"""
        row = db.get_outlook_by_email(address)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["network_release_count"], 0)

    def _run_one_job(self, job_id, label):
        # _prepare_registration_args 在手动模式下要求配置邮箱；这里只关心任务行上的邮箱。
        with patch.object(registration._email_cfg, "USE_EMAIL_SERVICE", True), \
                patch.object(registration_service, "_prepare_registration_args",
                             return_value=(None, "Test User", "1995-01-01")):
            registration_service._run_one_job(job_id, str(self.root / f"{label}.log"))

    def test_stop_request_without_running_instance_fails_claimed_email_without_counting(self):
        # 12.1b：任务行 running 但没有线程实例（服务重启、线程异常退出）时，停止直接落 stopped，
        # 并把它领取的邮箱标 failed。计数字段区分 reusable 的两种传法：传 True 时行会回池并计数。
        address = "stop-no-instance@example.com"
        job = self._claimed_job(address)
        db.update_job(job["id"], status="running")
        result = registration_service.request_stop_job(job["id"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "stopped")
        stored = db.get_job(job["id"])
        self.assertEqual(stored["status"], "stopped")
        self.assertIn("任务实例不存在", stored["error_message"])
        self._assert_email_failed_without_release(address)

    def test_stop_request_without_instance_ignores_email_released_by_last_round(self):
        # 最后一轮已回池的邮箱不会被停止路径改成 failed：settle 只对 registering 行生效。
        address = "stop-released@example.com"
        job = self._claimed_job(address)
        self.assertTrue(db.settle_failed_registration_email(address, reusable=True))
        db.update_job(job["id"], status="running")
        self.assertTrue(registration_service.request_stop_job(job["id"])["ok"])
        row = db.get_outlook_by_email(address)
        self.assertEqual(row["status"], "available")
        self.assertEqual(row["network_release_count"], 1)

    def test_stop_request_for_partial_success_job_reports_finished_without_side_effects(self):
        # 部分成功是终态：停止接口只回报已结束，不改任务行、不动邮箱，
        # 也不把它当成可停止的 running/stopping。
        address = "stop-partial@example.com"
        job = self._claimed_job(address)
        db.update_job(
            job["id"], status="partial_success",
            error="账号已保存，配置要求的 2FA 未完成",
        )
        result = registration_service.request_stop_job(job["id"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "partial_success")
        self.assertIn("已结束", result["message"])
        stored = db.get_job(job["id"])
        self.assertEqual(stored["status"], "partial_success")
        self.assertEqual(stored["error_message"], "账号已保存，配置要求的 2FA 未完成")
        row = db.get_outlook_by_email(address)
        self.assertEqual(row["status"], "registering")
        self.assertEqual(row["network_release_count"], 0)

    def test_retry_job_with_unusable_pool_row_fails_without_acquiring_or_registering(self):
        # 12.6：重试子任务带着任务行上的邮箱执行，但池行已不是 registering（这里被人工停用）。
        # 不领新邮箱、不建会话、以失败结束，错误说明指向池行状态。
        real_prepare = registration._prepare_persisted_email_registration
        for state, setup in (
            ("disabled", lambda address: db.delete_outlook(address)),
            ("failed", lambda address: db.settle_failed_registration_email(address, reusable=False)),
        ):
            with self.subTest(state=state):
                db._save_outlook([])
                address = f"retry-unusable-{state}@example.com"
                job = self._claimed_job(address)
                setup(address)
                self.assertEqual(db.get_outlook_by_email(address)["status"], state)
                with self._pipeline({}), patch.object(
                    registration, "_prepare_persisted_email_registration", real_prepare,
                ), patch.object(registration, "BrowserSession") as ctor, patch.object(
                    email_provider, "acquire_email",
                ) as acquire:
                    self._run_one_job(job["id"], f"unusable-{state}")
                stored = db.get_job(job["id"])
                self.assertEqual(stored["status"], "failed")
                self.assertIn("邮箱池条目不可用于注册", stored["error_message"])
                self.assertIn(f"status={state}", stored["error_message"])
                self.assertEqual(stored["failure_category"], "unknown")
                ctor.assert_not_called()
                acquire.assert_not_called()
                # 不可用的行保持原样：不回池、不改计数。
                row = db.get_outlook_by_email(address)
                self.assertEqual(row["status"], state)
                self.assertEqual(row["network_release_count"], 0)

    def _stop_then_raise(self, job_id, exc):
        """先置停止位再抛异常，与真实检查点一致：StopRequested 只在停止位已置时才会出现。"""
        def raise_after_stop(*_args, **_kwargs):
            job_context.request_stop(job_id)
            raise exc

        return raise_after_stop

    def test_stop_inside_round_marks_claimed_email_failed(self):
        # 12.7：单轮内部的检查点抛 StopRequested。run_registration 按失败处理邮箱但不回池，
        # 任务层按 stopped 收口。
        address = "stop-inside@example.com"
        job = self._claimed_job(address)
        with self._pipeline({}), patch.object(
            registration, "get_csrf_token",
            side_effect=self._stop_then_raise(job["id"], job_context.StopRequested("stopped by user")),
        ):
            self._run_one_job(job["id"], "stop-inside")
        stored = db.get_job(job["id"])
        self.assertEqual(stored["status"], "stopped")
        self._assert_email_failed_without_release(address)

    def test_stop_during_round_wait_marks_claimed_email_failed(self):
        # 12.7：第 1 轮网络类失败，轮次间等待时被停止。run_registration 没处理邮箱（池行仍 registering），
        # 由任务层的停止路径把它标 failed；结果与单轮内部停止相同。
        address = "stop-between@example.com"
        job = self._claimed_job(address)
        with self._rounds() as rounds, patch.object(
            registration, "get_csrf_token", side_effect=requests.ConnectionError("connection reset"),
        ):
            rounds.sleep.side_effect = self._stop_then_raise(
                job["id"], job_context.StopRequested("stopped between rounds"),
            )
            self._run_one_job(job["id"], "stop-between")
        self.assertEqual(rounds.constructor.call_count, 1)
        stored = db.get_job(job["id"])
        self.assertEqual(stored["status"], "stopped")
        self._assert_email_failed_without_release(address)

    def test_transport_error_while_stop_requested_is_treated_as_stop(self):
        # 12.7：停止位已置期间撞上网络类异常。停止检查先于邮箱处理，邮箱不回池、不计入回池次数。
        address = "stop-during-error@example.com"
        job = self._claimed_job(address)

        def stop_then_fail(*_args, **_kwargs):
            job_context.request_stop(job["id"])
            raise requests.ConnectionError("connection reset")

        with patch.object(registration_service, "run_registration", side_effect=stop_then_fail), \
                patch.object(email_provider, "settle_email_on_failure",
                             wraps=email_provider.settle_email_on_failure) as settle:
            self._run_one_job(job["id"], "stop-during-error")
        stored = db.get_job(job["id"])
        self.assertEqual(stored["status"], "stopped")
        self.assertIsNone(stored["failure_category"], "停止路径不分类，不写失败类别")
        self._assert_email_failed_without_release(address)
        settle.assert_called_once()
        self.assertFalse(settle.call_args.kwargs["reusable"])

    def test_partial_success_when_account_saved_but_2fa_not_completed(self):
        # 账号已保存、配置要求 2FA 但设置未完成：任务记 partial_success 而非 failed，
        # 失败类别为空，邮箱不回池；重试走账号分支而不是重新注册。
        address = "user@example.com"
        job = self._claimed_job(address)
        db.update_job(job["id"], status="running")
        with self._pipeline({"continue_url": "https://chatgpt.com/api/auth/callback/openai"}), \
                patch.object(registration, "wait_for_otp", return_value="123456"), \
                patch.object(registration._twofa_cfg, "ENABLE_2FA", True), \
                patch.object(registration, "_setup_registration_totp", return_value=""), \
                patch.object(registration_service, "_release_unconsumed_job_email") as release:
            self._run_one_job(job["id"], "partial-2fa")
        release.assert_not_called()
        stored = db.get_job(job["id"])
        self.assertEqual(stored["status"], "partial_success")
        self.assertIn("2FA", stored["error_message"])
        self.assertIsNone(stored["failure_category"])
        self.assertIsNotNone(stored["account_id"])
        row = db.get_outlook_by_email(address)
        self.assertNotEqual(row["status"], "available")
        self.assertEqual(row["network_release_count"], 0)
        info = registration_service.get_retry_info(stored)
        self.assertTrue(info["retryable"])
        self.assertEqual(info["retry_action"], "codex")

    def test_failed_round_reports_email_settled(self):
        # 单轮已在流程内决定邮箱去向时，返回结果带 email_settled，任务层不再重复处理。
        with self._pipeline({}), \
                patch.object(registration, "get_csrf_token",
                             side_effect=requests.ConnectionError("connection reset")), \
                patch.object(registration, "settle_email_on_failure", return_value=True) as settle:
            result = self._run()
        settle.assert_called_once()
        self.assertFalse(result["success"])
        self.assertTrue(result["email_settled"])

    def test_task_layer_skips_email_release_when_round_settled_it(self):
        address = "settled-task@example.com"
        job = self._claimed_job(address)
        result = {
            "success": False, "partial": False, "email": address,
            "error": "connection reset", "failure_category": "network",
            "email_settled": True,
        }
        with patch.object(registration_service, "run_registration", return_value=result), \
                patch.object(registration_service, "_release_unconsumed_job_email") as release:
            self._run_one_job(job["id"], "settled-task")
        release.assert_not_called()
        stored = db.get_job(job["id"])
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored["failure_category"], "network")


if __name__ == "__main__":
    unittest.main()
