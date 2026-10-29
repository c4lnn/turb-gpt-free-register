# -*- coding: utf-8 -*-
"""
注册后处理模块：
    1. 拉取 /api/auth/session，从中抽取 accessToken / user 信息
    2. 设置 2FA（TOTP），返回 secret
    3. 把账号信息（邮箱 + accessToken + TOTP secret）落盘成 JSON

整体复用注册阶段的 BrowserSession（同一 cookie jar / 同一 IP / 同一 UA），
避免再起新会话被风控关联或缺失登录态。
"""
import json
import logging
import random
import re
import time
from datetime import datetime
from pathlib import Path
import threading
from urllib.parse import urlencode

import pyotp

from core.session import BrowserSession
from core.humanize import delay as human_delay

logger = logging.getLogger(__name__)

# 输出目录（与项目根 .claude/ 工作区分离，单独放在 accounts/）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ACCOUNTS_DIR = _PROJECT_ROOT / "accounts"
_BATCH_ARCHIVE_LOCK = threading.RLock()


def normalize_chatgpt_session(session_info: dict, *, expected_email: str | None = None) -> dict:
    """校验并转换 /api/auth/session，供注册与已有账户刷新共用。"""
    if not isinstance(session_info, dict):
        raise ValueError("ChatGPT session 必须是对象")

    access_token = str(session_info.get("accessToken") or "").strip()
    user = session_info.get("user")
    account = session_info.get("account")
    expires = session_info.get("expires")
    if not access_token:
        raise ValueError("ChatGPT session 缺少 accessToken")
    if not isinstance(user, dict):
        raise ValueError("ChatGPT session.user 结构无效")
    if not isinstance(account, dict):
        raise ValueError("ChatGPT session.account 结构无效")
    if not isinstance(expires, str) or not expires.strip():
        raise ValueError("ChatGPT session.expires 结构无效")

    session_email = str(user.get("email") or "").strip()
    expected = str(expected_email or "").strip()
    if expected:
        if not session_email:
            raise ValueError("ChatGPT session.user 缺少 email，无法确认目标账户")
        if session_email.casefold() != expected.casefold():
            raise ValueError("ChatGPT session 邮箱与目标账户不一致")

    return {
        "access_token": access_token,
        "user_id": user.get("id"),
        "user_name": user.get("name"),
        "plan_type": account.get("planType"),
        "expires_at": expires.strip(),
        "session_email": session_email,
        "extra": {
            "user": dict(user),
            "account": dict(account),
            "expires": expires.strip(),
        },
    }


def _post_register_dwell_seconds() -> float:
    try:
        from config import register as _register_cfg

        raw = str(getattr(_register_cfg, "POST_REGISTER_DWELL_SECONDS_RANGE", "18,45") or "0,0").strip()
    except Exception:
        raw = "0,0"
    try:
        parts = [float(x.strip()) for x in raw.replace(";", ",").replace("|", ",").split(",") if x.strip()]
        if not parts:
            lo = hi = 0.0
        elif len(parts) == 1:
            lo = hi = parts[0]
        else:
            lo, hi = parts[0], parts[1]
    except Exception:
        lo = hi = 0.0
    lo, hi = max(0.0, lo), max(0.0, hi)
    if hi < lo:
        lo, hi = hi, lo
    seconds = random.uniform(lo, hi) if hi > lo else lo
    return max(0.0, min(300.0, seconds))


def post_register_dwell(email: str, *, label: str = "注册后") -> None:
    """注册成功后随机停留一段时间；供不同浏览器驱动复用。"""
    seconds = _post_register_dwell_seconds()
    if seconds <= 0:
        return
    logger.info("[%s] 注册成功后随机停留 %.1fs：%s", label, seconds, email)
    time.sleep(seconds)


def _account_material_line(email: str, row: dict | None = None) -> str:
    """优先输出 Outlook 原始素材；没有素材时退回邮箱地址。"""
    if row:
        return row.get("original_email_line") or row.get("email") or email
    return email


def _account_copy_line(material_line: str, access_token: str, totp_secret: str | None = None) -> str:
    """生成包含 token 的整行归档，方便从批次汇总文件里复制。"""
    return f"{material_line}----{access_token}----{totp_secret}" if totp_secret else f"{material_line}----{access_token}"


