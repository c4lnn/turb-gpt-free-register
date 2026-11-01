# -*- coding: utf-8 -*-
"""
OpenAI Auth 模块
处理 auth.openai.com 域名下的注册请求（步骤4-5、7-8、10、12）
以及 sentinel.openai.com 的 sentinel token 请求（步骤6、9、11）
"""
import json
import logging
import random
import secrets
import time
from urllib.parse import urlparse, urljoin

import pyotp

from config import openai_protocol as _protocol_cfg
from core.session import BrowserSession
from core.sentinel import build_sentinel_request_body
from core.sentinel_runner import SentinelSDK

logger = logging.getLogger(__name__)


def _rotate_document_navigation_id(session: BrowserSession) -> None:
    rotate = getattr(session, "rotate_document_navigation_id", None)
    if callable(rotate):
        rotate()


class SentinelRequestError(RuntimeError):
    """安全的 challenge 阶段诊断，不包含原始请求或响应。"""

    stage = "sentinel_challenge"

    def __init__(self, message: str, category: str = "sentinel_challenge_failed"):
        super().__init__(message)
        self.category = category


class EmailOtpInvalidError(RuntimeError):
    """邮箱验证码无效/过期，可重新发送后重试。"""


class AccountUnusableError(Exception):
    """
    邮箱对应的 OpenAI 账号已废（删除/停用/封禁），再试也是同样结果。

    与普通网络/风控错误区分：这类错误意味着这个邮箱素材本身不可用，
    上层应把邮箱标成 failed 直接剔除，而不是放回 available 反复重试。

    携带 error_code 便于日志与排查（如 account_deactivated）。
    """

    def __init__(self, message: str, error_code: str = ""):
        super().__init__(message)
        self.error_code = error_code


class PasswordRejectedError(RuntimeError):
    """已有账号登录密码被服务端明确拒绝。

    仅当服务端响应明确表示密码不正确时抛出；429/403/5xx、网络错误以及
    未知的 4xx 不得归入此类，以免误触发邮箱 OTP 回退。携带解析后的结构化
    result（可能含服务端 advertise 的继续路径），供上层选择支持的 OTP 切换。
    """

    def __init__(self, message: str, result: dict | None = None):
        super().__init__(message)
        self.result = result or {}


class PasswordRegistrationRejectedError(RuntimeError):
    """新号设置密码（user/register）被服务端明确拒绝。

    仅当服务端明确拒绝本次设置密码（如 registration_disallowed）时抛出；
    未知 4xx、CF 挑战、429、网络结果不明均不得归入此类，不得据此进入
    passwordless OTP 回退。携带解析后的结构化 result。
    """

    def __init__(self, message: str, result: dict | None = None):
        super().__init__(message)
        self.result = result or {}


# 远端返回这些 error code 时，判定邮箱素材已废，不再重试。
_ACCOUNT_DEAD_CODES = frozenset({
    "account_deactivated",   # 账号已删除/停用
    "account_deleted",
    "account_banned",
})

_ACCOUNT_DEAD_TEXT_MARKERS = (
    "account_deactivated",
    "account_deleted",
    "account_banned",
    "account deactivated",
    "account deleted",
    "account banned",
    "account has been deactivated",
    "account has been deleted",
    "account was deactivated",
    "account was deleted",
    "your account has been deactivated",
    "your account has been deleted",
    "your account was deactivated",
    "your account was deleted",
    "账号已停用",
    "账号已禁用",
    "账号已删除",
    "账户已停用",
    "账户已禁用",
    "账户已删除",
)


def detect_account_unusable_text(text: str) -> str:
    """从浏览器页面/异常文本里识别账号已废，返回规范 error_code；未命中返回空串。"""
    low = str(text or "").lower()
    for code in _ACCOUNT_DEAD_CODES:
        if code in low:
            return code
    if any(marker in low for marker in _ACCOUNT_DEAD_TEXT_MARKERS):
        if "delete" in low or "删除" in low:
            return "account_deleted"
        if "ban" in low or "封" in low:
            return "account_banned"
        return "account_deactivated"
    return ""


def detect_account_unusable_response_body(body: str) -> str:
    """
    按纯协议模式同源逻辑，从接口响应 JSON 的 error.code 识别账号已废。

    这不是页面文字识别；用于浏览器/指纹浏览器拦截
    /api/accounts/email-otp/validate 响应后，读取响应体里的结构化错误码。
    """
    try:
        payload = json.loads(body or "")
    except Exception:
        return ""
    err = payload.get("error") if isinstance(payload, dict) else None
    code = ""
    if isinstance(err, dict):
        code = str(err.get("code") or "")
    elif isinstance(payload, dict):
        code = str(payload.get("code") or payload.get("error_code") or "")
    return code if code in _ACCOUNT_DEAD_CODES else ""


def _extract_error_code(resp) -> str:
    """从响应体 JSON 里抽 error.code（拿不到返回空串）。"""
    try:
        payload = resp.json()
    except Exception:
        return ""
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(err, dict):
        return str(err.get("code") or "")
    return ""

def _raise_for_account_unusable_response(resp) -> None:
    """在通用 HTTP 异常前保留结构化账号废弃结果。"""
    code = _extract_error_code(resp)
    if code in _ACCOUNT_DEAD_CODES:
        raise AccountUnusableError(
            f"账号已废弃（{code}）", error_code=code,
        )



def _response_text(resp) -> str:
    return str(getattr(resp, "text", "") or "")


def _try_response_json(resp) -> dict:
    try:
        payload = resp.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _looks_like_json(resp) -> bool:
    """按响应 Content-Type 判断是否为结构化 JSON，避免把 HTML 落地误当 page 状态。"""
    ctype = str(getattr(getattr(resp, "headers", None), "get", lambda *_: "")("Content-Type") or "")
    return "json" in ctype.lower()


# 登录密码被明确拒绝的结构化 error.code（保守集合；精确码待真实运行确认）。
# 不含 invalid_credentials（也可能是非密码原因），不按"password+invalid"泛化正文。
_PASSWORD_REJECTION_CODES = frozenset({
    "invalid_password",
    "password_incorrect",
    "incorrect_password",
    "wrong_password",
    "password_rejected",
})

