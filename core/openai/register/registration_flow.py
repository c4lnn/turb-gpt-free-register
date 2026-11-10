# -*- coding: utf-8 -*-
"""注册主流程编排：按服务端证据完成新号注册或已有账号认证，并汇合到账号保存流程。

本模块只承担 OpenAI 协议侧的步骤编排（认证分类 → 凭据 → OTP/TOTP → 资料 →
OAuth 回调 → 2FA → 落库 → flow）。CLI 的参数解析、批量循环与日志级别，以及
WebUI 的任务队列，都属于入口/服务职责，不在这里；入口与任务服务都调用同一份
`run_registration`，不再反向依赖入口脚本。
"""
import logging
import time
from typing import Callable
from urllib.parse import urlsplit

# 可按 WebUI 热改的配置，统一按模块属性读取
from config import twofa as _twofa_cfg
from config import email as _email_cfg
from config import openai_protocol as _protocol_cfg
from core.infra import db
from core.infra.contracts.email_pool import canonical_status
from core.infra.humanize import delay as human_delay
from core.infra.job_context import StopRequested, record_progress
from core.mail.email_provider import acquire_email, wait_for_otp, release_email_if_unconsumed
from core.openai.account.account_export import (
    fetch_session,
    follow_oauth_callback,
    save_account_data,
    setup_2fa,
    setup_2fa_inline,
)
from core.openai.auth.chatgpt_auth import get_csrf_token, signin_openai
from core.openai.auth.openai_auth import (
    AccountUnusableError,
    EmailOtpInvalidError,
    PasswordRejectedError,
    PasswordRegistrationRejectedError,
    SentinelRequestError,
    build_sentinel_header,
    create_account,
    follow_authorize,
    generate_registration_password,
    get_auth_continue_url,
    get_auth_step,
    get_mfa_enabled,
    get_registration_state,
    navigate_create_account_password,
    navigate_email_otp_send,
    probe_registration_state,
    register_user,
    request_login_otp,
    request_sentinel_token,
    send_email_otp,
    validate_email_otp,
    verify_login_password,
    verify_login_totp,
)
from core.openai.auth.sentinel_runner import SentinelSDKError
from core.openai.register.profile_utils import generate_random_birthday
from core.openai.register.registration_preflight import registration_preflight
from core.openai.transport.session import BrowserSession, close_browser_session

logger = logging.getLogger(__name__)

_FINALIZE_SESSION_MAX_ATTEMPTS = 5
_FINALIZE_SESSION_BACKOFF_BASE = 2.0


def _finalize_registration_session(
    session: BrowserSession,
    continue_url: str,
    email: str,
    callback_referer: str = "https://auth.openai.com/about-you",
) -> tuple[dict, str]:
    """
    完成 OAuth 回调并拉取 accessToken。

    create_account 返回只代表创建接口通过，真正可用必须等 chatgpt.com
    写入登录态 cookie 且 /api/auth/session 返回 accessToken。
    """
    if not continue_url:
        raise RuntimeError("create_account 响应缺少 continue_url，无法完成 OAuth 回调")

    last_exc: Exception | None = None
    for attempt in range(1, _FINALIZE_SESSION_MAX_ATTEMPTS + 1):
        try:
            logger.info(
                f"[登录态] 完成 OAuth 回调并拉取 Token：{email} "
                f"(尝试 {attempt}/{_FINALIZE_SESSION_MAX_ATTEMPTS})"
            )
            follow_oauth_callback(session, continue_url, referer=callback_referer)
            human_delay("post_auth")
            session_info = fetch_session(session)
            access_token = session_info.get("accessToken")
            if not access_token:
                raise RuntimeError("session 响应缺少 accessToken")
            logger.info(f"[登录态] 已拿到 accessToken：{email}")
            return session_info, access_token
        except Exception as exc:
            last_exc = exc
            if attempt >= _FINALIZE_SESSION_MAX_ATTEMPTS:
                break
            backoff = _FINALIZE_SESSION_BACKOFF_BASE ** (attempt - 1)
            logger.warning(
                f"[登录态] 回调或拉取 Token 失败：{email}，"
                f"{type(exc).__name__}: {str(exc)[:180]}，{backoff:.1f}s 后重试"
            )
            time.sleep(backoff)

    raise RuntimeError(
        f"OAuth 回调/拉取 Token 重试耗尽：{email}，"
        f"最后错误：{type(last_exc).__name__ if last_exc else 'Unknown'}: {last_exc}"
    ) from last_exc


