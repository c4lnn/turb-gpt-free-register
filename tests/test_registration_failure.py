# -*- coding: utf-8 -*-
"""注册失败分类器：只看异常类型与结构化字段，不对消息文本做匹配。"""
import unittest

import requests
from requests import exceptions as requests_exceptions

from core.infra.job_context import StopRequested
from core.openai.auth.openai_auth import (
    AccountUnusableError,
    PasswordRegistrationRejectedError,
    PasswordRejectedError,
    SentinelRequestError,
)
from core.openai.auth.sentinel_runner import SentinelSDKError
from core.openai.register.registration_failure import (
    GEO_PROXY_CATEGORY,
    SENTINEL_SDK_SOLVE_CATEGORIES,
    FailureCategory,
    classify_failure,
)
from core.openai.register.registration_flow import _RegistrationFailure
from core.openai.register.registration_geo import (
    CATEGORY_GEO_INCOMPLETE,
    CATEGORY_GEO_MISMATCH,
    CATEGORY_GEO_PARSE,
    CATEGORY_GEO_SERVICE,
    CATEGORY_PROXY,
    STAGE_DETECT,
    STAGE_VALIDATE,
    RegistrationGeoError,
)

CF_CHALLENGE = "challenge"


def _response(status, headers=None):
    response = requests.Response()
    response.status_code = status
    for name, value in (headers or {}).items():
        response.headers[name] = value
    return response


def _http_error(status, headers=None):
    return requests_exceptions.HTTPError("http error", response=_response(status, headers))


def _sdk_error(category):
    return SentinelSDKError("sdk host failed", category, "solve")


def _geo_error(category):
    return RegistrationGeoError("geo gate failed", category, STAGE_VALIDATE)


def _instance_of(cls):
    """构造 requests 异常类的一个实例。JSONDecodeError 族继承 ValueError，需要 doc / pos。"""
    if issubclass(cls, ValueError):
        return cls("x", "{}", 0)
    return cls("x")


class FailureCategoryValueTests(unittest.TestCase):
    def test_category_values_are_the_closed_three_value_enum(self):
        # 封闭三值枚举，直接与任务记录 JSON 里的字符串比较。
        self.assertEqual(
            {FailureCategory.NETWORK, FailureCategory.ACCOUNT, FailureCategory.UNKNOWN},
            {"network", "account", "unknown"},
        )


class TransportFailureTests(unittest.TestCase):
    def test_connection_error_is_network(self):
        self.assertEqual(classify_failure(requests_exceptions.ConnectionError("x")), FailureCategory.NETWORK)

    def test_ssl_error_is_network_even_though_not_exported_at_top_level(self):
        # SSLError 是 ConnectionError 的子类，但 requests 顶层没有再导出它。
        # 分类器曾经因为写成 requests.SSLError 而在第二个判断处崩溃。
        self.assertFalse(hasattr(requests, "SSLError"))
        self.assertTrue(issubclass(requests_exceptions.SSLError, requests_exceptions.ConnectionError))
        self.assertEqual(classify_failure(requests_exceptions.SSLError("x")), FailureCategory.NETWORK)

    def test_proxy_error_is_network_even_though_not_exported_at_top_level(self):
        # ProxyError 同样只在 requests.exceptions 下可见。
        self.assertFalse(hasattr(requests, "ProxyError"))
        self.assertEqual(classify_failure(requests_exceptions.ProxyError("x")), FailureCategory.NETWORK)

    def test_timeouts_are_network(self):
        for cls in (requests_exceptions.Timeout, requests_exceptions.ReadTimeout,
                    requests_exceptions.ConnectTimeout):
            with self.subTest(cls=cls.__name__):
                self.assertEqual(classify_failure(cls("x")), FailureCategory.NETWORK)

    def test_every_requests_exception_class_has_a_verdict_and_none_crashes(self):
        # 遍历 requests.exceptions 的全部异常类（含 warning 以外的一切）。
        # 目的：分类器对任何异常都必须返回三个类别之一，不能抛异常。
        # failure_category 的默认值 unknown 与引入分类之前的邮箱处理一致，
        # 前提是分类器不会在失败处理路径上自己失败。
        classes = {
            name: cls for name, cls in vars(requests_exceptions).items()
            if isinstance(cls, type) and issubclass(cls, BaseException)
        }
        self.assertIn("SSLError", classes)
        network, other = set(), set()
        for name, cls in classes.items():
            with self.subTest(cls=name):
                category = classify_failure(_instance_of(cls))
                self.assertIn(category, {FailureCategory.NETWORK, FailureCategory.ACCOUNT, FailureCategory.UNKNOWN})
                (network if category == FailureCategory.NETWORK else other).add(name)
        # 连接与超时类归 network；URL / JSON / 重定向等本地请求构造错误归 unknown，
        # 它们不是链路问题，换出口重来结果不变。
        self.assertEqual(network, {
            "ConnectionError", "ConnectTimeout", "ReadTimeout", "ProxyError", "SSLError", "Timeout",
        })
        self.assertIn("HTTPError", other)
        self.assertIn("InvalidURL", other)
        self.assertIn("JSONDecodeError", other)