def create_batch_archive_dir(count: int, workers: int = 1) -> Path:
    """为一次运行创建批次归档目录，例如 accounts/20260509-10个-3线程。"""
    day = datetime.now().strftime("%Y%m%d")
    base_name = f"{day}-{count}个" if workers <= 1 else f"{day}-{count}个-{workers}线程"
    folder = _ACCOUNTS_DIR / base_name
    suffix = 2
    while folder.exists():
        folder = _ACCOUNTS_DIR / f"{base_name}-{suffix}"
        suffix += 1
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "注册成功的邮箱.txt").write_text("", encoding="utf-8")
    (folder / "注册成功的token.txt").write_text("", encoding="utf-8")
    (folder / "注册成功整行.txt").write_text("", encoding="utf-8")
    (folder / "注册成功账号.json").write_text("[]\n", encoding="utf-8")
    return folder


def _append_line(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(line + "\n")


def _append_batch_archive(
    *,
    row_id: int,
    email: str,
    access_token: str,
    totp_secret: str | None,
    email_source: str | None,
    proxy_used: str | None,
    extra: dict,
    batch_dir: Path | None,
) -> Path:
    """把注册成功账号追加到本次批次目录的 TXT/JSON 文件中。"""
    from core import db

    folder = batch_dir or create_batch_archive_dir(count=1)
    row = db.get_account(row_id) or {}
    folder.mkdir(parents=True, exist_ok=True)
    material_line = _account_material_line(email, row)
    copy_line = _account_copy_line(material_line, access_token, totp_secret)
    archive = {
        "id": row_id,
        "email": email,
        "email_source": email_source,
        "proxy_used": proxy_used,
        "access_token": access_token,
        "totp_secret": totp_secret,
        "material_line": material_line,
        "copy_line": copy_line,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "row": row,
        "extra": extra,
    }

    with _BATCH_ARCHIVE_LOCK:
        _append_line(folder / "注册成功的邮箱.txt", material_line)
        _append_line(folder / "注册成功的token.txt", access_token)
        _append_line(folder / "注册成功整行.txt", copy_line)

        json_path = folder / "注册成功账号.json"
        try:
            rows = json.loads(json_path.read_text(encoding="utf-8")) if json_path.exists() else []
        except Exception:
            rows = []
        if not isinstance(rows, list):
            rows = []
        rows.append(archive)
        json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return folder


def follow_oauth_callback(session: BrowserSession, continue_url: str, referer: str = "https://auth.openai.com/about-you") -> str:
    """
    步骤12.5: 跟随 create_account 返回的 continue_url，完成 OAuth 回调。

    create_account 成功后返回的 continue_url 一般指向
        https://auth.openai.com/authorize/continue?...
    它会再 302 到
        https://chatgpt.com/api/auth/callback/openai?code=...&state=...
    回调请求会让 chatgpt.com 设置 `__Secure-next-auth.session-token` cookie，
    之后 /api/auth/session 才能返回 accessToken。

    Returns:
        重定向链最终落点 URL（一般是 chatgpt.com 站内地址）
    """
    if not continue_url:
        raise ValueError("continue_url 为空，无法完成 OAuth 回调")

    # continue_url 通常是 auth.openai.com/authorize/continue；
    # OTP 后 external_url 分支也可能直接给 chatgpt.com 回调地址。
    # 按目标域名选择导航头，避免 auth step 正确但请求头语义不一致。
    if str(continue_url).startswith("https://chatgpt.com"):
        headers = session.get_chatgpt_navigate_headers(referer=referer)
    else:
        headers = session.get_auth_navigate_headers(referer=referer)

    logger.info(f"[OAuth回调] 跟随 continue_url 完成 OAuth 回调...")
    resp = session.get(continue_url, headers=headers, allow_redirects=True)
    logger.info(f"[OAuth回调] 完成, 最终落点: {resp.url}")
    return resp.url


def fetch_session(session: BrowserSession) -> dict:
    """
    GET https://chatgpt.com/api/auth/session
    注册成功后立刻调用，拿到 accessToken / user / account / expires。

    Returns:
        完整 session JSON，包含字段:
            - accessToken: str (Bearer token, 用于 backend-api 调用)
            - user: {id, name, email, idp, iat, mfa}
            - account: {id, planType, structure, ...}
            - expires: ISO 时间字符串
    """
    url = "https://chatgpt.com/api/auth/session"
    headers = session.get_nextauth_headers(referer="https://chatgpt.com/")

    logger.info("[Session] 拉取 ChatGPT session 信息...")
    resp = session.get(url, headers=headers)
    resp.raise_for_status()
    data = resp.json()

    if not data.get("accessToken"):
        logger.error(f"[Session] 响应中没有 accessToken: {data}")
        raise RuntimeError("未拿到 accessToken，登录态可能未建立")

    user = data.get("user") or {}
    account = data.get("account") or {}
    logger.info(
        f"[Session] 成功，user_id={user.get('id')}, email={user.get('email')}, "
        f"plan={account.get('planType')}, mfa={user.get('mfa')}"
    )
    return data


def _clear_twofa_session_circuit(
    session: BrowserSession, *, source: str = "可选预热"
) -> None:
    """清理 2FA 可恢复步骤产生的熔断状态。"""
    blocked_reason = str(getattr(session, "blocked_reason", "") or "")
    reset = getattr(session, "reset_circuit_breaker", None)
    if callable(reset):
        reset()
    elif getattr(session, "blocked_until", 0.0):
        session.blocked_until = 0.0
        session.blocked_reason = ""
    if blocked_reason:
        logger.info("[2FA] 已清理%s产生的熔断状态：%s", source, blocked_reason)


_RETRYABLE_REAUTH_HINTS = (
    "403", "408", "425", "429", "500", "502", "503", "504",
    "proxy", "socks", "timeout", "timed out", "connection", "closed",
    "reset", "temporarily unavailable", "熔断冷却",
)


def _is_retryable_reauth_error(exc: BaseException) -> bool:
    """仅重试限流、服务端错误和传输故障，不重试普通业务 4xx。"""
    response = getattr(exc, "response", None)
    try:
        status = int(getattr(response, "status_code", 0) or 0)
    except (TypeError, ValueError):
        status = 0
    if status:
        return status in (403, 408, 425, 429) or status >= 500
    text = str(exc or "").lower()
    return any(hint in text for hint in _RETRYABLE_REAUTH_HINTS)


def _trigger_reauth_with_retry(session: BrowserSession, email: str) -> str:
    """对 CSRF + signin 阶段的临时故障执行有限指数退避重试。"""
    from config import twofa as _twofa_cfg

    max_attempts = max(1, min(8, int(
        getattr(_twofa_cfg, "TWOFA_REAUTH_MAX_ATTEMPTS", 3) or 3
    )))
    base_delay = max(0.0, min(60.0, float(
        getattr(_twofa_cfg, "TWOFA_REAUTH_RETRY_DELAY", 3.0) or 0.0
    )))
    last_exc: BaseException | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            auth_url = _trigger_reauth(session, email)
            if attempt > 1:
                logger.info("[2FA] 重认证发起重试成功：attempt=%s/%s", attempt, max_attempts)
            return auth_url
        except Exception as exc:
            last_exc = exc
            retryable = _is_retryable_reauth_error(exc)
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
            if str(status) == "403" or (status is None and re.search(r"\b403\b", str(exc))):
                retryable = False
            if attempt >= max_attempts or not retryable:
                logger.warning(
                    "[2FA] 重认证发起失败且不再重试：attempt=%s/%s retryable=%s error=%s: %s",
                    attempt, max_attempts, retryable, type(exc).__name__, str(exc)[:200],
                )
                raise
            _clear_twofa_session_circuit(session, source="重认证请求")
            delay = min(120.0, base_delay * (2 ** (attempt - 1)))
            logger.warning(
                "[2FA] 重认证发起临时失败：attempt=%s/%s error=%s: %s；%.1fs 后重试",
                attempt, max_attempts, type(exc).__name__, str(exc)[:200], delay,
            )
            if delay > 0:
                time.sleep(delay)

    assert last_exc is not None
    raise last_exc


def _follow_reauth_with_retry(session: BrowserSession, auth_url: str) -> str:
    """重试跨站 authorize 导航；首个 403 下发的 CF Cookie 可供下一轮复用。"""
    from config import twofa as _twofa_cfg

    max_attempts = max(1, min(8, int(
        getattr(_twofa_cfg, "TWOFA_REAUTH_MAX_ATTEMPTS", 3) or 3
    )))
    base_delay = max(0.0, min(60.0, float(
        getattr(_twofa_cfg, "TWOFA_REAUTH_RETRY_DELAY", 3.0) or 0.0
    )))

    _warm_auth_document_for_reauth(session)

    for attempt in range(1, max_attempts + 1):
        try:
            result = _follow_reauth(session, auth_url)
            if attempt > 1:
                logger.info("[2FA] authorize 导航重试成功：attempt=%s/%s", attempt, max_attempts)
            return result
        except Exception as exc:
            retryable = _is_retryable_reauth_error(exc)
            if attempt >= max_attempts or not retryable:
                logger.warning(
                    "[2FA] authorize 导航失败且不再重试：attempt=%s/%s retryable=%s error=%s: %s",
                    attempt, max_attempts, retryable, type(exc).__name__, str(exc)[:200],
                )
                raise
            _clear_twofa_session_circuit(session, source="authorize 导航")
            delay = min(120.0, base_delay * (2 ** (attempt - 1)))
            logger.warning(
                "[2FA] authorize 导航临时失败：attempt=%s/%s error=%s: %s；"
                "%.1fs 后复用 CF Cookie 重试",
                attempt, max_attempts, type(exc).__name__, str(exc)[:200], delay,
            )
            if delay > 0:
                time.sleep(delay)

    raise RuntimeError("authorize 导航重试耗尽")


def _warm_auth_document_for_reauth(session: BrowserSession) -> None:
    """预热 auth.openai.com 顶层文档；403 时保留新 Cookie 后有限重试。"""
    get_headers = getattr(session, "get_auth_navigate_headers", None)
    request_get = getattr(session, "get", None)
    if not callable(get_headers) or not callable(request_get):
        return
    headers = get_headers(referer="", user_initiated=False)
    for attempt in range(1, 3):
        try:
            resp = request_get(
                "https://auth.openai.com/log-in",
                headers=headers,
                allow_redirects=True,
            )
            status = int(getattr(resp, "status_code", 0) or 0)
            if status < 400:
                logger.info("[2FA] Auth document 预热完成")
                return
            logger.info("[2FA] Auth document 预热返回 HTTP %s，保留响应 Cookie", status)
        except Exception as exc:
            logger.debug("[2FA] Auth document 预热异常：%s: %s", type(exc).__name__, str(exc)[:160])
        _clear_twofa_session_circuit(session, source="Auth document 预热")
        if attempt < 2:
            time.sleep(float(attempt))
    logger.info("[2FA] Auth document 预热未通过，继续正式 authorize 重试链")


def _trigger_reauth(session: BrowserSession, email: str) -> str:
    """
    步骤2-3: 发起密码重认证，返回 OpenAI authorize URL。
    重定向链会自动触发邮箱发送一份新的 OTP（用于 2FA 重认证）。
    """
    # 重新拿一次 csrf（旧的可能已过期）
    csrf_url = "https://chatgpt.com/api/auth/csrf"
    csrf_resp = session.get(csrf_url, headers=session.get_nextauth_headers(referer="https://chatgpt.com/"))
    csrf_resp.raise_for_status()
    csrf_token = csrf_resp.json()["csrfToken"]
    logger.info(f"[2FA] 重认证 CSRF: {csrf_token[:20]}...")

    # POST /api/auth/signin/openai 带 reauth 参数
    query = {
        "connection": "password",
        "login_hint": email,
        "reauth": "password",
        "max_age": "0",
        "ext-oai-did": session.device_id,
    }
    signin_url = "https://chatgpt.com/api/auth/signin/openai?" + urlencode(query)

    headers = session.get_nextauth_headers(referer="https://chatgpt.com/")
    headers["content-type"] = "application/x-www-form-urlencoded"
    headers["origin"] = "https://chatgpt.com"

    body = urlencode({
        "callbackUrl": "https://chatgpt.com/?action=enable&factor=totp",
        "csrfToken": csrf_token,
        "json": "true",
    })

    logger.info("[2FA] 发起重认证 signin/openai...")
    resp = session.post(signin_url, headers=headers, data=body)
    resp.raise_for_status()
    auth_url = resp.json().get("url")
    if not auth_url:
        raise RuntimeError(f"未拿到 reauth authorize URL: {resp.text}")
    return auth_url


def _follow_reauth(session: BrowserSession, auth_url: str) -> str:
    """
    步骤3: 跟随 authorize URL 触发邮箱 OTP 发送。
    auth.openai.com 会重定向到 /email-verification 页面，期间发送 OTP 邮件。
    """
    headers = session.get_auth_navigate_headers(referer="https://chatgpt.com/")
    logger.info("[2FA] 跟随 authorize URL，触发 OTP 发送...")
    resp = session.get(auth_url, headers=headers, allow_redirects=True)
    resp.raise_for_status()
    logger.info(f"[2FA] 落点 URL: {resp.url}")
    return str(getattr(resp, "url", "") or "")


class TwofaOtpRequestError(RuntimeError):
    """保留响应供上层识别，但不将响应体或凭证写入错误日志。"""

    def __init__(self, response, *, code: str, challenge: bool, business_error: bool = False):
        self.response = response
        self.status_code = int(response.status_code)
        self.error_code = code
        self.challenge = challenge
        self.business_error = business_error
        headers = {str(k).lower(): str(v) for k, v in (getattr(response, "headers", {}) or {}).items()}
        request_id = headers.get("cf-ray") or headers.get("x-request-id") or ""
        request_id = re.sub(r"[^a-zA-Z0-9_-]", "", request_id)[:80]
        hint = ("服务端要求交互验证，自动重试已停止，可在官方页面完成设置" if challenge else
                "服务端明确拒绝重认证，请检查账号或登录权限" if code or business_error else
                "重认证 OTP 请求被拒绝，未返回明确错误码")
        super().__init__(f"{hint}（HTTP {self.status_code}，阶段=OTP 验证" +
                         (f"，error_code={code}" if code else "") +
                         (f"，request_id={request_id}" if request_id else "") + "）")


def _otp_request_error(response) -> TwofaOtpRequestError:
    try:
        payload = response.json()
    except Exception:
        payload = {}
    error = payload.get("error") if isinstance(payload, dict) else None
    code = (error.get("code") or error.get("type") or "") if isinstance(error, dict) else ""
    if not code and isinstance(payload, dict):
        code = payload.get("code") or payload.get("error_code") or ""
    # 仅允许短错误标识，避免把服务端回显的敏感内容混入日志。
    code = str(code)
    code = code if re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", code) else ""
    headers = {str(k).lower(): str(v) for k, v in (getattr(response, "headers", {}) or {}).items()}
    body = str(getattr(response, "text", "") or "")[:8192].lower()
    challenge = (headers.get("cf-mitigated", "").lower() == "challenge" or
                 any(marker in body for marker in ("cf-chl-", "challenge-platform", "captcha")))
    return TwofaOtpRequestError(response, code=code, challenge=challenge, business_error=bool(error))


def _validate_twofa_otp_with_retry(session: BrowserSession, code: str) -> str:
    """只重试无业务拒绝的 403/429；不重发邮件，不重放结果未知的超时 POST。"""
    from config import twofa as cfg
    attempts = max(1, min(8, int(getattr(cfg, "TWOFA_REAUTH_MAX_ATTEMPTS", 3) or 3)))
    delay = max(0.0, min(60.0, float(getattr(cfg, "TWOFA_REAUTH_RETRY_DELAY", 3.0) or 0.0)))
    for attempt in range(1, attempts + 1):
        try:
            return _validate_reauth_otp(session, code)
        except TwofaOtpRequestError as exc:
            if exc.challenge or exc.error_code or exc.business_error or attempt >= attempts:
                raise
            headers = {str(k).lower(): str(v) for k, v in (getattr(exc.response, "headers", {}) or {}).items()}
            retry_after = headers.get("retry-after", "").strip()
            # 非数字（HTTP-date）或较长冷却交回用户，不能提前重试违反服务端冷却。
            if retry_after and (not retry_after.isdigit() or len(retry_after) > 3 or int(retry_after) > 120):
                raise
            wait = max(min(120.0, delay * 2 ** (attempt - 1)), int(retry_after or 0))
            logger.warning("[2FA] OTP 请求临时拒绝 HTTP %s，%.1fs 后复用原会话重试（%s/%s），不重新发码",
                           exc.status_code, wait, attempt, attempts)
            if wait:
                time.sleep(wait)
            _clear_twofa_session_circuit(session, source="OTP 临时拒绝")
    raise RuntimeError("2FA OTP 重试耗尽")


def _validate_reauth_otp(session: BrowserSession, code: str) -> str:
    """
    步骤4: 提交邮箱 OTP 验证。
    返回 continue_url（带 code 参数的 callback URL，用于跳回 chatgpt.com）。
    """
    url = "https://auth.openai.com/api/accounts/email-otp/validate"
    headers = session.get_auth_headers(referer="https://auth.openai.com/email-verification")
    body = json.dumps({"code": code})

    logger.info(f"[2FA] 提交重认证 OTP: {code}")
    resp = session.post(url, headers=headers, data=body)
    if resp.status_code in (403, 429):
        raise _otp_request_error(resp)
    resp.raise_for_status()
    data = resp.json()
    continue_url = data.get("continue_url")
    if not continue_url:
        raise RuntimeError(f"OTP 验证响应缺少 continue_url: {data}")
    return continue_url


def _exchange_new_token(session: BrowserSession, continue_url: str) -> str:
    """
    步骤5: 跟随 continue_url 完成回调，再次拉 /api/auth/session 拿到新 accessToken
    （此时 token 内嵌的 pwd_auth_time 是新鲜的，2FA enroll 才会接受）。
    """
    headers = session.get_auth_navigate_headers(referer="https://auth.openai.com/email-verification")
    logger.info("[2FA] 跟随 continue_url，刷新 session-token cookie...")
    callback = session.get(continue_url, headers=headers, allow_redirects=True)
    callback.raise_for_status()

    # 拿新的 accessToken
    new_session = fetch_session(session)
    new_token = new_session["accessToken"]
    logger.info("[2FA] 已获取重认证后的新 accessToken")
    return new_token


def _enroll_totp(session: BrowserSession, access_token: str) -> tuple[str, str]:
    """
    步骤6: 注册 TOTP，返回 (secret, session_id)
    """
    url = "https://chatgpt.com/backend-api/accounts/mfa/enroll"
    headers = session.get_chatgpt_headers(referer="https://chatgpt.com/")
    headers["authorization"] = f"Bearer {access_token}"
    headers["oai-device-id"] = session.device_id
    headers["oai-language"] = session.navigator_language()

    body = json.dumps({"factor_type": "totp"})

    logger.info("[2FA] 注册 TOTP...")
    resp = session.post(url, headers=headers, data=body)
    if resp.status_code != 200:
        logger.error(f"[2FA] enroll 失败 {resp.status_code}: {resp.text}")
        resp.raise_for_status()
    data = resp.json()
    secret = data.get("secret")
    session_id = data.get("session_id")
    if not secret or not session_id:
        raise RuntimeError(f"enroll 响应字段缺失: {data}")
    logger.info(f"[2FA] TOTP secret 已获取: {secret}")
    return secret, session_id


def _activate_totp(
    session: BrowserSession,
    access_token: str,
    secret: str,
    session_id: str,
) -> bool:
    """
    步骤7: 用 secret 生成 6 位 TOTP 码，激活 2FA。
    """
    url = "https://chatgpt.com/backend-api/accounts/mfa/user/activate_enrollment"
    headers = session.get_chatgpt_headers(referer="https://chatgpt.com/")
    headers["authorization"] = f"Bearer {access_token}"
    headers["oai-device-id"] = session.device_id
    headers["oai-language"] = session.navigator_language()

    totp_code = pyotp.TOTP(secret).now()
    body = json.dumps({
        "code": totp_code,
        "factor_type": "totp",
        "session_id": session_id,
    })

    logger.info(f"[2FA] 激活 enrollment, code={totp_code}")
    resp = session.post(url, headers=headers, data=body)
    if resp.status_code != 200:
        logger.error(f"[2FA] activate 失败 {resp.status_code}: {resp.text}")
        resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"激活返回 success=false: {data}")
    return True


