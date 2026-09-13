# -*- coding: utf-8 -*-
"""
OpenAI Auth 模块
处理 auth.openai.com 域名下的注册请求（步骤4-5、7-8、10、12）
以及 sentinel.openai.com 的 sentinel token 请求（步骤6、9、11）
"""
import json
import logging
import time
from urllib.parse import parse_qsl, urlsplit

from core.session import BrowserSession
from core.sentinel import (
    generate_requirements_token,
    build_sentinel_request_body,
)
from core.sentinel_runner import generate_sentinel_token
from core.sentinel_runner import validate_sentinel_resource_consistency

logger = logging.getLogger(__name__)


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


def _safe_url_label(url: str) -> str:
    """仅保留 URL 的 origin/path 和 query 字段名，避免日志泄露认证参数。"""
    try:
        parsed = urlsplit(str(url or ""))
        keys = sorted({key for key, _ in parse_qsl(parsed.query, keep_blank_values=True)})
        suffix = f" query_keys={keys}" if keys else ""
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}{suffix}" or "<empty-url>"
    except Exception:
        return "<invalid-url>"


def _response_summary(resp) -> str:
    """返回只含状态/字段名/存在性/长度的响应摘要。"""
    status = int(getattr(resp, "status_code", 0) or 0)
    text = str(getattr(resp, "text", "") or "")
    try:
        payload = resp.json()
    except Exception:
        return f"status={status} body_present={bool(text)} body_length={len(text)}"
    if isinstance(payload, dict):
        return f"status={status} json_fields={sorted(str(key) for key in payload)}"
    return f"status={status} json_type={type(payload).__name__}"


def _attach_auth_document_navigation_header(session: BrowserSession, headers: dict) -> dict:
    """只在当前会话捕获到真实值时为目标 Auth JSON 请求补头。"""
    attach = getattr(session, "attach_auth_document_navigation_header", None)
    if callable(attach):
        return attach(headers)
    return headers


def _validate_authorize_url(authorize_url: str) -> None:
    """确保 protocol 的 authorize 入口始终是 auth.openai.com 的 GET 端点。"""
    parsed = urlsplit(str(authorize_url or ""))
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() != "auth.openai.com"
        or parsed.path != "/api/accounts/authorize"
    ):
        raise ValueError(
            "authorize URL 必须是 "
            "https://auth.openai.com/api/accounts/authorize?...，"
            f"实际为 {_safe_url_label(authorize_url)}"
        )


# 步骤4 网络层临时性错误（代理抽风 / TLS 握手失败 / 重置等）的重试参数
_FOLLOW_AUTH_MAX_ATTEMPTS = 3
_FOLLOW_AUTH_BACKOFF_BASE = 2.0  # 第 N 次重试前等 2^(N-1) 秒
_SENTINEL_MAX_ATTEMPTS = 3
_SENTINEL_FLOWS = frozenset({
    "authorize_continue",
    "oauth_create_account",
})


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
        "curl: (35)",
        "curl: (52)",                # empty reply from server
        "curl: (56)",                # network recv failure
    )
    return any(k in msg for k in transient_keywords)