class CloudflareChallengeTests(unittest.TestCase):
    def test_403_with_cf_mitigated_challenge_is_network(self):
        error = _http_error(403, {"cf-mitigated": CF_CHALLENGE})
        self.assertEqual(classify_failure(error), FailureCategory.NETWORK)

    def test_cf_mitigated_value_is_case_insensitive_and_ignores_surrounding_space(self):
        # 与 core/infra/http_diagnostics.py 的读法一致。
        for value in ("Challenge", "CHALLENGE", " challenge "):
            with self.subTest(value=value):
                self.assertEqual(classify_failure(_http_error(403, {"cf-mitigated": value})), FailureCategory.NETWORK)

    def test_403_without_cf_mitigated_is_unknown(self):
        self.assertEqual(classify_failure(_http_error(403)), FailureCategory.UNKNOWN)

    def test_403_with_other_cf_mitigated_value_is_unknown(self):
        self.assertEqual(classify_failure(_http_error(403, {"cf-mitigated": "block"})), FailureCategory.UNKNOWN)

    def test_http_error_without_response_is_unknown(self):
        self.assertEqual(classify_failure(requests_exceptions.HTTPError("x")), FailureCategory.UNKNOWN)

    def test_409_and_5xx_are_unknown(self):
        for status in (409, 429, 500, 502, 503):
            with self.subTest(status=status):
                self.assertEqual(classify_failure(_http_error(status)), FailureCategory.UNKNOWN)

    def test_response_status_is_not_inspected_when_cf_mitigated_is_present(self):
        # 判定只看响应头，不看状态码：带 cf-mitigated: challenge 的响应无论状态码都归 network。
        for status in (403, 503):
            with self.subTest(status=status):
                error = _http_error(status, {"cf-mitigated": CF_CHALLENGE})
                self.assertEqual(classify_failure(error), FailureCategory.NETWORK)