def _prepare_persisted_email_registration(email: str) -> str | None:
    """识别 provider 已领取的条目；used 仅允许服务端明确的已有账号登录。"""
    # 持久化来源由各自 provider 领取；这里仅识别已由上层领取的条目，
    # 不把用户手动输入且未入池的地址强行写入某个来源。
    for getter in (
        db.get_outlook_by_email,
        db.get_generic_api_email_by_email,
        db.get_domain_email_by_email,
        db.get_icloud_email_by_email,
    ):
        row = getter(email)
        if row is None:
            continue
        state = canonical_status(row.get("status"), missing="disabled", unknown="disabled")
        if state not in ("registering", "used"):
            raise RuntimeError(f"邮箱池条目不可用于注册或登录: status={state}")
        return state
    return None


class _RegistrationFailure(RuntimeError):
    def __init__(self, message: str, stage: str, category: str):
        super().__init__(message)
        self.stage = stage
        self.category = category

def _safe_registration_error_message(exc: Exception) -> str:
    """Sentinel errors expose stage/category, never arbitrary payload text, in task diagnostics."""
    if isinstance(exc, (SentinelRequestError, SentinelSDKError)):
        return "Sentinel flow 执行失败"
    return str(exc)


def _validate_registration_otp(session, email, otp_after_ts, otp_code, state):
    current_otp = otp_code
    for attempt in range(1, 4):
        if current_otp is None:
            logger.info("[OTP] 等待邮箱验证码（第 %s/3 次）", attempt)
            current_otp = wait_for_otp(email, after_ts=otp_after_ts, direct=True)
        human_delay("otp_input")
        header = so_header = None
        if getattr(_protocol_cfg, "SEND_SENTINEL_ON_EMAIL_OTP_VALIDATE", False):
            proof = request_sentinel_token(session, "email_otp_validate")
            header, so_header = build_sentinel_header(session, proof, "email_otp_validate")
            human_delay("challenge")
        try:
            return validate_email_otp(
                session, current_otp, header, so_header,
                referer="https://auth.openai.com/",
            )
        except EmailOtpInvalidError:
            if attempt == 3:
                raise
            logger.warning("[OTP] 验证码错误或过期，重新发送并获取当前验证码")
            otp_after_ts = time.time()
            state = request_login_otp(session, state, resend=True)
            current_otp = None

def _persist_registration_password(email, password, progress, session):
    progress["credential_persistence"] = "unknown"
    try:
        account_id = db.save_registration_password(
            email, password,
            extra={
                "device_id": session.device_id,
                "sentinel_sid": getattr(session, "sentinel_sid", None),
                "browser_profile": getattr(session, "browser_profile", None),
            },
        )
    except Exception as exc:
        # 权威写入后衍生输出也可能报错：读回密码，而非删除记录或重提密码。
        try:
            stored = db.get_account_credentials(email)
        except Exception:
            stored = None
        if stored is not None and stored.get("registration_password") == password:
            account_id = stored.get("account_id")
        else:
            progress["credential_persistence"] = "failed" if stored is not None else "unknown"
            raise _RegistrationFailure(
                "服务端密码已生效，本地凭据保存" + (
                    "失败" if stored is not None else "结果未确认"
                ),
                "credential_persistence",
                "credential_write_failed" if stored is not None else "credential_write_unconfirmed",
            ) from exc
    if account_id is None:
        raise _RegistrationFailure(
            "服务端密码已生效，本地凭据保存未返回账号标识",
            "credential_persistence", "credential_write_unconfirmed",
        )
    progress["credential_persistence"] = "saved"
    logger.info("[密码] 服务端已确认生效，凭据已保存，账号ID=%s", account_id)
    return account_id
