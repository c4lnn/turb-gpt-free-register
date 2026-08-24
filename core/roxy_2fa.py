# -*- coding: utf-8 -*-
"""Roxy Selenium 页面上下文中的 ChatGPT TOTP 设置。

本模块只操作当前 Roxy/Selenium 浏览器 Profile，不创建 ``BrowserSession``，
以保持 Cookie、``oai-did``、指纹和代理连续。实现参考了公开的 MFA 请求
契约；未复制第三方 Playwright、MongoDB 或支付相关源码。
"""
from __future__ import annotations

import base64
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlparse

import pyotp

logger = logging.getLogger(__name__)

_CHATGPT_HOSTS = frozenset({"chatgpt.com", "auth.openai.com"})
_CHATGPT_MFA_ENROLL_URL = "https://chatgpt.com/backend-api/accounts/mfa/enroll"
_CHATGPT_MFA_ACTIVATE_URL = "https://chatgpt.com/backend-api/accounts/mfa/user/activate_enrollment"
_CHATGPT_MFA_VALIDATE_URL = "https://chatgpt.com/backend-api/models"
_ASYNC_SCRIPT_TIMEOUT_SECONDS = 30
_DEFAULT_SCRIPT_TIMEOUT_RESTORE_SECONDS = 12
_DEFAULT_REAUTH_PAGE_TIMEOUT_SECONDS = 30
_DEFAULT_OTP_ATTEMPTS = 3
_AUTHENTICATOR_PATTERN = re.compile(
    r"authenticator(?:\s+app)?|authentication\s+app|verification\s+app|\btotp\b|"
    r"two[-\s]?factor|two[-\s]?step|验证器|身份验证器|動態口令|动态口令|"
    r"認証アプリ|ワンタイム認証コード|인증\s*앱",
    re.IGNORECASE,
)