def network_preflight(session: BrowserSession) -> None:
    """
    注册前网络预检：只建立边缘节点/cookie/基础连通性，不携带邮箱、不触发 OTP。

    这样真正会“烧邮箱”的 authorize 重定向发生前，已经确认当前代理、TLS
    impersonate、ChatGPT/Auth/Sentinel 三段链路都可达。
    """
    validate_sentinel_resource_consistency()
    from config import SENTINEL_FRAME_URL

    checks = [
        ("chatgpt-login", lambda: session.get(
            "https://chatgpt.com/login",
            headers=session.get_chatgpt_navigate_headers(referer="https://chatgpt.com/"),
            allow_redirects=True,
        )),
        ("auth-login", lambda: session.get(
            "https://auth.openai.com/log-in",
            headers=session.get_auth_navigate_headers(referer="https://chatgpt.com/login"),
            allow_redirects=True,
        )),
        ("sentinel-frame", lambda: session.get(
            SENTINEL_FRAME_URL,
            headers=session.get_auth_navigate_headers(referer="https://auth.openai.com/log-in", target_origin="https://sentinel.openai.com"),
            allow_redirects=True,
        )),
    ]
    for label, fn in checks:
        last_exc = None
        for attempt in range(1, _FOLLOW_AUTH_MAX_ATTEMPTS + 1):
            try:
                logger.info(f"[预检] {label} ({attempt}/{_FOLLOW_AUTH_MAX_ATTEMPTS})")
                resp = fn()
                if getattr(resp, "status_code", 0) >= 400:
                    raise RuntimeError(
                        f"{label} status={resp.status_code}, "
                        f"body_present={bool(getattr(resp, 'text', '') or '')}"
                    )
                break
            except Exception as exc:
                last_exc = exc
                if not _is_transient_network_error(exc) or attempt >= _FOLLOW_AUTH_MAX_ATTEMPTS:
                    raise
                backoff = _FOLLOW_AUTH_BACKOFF_BASE ** (attempt - 1)
                logger.warning(
                    "[预检] %s 临时失败：%s，%s 后重试",
                    label,
                    type(exc).__name__,
                    f"{backoff:.1f}s",
                )
                time.sleep(backoff)
        else:
            raise last_exc if last_exc else RuntimeError(f"[预检] {label} 未完成")


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
    _validate_authorize_url(authorize_url)
    headers = session.get_auth_navigate_headers(referer="https://chatgpt.com/")

    last_exc: Exception | None = None
    for attempt in range(1, _FOLLOW_AUTH_MAX_ATTEMPTS + 1):
        try:
            logger.info(f"[步骤4] 跟随 authorize URL 重定向 (尝试 {attempt}/{_FOLLOW_AUTH_MAX_ATTEMPTS})...")
            resp = session.get(authorize_url, headers=headers, allow_redirects=True)
            resp.raise_for_status()
            final_url = str(getattr(resp, "url", "") or "")
            if "/api/accounts/user/register" in final_url or "/create-account/password" in final_url:
                raise RuntimeError(
                    "[步骤4] 落入旧密码注册路径，已拒绝继续烧邮箱: "
                    f"{_safe_url_label(final_url)}"
                )
            logger.info("[步骤4] 重定向完成, 最终URL: %s", _safe_url_label(final_url))
            return final_url
        except Exception as exc:
            last_exc = exc
            if not _is_transient_network_error(exc):
                # 非临时性错误（比如 4xx 业务错误）直接抛出，不重试
                raise
            if attempt >= _FOLLOW_AUTH_MAX_ATTEMPTS:
                break
            backoff = _FOLLOW_AUTH_BACKOFF_BASE ** (attempt - 1)
            logger.warning(
                "[步骤4] 临时性网络错误 (%s)，%s 后重试...",
                type(exc).__name__,
                f"{backoff:.1f}s",
            )
            time.sleep(backoff)

    # 三次都失败：抛出最后一次异常
    raise last_exc if last_exc else RuntimeError("步骤4 重试耗尽但无异常记录")