class SentinelFailureTests(unittest.TestCase):
    def test_sentinel_request_error_is_network_for_every_category(self):
        for category in ("sentinel_network_failed", "sentinel_service_failed",
                         "sentinel_challenge_failed"):
            with self.subTest(category=category):
                error = SentinelRequestError("x", category)
                self.assertEqual(classify_failure(error), FailureCategory.NETWORK)

    def test_sentinel_request_error_default_category_is_network(self):
        self.assertEqual(classify_failure(SentinelRequestError("x")), FailureCategory.NETWORK)

    def test_solve_categories_are_network(self):
        for category in sorted(SENTINEL_SDK_SOLVE_CATEGORIES):
            with self.subTest(category=category):
                self.assertEqual(classify_failure(_sdk_error(category)), FailureCategory.NETWORK)

    def test_environment_categories_are_unknown(self):
        # 本地缺 Node 或 runner 文件时，换出口和换邮箱都无效，不归 network。
        for category in ("sdk_missing", "node_missing", "requirements_missing",
                         "sdk_exec_failed", "closed", "timeout"):
            with self.subTest(category=category):
                self.assertEqual(classify_failure(_sdk_error(category)), FailureCategory.UNKNOWN)

    def test_unlisted_sdk_category_is_unknown(self):
        for category in ("reuse", "some_future_category"):
            with self.subTest(category=category):
                self.assertEqual(classify_failure(_sdk_error(category)), FailureCategory.UNKNOWN)

    def test_sdk_categories_raised_in_runner_are_all_accounted_for(self):
        # sentinel_runner.py 里实际构造的 category 字面量，除求解类外都必须落到 unknown。
        # 新增求解类 category 时，这个用例会逼出一次显式确认。
        raised = {
            "sdk_missing", "closed", "reuse", "flow_mismatch", "requirements_missing",
            "no_requirements", "invalid_challenge", "empty_main_token", "token_context_mismatch",
            "node_missing", "sdk_exec_failed", "timeout", "protocol_error",
        }
        self.assertEqual(raised & SENTINEL_SDK_SOLVE_CATEGORIES, SENTINEL_SDK_SOLVE_CATEGORIES)
        for category in sorted(raised - SENTINEL_SDK_SOLVE_CATEGORIES):
            with self.subTest(category=category):
                self.assertEqual(classify_failure(_sdk_error(category)), FailureCategory.UNKNOWN)


class GeoFailureTests(unittest.TestCase):
    def test_proxy_failure_is_network(self):
        self.assertEqual(GEO_PROXY_CATEGORY, CATEGORY_PROXY)
        error = RegistrationGeoError("x", CATEGORY_PROXY, STAGE_DETECT)
        self.assertEqual(classify_failure(error), FailureCategory.NETWORK)

    def test_other_geo_failures_are_unknown(self):
        for category in (CATEGORY_GEO_MISMATCH, CATEGORY_GEO_INCOMPLETE,
                         CATEGORY_GEO_SERVICE, CATEGORY_GEO_PARSE):
            with self.subTest(category=category):
                self.assertEqual(classify_failure(_geo_error(category)), FailureCategory.UNKNOWN)


class OtherFailureTests(unittest.TestCase):
    def test_account_unusable_is_account(self):
        error = AccountUnusableError("x", "account_banned")
        self.assertEqual(classify_failure(error), FailureCategory.ACCOUNT)

    def test_registration_failure_is_unknown(self):
        self.assertEqual(classify_failure(_RegistrationFailure("x", "probe", "classification_probe_failed")),
                         FailureCategory.UNKNOWN)

    def test_password_rejections_are_unknown(self):
        self.assertEqual(classify_failure(PasswordRejectedError("x")), FailureCategory.UNKNOWN)
        self.assertEqual(classify_failure(PasswordRegistrationRejectedError("x")), FailureCategory.UNKNOWN)

    def test_stop_requested_is_unknown(self):
        self.assertEqual(classify_failure(StopRequested("stop")), FailureCategory.UNKNOWN)

    def test_plain_runtime_error_is_unknown(self):
        self.assertEqual(classify_failure(RuntimeError("x")), FailureCategory.UNKNOWN)

    def test_message_text_is_never_inspected(self):
        # 消息里带 OSS 关键词表里的词，也不能改变判定结果。
        for text in ("403 forbidden", "just a moment", "tls handshake failed", "connection reset",
                     "Sentinel QuickJS 模块缺失"):
            with self.subTest(text=text):
                self.assertEqual(classify_failure(RuntimeError(text)), FailureCategory.UNKNOWN)
                self.assertEqual(classify_failure(
                    SentinelSDKError(text, "sdk_missing", "requirements")), FailureCategory.UNKNOWN)
                self.assertEqual(classify_failure(
                    SentinelRequestError(text, "sentinel_challenge_failed")), FailureCategory.NETWORK)
                self.assertEqual(classify_failure(
                    _http_error(403, {"x-note": text})), FailureCategory.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