class RoxyTwoFactorError(RuntimeError):
    """不会携带敏感响应正文的 Roxy 2FA 阶段错误。"""

    def __init__(
        self,
        stage: str,
        code: str,
        message: str,
        *,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.code = code
        self.message = message
        self.http_status = http_status


@dataclass(frozen=True)
class RoxyTwoFactorHooks:
    """复用 Roxy 注册既有 DOM/邮箱能力，避免导入注册模块形成循环依赖。"""

    wait_for_otp: Callable[..., str]
    clear_otp_inputs: Callable[[], None]
    type_otp: Callable[[str], None]
    submit_otp: Callable[[], None]
    wait_after_otp_submit: Callable[[], str]
    is_email_verification_page: Callable[[], bool]
    click_resend_otp: Callable[[], None]
    fetch_session: Callable[[], dict]
    check_stop: Callable[[], None] | None = None


def disabled_result() -> dict:
    """返回不产生网络副作用的 disabled 结果。"""
    return _result("disabled")


def public_result(result: dict | None) -> dict:
    """删除敏感凭证后用于 ``extra.twofa``、日志和任务结果的摘要。"""
    source = result if isinstance(result, dict) else {}
    error = source.get("error")
    safe_error = None
    if isinstance(error, dict):
        stage = str(error.get("stage") or "unknown")
        code = str(error.get("code") or "unknown")
        safe_error = {
            "stage": stage,
            "code": code,
            # 不信任调用方传入的 error.message；错误码已经足够供日志/任务页诊断。
            "message": _public_error_message(stage, code),
        }
        status = error.get("http_status")
        if isinstance(status, int) and status > 0:
            safe_error["http_status"] = status

    validation = source.get("validation")
    safe_validation = None
    if isinstance(validation, dict):
        safe_validation = {
            "status": str(validation.get("status") or "unknown"),
            "code": str(validation.get("code") or "unknown"),
        }
        status = validation.get("http_status")
        if isinstance(status, int) and status > 0:
            safe_validation["http_status"] = status

    out = {
        "status": str(source.get("status") or "failed"),
        "activated_at": source.get("activated_at"),
        "error": safe_error,
    }
    if safe_validation is not None:
        out["validation"] = safe_validation
    return out


def setup_roxy_2fa(
    driver,
    email: str,
    *,
    hooks: RoxyTwoFactorHooks,
    max_otp_attempts: int = _DEFAULT_OTP_ATTEMPTS,
    reauth_page_timeout: int = _DEFAULT_REAUTH_PAGE_TIMEOUT_SECONDS,
    script_timeout_restore: int = _DEFAULT_SCRIPT_TIMEOUT_RESTORE_SECONDS,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """在当前 Roxy Profile 中设置 TOTP，所有失败均返回安全结果。"""
    normalized_email = str(email or "").strip()
    if not normalized_email:
        return _failed("reauth", "email_missing", "2FA 设置缺少目标邮箱")

    try:
        _check_stop(hooks)
        reauth_requested_at = clock()
        reauth_url = _begin_reauth(
            driver,
            normalized_email,
            script_timeout_restore=script_timeout_restore,
        )
        _check_stop(hooks)
        _navigate_to_reauth(driver, reauth_url)

        if _wait_for_authenticator_or_email_page(
            driver,
            hooks,
            timeout_seconds=reauth_page_timeout,
            sleep=sleep,
        ) == "already_enabled":
            return _result(
                "already_enabled",
                error={
                    "stage": "reauth",
                    "code": "already_enabled",
                    "message": "账号已要求认证器验证码，不能创建新的 TOTP Secret",
                },
            )

        _submit_reauth_otp(
            hooks,
            normalized_email,
            first_after_ts=reauth_requested_at,
            max_attempts=max_otp_attempts,
            clock=clock,
        )
        _check_stop(hooks)
        try:
            refreshed_session = hooks.fetch_session()
        except Exception as exc:
            if _is_stop_requested_error(exc):
                raise
            raise RoxyTwoFactorError(
                "session",
                "reauth_session_fetch_failed",
                "2FA 重认证后读取 ChatGPT Session 失败",
            ) from None
        session_info = _validated_session(refreshed_session, normalized_email)
        access_token = str(session_info["accessToken"])

        enroll = _page_api_request(
            driver,
            _CHATGPT_MFA_ENROLL_URL,
            access_token,
            {"factor_type": "totp"},
            failure_stage="enroll",
            script_timeout_restore=script_timeout_restore,
        )
        secret, session_id = _parse_enrollment(enroll)

        _wait_for_totp_window(clock=clock, sleep=sleep)
        activation = _page_api_request(
            driver,
            _CHATGPT_MFA_ACTIVATE_URL,
            access_token,
            {
                "code": pyotp.TOTP(secret).now(),
                "factor_type": "totp",
                "session_id": session_id,
            },
            failure_stage="activate",
            script_timeout_restore=script_timeout_restore,
        )
        _validate_activation(activation)

        # 激活成功后必须保留 Secret。models 探测只提供诊断，不能因为探测失败
        # 丢弃刚刚激活的 Secret，使账号进入无法再次登录的状态。
        validation = _validate_fresh_token(
            driver,
            access_token,
            script_timeout_restore=script_timeout_restore,
        )
        activated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        logger.info("[Roxy 2FA] 设置完成：status=enabled validation=%s", validation["status"])
        return _result(
            "enabled",
            secret=secret,
            access_token=access_token,
            session_info=session_info,
            activated_at=activated_at,
            validation=validation,
        )
    except RoxyTwoFactorError as exc:
        logger.warning(
            "[Roxy 2FA] 设置失败：stage=%s code=%s http_status=%s",
            exc.stage,
            exc.code,
            exc.http_status or "-",
        )
        return _failed(exc.stage, exc.code, exc.message, http_status=exc.http_status)
    except Exception as exc:
        if _is_stop_requested_error(exc):
            raise
        # Selenium、邮箱 provider 等第三方异常可能含 URL 或凭证，不能直接写入日志。
        logger.warning("[Roxy 2FA] 设置出现未分类异常：type=%s", type(exc).__name__)
        return _failed("internal", "unexpected_error", "Roxy 2FA 设置出现未预期错误")


def _result(
    status: str,
    *,
    secret: str | None = None,
    access_token: str | None = None,
    session_info: dict | None = None,
    activated_at: str | None = None,
    error: dict | None = None,
    validation: dict | None = None,
) -> dict:
    return {
        "status": status,
        "secret": secret,
        "access_token": access_token,
        "session_info": session_info,
        "activated_at": activated_at,
        "error": error,
        "validation": validation,
    }


def _failed(stage: str, code: str, message: str, *, http_status: int | None = None) -> dict:
    error = {"stage": stage, "code": code, "message": message}
    if isinstance(http_status, int) and http_status > 0:
        error["http_status"] = http_status
    return _result("failed", error=error)


def _check_stop(hooks: RoxyTwoFactorHooks) -> None:
    if hooks.check_stop is not None:
        hooks.check_stop()


def _begin_reauth(driver, email: str, *, script_timeout_restore: int) -> str:
    try:
        result = _execute_async(
            driver,
            r"""
        const email = String(arguments[0] || '').trim();
        const done = arguments[arguments.length - 1];
        (async () => {
          try {
            const csrfResponse = await fetch('https://chatgpt.com/api/auth/csrf', {
              credentials: 'include',
              headers: {'accept': 'application/json'}
            });
            const csrf = await csrfResponse.json().catch(() => ({}));
            const csrfToken = String(csrf.csrfToken || '');
            if (!csrfResponse.ok || !csrfToken) {
              done({ok:false, stage:'csrf', status:csrfResponse.status});
              return;
            }
            const deviceCookie = document.cookie.split(';').map(v => v.trim())
              .find(v => v.startsWith('oai-did='));
            const deviceId = deviceCookie
              ? decodeURIComponent(deviceCookie.slice('oai-did='.length))
              : '';
            const query = new URLSearchParams({
              connection: 'password',
              login_hint: email,
              reauth: 'password',
              max_age: '0'
            });
            if (deviceId) query.set('ext-oai-did', deviceId);
            const body = new URLSearchParams({
              callbackUrl: 'https://chatgpt.com/?action=enable&factor=totp',
              csrfToken,
              json: 'true'
            });
            const response = await fetch(
              `https://chatgpt.com/api/auth/signin/openai?${query.toString()}`,
              {
                method: 'POST',
                credentials: 'include',
                headers: {
                  'accept': 'application/json',
                  'content-type': 'application/x-www-form-urlencoded'
                },
                body: body.toString()
              }
            );
            const payload = await response.json().catch(() => ({}));
            const url = String(payload.url || '');
            if (!response.ok || !url) {
              done({ok:false, stage:'signin', status:response.status});
              return;
            }
            done({ok:true, stage:'signin', status:response.status, url});
          } catch (_) {
            done({ok:false, stage:'request', status:0});
          }
        })();
        """,
            email,
            script_timeout_restore=script_timeout_restore,
        )
    except RoxyTwoFactorError:
        raise RoxyTwoFactorError("reauth", "reauth_request_failed", "2FA 重认证请求失败") from None
    if not isinstance(result, dict):
        raise RoxyTwoFactorError("reauth", "reauth_response_invalid", "2FA 重认证响应无效")
    status = _as_status(result.get("status"))
    if not result.get("ok"):
        stage = str(result.get("stage") or "request")
        code = "reauth_csrf_failed" if stage == "csrf" else "reauth_start_failed"
        raise RoxyTwoFactorError("reauth", code, "2FA 重认证启动失败", http_status=status)
    url = str(result.get("url") or "")
    if not _is_trusted_openai_url(url):
        raise RoxyTwoFactorError("reauth", "reauth_url_untrusted", "2FA 重认证返回了不受信任的地址")
    return url


def _navigate_to_reauth(driver, url: str) -> None:
    try:
        driver.get(url)
    except Exception:
        # Roxy/Chrome 偶发渲染器超时；若当前页已经落在可信认证站点则继续。
        try:
            driver.execute_script("window.stop();")
        except Exception:
            pass
        if not _is_trusted_openai_url(_current_url(driver)):
            raise RoxyTwoFactorError("reauth", "reauth_navigation_failed", "2FA 重认证页面加载失败") from None
    if not _is_trusted_openai_url(_current_url(driver)):
        raise RoxyTwoFactorError("reauth", "reauth_navigation_untrusted", "2FA 重认证跳转到了不受信任的页面")


def _wait_for_authenticator_or_email_page(
    driver,
    hooks: RoxyTwoFactorHooks,
    *,
    timeout_seconds: int,
    sleep: Callable[[float], None],
) -> str:
    deadline = time.monotonic() + max(1, int(timeout_seconds or 1))
    while time.monotonic() < deadline:
        _check_stop(hooks)
        if _is_authenticator_page(driver):
            return "already_enabled"
        try:
            if hooks.is_email_verification_page():
                return "email"
        except Exception:
            pass
        sleep(0.5)
    raise RoxyTwoFactorError("reauth", "reauth_email_code_missing", "2FA 重认证未进入邮箱验证码页面")


def _submit_reauth_otp(
    hooks: RoxyTwoFactorHooks,
    email: str,
    *,
    first_after_ts: float,
    max_attempts: int,
    clock: Callable[[], float],
) -> None:
    attempts = max(1, int(max_attempts or 1))
    after_ts = first_after_ts
    last_failure: RoxyTwoFactorError | None = None
    for attempt in range(1, attempts + 1):
        _check_stop(hooks)
        try:
            code = hooks.wait_for_otp(email, after_ts=after_ts)
        except Exception as exc:
            if _is_stop_requested_error(exc):
                raise
            last_failure = RoxyTwoFactorError("otp", "reauth_otp_timeout", "2FA 重认证邮箱验证码获取失败")
        else:
            try:
                hooks.clear_otp_inputs()
                hooks.type_otp(code)
                _check_stop(hooks)
                hooks.submit_otp()
                outcome = str(hooks.wait_after_otp_submit() or "").strip().lower()
                if outcome == "accepted":
                    return
                last_failure = RoxyTwoFactorError("otp", "reauth_otp_invalid", "2FA 重认证邮箱验证码无效或已过期")
            except RoxyTwoFactorError:
                raise
            except Exception as exc:
                if _is_stop_requested_error(exc):
                    raise
                last_failure = RoxyTwoFactorError("otp", "reauth_otp_submit_failed", "2FA 重认证邮箱验证码提交失败")

        if attempt >= attempts:
            raise last_failure or RoxyTwoFactorError("otp", "reauth_otp_failed", "2FA 重认证邮箱验证码失败")
        after_ts = clock()
        try:
            hooks.click_resend_otp()
        except Exception:
            raise RoxyTwoFactorError("otp", "reauth_otp_resend_failed", "2FA 重认证验证码重发失败") from None


def _validated_session(session_info: dict, expected_email: str) -> dict:
    if not isinstance(session_info, dict):
        raise RoxyTwoFactorError("session", "reauth_session_invalid", "2FA 重认证后 Session 响应无效")
    token = str(session_info.get("accessToken") or "").strip()
    user = session_info.get("user")
    account = session_info.get("account")
    expires = session_info.get("expires")
    if not token or not isinstance(user, dict) or not isinstance(account, dict) or not str(expires or "").strip():
        raise RoxyTwoFactorError("session", "reauth_session_missing", "2FA 重认证后未取得完整 Session")
    session_email = str(user.get("email") or "").strip()
    if not session_email or session_email.casefold() != expected_email.casefold():
        raise RoxyTwoFactorError("session", "reauth_session_email_mismatch", "2FA 重认证后的 Session 邮箱不匹配")
    return session_info


def _parse_enrollment(response: dict) -> tuple[str, str]:
    status = _as_status(response.get("status")) if isinstance(response, dict) else None
    if not isinstance(response, dict) or not response.get("ok"):
        raise RoxyTwoFactorError("enroll", "totp_enroll_failed", "2FA TOTP enrollment 失败", http_status=status)
    data = response.get("data")
    if not isinstance(data, dict):
        raise RoxyTwoFactorError("enroll", "totp_enroll_response_invalid", "2FA enrollment 响应无效", http_status=status)
    secret = _normalize_totp_secret(data.get("secret"))
    session_id = str(data.get("session_id") or "").strip()
    if not session_id:
        raise RoxyTwoFactorError("enroll", "totp_enroll_response_invalid", "2FA enrollment 响应缺少会话标识", http_status=status)
    return secret, session_id


def _validate_activation(response: dict) -> None:
    status = _as_status(response.get("status")) if isinstance(response, dict) else None
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(response, dict) or not response.get("ok") or not isinstance(data, dict) or data.get("success") is not True:
        raise RoxyTwoFactorError("activate", "totp_activate_failed", "2FA TOTP 激活失败", http_status=status)


def _validate_fresh_token(driver, access_token: str, *, script_timeout_restore: int) -> dict:
    try:
        response = _page_api_request(
            driver,
            _CHATGPT_MFA_VALIDATE_URL,
            access_token,
            None,
            failure_stage="validate",
            script_timeout_restore=script_timeout_restore,
        )
    except RoxyTwoFactorError as exc:
        return {"status": "failed", "code": exc.code, "http_status": exc.http_status}
    status = _as_status(response.get("status")) if isinstance(response, dict) else None
    if isinstance(response, dict) and response.get("ok"):
        return {"status": "ok", "code": "token_valid"}
    # activation 已成功；探测失败只能作为诊断，不能丢弃刚生成的 Secret。
    return {"status": "failed", "code": "token_validation_failed", "http_status": status}


def _page_api_request(
    driver,
    url: str,
    access_token: str,
    body: dict | None,
    *,
    failure_stage: str,
    script_timeout_restore: int,
) -> dict:
    try:
        result = _execute_async(
            driver,
            r"""
        const url = String(arguments[0] || '');
        const token = String(arguments[1] || '');
        const body = arguments[2];
        const done = arguments[arguments.length - 1];
        (async () => {
          try {
            const deviceCookie = document.cookie.split(';').map(v => v.trim())
              .find(v => v.startsWith('oai-did='));
            const deviceId = deviceCookie
              ? decodeURIComponent(deviceCookie.slice('oai-did='.length))
              : '';
            const headers = {
              'accept': 'application/json',
              'authorization': `Bearer ${token}`,
              'content-type': 'application/json',
              'oai-language': navigator.language || 'en-US'
            };
            if (deviceId) headers['oai-device-id'] = deviceId;
            const response = await fetch(url, {
              method: body === null ? 'GET' : 'POST',
              credentials: 'include',
              headers,
              body: body === null ? undefined : JSON.stringify(body)
            });
            const data = await response.json().catch(() => ({}));
            done({ok: response.ok, status: response.status, data});
          } catch (_) {
            done({ok:false, status:0, data:{}});
          }
        })();
        """,
            url,
            access_token,
            body,
            script_timeout_restore=script_timeout_restore,
        )
    except RoxyTwoFactorError:
        code = {
            "enroll": "totp_enroll_request_failed",
            "activate": "totp_activate_request_failed",
            "validate": "token_validation_request_failed",
        }.get(failure_stage, "page_api_request_failed")
        raise RoxyTwoFactorError(failure_stage, code, "Roxy 页面上下文请求失败") from None
    if not isinstance(result, dict):
        return {"ok": False, "status": 0, "data": {}}
    return {
        "ok": bool(result.get("ok")),
        "status": _as_status(result.get("status")) or 0,
        "data": result.get("data") if isinstance(result.get("data"), dict) else {},
    }


def _execute_async(driver, script: str, *args, script_timeout_restore: int):
    restore_timeout = max(1, int(script_timeout_restore or _DEFAULT_SCRIPT_TIMEOUT_RESTORE_SECONDS))
    try:
        try:
            driver.set_script_timeout(_ASYNC_SCRIPT_TIMEOUT_SECONDS)
        except Exception:
            pass
        return driver.execute_async_script(script, *args)
    except Exception:
        raise RoxyTwoFactorError("page_api", "page_script_failed", "Roxy 页面上下文请求失败") from None
    finally:
        try:
            driver.set_script_timeout(restore_timeout)
        except Exception:
            pass


def _is_authenticator_page(driver) -> bool:
    try:
        snapshot = driver.execute_script(
            r"""
            const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
              && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
            const hasCodeInput = [...document.querySelectorAll('input')].filter(visible).some(el => {
              const attrs = [el.type, el.name, el.id, el.autocomplete, el.inputMode, el.getAttribute('aria-label')]
                .join(' ').toLowerCase();
              return /one-time|otp|code|numeric|tel/.test(attrs);
            });
            return {text: String(document.body && document.body.innerText || '').slice(0, 5000), hasCodeInput};
            """
        )
    except Exception:
        return False
    if not isinstance(snapshot, dict):
        return False
    return bool(snapshot.get("hasCodeInput")) and bool(
        _AUTHENTICATOR_PATTERN.search(str(snapshot.get("text") or ""))
    )


def _is_trusted_openai_url(value: str) -> bool:
    try:
        parsed = urlparse(str(value or ""))
    except Exception:
        return False
    return parsed.scheme == "https" and (parsed.hostname or "").lower() in _CHATGPT_HOSTS


def _current_url(driver) -> str:
    try:
        return str(driver.current_url or "")
    except Exception:
        return ""


def _normalize_totp_secret(value: object) -> str:
    secret = "".join(str(value or "").split()).upper().rstrip("=")
    if not secret:
        raise RoxyTwoFactorError("enroll", "totp_enroll_response_invalid", "2FA enrollment 响应缺少有效 Secret")
    try:
        padding = "=" * (-len(secret) % 8)
        base64.b32decode(secret + padding, casefold=True)
    except (TypeError, ValueError):
        raise RoxyTwoFactorError("enroll", "totp_enroll_response_invalid", "2FA enrollment 响应缺少有效 Secret") from None
    return secret


def _wait_for_totp_window(*, clock: Callable[[], float], sleep: Callable[[float], None]) -> None:
    remaining = 30 - (clock() % 30)
    if remaining < 4:
        sleep(remaining + 0.25)


def _as_status(value: object) -> int | None:
    try:
        status = int(value)
    except (TypeError, ValueError):
        return None
    return status if status > 0 else None


def _is_stop_requested_error(exc: BaseException) -> bool:
    """避免导入注册服务造成循环依赖，同时保留任务停止的原有传播语义。"""
    return type(exc).__name__ == "StopRequested"


def _public_error_message(stage: str, code: str) -> str:
    if code == "already_enabled":
        return "账号已要求认证器验证码"
    labels = {
        "reauth": "2FA 重认证失败",
        "otp": "2FA 邮箱验证码处理失败",
        "session": "2FA 重认证后的登录态无效",
        "enroll": "2FA enrollment 失败",
        "activate": "2FA activation 失败",
        "validate": "2FA 凭证验证失败",
        "integration": "Roxy 2FA 集成失败",
    }
    return labels.get(str(stage or "").strip().lower(), "Roxy 2FA 设置失败")