def request_sentinel_token(session: BrowserSession, flow: str) -> dict:
    """
    步骤6/9/11: 请求 Sentinel Token。
    POST https://sentinel.openai.com/backend-api/sentinel/req

    Args:
        session: 浏览器会话
        flow: 流程类型
            - "username_password_create": 步骤6
            - "authorize_continue": 步骤9
            - "oauth_create_account": 步骤11

    Returns:
        sentinel 响应 JSON，包含 token、turnstile、proofofwork 等
    """
    if flow not in _SENTINEL_FLOWS:
        raise ValueError(f"不支持的 protocol Sentinel flow: {flow}")

    validate_sentinel_resource_consistency()
    url = "https://sentinel.openai.com/backend-api/sentinel/req"
    last_exc: Exception | None = None
    for attempt in range(1, _SENTINEL_MAX_ATTEMPTS + 1):
        # 每次尝试都重新生成 p/challenge，禁止复用已失败或已过期的上下文。
        p = generate_requirements_token(
            getattr(session, "sentinel_sid", session.device_id),
            profile=getattr(session, "browser_profile", None),
        )
        body = build_sentinel_request_body(p, session.device_id, flow)
        headers = session.get_sentinel_headers()

        logger.info(
            "[Sentinel] 请求 challenge, flow=%s, attempt=%s/%s",
            flow,
            attempt,
            _SENTINEL_MAX_ATTEMPTS,
        )
        try:
            resp = session.post(url, headers=headers, data=body)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            last_exc = exc
            if attempt >= _SENTINEL_MAX_ATTEMPTS or not _is_transient_network_error(exc):
                raise
            backoff = _FOLLOW_AUTH_BACKOFF_BASE ** (attempt - 1)
            logger.warning(
                "[Sentinel] challenge 传输失败，flow=%s，%s 后重试：%s",
                flow,
                f"{backoff:.1f}s",
                type(exc).__name__,
            )
            time.sleep(backoff)
            continue

        if not isinstance(data, dict):
            raise RuntimeError(f"Sentinel challenge 响应格式异常: type={type(data).__name__}")

        pow_data = data.get("proofofwork") or {}
        turnstile_data = data.get("turnstile") or {}
        so_data = data.get("so") or {}
        requires = []
        if turnstile_data.get("required"):
            requires.append("turnstile")
        if so_data.get("required"):
            requires.append("so")
        if pow_data.get("required"):
            requires.append("pow")
        logger.info(
            "[Sentinel] challenge 获取成功, flow=%s, requires=%s, has_persona=%s, "
            "has_pow_seed=%s, pow_difficulty_length=%s",
            flow,
            requires or "无",
            bool(data.get("persona")),
            bool(pow_data.get("seed")),
            len(str(pow_data.get("difficulty") or "")),
        )
        return data

    raise last_exc if last_exc else RuntimeError(f"Sentinel challenge 未完成: flow={flow}")


