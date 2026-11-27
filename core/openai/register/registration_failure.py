"""注册失败分类：由异常对象判定失败类别，供邮箱处理与整轮重试共用。

失败类别是封闭的三值枚举（``FailureCategory``）：``network``（出口或链路问题，换出口重来有意义）、
``account``（邮箱对应的账号已废）、``unknown``（其余全部）。

分类器 ``classify_failure`` 是纯函数，只看异常类型和异常对象携带的结构化字段（``category``、
``response.headers``），不对 ``str(exc)`` 做子串匹配。本地已有的结构化信号足以区分这些失败，
不需要 OSS ``registrar.classify_error`` 那样的消息关键词表。

判定表（自上而下，先命中先得）：

| 异常类型                                                   | 附加条件                                         | 类别      |
| ---------------------------------------------------------- | ------------------------------------------------ | --------- |
| ``AccountUnusableError``                                   | 无                                               | ``account`` |
| ``requests.ConnectionError`` / ``Timeout`` / ``ProxyError`` | 无                                               | ``network`` |
| ``requests.HTTPError``                                     | ``response.headers["cf-mitigated"]`` 等于 ``challenge`` | ``network`` |
| ``SentinelRequestError``                                   | 无（三种 ``category`` 全部）                     | ``network`` |
| ``SentinelSDKError``                                       | ``category`` 属于求解类六个值                    | ``network`` |
| ``RegistrationGeoError``                                   | ``category == "proxy"``                          | ``network`` |
| 其余全部                                                   | 无                                               | ``unknown`` |

要点：

- ``cf-mitigated`` 只读响应头，不看状态码，不解析响应体。只有带 ``cf-mitigated: challenge`` 的响应
  归 ``network``；其余 ``HTTPError``（非 CF 的 403、409 含 ``already exists``、429、5xx）归 ``unknown``。
  ``exc.response`` 为 ``None`` 时按非 CF 处理。
- ``SentinelSDKError`` 的求解类：``protocol_error``、``flow_mismatch``、``invalid_challenge``、
  ``no_requirements``、``empty_main_token``、``token_context_mismatch``。
- ``SentinelSDKError`` 的环境类：``sdk_missing``、``node_missing``、``requirements_missing``、
  ``sdk_exec_failed``、``closed``、``timeout``，归 ``unknown``。两组之外还有 ``reuse`` 和子进程消息里
  透传的动态 ``category``，同样落 ``unknown``。
- ``requests.SSLError`` 是 ``ConnectionError`` 的子类，随第二行归 ``network``。
  ``requests.ProxyError`` 同为 ``ConnectionError`` 子类，显式列出只为表明它在这一行。这两个类
  ``requests`` 顶层没有再导出，必须从 ``requests.exceptions`` 取。
- ``_RegistrationFailure``、``PasswordRejectedError``、``PasswordRegistrationRejectedError``、
  ``StopRequested`` 以及任何未列出的异常，归 ``unknown``。

与 OSS 的两处对照：

1. **``cf-mitigated`` 判定比 OSS 的关键词精确。** OSS ``webui/registrar.py`` 的
   ``_NETWORK_ERROR_PATTERNS`` 含 ``"just a moment"`` 和 ``"403 forbidden"``，按错误消息子串命中，
   任何消息里带 403 字样的失败都会被当成网络类。本地读结构化响应头，非 CF 的 403 归 ``unknown``，
   因为本地没有信号判断它是出口维度还是邮箱维度，OSS 同样区分不了。
2. **``SentinelSDKError`` 环境类归 ``unknown``，是与 OSS ``sentinel.py:83`` 的有意差异。** OSS 那条
   ``Sentinel QuickJS 模块缺失`` 的消息命中了 ``sentinel quickjs`` 关键词，被归为 ``network``、
   邮箱回池。那是消息文本的巧合命中，不是刻意设计。本地缺 Node 或 runner 文件时，换出口和换邮箱
   都不会改变结果，邮箱回池后只会被反复领取、反复因缺文件失败，所以归 ``unknown``。

默认归 ``unknown`` 而不是 ``network``：``unknown`` 的邮箱处理与引入分类之前完全一致（标 ``failed``）。
分类器漏掉或判错任何一种异常，行为都退回引入之前，不会更差。
"""

from __future__ import annotations

from requests import exceptions as requests_exceptions

from core.openai.auth.openai_auth import AccountUnusableError, SentinelRequestError
from core.openai.auth.sentinel_runner import SentinelSDKError
from core.openai.register.registration_geo import RegistrationGeoError


class FailureCategory:
    """失败类别的三个取值。封闭枚举，不使用 ``Enum`` 以便直接与 JSON 中的字符串比较。"""

    NETWORK = "network"
    ACCOUNT = "account"
    UNKNOWN = "unknown"


# Sentinel SDK 宿主在「求解」阶段失败：challenge 或 token 的内容、流程、上下文不符。
# 换出口整轮重来会重建 challenge 上下文，所以归 network。
# 另一组是环境类（sdk_missing / node_missing / requirements_missing / sdk_exec_failed / closed / timeout）：
# 本地缺 Node 或 runner 文件，换出口和换邮箱都无效，归 unknown。分类器不命中求解类即落到 unknown，
# 所以环境类不需要单独列出。
SENTINEL_SDK_SOLVE_CATEGORIES = frozenset({
    "protocol_error",
    "flow_mismatch",
    "invalid_challenge",
    "no_requirements",
    "empty_main_token",
    "token_context_mismatch",
})

# RegistrationGeoError 中归 network 的唯一 category：代理连接或认证失败。
GEO_PROXY_CATEGORY = "proxy"


def _is_cloudflare_challenge(response) -> bool:
    """响应是否为 Cloudflare 挑战页。与 ``core/infra/http_diagnostics.py`` 读同一个头、同一判定。"""
    headers = getattr(response, "headers", None)
    if not headers:
        return False
    return headers.get("cf-mitigated", "").strip().lower() == "challenge"


def classify_failure(exc: BaseException) -> str:
    """由异常对象判定失败类别，返回 ``FailureCategory`` 的三个取值之一。

    只做类型判断、``category`` 等值比较和响应头读取，不读异常消息。
    """
    if isinstance(exc, AccountUnusableError):
        return FailureCategory.ACCOUNT

    if isinstance(exc, (requests_exceptions.ConnectionError, requests_exceptions.Timeout,
                        requests_exceptions.ProxyError)):
        return FailureCategory.NETWORK

    if isinstance(exc, requests_exceptions.HTTPError) and _is_cloudflare_challenge(exc.response):
        return FailureCategory.NETWORK

    if isinstance(exc, SentinelRequestError):
        return FailureCategory.NETWORK

    if isinstance(exc, SentinelSDKError) and exc.category in SENTINEL_SDK_SOLVE_CATEGORIES:
        return FailureCategory.NETWORK

    if isinstance(exc, RegistrationGeoError) and exc.category == GEO_PROXY_CATEGORY:
        return FailureCategory.NETWORK

    return FailureCategory.UNKNOWN