def setup_2fa(
    session: BrowserSession,
    email: str,
    otp_code: str | None = None,
    access_token: str | None = None,
) -> str:
    """
    完整的 2FA 设置流程。
    会触发再发一份邮箱验证码：
        - USE_EMAIL_SERVICE=True 时自动从 Outlook 账号池拉取
        - 否则需要用户手动输入

    Args:
        session: 已完成注册的会话
        email: 账号邮箱（用作 login_hint）
        otp_code: 邮箱验证码（None 则按上述策略获取）
        access_token: 可选，复用现有 accessToken 预热登录态（None 则走纯重认证）

    Returns:
        TOTP secret（Base32 字符串），可直接用于 pyotp.TOTP() 生成 6 位动态码
    """
    # 用模块属性读，支持 WebUI 热加载
    from config import email as _email_cfg
    from core.chatgpt_bootstrap import authenticated_bootstrap

    logger.info("=" * 60)
    logger.info("开始设置 2FA")
    logger.info("=" * 60)

    if access_token:
        try:
            logger.info("[2FA] 使用现有 accessToken 预热登录态...")
            authenticated_bootstrap(session, access_token, strict=False)
            human_delay("navigate")
            logger.info("[2FA] accessToken 可选预热流程结束（不代表网页登录态已建立）")
        except Exception as exc:
            logger.warning("[2FA] accessToken 预热失败，继续按重认证流程执行：%s: %s", type(exc).__name__, str(exc)[:180])
        finally:
            # authenticated_bootstrap(strict=False) 是可选预热。其非关键接口返回
            # 403 时会开启会话级熔断，若不清理，下一步 CSRF 请求甚至不会发出。
            _clear_twofa_session_circuit(session, source="可选预热")

    # 阶段一：重认证
    logger.info("[2FA] 阶段1：发起重认证")
    reauth_otp_after_ts = time.time()
    auth_url = _trigger_reauth_with_retry(session, email)
    logger.info("[2FA] 重认证 authorize URL 已获取")
    human_delay("api")
    _follow_reauth_with_retry(session, auth_url)
    logger.info("[2FA] 已跟随重认证 authorize URL")
    # 浏览器登录页在落到 email-verification 后还会显式 GET
    # /api/accounts/email-otp/send；仅跟随 authorize URL 有时只打开页面而不真正
    # 投递邮件，尤其是复用 accessToken 的 reauth 场景。与查活/网页登录保持一致，
    # 显式触发一次发送。
    from core.openai_auth import send_email_otp
    send_email_otp(session)
    logger.info("[2FA] 已显式触发邮箱重认证 OTP 发送")
    human_delay("navigate")

    if otp_code is None:
        if _email_cfg.USE_EMAIL_SERVICE:
            from core.email_provider import wait_for_otp
            logger.info("[2FA] 自动等待邮箱重认证 OTP...")
            try:
                otp_code = wait_for_otp(email, after_ts=reauth_otp_after_ts)
            except Exception as first_wait_exc:
                # 重认证页本身没有可靠的 resend API；重新发起一次 authorize
                # 流程会让 auth.openai.com 再发送一封新的 OTP。只自动重发一次，
                # 避免邮箱服务异常时无限重复触发验证码。
                logger.warning(
                    "[2FA] 首次等待重认证 OTP 超时，尝试重新发送验证码：%s: %s",
                    type(first_wait_exc).__name__, str(first_wait_exc)[:180],
                )
                reauth_otp_after_ts = time.time()
                resend_auth_url = _trigger_reauth_with_retry(session, email)
                human_delay("api")
                _follow_reauth_with_retry(session, resend_auth_url)
                send_email_otp(session)
                logger.info("[2FA] 已重新触发重认证 OTP，开始第二轮等待")
                # Remail 的 receivedAt 可能比本地发送时间早几十秒（网关缓存/时钟
                # 偏差），重发后的第二轮放宽时间下界，避免已到邮箱却被 after_ts
                # 过滤掉；若拿到旧码，后面的 401 重试逻辑仍会校验。
                broad_after_ts = max(0.0, reauth_otp_after_ts - 120.0)
                logger.info("[2FA] 第二轮取码启用时间偏差容错：after_ts=%.0f", broad_after_ts)
                otp_code = wait_for_otp(email, after_ts=broad_after_ts)
            logger.info("[2FA] 已收到邮箱重认证 OTP")
        else:
            logger.info("")
            logger.info("[2FA] 请检查邮箱，输入新收到的 6 位验证码")
            otp_code = input(">>> 2FA 验证码: ").strip()
            logger.info("[2FA] 已手动输入邮箱重认证 OTP")

    human_delay("otp_input")
    logger.info("[2FA] 正在提交邮箱重认证 OTP...")
    try:
        continue_url = _validate_twofa_otp_with_retry(session, otp_code)
    except Exception as first_exc:
        # 部分取码接口会短暂返回缓存中的上一封邮件。若服务端拒绝验证码，
        # 重新轮询一次并提交最新候选，避免第一次旧码直接终止整个 2FA 流程。
        status_code = getattr(getattr(first_exc, "response", None), "status_code", None)
        if status_code != 401 or not bool(getattr(_email_cfg, "USE_EMAIL_SERVICE", False)):
            raise
        logger.warning("[2FA] 首次 OTP 被拒绝，重新获取最新验证码后再试一次")
        from core.email_provider import wait_for_otp
        retry_settle = max(8, int(getattr(_email_cfg, "OTP_SETTLE_SECONDS", 5) or 5))
        fresh_otp = wait_for_otp(
            email,
            after_ts=reauth_otp_after_ts,
            settle_seconds=retry_settle,
        )
        if fresh_otp == otp_code:
            logger.warning("[2FA] 重试仍获取到相同验证码，继续提交以保留原始错误信息")
        else:
            logger.info("[2FA] 已获取新验证码，替换首次候选")
        otp_code = fresh_otp
        continue_url = _validate_twofa_otp_with_retry(session, otp_code)
    logger.info("[2FA] 邮箱重认证 OTP 验证通过")
    human_delay("api")
    logger.info("[2FA] 正在交换新 token...")
    new_token = _exchange_new_token(session, continue_url)
    logger.info("[2FA] 已拿到新 token")
    human_delay("api")

    # 阶段二：enroll + activate
    logger.info("[2FA] 阶段2：开始 enroll TOTP")
    secret, session_id = _enroll_totp(session, new_token)
    logger.info("[2FA] enroll 成功，session_id=%s", session_id)
    human_delay("form")
    logger.info("[2FA] 正在激活 TOTP enrollment")
    _activate_totp(session, new_token, secret, session_id)
    logger.info("[2FA] TOTP 激活完成")

    logger.info("=" * 60)
    logger.info(f"✅ 2FA 设置完成! Secret: {secret}")
    logger.info("=" * 60)
    return secret