def build_sentinel_header(session: BrowserSession, sentinel_resp: dict, flow: str) -> tuple:
    """
    根据 sentinel 响应构建 openai-sentinel-token 和 openai-sentinel-so-token 请求头值。

    实现策略：把 challenge 喂给 sentinel-runner.js（Node + sdk.js 在 vm 沙箱中执行），
    让真实 SDK 自己产出包含 turnstile / so / pow 的最终 token，避免硬塞 dx 被风控拒绝。

    Args:
        session: 浏览器会话（提供 device_id 与 user_agent，必须与后续 HTTP 请求保持一致）
        sentinel_resp: sentinel/req 的响应 JSON
        flow: 流程类型，必须与请求 challenge 时传入的 flow 完全一致

    Returns:
        (sentinel_header, so_header) 元组
        sentinel_header: openai-sentinel-token 请求头的值（runner 直接产出的 JSON 字符串）
        so_header: openai-sentinel-so-token 请求头的值（若 SDK 输出含 so 字段则填充，否则为 None）
    """
    if flow not in _SENTINEL_FLOWS:
        raise ValueError(f"不支持的 protocol Sentinel flow: {flow}")
    if not isinstance(sentinel_resp, dict):
        raise TypeError("Sentinel challenge 必须是 JSON 对象")

    from config import USER_AGENT

    header_value = generate_sentinel_token(
        challenge=sentinel_resp,
        flow=flow,
        device_id=session.device_id,
        user_agent=(getattr(session, "browser_profile", {}) or {}).get("user_agent") or USER_AGENT,
        browser_profile=getattr(session, "browser_profile", None),
        sentinel_sid=getattr(session, "sentinel_sid", None),
        react_listening_key=getattr(session, "react_listening_key", None),
        react_container_key=getattr(session, "react_container_key", None),
        react_resources_key=getattr(session, "react_resources_key", None),
        cookie=session.auth_cookie_header() if hasattr(session, "auth_cookie_header") else f"oai-did={session.device_id}",
    )

    try:
        parsed = json.loads(header_value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Sentinel runner 输出不是合法 JSON") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("Sentinel runner 输出必须是 JSON 对象")
    if parsed.get("id") != session.device_id:
        raise RuntimeError("Sentinel token 的 device id 与当前 BrowserSession 不一致")
    if parsed.get("flow") != flow:
        raise RuntimeError("Sentinel token 的 flow 与目标注册步骤不一致")
    if not parsed.get("c"):
        raise RuntimeError("Sentinel token 缺少 enforcement token")

    # 解析 runner 输出，单独抽出 so 字段填充 openai-sentinel-so-token。
    so_header = None
    so_value = parsed.get("so")
    if so_value:
        so_header = json.dumps(
            {
                "so": so_value,
                "c": parsed["c"],
                "id": session.device_id,
                "flow": flow,
            },
            separators=(',', ':'),
        )
        logger.info("[Sentinel] 检测到 SO 字段，已构建 so-token 头")

    return header_value, so_header


def get_fresh_sentinel_headers(session: BrowserSession, flow: str) -> tuple[str, str | None]:
    """为一个注册业务请求获取同一会话、同一 flow 的 fresh Sentinel headers。"""
    challenge = request_sentinel_token(session, flow)
    return build_sentinel_header(session, challenge, flow)


# ============================================================
# 密码分支专用函数（已停用，保留作备用）
# 当前 OpenAI 主流程：follow_authorize 自动跳到 /email-verification 并发 OTP，
# 不再走密码注册路径。如未来需要恢复密码注册（点击"使用密码继续"按钮的分支），
# 可参考下方实现解封即可。
# ============================================================

# def get_create_account_page(session: BrowserSession) -> None:
#     """
#     [备用] 步骤5: 访问创建账号-密码页面（密码分支）。
#     GET https://auth.openai.com/create-account/password
#     """
#     url = "https://auth.openai.com/create-account/password"
#     headers = session.get_auth_navigate_headers(referer="https://auth.openai.com/email-verification")
#     headers["sec-fetch-site"] = "same-origin"
#
#     logger.info("[步骤5] 访问创建账号-密码页（切换密码分支）...")
#     resp = session.get(url, headers=headers, allow_redirects=True)
#     resp.raise_for_status()
#     logger.info(f"[步骤5] 创建账号-密码页访问成功, 落点: {resp.url}")


# def register_user(session: BrowserSession, email: str, password: str, sentinel_header: str) -> dict:
#     """
#     [备用] 步骤7: 提交注册请求（邮箱+密码）。
#     POST https://auth.openai.com/api/accounts/user/register
#
#     Returns:
#         注册响应 JSON，例如:
#         {
#             "continue_url": "https://auth.openai.com/api/accounts/email-otp/send",
#             "method": "GET",
#             "page": {"type": "email_otp_send", "backstack_behavior": "default"}
#         }
#     """
#     url = "https://auth.openai.com/api/accounts/user/register"
#
#     headers = session.get_auth_headers(referer="https://auth.openai.com/create-account/password")
#     headers["openai-sentinel-token"] = sentinel_header
#
#     body = json.dumps({
#         "password": password,
#         "username": email,
#     })
#
#     logger.info(f"[步骤7] 提交注册请求, 邮箱: {email}")
#     resp = session.post(url, headers=headers, data=body)
#
#     if resp.status_code != 200:
#         logger.error(f"[步骤7] 请求失败, 状态码: {resp.status_code}")
#         logger.error(f"[步骤7] 响应内容: {resp.text}")
#         resp.raise_for_status()
#
#     data = resp.json()
#     logger.info(f"[步骤7] 注册请求成功: {data.get('page', {}).get('type')}")
#     return data


# def send_email_otp(session: BrowserSession) -> None:
#     """
#     [备用] 步骤8: 触发发送邮箱验证码。
#     GET https://auth.openai.com/api/accounts/email-otp/send
#     """
#     url = "https://auth.openai.com/api/accounts/email-otp/send"
#
#     headers = session.get_auth_navigate_headers(referer="https://auth.openai.com/create-account/password")
#     headers["sec-fetch-site"] = "same-origin"
#     headers["sec-fetch-user"] = "?1"
#
#     logger.info("[步骤8] 触发发送邮箱验证码...")
#     resp = session.get(url, headers=headers, allow_redirects=True)
#     logger.info(f"[步骤8] 验证码发送请求完成, 状态码: {resp.status_code}")


def navigate_about_you(session: BrowserSession, about_url: str | None = None) -> str:
    """进入 about-you 页面状态；服务端未返回 continue_url 时使用默认页面 URL 兜底。"""
    url = str(about_url or "https://auth.openai.com/about-you")
    if url.startswith("/"):
        url = "https://auth.openai.com" + url
    headers = session.get_auth_navigate_headers(referer="https://auth.openai.com/email-verification")
    headers["sec-fetch-site"] = "same-origin"
    logger.info("[步骤10.5] 导航到 about-you 页面，建立资料页状态")
    resp = session.get(url, headers=headers, allow_redirects=True)
    if resp.status_code >= 400:
        raise RuntimeError(f"about-you 导航失败: {_response_summary(resp)}")
    final_url = str(getattr(resp, "url", "") or url)
    if "/api/accounts/user/register" in final_url or "/create-account/password" in final_url:
        raise RuntimeError(
            "about-you 导航落入旧密码注册路径: "
            f"{_safe_url_label(final_url)}"
        )
    logger.info("[步骤10.5] about-you 导航完成，落点: %s", _safe_url_label(final_url))
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
        logger.warning("[OTP] 重新发送验证码失败: %s", _response_summary(resp))
        resp.raise_for_status()
    logger.info("[OTP] 重新发送验证码请求完成，status=%s", resp.status_code)


def validate_email_otp(session: BrowserSession, code: str, sentinel_header: str | None = None, so_header: str | None = None) -> dict:
    """
    步骤10: 提交邮箱验证码验证。
    POST https://auth.openai.com/api/accounts/email-otp/validate

    Args:
        session: 浏览器会话
        code: 6位数字验证码
        sentinel_header: openai-sentinel-token 头的值（authorize_continue flow）

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
    _attach_auth_document_navigation_header(session, headers)
    if sentinel_header:
        headers["openai-sentinel-token"] = sentinel_header
    if so_header:
        headers["openai-sentinel-so-token"] = so_header
        logger.info("[步骤10] 已添加 openai-sentinel-so-token 头")

    body = json.dumps({"code": code})

    logger.info(
        "[步骤10] 提交邮箱验证码: present=%s length=%s",
        bool(code),
        len(str(code or "")),
    )
    resp = session.post(url, headers=headers, data=body)

    if resp.status_code != 200:
        logger.error(f"[步骤10] 请求失败, 状态码: {resp.status_code}")
        logger.error("[步骤10] 响应摘要: %s", _response_summary(resp))
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
            raise EmailOtpInvalidError(
                "邮箱验证码无效或已过期: "
                f"status={resp.status_code}, error_code={err_code or 'unknown'}"
            )
        resp.raise_for_status()

    data = resp.json()
    page_type = data.get('page', {}).get('type')
    logger.info("[步骤10] 验证码验证成功: page_type=%s", page_type)
    logger.info("[步骤10] 验证响应摘要: %s", _response_summary(resp))
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
    _attach_auth_document_navigation_header(session, headers)
    headers["openai-sentinel-token"] = sentinel_header
    if so_header:
        headers["openai-sentinel-so-token"] = so_header
        logger.info(f"[步骤12] 已添加 openai-sentinel-so-token 头")

    body = json.dumps({
        "name": name,
        "birthdate": birthday,
    })

    logger.info(
        "[步骤12] 提交用户信息, name_present=%s name_length=%s birthday_present=%s",
        bool(name),
        len(str(name or "")),
        bool(birthday),
    )
    resp = session.post(url, headers=headers, data=body)

    if resp.status_code != 200:
        logger.error(f"[步骤12] 请求失败, 状态码: {resp.status_code}")
        logger.error("[步骤12] 响应摘要: %s", _response_summary(resp))
        resp.raise_for_status()

    data = resp.json()
    logger.info("[步骤12] 创建接口返回成功，等待 OAuth 回调建立登录态")
    return data