def _setup_registration_totp(
    session, email: str, access_token: str, registration_password: str | None,
    on_access_token=None,
) -> str:
    """先复用注册会话；失败后同代理、新会话重认证并优先恢复 pending secret。"""
    def persist_pending(secret: str) -> None:
        db.save_pending_totp_secret(email, secret)

    try:
        logger.info("[2FA] 尝试复用注册会话绑定 TOTP")
        secret = setup_2fa_inline(
            session, access_token, on_pending_secret=persist_pending,
        )
    except Exception as fast_exc:
        if isinstance(fast_exc, db.CredentialPersistenceError):
            raise
        logger.warning(
            "[2FA] 同会话绑定失败，改用独立会话重认证: %s: %s",
            type(fast_exc).__name__, str(fast_exc)[:180],
        )
        if not str(registration_password or "").strip():
            raise RuntimeError("TOTP 快路径失败，慢路径需要已保存的 OpenAI 登录密码") from fast_exc
        reauth_session = BrowserSession(proxy=session.proxy, server_device_id=True)
        try:
            secret = setup_2fa(
                reauth_session,
                email,
                password=registration_password,
                on_pending_secret=persist_pending,
                on_access_token=on_access_token,
                skip_if_mfa_enabled=True,
                on_already_enabled=lambda: db.promote_pending_totp_secret(email),
            )
        finally:
            close_browser_session(reauth_session)
    return db.promote_pending_totp_secret(email) or secret