# 设置密码（user/register）被明确拒绝的结构化 error.code（保守集合）。
# 不含 already exists（属账号已存在冲突，不是"可回退 OTP"的拒绝），不含 409 invalid_state。
_REGISTRATION_REJECTION_CODES = frozenset({
    "registration_disallowed",
    "registration_not_allowed",
    "signup_disallowed",
})


def _is_explicit_password_rejection(status: int, body: str, code: str) -> bool:
    """仅把明确的密码拒绝归为密码错误；限流/CF/5xx/网络/未知 4xx 一律不归。"""
    if status not in (400, 401, 422):
        return False
    if code and code.strip().lower() in _PASSWORD_REJECTION_CODES:
        return True
    low = (body or "").lower()
    return "incorrect password" in low or "wrong password" in low


def _is_explicit_registration_rejection(status: int, body: str, code: str) -> bool:
    """仅把 user/register 的明确业务拒绝（registration_disallowed）归为设置密码拒绝。

    already exists / 409 invalid_state 表示账号已存在或状态冲突，不是"可继续 OTP"
    的密码设置拒绝，不得据此进入回退。
    """
    if status not in (400, 401, 403, 409, 422):
        return False
    if code and code.strip().lower() in _REGISTRATION_REJECTION_CODES:
        return True
    low = (body or "").lower()
    return "registration_disallowed" in low or "registration not allowed" in low


def _proxy_retry_config() -> tuple[int, float]:
    """读取可热加载的代理网络重试配置，并约束异常配置值。"""
    attempts = max(1, int(getattr(_protocol_cfg, "OPENAI_PROXY_RETRY_MAX_ATTEMPTS", 3)))
    delay = max(0.0, float(getattr(_protocol_cfg, "OPENAI_PROXY_RETRY_DELAY", 1.0)))
    return attempts, delay


def _is_transient_network_error(exc: Exception) -> bool:
    """识别可重试的临时性网络错误（TLS / 连接超时 / 连接重置 / 代理拒绝）。"""
    name = type(exc).__name__
    msg = str(exc).lower()
    transient_classes = ("SSLError", "ConnectionError", "Timeout", "CurlError", "ProxyError")
    if any(t.lower() in name.lower() for t in transient_classes):
        return True
    transient_keywords = (
        "wrong_version_number",      # 代理给了非 TLS 响应
        "tls connect",
        "ssl",
        "connection reset",
        "connection refused",
        "timed out",
        "proxy",
        "curl: (97)",                # SOCKS5 连接目标主机失败
        "curl: (35)",
        "curl: (52)",                # empty reply from server
        "curl: (56)",                # network recv failure
    )
    return any(k in msg for k in transient_keywords)


def _is_retryable_authorize_error(exc: Exception) -> bool:
    """authorize 导航的 403/429/5xx 可在同一会话内复用新 CF Cookie 重试。"""
    response = getattr(exc, "response", None)
    try:
        status = int(getattr(response, "status_code", 0) or 0)
    except (TypeError, ValueError):
        status = 0
    if status in (403, 408, 425, 429) or status >= 500:
        return True
    text = str(exc or "").lower()
    return _is_transient_network_error(exc) or any(
        marker in text for marker in (
            "http 403", "http error 403", "status=403",
            "http 429", "http error 429", "status=429", "熔断冷却",
        )
    )


