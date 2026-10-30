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
            resp.raise_for_status()
            final_url = str(getattr(resp, "url", "") or "")
            _rotate_document_navigation_id(session)
            logger.info(f"[步骤4] 重定向完成, 最终URL: {final_url}")
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
    iframe_flow = flow == "username_password_create"
    context_name = "password" if iframe_flow else "top_level"
    ready_contexts = getattr(session, "_sentinel_frame_contexts", None)
    if not isinstance(ready_contexts, set):
        ready_contexts = set()
        session._sentinel_frame_contexts = ready_contexts
    if context_name not in ready_contexts:
        from config import SENTINEL_SV
        frame_url = f"https://sentinel.openai.com/backend-api/sentinel/frame.html?sv={SENTINEL_SV}"
        _request_with_proxy_retry(
            session,
            f"Sentinel iframe:{flow}",
            lambda: session.get(
                frame_url,
                headers=session.get_sentinel_frame_headers(user_initiated=iframe_flow),
                allow_redirects=True,
            ),
        )
        ready_contexts.add(context_name)

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
    resp = session.get(url, headers=headers, allow_redirects=True)
    resp.raise_for_status()
    _rotate_document_navigation_id(session)
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
    resp = session.get(url, headers=headers, allow_redirects=True)
    if resp.status_code >= 400:
        logger.warning("[OTP] 重新发送验证码失败 status=%s", resp.status_code)
        resp.raise_for_status()
    _rotate_document_navigation_id(session)
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