def run_registration(
    email: str | None,
    name: str,
    birthday: str | None = None,
    proxy: str = None,
    otp_code: str = None,
    batch_dir=None,
    on_email_acquired: Callable[[str], None] | None = None,
):
    """按服务端证据完成新号注册或已有账号认证，并汇合到原账号保存流程。"""
    progress = {
        "account_kind": "unknown", "auth_step": "initialize",
        "password_state": "not_attempted", "password_effective": False,
        "credential_persistence": "not_attempted", "authenticated": False,
        "account_saved": False, "twofa_status": "not_attempted",
    }
    record_progress(progress)

    # 纯协议驱动没有“邮箱输入框”可等待，因此在创建 BrowserSession 前领取。
    if not str(email or "").strip():
        if not _email_cfg.USE_EMAIL_SERVICE:
            raise RuntimeError(
                "手动模式未配置邮箱。请在 WebUI 配置页设置 REGISTER_EMAIL，"
                "或开启 USE_EMAIL_SERVICE 并从邮箱池领取。"
            )
        email = acquire_email(direct=True)
        if on_email_acquired:
            on_email_acquired(email)

    session = None
    account_id = None
    registration_password = None
    try:
        persisted_email = _prepare_persisted_email_registration(email)
        # 构造与预检都进入同一错误收口；构造中途失败由会话释放部分资源。
        session = BrowserSession(proxy=proxy, server_device_id=True)
        reset_auth_context = getattr(session, "reset_auth_document_navigation_context", None)
        if callable(reset_auth_context):
            reset_auth_context()
        if not birthday:
            birthday = generate_random_birthday()
        logger.info("[注册] 开始，proxy_configured=%s", bool(session.proxy))
        logger.info("[注册] 本次随机生日已生成: present=%s", bool(birthday))
        # 网络预检必须在 signin/follow_authorize 之前完成；预检不带邮箱，不会触发 OTP。
        registration_preflight(session)
        human_delay("navigate")

        # ==================== 阶段1: ChatGPT 认证 ====================
        # 步骤2: 获取 CSRF token
        csrf_token = get_csrf_token(session)
        human_delay("api")

        # 步骤3: 发起 OAuth signin
        authorize_url = signin_openai(session, csrf_token, email)
        human_delay("api")

        # ==================== 阶段2: OpenAI Auth ====================
        # 步骤4: 跟随 authorize URL（建立 auth.openai.com 的 cookies）
        progress["auth_step"] = "authorize"
        otp_after_ts = time.time()
        authorize_final_url = follow_authorize(session, authorize_url)
        human_delay("navigate")

        state = get_registration_state(session, authorize_final_url, intent="signup")
        if state.get("kind", "unknown") == "unknown":
            progress["auth_step"] = "classification_probe"
            logger.info("[认证] signup 落地状态未知，执行一次 authorize_continue 探测")
            try:
                state = probe_registration_state(
                    session, email,
                    referer="https://auth.openai.com/",
                    screen_hint="signup",
                )
            except StopRequested:
                raise
            except Exception as exc:
                category = getattr(exc, "category", None)
                if isinstance(exc, (SentinelRequestError, SentinelSDKError)):
                    logger.warning(
                        "[认证] authorize_continue 探测失败，终止注册: error_type=%s error_code=%s",
                        type(exc).__name__, category or "sentinel_failed",
                    )
                    raise
                logger.warning(
                    "[认证] authorize_continue 探测失败，终止注册: error_type=%s error_code=classification_probe_failed",
                    type(exc).__name__,
                )
                raise _RegistrationFailure(
                    f"authorize_continue 探测失败（{type(exc).__name__}）",
                    "classification_probe", "classification_probe_failed",
                ) from None
        if state.get("kind", "unknown") == "unknown":
            # OSS 的成功响应分支不保留 unknown；其解析异常最终按新号处理。
            state = {
                **state,
                "kind": "new_password",
                "step": "password",
                "classification_source": "probe_unknown_new_fallback",
            }
        kind = state.get("kind", "unknown")
        progress["account_kind"] = kind
        progress["auth_step"] = "classification"
        logger.info(
            "[认证] 服务端账号类型=%s，步骤=%s，分类来源=%s",
            kind, state.get("step"), state.get("classification_source", "unknown"),
        )
        if kind not in ("new_password", "new_passwordless", "existing"):
            raise _RegistrationFailure(
                "authorize 状态未知或证据冲突，停止注册", "classification", "account_state_unknown",
            )
        if persisted_email == "used" and kind != "existing":
            raise _RegistrationFailure(
                "已使用邮箱的服务端状态不是已有账号，停止注册", "classification", "email_state_conflict",
            )
        progress["auth_step"] = "credential_lookup"
        credentials = db.get_account_credentials(email)
        totp_secret = credentials.get("totp_secret") or credentials.get("pending_tfa_secret")
        registration_landing_url = state.get("continue_url") or authorize_final_url
        callback_referer = registration_landing_url

        if kind == "existing" and state.get("otp_delivery_required"):
            progress["auth_step"] = "otp"
            otp_after_ts = time.time()
            send_email_otp(session, referer="https://auth.openai.com/create-account/password")
            state = {
                **state,
                "kind": "existing",
                "step": "otp",
                "page": {"type": "email_otp_verification", "payload": {}},
                "continue_url": "https://auth.openai.com/email-verification",
                "otp_delivery_required": False,
                "classification_source": "existing_otp_sent",
            }
            callback_referer = "https://auth.openai.com/email-verification"
        if kind in ("new_password", "new_passwordless"):
            progress["auth_step"] = "password"
            navigate_create_account_password(session, registration_landing_url)
            human_delay("navigate")
            candidate = generate_registration_password()
            logger.info("[密码候选/待提交] email=%s password=%s", email, candidate)
            proof = request_sentinel_token(session, "username_password_create")
            header, so_header = build_sentinel_header(session, proof, "username_password_create")
            human_delay("challenge")
            progress["password_state"] = "submission_unknown"
            try:
                state = register_user(session, email, candidate, header, so_header)
            except PasswordRegistrationRejectedError as exc:
                progress["password_state"] = "rejected"
                if kind != "new_passwordless":
                    raise
                otp_after_ts = time.time()
                state = request_login_otp(session, exc.result)
            else:
                progress["password_state"] = "confirmed"
                progress["password_effective"] = True
                progress["auth_step"] = "credential_persistence"
                account_id = _persist_registration_password(email, candidate, progress, session)
                registration_password = candidate
                if get_auth_step(state) == "otp":
                    progress["auth_step"] = "otp"
                    otp_after_ts = time.time()
                    navigate_email_otp_send(session, get_auth_continue_url(state))
                    if kind == "new_passwordless":
                        # 设置密码已切换 challenge，不能使用此前给入的验证码。
                        otp_code = None
        elif get_auth_step(state) == "password":
            progress["auth_step"] = "password"
            password = credentials.get("registration_password")
            if password:
                try:
                    state = verify_login_password(session, password)
                except PasswordRejectedError as exc:
                    progress["password_state"] = "rejected"
                    otp_after_ts = time.time()
                    state = request_login_otp(session, exc.result)
                else:
                    progress["password_state"] = "verified"
                    progress["password_effective"] = True
            else:
                otp_after_ts = time.time()
                state = request_login_otp(session, state)

        if kind == "existing" and get_auth_step(state) == "otp" and urlsplit(
            get_auth_continue_url(state)
        ).path == "/api/accounts/email-otp/send":
            progress["auth_step"] = "otp"
            otp_after_ts = time.time()
            state = request_login_otp(session, state)

        visited = set()
        while True:
            step = get_auth_step(state)
            progress["auth_step"] = step
            if step in visited or step not in ("otp", "totp", "profile", "callback"):
                raise _RegistrationFailure(
                    "服务端后续认证步骤未知、冲突或重复", "auth_transition", "auth_step_unknown",
                )
            visited.add(step)
            if step == "otp":
                state = _validate_registration_otp(session, email, otp_after_ts, otp_code, state)
                callback_referer = "https://auth.openai.com/email-verification"
            elif step == "totp":
                state = verify_login_totp(session, state, totp_secret)
                callback_referer = "https://auth.openai.com/mfa"
            elif step == "profile":
                if kind == "existing":
                    raise _RegistrationFailure(
                        "已有账号意外要求创建资料，停止而不修改账号", "profile", "existing_profile_unexpected",
                    )
                proof = request_sentinel_token(session, "oauth_create_account")
                header, so_header = build_sentinel_header(session, proof, "oauth_create_account")
                human_delay("challenge")
                human_delay("form")
                state = create_account(session, name, birthday, header, so_header)
                callback_referer = "https://auth.openai.com/about-you"
            else:
                session_info, access_token = _finalize_registration_session(
                    session, get_auth_continue_url(state), email, callback_referer=callback_referer,
                )
                progress["authenticated"] = True
                break


        twofa_required = bool(_twofa_cfg.ENABLE_2FA)
        twofa_satisfied = not twofa_required
        if twofa_required:
            try:
                mfa_enabled = get_mfa_enabled(session, access_token)
                mfa_state_known = True
            except StopRequested:
                raise
            except Exception as exc:
                mfa_enabled = False
                mfa_state_known = False
                logger.warning("[2FA] 服务端绑定状态未确认: %s", type(exc).__name__)

        if not twofa_required:
            progress["twofa_status"] = "disabled_by_config"
        elif mfa_state_known and mfa_enabled:
            if totp_secret:
                try:
                    totp_secret = db.promote_pending_totp_secret(email)
                except Exception:
                    pass
                twofa_satisfied = bool(totp_secret)
                progress["twofa_status"] = "already_enabled" if twofa_satisfied else "failed"
            else:
                progress["twofa_status"] = "failed"
                logger.warning("[2FA] 服务端已启用但本地没有正式或 pending secret，不重复绑定")
        else:
            try:
                def persist_slow_access_token(new_token: str) -> None:
                    nonlocal access_token
                    access_token = str(new_token or "").strip()

                totp_secret = _setup_registration_totp(
                    session, email, access_token,
                    registration_password or credentials.get("registration_password"),
                    on_access_token=persist_slow_access_token,
                )
                twofa_satisfied = bool(totp_secret)
                progress["twofa_status"] = "enabled" if twofa_satisfied else "failed"
            except StopRequested:
                raise
            except Exception as exc:
                logger.warning("[2FA] 设置失败，仍保存已获得的登录态: %s: %s", type(exc).__name__, str(exc)[:180])

        codex_result = {"status": "not_started", "ok": False, "message": "注册流程不启动 Codex OAuth"}


        # ==================== 阶段8: 持久化账号 ====================
        from core.mail.email_provider import resolve_email_source
        progress["auth_step"] = "account_persistence"
        account_id = save_account_data(
            email=email,
            access_token=access_token,
            totp_secret=totp_secret,
            email_source=resolve_email_source(email),
            proxy_used=session.proxy or None,
            batch_dir=batch_dir,
            extra={
                "user": session_info.get("user"),
                "account": session_info.get("account"),
                "expires": session_info.get("expires"),
                "device_id": session.device_id,
                "sentinel_sid": getattr(session, "sentinel_sid", None),
                "browser_profile": getattr(session, "browser_profile", None),
                "registration_password": registration_password,
                "codex": codex_result,
                "registration": {**progress, "account_saved": True},
            },
        )
        progress["account_saved"] = True

        logger.info(
            "[完成] %s，账号ID=%s，access_token_present=%s access_token_length=%s",
            email,
            account_id,
            bool(access_token),
            len(str(access_token or "")),
        )

        # ==================== 阶段9: 后置自动触发 flow ====================
        # 只有走完回调、拿到 token 并保存成功的账号，才会触发 flow。
        # flow 请求不影响账号保存状态，但会记录结果并参与批量统计。
        flow_result = {"status": "skipped", "ok": False, "message": "未触发"}
        progress["auth_step"] = "flow"
        try:
            from core.link.flow_trigger import trigger_flow
            flow_result = trigger_flow(access_token)
        except StopRequested:
            raise
        except Exception as exc:
            flow_result = {"status": "failed", "ok": False, "message": f"{type(exc).__name__}: {exc}"}

        if flow_result.get("ok"):
            logger.info(
                f"[Flow] 成功：{email}，HTTP={flow_result.get('http_status')}, "
                f"flow_id={flow_result.get('flow_id') or '未解析'}"
            )
        elif flow_result.get("status") == "skipped":
            logger.info(f"[Flow] 跳过：{email}，原因={flow_result.get('message')}")
        else:
            logger.warning(
                f"[Flow] 失败：{email}，HTTP={flow_result.get('http_status') or '无'}, "
                f"原因={flow_result.get('message')}"
            )

        logger.debug("[完成] TOTP Secret 是否设置: %s", bool(totp_secret))

        # 注册任务只要求有效 token、账号保存及配置要求的 2FA；Codex 由独立补跑入口负责。
        task_success = bool(access_token) and bool(account_id) and twofa_satisfied
        task_error = None
        if not task_success:
            task_error = "注册账号已保存但必需的 2FA 或账号持久化未完成"
            logger.warning("[任务结果] %s: %s", email, task_error)
        progress["auth_step"] = "complete"

        return {"success": task_success, "email": email, "account_id": account_id,
                "access_token": access_token, "totp_secret": totp_secret,
                "flow": flow_result, "codex": codex_result,
                "error": task_error, "registration": dict(progress)}

    except Exception as e:
        error_code = getattr(e, "category", None)
        if isinstance(e, AccountUnusableError):
            error_code = e.error_code or error_code
        error_stage = getattr(e, "stage", None) or progress["auth_step"]
        error_message = _safe_registration_error_message(e)
        if isinstance(e, (SentinelRequestError, SentinelSDKError)):
            logger.error(
                "[失败] %s: stage=%s error_code=%s reason=%s",
                email, error_stage, error_code or "sentinel_failed", error_message,
            )
        else:
            logger.error(f"[失败] {email}: {type(e).__name__}: {error_message}")
        logger.debug("详细错误信息:", exc_info=True)
        # 已有账号登录失败只影响任务；新号、未知状态保留原生命周期策略。
        try:
            if email and progress["account_kind"] != "existing":
                changed = release_email_if_unconsumed(
                    email,
                    note=f"openai_registration: {error_message[:300]}",
                )
                logger.warning(f"[邮箱] {email} 注册失败，已标记不可复用 changed={changed}")
        except Exception:
            pass
        return {"success": False, "email": email, "error": error_message,
                "account_id": account_id, "registration": dict(progress),
                "error_code": error_code,
                "error_stage": error_stage}
    finally:
        try:
            if session is not None:
                close_browser_session(session)
        except Exception as exc:
            logger.warning("[注册] 会话清理失败: %s", type(exc).__name__)