def _exception_http_status(exc: Exception) -> int:
    """尽量从 curl/requests 风格异常中读取 HTTP 状态码。"""
    try:
        return int(getattr(getattr(exc, "response", None), "status_code", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _reset_retryable_circuit(session: BrowserSession) -> None:
    """仅清除本地熔断，保留当前 Session 的 Cookie Jar 和完整身份上下文。"""
    reset = getattr(session, "reset_circuit_breaker", None)
    if callable(reset):
        reset()
    else:
        session.blocked_until = 0.0
        session.blocked_reason = ""


def _check_stop_requested() -> None:
    """懒加载任务服务，避免模块导入阶段形成 main/registration_service 循环依赖。"""
    from core.registration_service import check_stop_requested
    check_stop_requested()


def _interruptible_sleep(seconds: float, interval: float = 0.25) -> None:
    """分段等待，使 WebUI 手动停止无需等完整指数退避结束。"""
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        _check_stop_requested()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(max(0.01, interval), remaining))


def _request_with_proxy_retry(session: BrowserSession, label: str, fn):
    """对代理/TLS/超时及可恢复 HTTP 错误进行有限指数退避重试。"""
    max_attempts, retry_delay = _proxy_retry_config()
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        _check_stop_requested()
        try:
            response = fn()
            response.raise_for_status()
            if attempt > 1:
                logger.info("[%s] 代理链路重试成功 (%s/%s)", label, attempt, max_attempts)
            return response
        except Exception as exc:
            last_exc = exc
            if not _is_retryable_authorize_error(exc) or attempt >= max_attempts:
                raise
            _reset_retryable_circuit(session)
            backoff = retry_delay * (2 ** (attempt - 1))
            logger.warning(
                "[%s] 代理链路临时失败 (%s/%s): %s HTTP=%s，保留当前会话，%.1fs 后重试",
                label, attempt, max_attempts, type(exc).__name__, _exception_http_status(exc), backoff,
            )
            _interruptible_sleep(backoff)
    raise last_exc if last_exc else RuntimeError(f"{label} 重试耗尽但无异常记录")


def _registration_preflight(session: BrowserSession) -> None:
    """初始 trace 冻结画像；有界 CSRF warmup 直接建立正式注册会话。"""
    from core.registration_service import StopRequested
    from core.registration_geo import (
        RegistrationGeoError, CATEGORY_PROXY, CATEGORY_GEO_SERVICE, CATEGORY_GEO_PARSE, STAGE_DETECT,
    )

    _check_stop_requested()
    fields = None
    try:
        logger.info("[注册预检:trace] 初始出口观测")
        response = session.get(
            "https://cloudflare.com/cdn-cgi/trace",
            headers=session.get_auth_navigate_headers(referer=""),
            timeout=15,
        )
        status = int(response.status_code)
        if status != 200:
            category = CATEGORY_PROXY if status == 407 else CATEGORY_GEO_SERVICE
            raise RegistrationGeoError(f"地域观测服务不可用：HTTP {status}", category, STAGE_DETECT)
    except StopRequested:
        raise
    except Exception as exc:
        if isinstance(exc, RegistrationGeoError):
            error = exc
        else:
            category, reason = session._classify_geo_exception(exc)
            error = RegistrationGeoError(f"地域观测不可用：{reason}", category, STAGE_DETECT)
        _reset_retryable_circuit(session)
        if not session._registration_geo_frozen:
            raise error from None
        logger.warning("[注册预检:trace] 后续观测不可用 category=%s，保留冻结画像", error.category)
    else:
        try:
            fields = dict(line.split("=", 1) for line in response.text.splitlines() if "=" in line)
            if not fields:
                raise ValueError("empty trace")
        except Exception:
            raise RegistrationGeoError("地域观测响应无法解析", CATEGORY_GEO_PARSE, STAGE_DETECT) from None
    if fields is not None:
        session.validate_registration_geo(fields)

    headers = session.get_chatgpt_navigate_headers(referer="", user_initiated=False)
    headers.update({
        "accept": "application/json",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    })
    headers.pop("upgrade-insecure-requests", None)
    attempts = max(1, int(_protocol_cfg.OPENAI_REGISTRATION_WARMUP_ATTEMPTS))
    timeout = max(1.0, float(_protocol_cfg.OPENAI_REGISTRATION_WARMUP_TIMEOUT))
    for attempt in range(1, attempts + 1):
        _check_stop_requested()
        if attempt > 1:
            _interruptible_sleep(3 + 2 * (attempt - 1))
            session.reset_preflight_transport()
        try:
            logger.info("[注册预检:warmup] 初始化服务端设备 cookie (%s/%s)", attempt, attempts)
            response = session.get(
                "https://chatgpt.com/api/auth/csrf",
                headers=headers,
                allow_redirects=True,
                timeout=timeout,
            )
            response.raise_for_status()
        except StopRequested:
            raise
        except Exception as exc:
            logger.warning("[注册预检:warmup] 请求未正常完成 (%s)，检查已接收 cookie", type(exc).__name__)
        _check_stop_requested()
        device_id = session.get_server_device_id()
        if device_id:
            session.device_id = device_id
            _reset_retryable_circuit(session)
            logger.info("[注册预检:warmup] 服务端设备身份已绑定 (%s/%s)", attempt, attempts)
            return
        logger.warning("[注册预检:warmup] 缺少有效服务端设备 cookie，剩余尝试=%s", attempts - attempt)
    raise RuntimeError(f"注册初始化失败：{attempts} 次 warmup 后仍无有效服务端 oai-did")


def network_preflight(session: BrowserSession) -> None:
    """注册使用服务端设备初始化；独立 Codex 等沿用既有预检策略。"""
    if getattr(session, "server_device_id", False) is True:
        _registration_preflight(session)
        return
    # 成功链路在 OAuth authorize 前只访问 ChatGPT 登录页；提前直打
    # auth/log-in 和 Sentinel frame 会制造浏览器中不存在的跨站访问序列。
    timeout = max(1.0, float(getattr(_protocol_cfg, "OPENAI_PREFLIGHT_TIMEOUT", 12.0)))
    checks = [
        ("chatgpt-auth-login", lambda: session.get(
            "https://chatgpt.com/auth/login",
            # 成功浏览器样本是地址栏级顶层导航：无 Referer、
            # Sec-Fetch-Site=none。伪造同源 Referer/缓存刷新头会触发 CF challenge。
            headers=session.get_chatgpt_navigate_headers(referer=""),
            allow_redirects=True,
            timeout=timeout,
        )),
    ]
    for label, fn in checks:
        resp = _request_with_proxy_retry(session, f"预检:{label}", fn)
        observe = getattr(session, "observe_chatgpt_document", None)
        if callable(observe):
            observe(resp)

    # 预检通过后再补齐 oai-did/oai-locale 一方 Cookie。预检阶段携带这些主动 Cookie
    # 会被 Cloudflare 判定异常返回 403，故在连通性确认后再写入，保持 OAuth/Sentinel
    # 设备 ID 一致性的同时不污染预检。
    seed = getattr(session, "seed_oai_cookies", None)
    if callable(seed):
        seed()


def follow_authorize(session: BrowserSession, authorize_url: str) -> str:
    """
    步骤4: 跟随 authorize URL 重定向。
    GET auth.openai.com/api/accounts/authorize?...

    这个请求会产生一系列重定向，建立 auth.openai.com 的 session cookies。
    遇到临时性网络错误（代理抽风 / TLS 握手失败 等）会自动重试。

    Args:
        session: 浏览器会话
        authorize_url: 从步骤3获取的 authorize URL
    """
    headers = session.get_auth_navigate_headers(referer="https://chatgpt.com/")

    max_attempts, retry_delay = _proxy_retry_config()
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            logger.info(f"[步骤4] 跟随 authorize URL 重定向 (尝试 {attempt}/{max_attempts})...")
            resp = session.get(authorize_url, headers=headers, allow_redirects=True)
            _rotate_document_navigation_id(session)
            resp.raise_for_status()
            final_url = str(getattr(resp, "url", "") or "")
            # 保留本次已取得的最终响应的可安全解析状态，供注册专用分类入口使用；
            # 不新增第二次 authorize 导航。落地为 HTML 文档时无结构化 page.type/mode，
            # 仅记录落地 URL 与状态码，由 get_registration_state 按路径严格判定。
            session.registration_authorize_state = {
                "landing_url": final_url,
                "status": int(getattr(resp, "status_code", 0) or 0),
                "page": _try_response_json(resp) if _looks_like_json(resp) else {},
            }
            landing = urlparse(final_url)
            logger.info(
                "[步骤4] 重定向完成, 最终落点: %s%s",
                landing.netloc, landing.path,
            )
            return final_url
        except Exception as exc:
            last_exc = exc
            if _exception_http_status(exc) == 403:
                if attempt >= max_attempts:
                    logger.warning("[步骤4] authorize HTTP 403，已达到最大重试次数")
                    raise
                _reset_retryable_circuit(session)
                backoff = retry_delay * (2 ** (attempt - 1))
                logger.warning(
                    "[步骤4] authorize HTTP 403，保留当前指纹和 Cookie，%.1fs 后重试 (%s/%s)",
                    backoff, attempt, max_attempts,
                )
                time.sleep(backoff)
                continue
            if not _is_retryable_authorize_error(exc):
                # 非临时性错误（比如 4xx 业务错误）直接抛出，不重试
                raise
            if attempt >= max_attempts:
                break
            # 首次 403 常会同时刷新 __cf_bm；保留同一个 BrowserSession/Cookie
            # Jar，只清掉本地熔断后重试，不能重建会话丢掉该 Cookie。
            _reset_retryable_circuit(session)
            backoff = retry_delay * (2 ** (attempt - 1))
            logger.warning(
                f"[步骤4] authorize 临时失败 ({type(exc).__name__}: {str(exc)[:120]})，"
                f"保留当前 session/deviceId/CF Cookie，{backoff:.1f}s 后重试..."
            )
            time.sleep(backoff)

    # 三次都失败：抛出最后一次异常
    raise last_exc if last_exc else RuntimeError("步骤4 重试耗尽但无异常记录")


def request_sentinel_token(session: BrowserSession, flow: str) -> dict:
    """当前步骤单 flow 采集、请求和求解，始终使用同一个 SDK 上下文。"""
    from core.registration_service import StopRequested

    if not session.device_id:
        raise SentinelRequestError("Sentinel 设备上下文尚未初始化")

    profile = dict(getattr(session, "browser_profile", None) or {})
    cookie = session.auth_cookie_header() if hasattr(session, "auth_cookie_header") else ""
    with SentinelSDK(
        flow=flow, device_id=session.device_id, browser_profile=profile,
        cookie=cookie, check_stop_requested=_check_stop_requested,
    ) as sdk:
        request_p = sdk.requirements()
        body = build_sentinel_request_body(request_p, session.device_id, flow)
        try:
            response = _request_with_proxy_retry(
                session, f"Sentinel challenge:{flow}",
                lambda: session.post(
                    "https://sentinel.openai.com/backend-api/sentinel/req",
                    headers=session.get_sentinel_headers(), data=body,
                ),
            )
        except StopRequested:
            raise
        except Exception as exc:
            status = _exception_http_status(exc)
            category = "sentinel_service_failed" if status else "sentinel_network_failed"
            raise SentinelRequestError(
                f"Sentinel challenge 请求失败（{type(exc).__name__}，HTTP={status}）", category
            ) from None
        try:
            challenge = response.json()
        except (TypeError, ValueError):
            raise SentinelRequestError("Sentinel challenge 响应解析失败") from None
        if not isinstance(challenge, dict) or not isinstance(challenge.get("token"), str) or not challenge["token"].strip():
            raise SentinelRequestError("Sentinel challenge 响应缺少有效 token")
        header, so_header = sdk.solve(challenge)

    result = dict(challenge)
    result.update({
        "_sentinel_flow": flow,
        "_sentinel_device_id": session.device_id,
        "_sentinel_header": header,
        "_sentinel_so_header": so_header,
    })
    logger.info("[Sentinel] 当前单 flow 已求解，flow=%s so_present=%s", flow, bool(so_header))
    return result




def build_sentinel_header(session: BrowserSession, sentinel_resp: dict, flow: str) -> tuple:
    """只消费本次已求解结果，拒绝跨 flow、设备或 challenge 混用。"""
    if (sentinel_resp.get("_sentinel_flow") != flow
            or sentinel_resp.get("_sentinel_device_id") != session.device_id):
        raise SentinelRequestError("Sentinel 凭据与当前 flow 或设备不一致")
    header = sentinel_resp.get("_sentinel_header")
    so_header = sentinel_resp.get("_sentinel_so_header")
    try:
        parsed = json.loads(header) if isinstance(header, str) and header else None
    except (TypeError, ValueError):
        parsed = None
    if (not isinstance(parsed, dict)
            or parsed.get("flow") != flow or parsed.get("id") != session.device_id
            or parsed.get("c") != sentinel_resp.get("token")):
        raise SentinelRequestError("Sentinel 主 token 缺失或不属于当前 challenge")
    pow_info = sentinel_resp.get("proofofwork")
    proof_required = not (isinstance(pow_info, dict) and pow_info.get("required") is False)
    if proof_required and (not isinstance(parsed.get("p"), str) or not parsed["p"].strip()):
        raise SentinelRequestError("Sentinel 当前 challenge 所需 proof 缺失")
    so_required = (sentinel_resp.get("so") or {}).get("required") is True
    if so_header:
        try:
            parsed_so = json.loads(so_header)
        except (TypeError, ValueError):
            raise SentinelRequestError("Sentinel SO token 无效") from None
        if (not isinstance(parsed_so, dict) or not parsed_so.get("so")
                or parsed_so.get("flow") != flow or parsed_so.get("id") != session.device_id
                or parsed_so.get("c") != sentinel_resp.get("token")):
            raise SentinelRequestError("Sentinel SO token 不属于当前 challenge")
    elif so_required:
        raise SentinelRequestError("Sentinel 服务端要求 SO token，但当前结果缺失")
    return header, so_header or None


def generate_registration_password(length: int = 14) -> str:
    """生成强密码；配置 REGISTER_PASSWORD 时优先使用。"""
    try:
        from config import register as register_cfg
        configured = str(getattr(register_cfg, "REGISTER_PASSWORD", "") or "").strip()
        if configured:
            logger.info("[密码] 使用已配置的注册密码")
            return configured
    except Exception:
        pass
    length = max(8, min(int(length), 64))
    groups = (
        "ABCDEFGHJKLMNPQRSTUVWXYZ",
        "abcdefghjkmnpqrstuvwxyz",
        "23456789",
        "!@#$%^&*?_-+=",
    )
    chars = [secrets.choice(group) for group in groups]
    alphabet = "".join(groups)
    chars.extend(secrets.choice(alphabet) for _ in range(length - len(chars)))
    random.SystemRandom().shuffle(chars)
    password = "".join(chars)
    logger.info("[密码] 已生成注册密码")
    return password


def navigate_create_account_password(
    session: BrowserSession,
    current_url: str | None = None,
) -> str:
    """进入注册密码页，确保协议注册不再走无密码 OTP 分支。"""
    current = str(current_url or "")
    if "/create-account/password" in current:
        return current

    url = "https://auth.openai.com/create-account/password"
    referer = current if current.startswith("https://auth.openai.com/") else "https://auth.openai.com/"
    headers = session.get_auth_navigate_headers(referer=referer)
    headers["sec-fetch-site"] = "same-origin"
    headers["sec-fetch-user"] = "?1"
    logger.info("[步骤5] 切换到创建账号密码页...")
    resp = session.get(url, headers=headers, allow_redirects=True)
    resp.raise_for_status()
    _rotate_document_navigation_id(session)
    final_url = str(getattr(resp, "url", "") or url)
    if "/create-account/password" not in final_url:
        raise RuntimeError(f"无法进入注册密码页，最终落点: {final_url}")
    logger.info("[步骤5] 已进入创建账号密码页")
    return final_url


def register_user(
    session: BrowserSession,
    email: str,
    password: str,
    sentinel_header: str,
    so_header: str | None = None,
) -> dict:
    """按成功样本提交邮箱和密码，返回 OTP 发送导航地址。"""
    url = "https://auth.openai.com/api/accounts/user/register"
    headers = session.get_auth_headers(referer="https://auth.openai.com/create-account/password")
    headers["openai-sentinel-token"] = sentinel_header
    if so_header:
        headers["openai-sentinel-so-token"] = so_header
    body = json.dumps({"password": password, "username": email}, separators=(",", ":"))
    logger.info("[步骤7] 提交邮箱和密码：%s", email)
    resp = session.post(url, headers=headers, data=body)
    if resp.status_code != 200:
        body_text = _response_text(resp)
        code = _extract_error_code(resp)
        if _is_explicit_registration_rejection(resp.status_code, body_text, code):
            # 保留结构化 result（可能含服务端 advertise 的 continue_url），
            # 供上层判断是否存在可继续的 OTP 路径；不得据此擅自回退。
            raise PasswordRegistrationRejectedError(
                f"设置密码被服务端明确拒绝: status={resp.status_code} code={code or 'unknown'}",
                result=_try_response_json(resp),
            )
        logger.error("[步骤7] user/register 失败 status=%s", resp.status_code)
        resp.raise_for_status()
    data = resp.json()
    return data


def navigate_email_otp_send(session: BrowserSession, continue_url: str | None = None) -> str:
    """跟随 user/register 返回地址发送 OTP，并建立新的验证页 document 状态。"""
    url = str(continue_url or "https://auth.openai.com/api/accounts/email-otp/send")
    if url.startswith("/"):
        url = "https://auth.openai.com" + url
    headers = session.get_auth_navigate_headers(referer="https://auth.openai.com/create-account/password")
    headers["sec-fetch-site"] = "same-origin"
    headers["sec-fetch-user"] = "?1"
    _rotate_document_navigation_id(session)
    resp = session.get(url, headers=headers, allow_redirects=True)
    resp.raise_for_status()
    final_url = str(getattr(resp, "url", "") or "")
    if "/email-verification" not in final_url:
        raise RuntimeError(f"OTP 发送导航落点异常: {final_url}")
    return final_url





def send_email_otp(session: BrowserSession, referer: str = "https://auth.openai.com/email-verification") -> None:
    """重新发送邮箱验证码。用于验证码错误/过期后重新取码。"""
    url = "https://auth.openai.com/api/accounts/email-otp/send"
    headers = session.get_auth_navigate_headers(referer=referer)
    headers["sec-fetch-site"] = "same-origin"
    headers["sec-fetch-user"] = "?1"
    logger.info("[OTP] 请求重新发送邮箱验证码...")
    _rotate_document_navigation_id(session)
    resp = session.get(url, headers=headers, allow_redirects=True)
    if resp.status_code >= 400:
        logger.warning("[OTP] 重新发送验证码失败 status=%s", resp.status_code)
        resp.raise_for_status()
    logger.info("[OTP] 重新发送验证码请求完成，status=%s", resp.status_code)


def validate_email_otp(session: BrowserSession, code: str, sentinel_header: str | None = None, so_header: str | None = None) -> dict:
    """
    步骤10: 提交邮箱验证码验证。
    POST https://auth.openai.com/api/accounts/email-otp/validate

    Args:
        session: 浏览器会话
        code: 6位数字验证码
        sentinel_header: openai-sentinel-token 头的值（email_otp_validate flow）

    Returns:
        验证响应 JSON，例如:
        {
            "continue_url": "https://auth.openai.com/about-you",
            "method": "GET",
            "page": {"type": "about_you", "backstack_behavior": "default"}
        }
    """
    url = "https://auth.openai.com/api/accounts/email-otp/validate"

    headers = session.get_auth_headers(referer="https://auth.openai.com/email-verification")
    if sentinel_header:
        headers["openai-sentinel-token"] = sentinel_header
    if so_header:
        headers["openai-sentinel-so-token"] = so_header
        logger.info("[步骤10] 已添加 openai-sentinel-so-token 头")

    body = json.dumps({"code": code})

    logger.info(
        "[步骤10] 提交邮箱验证码: present=%s length=%s",
        bool(code), len(code) if code else 0,
    )
    resp = session.post(url, headers=headers, data=body)

    if resp.status_code != 200:
        logger.error(f"[步骤10] 请求失败, 状态码: {resp.status_code}")
        # 先看是不是"账号已废"——这类邮箱再试也没用，单独抛出让上层标 failed
        err_code = _extract_error_code(resp)
        if err_code in _ACCOUNT_DEAD_CODES:
            raise AccountUnusableError(
                f"账号已废弃（{err_code}），邮箱不可再用", error_code=err_code,
            )
        low = (resp.text or '').lower()
        if resp.status_code in (400, 401, 422) and any(k in low for k in (
            'invalid', 'incorrect', 'expired', 'code', 'otp', 'verification',
            '验证码', '認証コード', '確認コード', 'コード'
        )):
            raise EmailOtpInvalidError(f"邮箱验证码无效或已过期: status={resp.status_code}")
        resp.raise_for_status()

    data = resp.json()
    page_type = data.get('page', {}).get('type')
    logger.info(f"[步骤10] 验证码验证成功: {page_type}")
    logger.info("[步骤10] 后续地址存在: %s", bool(data.get("continue_url")))
    return data


def create_account(session: BrowserSession, name: str, birthday: str, sentinel_header: str, so_header: str = None) -> dict:
    """
    步骤12: 提交用户信息，完成注册。
    POST https://auth.openai.com/api/accounts/create_account

    Args:
        session: 浏览器会话
        name: 用户显示名称
        birthday: 生日，格式 "YYYY-MM-DD"
        sentinel_header: openai-sentinel-token 头的值
        so_header: openai-sentinel-so-token 头的值

    Returns:
        创建账号响应 JSON
    """
    url = "https://auth.openai.com/api/accounts/create_account"

    headers = session.get_auth_headers(referer="https://auth.openai.com/about-you")
    headers["openai-sentinel-token"] = sentinel_header
    if so_header:
        headers["openai-sentinel-so-token"] = so_header
        logger.info(f"[步骤12] 已添加 openai-sentinel-so-token 头")

    body = json.dumps({
        "name": name,
        "birthdate": birthday,
    })

    logger.info(f"[步骤12] 提交用户信息, 名称: {name}, 生日: {birthday}")
    resp = session.post(url, headers=headers, data=body)

    if resp.status_code != 200:
        logger.error(f"[步骤12] 请求失败, 状态码: {resp.status_code}")
        resp.raise_for_status()

    data = resp.json()
    log_cookies = getattr(session, "log_cookie_names", None)
    if callable(log_cookies):
        log_cookies("create_account_complete")
    logger.info("[步骤12] 创建接口返回成功，等待 OAuth 回调建立登录态")
    return data


# ============================================================
# 账号状态识别与已有账号登录（协议分流）
# ============================================================

_AUTH_HOSTS = frozenset({"auth.openai.com", "chatgpt.com"})


def _url_host_path(url: str) -> tuple[str, str]:
    try:
        parsed = urlparse(str(url or ""))
    except Exception:
        return "", ""
    return (parsed.hostname or "").lower(), (parsed.path or "")


def get_auth_continue_url(result) -> str:
    """从 auth step 响应统一提取继续地址；main 只调用此处，不复制字段探测。"""
    if not isinstance(result, dict):
        return ""
    page = result.get("page") or {}
    page = page if isinstance(page, dict) else {}
    return str(
        result.get("continue_url")
        or result.get("external_url")
        or result.get("url")
        or page.get("continue_url")
        or page.get("external_url")
        or page.get("url")
        or ""
    ).strip()


def _step_from_page_type(page_type: str) -> str:
    pt = (page_type or "").strip().lower()
    if pt == "mfa_challenge":
        return "totp"
    if pt in ("email_otp_verification", "email_otp_send"):
        return "otp"
    if pt in ("login_password", "create_account_password"):
        return "password"
    if pt in ("about_you", "about-you"):
        return "profile"
    if pt == "external_url":
        return "callback"
    return "unknown"


def _step_from_continue_url(continue_url: str) -> str:
    cu = str(continue_url or "").strip()
    if not cu:
        return "unknown"
    host, path = _url_host_path(cu)
    if host and host not in _AUTH_HOSTS:
        return "unknown"
    low_path = path.lower()
    if "/mfa-challenge/" in low_path:
        return "totp"
    if ("/email-verification" in low_path
            or "/email-otp/send" in low_path
            or "/email-otp/resend" in low_path):
        return "otp"
    if "/log-in/password" in low_path or "/create-account/password" in low_path:
        return "password"
    if "/about-you" in low_path:
        return "profile"
    if host == "chatgpt.com" or "/authorize/continue" in low_path:
        return "callback"
    return "unknown"


def get_auth_step(result) -> str:
    """从 auth step 响应判定下一步认证因子，并校验 host/path 与证据冲突。

    返回 password/otp/totp/profile/callback/unknown；page.type 与 continue_url
    都明确但指向不同因子时判 unknown（证据冲突），不猜测。
    """
    if not isinstance(result, dict):
        return "unknown"
    page = result.get("page") or {}
    page = page if isinstance(page, dict) else {}
    page_type = str(page.get("type") or "").strip().lower()
    continue_url = get_auth_continue_url(result)

    # continue_url 存在但主机不在允许域内 → 严格 unknown，不允许已知 page.type 独断。
    if continue_url:
        host, _path = _url_host_path(continue_url)
        if host and host not in _AUTH_HOSTS:
            return "unknown"

    page_step = _step_from_page_type(page_type)
    url_step = _step_from_continue_url(continue_url)
    if page_step != "unknown" and url_step != "unknown" and page_step != url_step:
        return "unknown"
    return page_step if page_step != "unknown" else url_step


def _classify_landing_path(landing_url: str, intent: str = "signup") -> tuple[str, str, bool]:
    """按 OSS 的 authorize 落地 URL 子串及 intent 分类。"""
    low_url = str(landing_url or "").strip().lower()
    if "/create-account" in low_url or "/about-you" in low_url:
        return "new_password", "password", False
    if "/log-in" in low_url:
        if "/log-in/password" in low_url:
            return "existing", "password", False
        return "existing", "otp", True
    if "/email-verification" in low_url:
        if (str(intent or "signup").strip().lower() or "signup") == "signup":
            return "new_password", "password", False
        return "existing", "otp", True
    return "unknown", "unknown", False


def _registration_state(
    kind: str, step: str, page: dict, continue_url: str, source: str,
    otp_delivery_required: bool = False,
) -> dict:
    return {
        "kind": kind,
        "step": step,
        "page": page,
        "continue_url": continue_url,
        "classification_source": source,
        "otp_delivery_required": otp_delivery_required,
    }


def get_registration_state(session, landing_url, intent="signup", page_state=None) -> dict:
    """仅按 OSS 落地 URL 分类；未知落地由 authorize_continue 探测。"""
    authorize_state = getattr(session, "registration_authorize_state", None)
    authorize_state = authorize_state if isinstance(authorize_state, dict) else {}
    page = page_state if isinstance(page_state, dict) else authorize_state.get("page") or {}
    page = page if isinstance(page, dict) else {}
    landing = str(landing_url or authorize_state.get("landing_url") or "").strip()
    kind, step, otp_delivery_required = _classify_landing_path(landing, intent)
    source = "landing_url" if kind != "unknown" else "unclassified_landing"
    return _registration_state(kind, step, page, landing, source, otp_delivery_required)


def _classify_authorize_probe(data: dict, *, strict: bool = False) -> dict:
    """按 OSS authorize_continue 响应映射认证状态；strict 用于拒绝未知慢路径落点。"""
    page = data.get("page") if isinstance(data.get("page"), dict) else {}
    payload = page.get("payload") if isinstance(page.get("payload"), dict) else {}
    page_type = str(page.get("type") or "").strip().lower()
    mode = str(payload.get("email_verification_mode") or "").strip().lower()
    continue_url = get_auth_continue_url(data)
    low_url = continue_url.lower()

    if page_type == "create_account_password" or "/create-account/password" in low_url:
        kind, step, otp_delivery_required = "new_password", "password", False
    elif page_type == "email_otp_verification":
        if mode == "passwordless_signup":
            kind, step, otp_delivery_required = "new_passwordless", "otp", False
        else:
            kind, step = "existing", "otp"
            otp_delivery_required = mode != "passwordless_login"
    elif page_type == "login_password" or "/log-in/password" in low_url:
        kind, step, otp_delivery_required = "existing", "password", False
    elif page_type == "mfa_challenge":
        kind, step, otp_delivery_required = "existing", "totp", False
    elif strict:
        kind, step, otp_delivery_required = "existing", "unknown", False
    else:
        # 注册兼容路径保留 OSS 对未知 page.type 的 OTP 处理方式。
        kind, step, otp_delivery_required = "existing", "otp", True

    return _registration_state(
        kind, step, page, continue_url, "authorize_continue", otp_delivery_required,
    )


def probe_registration_state(session, email: str, *, strict: bool = False) -> dict:
    """用 authorize_continue 探测落点；strict 慢路径拒绝无效或未知响应。"""
    sentinel_resp = request_sentinel_token(session, "authorize_continue")
    sentinel_header, so_header = build_sentinel_header(
        session, sentinel_resp, "authorize_continue",
    )
    headers = session.get_auth_headers(referer="https://auth.openai.com/create-account")
    headers["accept"] = "application/json"
    headers["content-type"] = "application/json"
    headers["openai-sentinel-token"] = sentinel_header
    if so_header:
        headers["openai-sentinel-so-token"] = so_header
    response = session.post(
        "https://auth.openai.com/api/accounts/authorize/continue",
        headers=headers,
        data=json.dumps({"username": {"kind": "email", "value": email}}),
        allow_redirects=False,
    )
    status = int(getattr(response, "status_code", 0) or 0)
    if status not in (200, 204):
        raise RuntimeError(f"authorize_continue 状态探测失败: HTTP {status}")

    try:
        data = response.json()
    except Exception:
        if strict:
            raise RuntimeError("authorize_continue 响应无法解析") from None
        return _registration_state("new_password", "password", {}, "", "probe_parse_fallback")
    if not isinstance(data, dict):
        if strict:
            raise RuntimeError("authorize_continue 响应结构无效")
        return _registration_state("new_password", "password", {}, "", "probe_parse_fallback")
    state = _classify_authorize_probe(data, strict=strict)
    if strict and state["step"] == "unknown":
        raise RuntimeError("authorize_continue 返回未知登录落点")
    return state




def _extract_factor_id(result, continue_url: str) -> str:
    """从 step 响应提取 MFA factor_id：优先 page.payload.factor_id，回退 continue_url。"""
    if isinstance(result, dict):
        page = result.get("page") or {}
        if isinstance(page, dict):
            payload = page.get("payload") or {}
            if isinstance(payload, dict):
                factor_id = str(payload.get("factor_id") or "").strip()
                if factor_id:
                    return factor_id
    cu = str(continue_url or "")
    if "/mfa-challenge/" in cu:
        return cu.rstrip("/").rsplit("/", 1)[-1]
    return ""


def _totp_code(secret: str) -> str:
    try:
        return pyotp.TOTP(str(secret or "")).now()
    except Exception:
        return ""


def _mfa_issue_challenge(session, factor_id: str) -> dict:
    headers = session.get_auth_headers(referer="https://auth.openai.com/")
    headers["accept"] = "*/*"
    headers.pop("openai-sentinel-token", None)
    headers.pop("openai-sentinel-so-token", None)
    resp = session.post(
        "https://auth.openai.com/api/accounts/mfa/issue_challenge",
        headers=headers,
        data=json.dumps({"id": factor_id, "type": "totp", "force_fresh_challenge": False}),
        allow_redirects=False,
    )
    if resp.status_code >= 400:
        _raise_for_account_unusable_response(resp)
        resp.raise_for_status()
    return resp.json()


def _mfa_verify(session, factor_id: str, code: str) -> dict:
    flow = "verify_mfa"
    sentinel_resp = request_sentinel_token(session, flow)
    sentinel_header, so_header = build_sentinel_header(session, sentinel_resp, flow)
    headers = session.get_auth_headers(referer="https://auth.openai.com/")
    headers["accept"] = "application/json"
    headers["openai-sentinel-token"] = sentinel_header
    if (sentinel_resp.get("so") or {}).get("required") is True:
        headers["openai-sentinel-so-token"] = so_header
    resp = session.post(
        "https://auth.openai.com/api/accounts/mfa/verify",
        headers=headers,
        data=json.dumps({"id": factor_id, "type": "totp", "code": code}),
        allow_redirects=False,
    )
    if resp.status_code >= 400:
        _raise_for_account_unusable_response(resp)
        resp.raise_for_status()
    return resp.json()


def verify_login_password(
    session, password: str,
) -> dict:
    """密码验证；每次为 password_verify flow 取得独立 Sentinel 凭据。"""
    flow = "password_verify"
    sentinel_resp = request_sentinel_token(session, flow)
    sentinel_header, so_header = build_sentinel_header(session, sentinel_resp, flow)
    headers = session.get_auth_headers(referer="https://auth.openai.com/")
    headers["accept"] = "application/json"
    headers["openai-sentinel-token"] = sentinel_header
    if (sentinel_resp.get("so") or {}).get("required") is True:
        headers["openai-sentinel-so-token"] = so_header
    resp = session.post(
        "https://auth.openai.com/api/accounts/password/verify",
        headers=headers,
        data=json.dumps({"password": password}),
        allow_redirects=False,
    )
    if resp.status_code >= 400:
        body_text = _response_text(resp)
        code = _extract_error_code(resp)
        _raise_for_account_unusable_response(resp)
        if _is_explicit_password_rejection(resp.status_code, body_text, code):
            raise PasswordRejectedError(
                f"登录密码被服务端明确拒绝: status={resp.status_code} code={code or 'unknown'}",
                result=_try_response_json(resp),
            )
        resp.raise_for_status()
    return resp.json()




def _login_otp_resend(session) -> dict:
    """POST /api/accounts/email-otp/resend：复用当前 challenge 重发，不切换 state。"""
    url = "https://auth.openai.com/api/accounts/email-otp/resend"
    headers = session.get_auth_headers(referer="https://auth.openai.com/email-verification")
    logger.info("[登录] 复用当前 challenge 重发邮箱 OTP")
    resp = session.post(url, headers=headers, allow_redirects=False)
    if resp.status_code >= 400:
        resp.raise_for_status()
    return {
        "page": {"type": "email_otp_verification", "payload": {}},
        "continue_url": "https://auth.openai.com/email-verification",
    }


def _login_otp_follow_send(session, continue_url: str) -> dict:
    """跟随服务端指示的 email-otp/send 继续路径触发新发码（password/verify 后）。"""
    url = str(continue_url or "")
    if url.startswith("/"):
        url = urljoin("https://auth.openai.com", url)
    headers = session.get_auth_navigate_headers(referer="https://auth.openai.com/log-in/password")
    headers["sec-fetch-site"] = "same-origin"
    headers["sec-fetch-user"] = "?1"
    logger.info("[登录] 跟随服务端指示的 email-otp/send 发送验证码")
    _rotate_document_navigation_id(session)
    resp = session.get(url, headers=headers, allow_redirects=True)
    resp.raise_for_status()
    final_url = str(getattr(resp, "url", "") or "")
    return {
        "page": {"type": "email_otp_verification", "payload": {}},
        "continue_url": final_url or "https://auth.openai.com/email-verification",
    }


def _is_login_otp_send_path(continue_url: str) -> bool:
    """continue_url 是否明确指向登录路径的 email-otp/send（校验 host + 精确 path）。

    相对路径（无 host）视为 auth.openai.com 同源，同样按精确 path 校验。
    """
    cu = str(continue_url or "").strip()
    if not cu:
        return False
    host, path = _url_host_path(cu)
    if host and host != "auth.openai.com":
        return False
    return path.rstrip("/").endswith("/email-otp/send")


def request_login_otp(session, result, *, resend: bool = False) -> dict:
    """登录路径邮箱 OTP 投递/重发，返回规范化下一步状态。

    - resend=True：POST /api/accounts/email-otp/resend（复用当前 challenge）。
    - resend=False：仅当 continue_url 明确指向 /email-otp/send 时才 GET 该地址
      （服务端指示的新发码）；否则仅对已知 password/otp 步骤 POST resend 复用
      challenge，绝不无条件新建 challenge。
    - unknown/profile/callback 或无可支持路径 → 明确失败，不发任何请求。
    """
    continue_url = get_auth_continue_url(result)
    step = get_auth_step(result)
    has_send_path = _is_login_otp_send_path(continue_url)

    if resend:
        if step not in ("password", "otp") and not has_send_path:
            raise RuntimeError(f"OTP 重发不支持当前步骤: step={step}")
        return _login_otp_resend(session)

    if has_send_path:
        return _login_otp_follow_send(session, continue_url)

    if step in ("password", "otp"):
        return _login_otp_resend(session)

    raise RuntimeError(f"OTP 投递无支持路径: step={step}")


def verify_login_totp(session, result, secret: str) -> dict:
    """密码/邮箱 OTP 后识别并完成 TOTP 校验。缺 factor_id 或缺 secret 即失败。"""
    continue_url = get_auth_continue_url(result)
    factor_id = _extract_factor_id(result, continue_url)
    if not factor_id:
        raise RuntimeError("进入 MFA 但未拿到 factor_id")
    secret = str(secret or "").strip()
    if not secret:
        raise RuntimeError("进入 MFA 但账号没有 totp_secret，无法生成动态码")
    _mfa_issue_challenge(session, factor_id)
    code = _totp_code(secret)
    if not code:
        raise RuntimeError("无法生成 TOTP 动态码")
    logger.info("[登录] 提交 TOTP 动态码，factor_id_present=%s", bool(factor_id))
    return _mfa_verify(session, factor_id, code)


def get_mfa_enabled(session, access_token: str) -> bool:
    """读取服务端 MFA 状态。仅接受布尔 true/false；其他类型/缺失/解析失败 raise 未知。"""
    token = str(access_token or "").strip()
    if not token:
        raise RuntimeError("查询 MFA 状态缺少 access_token")
    url = "https://chatgpt.com/backend-api/accounts/mfa_info"
    headers = session.get_chatgpt_headers(referer="https://chatgpt.com/")
    headers["authorization"] = f"Bearer {token}"
    headers["oai-device-id"] = session.device_id
    headers["oai-language"] = session.navigator_language()
    resp = session.get(url, headers=headers)
    if resp.status_code != 200:
        resp.raise_for_status()
    try:
        payload = resp.json()
    except Exception:
        raise RuntimeError("mfa_info 响应无法解析，无法确认 2FA 状态") from None
    if not isinstance(payload, dict) or "mfa_enabled" not in payload:
        raise RuntimeError("mfa_info 响应缺少 mfa_enabled 字段，无法确认 2FA 状态")
    value = payload.get("mfa_enabled")
    if value is True:
        return True
    if value is False:
        return False
    raise RuntimeError("mfa_info 的 mfa_enabled 值不明确，无法确认 2FA 状态")