def save_account_data(
    email: str,
    access_token: str,
    totp_secret: str | None = None,
    extra: dict | None = None,
    output_path: Path | None = None,  # 兼容老接口，已废弃
    email_source: str | None = None,
    proxy_used: str | None = None,
    batch_dir: Path | None = None,
    auto_plan_check: bool | None = None,
) -> int:
    """
    将账号信息保存到本地 JSON/TXT 文件存储。
    返回新插入/更新的 row id。
    """
    from core.db import insert_account
    extra = extra or {}
    normalized = normalize_chatgpt_session(
        {
            "accessToken": access_token,
            "user": extra.get("user"),
            "account": extra.get("account"),
            "expires": extra.get("expires"),
        },
        expected_email=email,
    )
    user = normalized["extra"]["user"]
    account = normalized["extra"]["account"]
    # 从 extra.codex 抽出顶层 codex 状态/错误，方便 WebUI 直接读账号字段
    codex = extra.get("codex") or {}
    codex_status = codex.get("status")  # success / failed / skipped
    codex_error = None
    if codex_status == "failed":
        codex_error = codex.get("message")

    row_id = insert_account(
        email=email,
        access_token=normalized["access_token"],
        totp_secret=totp_secret,
        user_id=user.get("id"),
        user_name=user.get("name"),
        plan_type=account.get("planType"),
        expires_at=normalized["expires_at"],
        device_id=extra.get("device_id"),
        proxy_used=proxy_used,
        email_source=email_source,
        extra=extra,
        codex_status=codex_status,
        codex_error=codex_error,
    )
    batch_folder = _append_batch_archive(
        row_id=row_id,
        email=email,
        access_token=access_token,
        totp_secret=totp_secret,
        email_source=email_source,
        proxy_used=proxy_used,
        extra=extra,
        batch_dir=batch_dir,
    )
    logger.info(f"[Save] 账号已写入 DB, id={row_id}, email={email}")
    logger.info(f"[Save] 批次归档目录: {batch_folder}")
    if auto_plan_check is None:
        try:
            from config import register as _register_cfg

            auto_plan_check = bool(getattr(_register_cfg, "AUTO_PLAN_CHECK_AFTER_REGISTER", False))
        except Exception:
            auto_plan_check = False
    if not auto_plan_check:
        logger.info(f"[Plan] 注册后自动套餐查询已跳过: id={row_id}, email={email}")
        return row_id
    # session 中的 account.planType 不能说明 Plus 试用资格。账号落库后只负责
    # 入队，由专用线程池异步查询并回写，避免占用注册工作线程。
    try:
        from core.plan_check_service import enqueue_account_plan_check

        queued = enqueue_account_plan_check(
            account_id=row_id,
            email=email,
            access_token=access_token,
            trigger="registration_auto",
        )
        if queued.get("accepted"):
            logger.info(f"[Plan] 注册后自动查询已入队: id={row_id}, email={email}")
        elif queued.get("busy"):
            logger.info(f"[Plan] 账号已有套餐查询，注册流程不重复入队: id={row_id}, email={email}")
        else:
            logger.warning(f"[Plan] 注册后自动查询入队失败（不影响注册结果）: {email}, {queued.get('error')}")
    except Exception as exc:
        logger.warning(
            f"[Plan] 注册后自动查询入队异常（不影响注册结果）: "
            f"{email}, {type(exc).__name__}: {str(exc)[:180]}"
        )
    return row_id
