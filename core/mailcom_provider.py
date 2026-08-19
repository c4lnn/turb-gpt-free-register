# -*- coding: utf-8 -*-
"""mail.com 邮箱池调度与 mailbox AT 恢复状态机。"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any, Callable

from core import db
from core.mailcom_client import (
    MailComAccount,
    MailComAuthError,
    MailComClient,
    MailComCredentialError,
    MailComError,
    MailComInvalidTokenError,
    clear_account_context,
    put_account_context,
)

logger = logging.getLogger(__name__)

_EMAIL_LOCKS: dict[str, threading.Lock] = {}
_EMAIL_LOCKS_GUARD = threading.Lock()


class MailComProviderError(RuntimeError):
    """邮箱池、token 恢复或 OTP 获取失败。"""


def _key(email: str) -> str:
    return str(email or "").strip().casefold()


def _mask_email(email: str) -> str:
    local, sep, domain = str(email or "").partition("@")
    if not sep:
        return "[redacted-email]"
    return f"{local[:1]}***@{domain}"


def _email_lock(email: str) -> threading.Lock:
    key = _key(email)
    with _EMAIL_LOCKS_GUARD:
        return _EMAIL_LOCKS.setdefault(key, threading.Lock())


def _expires_at(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _has_valid_token(record: dict | None, now: float) -> bool:
    if not record or not str(record.get("mail_access_token") or "").strip():
        return False
    expires = _expires_at(record.get("mail_access_token_expires_at"))
    return expires is not None and now < expires


def _account_from_record(record: dict) -> MailComAccount:
    return MailComAccount(
        id=record.get("id"),
        email=str(record.get("email") or ""),
        password=str(record.get("password") or ""),
        status=str(record.get("status") or "available"),
        mail_access_token=str(record.get("mail_access_token") or ""),
        mail_access_token_expires_at=_expires_at(record.get("mail_access_token_expires_at")),
        mail_access_token_updated_at=record.get("mail_access_token_updated_at"),
        mail_auth_error=record.get("mail_auth_error"),
        used_at=record.get("used_at"),
    )


class MailComProvider:
    """可注入 client 的 provider，供测试验证 token 恢复并发行为。"""

    def __init__(self, *, client_factory: Callable[..., MailComClient] | None = None,
                 alias_service: Any | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.client_factory = client_factory
        self.alias_service = alias_service
        self.clock = clock

    def pick_account(self) -> MailComAccount:
        job_id = None
        try:
            from core import registration_service
            job_id = getattr(registration_service._THREAD_CTX, "job_id", None)
        except Exception:
            job_id = None
        alias = db.claim_next_mailcom_alias(job_id=job_id)
        if alias is None:
            raise MailComProviderError("mail.com 别名池没有可用地址，或所有母号当前都有注册任务")
        alias_account = MailComAccount(
            id=alias.get("id"),
            email=str(alias.get("alias_email") or ""),
            status="leased",
            used_at=alias.get("lease_started_at") or alias.get("created_at"),
        )
        logger.info(
            "[MailCom] 已领取别名: alias=%s parent=%s",
            _mask_email(alias_account.email),
            _mask_email(str(alias.get("parent_email") or "")),
        )
        return alias_account

    def get_account_context(self, email: str) -> MailComAccount | None:
        alias = db.get_mailcom_alias_internal(email)
        if alias:
            if str(alias.get("status") or "") not in {"available", "leased", "registered"}:
                return None
            email = alias.get("parent_email") or email
        record = db.get_mailcom_internal_record(email)
        if not record or str(record.get("status") or "") in {"disabled", "failed"}:
            return None
        return _account_from_record(record)

    def release_account(self, email: str, status: str = "available", note: str | None = None) -> None:
        alias = db.get_mailcom_alias_internal(email)
        if alias:
            # 别名任务失败只影响该别名；不能把共享母号误标为 failed/disabled。
            if status in {"failed", "disabled"}:
                db.mark_mailcom_alias_registration_failed(email, note or f"任务状态: {status}")
            elif status == "available":
                db.mark_mailcom_alias_registration_failed(email, note or "任务未消耗别名")
            return
        db.release_mailcom_email(email, status=status, note=note)
        if status != "used":
            clear_account_context(email)

    def _new_client(self, *, access_token: str = "") -> MailComClient:
        factory = self.client_factory
        if factory is None:
            # 延迟解析便于热加载/测试替换，也避免改密历史模块成为依赖。
            from core.mailcom_client import MailComClient as factory
        try:
            return factory(access_token=access_token)
        except TypeError:
            # 兼容极简 mock factory；仅测试环境可能使用。
            client = factory()
            if access_token:
                setter = getattr(client, "set_access_token", None)
                if callable(setter):
                    setter(access_token)
                else:
                    client.access_token = access_token
            return client

    def _login_and_persist(self, record: dict, *, expected_token: str | None) -> dict:
        email = str(record.get("email") or "")
        password = str(record.get("password") or "")
        if not email or not password:
            db.release_mailcom_email(email, status="disabled", note="mail.com 账号或密码未配置")
            raise MailComProviderError("mail.com 账号或密码未配置")
        try:
            token = self._new_client().authenticate(email, password)
        except MailComCredentialError as exc:
            db.release_mailcom_email(email, status="disabled", note="mail.com 账密无效或需要人工验证")
            db.clear_mailcom_auth(email, expected_token=expected_token, error=exc.error_type)
            raise MailComProviderError("mail.com 账密无效、账号锁定或需要未支持的二次验证") from exc
        except MailComError as exc:
            # 网络、403/风控和协议错误不会清空可能仍可工作的旧 AT。
            current_error = exc.error_type
            db.record_mailcom_auth_error(email, current_error)
            raise MailComProviderError(f"mail.com 登录/换 token 失败（{exc.error_type}）") from exc
        written = db.update_mailcom_auth(
            email,
            token.access_token,
            token.expires_at,
            expected_token=expected_token,
            auth_error=None,
        )
        if not written:
            # 另一任务在我们登录期间已经写入新 AT，优先复用数据库值。
            current = db.get_mailcom_internal_record(email)
            if current and _has_valid_token(current, self.clock()):
                return current
            raise MailComProviderError("mail.com AT 条件写入冲突且没有可复用的新 token")
        current = db.get_mailcom_internal_record(email)
        if not current:
            raise MailComProviderError("mail.com AT 写入后账号记录不存在")
        put_account_context(_account_from_record(current))
        return current

    def _ensure_token(self, email: str, *, expected_token: str | None = None,
                      force_login: bool = False) -> dict:
        """邮箱级锁 + 二次检查，返回持久化的新/旧 token 记录。"""
        lock = _email_lock(email)
        with lock:
            record = db.get_mailcom_internal_record(email)
            if not record:
                raise MailComProviderError("mail.com 邮箱池中找不到该账号")
            if record.get("status") == "disabled":
                raise MailComProviderError("mail.com 账号已禁用，请修复账密或人工验证后再启用")
            now = self.clock()
            current_token = str(record.get("mail_access_token") or "")
            # 第二次检查能让等待锁的任务直接复用第一个任务写回的新 AT。
            if _has_valid_token(record, now) and (not force_login or current_token != str(expected_token or "")):
                return record
            if force_login and expected_token is not None and current_token == str(expected_token):
                # 精确 invalid_token 才清除旧 AT；非认证错误永不走这个分支。
                db.clear_mailcom_auth(email, expected_token=current_token, error="invalid_token")
                record = db.get_mailcom_internal_record(email) or record
                expected_token = ""
            return self._login_and_persist(record, expected_token=expected_token)

    def fetch_latest_otp(
        self,
        email: str,
        after_ts: float | None = None,
        max_wait: int | None = None,
        poll_interval: int | None = None,
        settle_seconds: int | None = None,
        recipient: str | None = None,
    ) -> str:
        alias = db.get_mailcom_alias_internal(email)
        if alias is None:
            parent_candidate = db.get_mailcom_internal_record(email)
            if parent_candidate is None:
                raise MailComProviderError("mail.com 别名映射不存在，不能把别名当作独立收件箱取码")
            parent_email = str(email or "")
            target_recipient = recipient
        else:
            parent_email = str(alias.get("parent_email") or "")
            target_recipient = str(alias.get("alias_email") or email)
        record = db.get_mailcom_internal_record(parent_email)
        if not record:
            raise MailComProviderError("mail.com 别名对应的母号不在邮箱池中")
        if alias and str(alias.get("status") or "") not in {"leased", "registered"}:
            raise MailComProviderError("mail.com 别名已失效，不能继续取码")
        if str(record.get("status") or "") in {"disabled", "failed"}:
            raise MailComProviderError("mail.com 别名对应的母号不可用")
        if alias:
            started_at = alias.get("registration_started_at")
            try:
                started_at = float(started_at) if started_at is not None else None
            except (TypeError, ValueError):
                started_at = None
            if started_at is not None:
                after_ts = max(float(after_ts), started_at) if after_ts is not None else started_at
        original_token = str(record.get("mail_access_token") or "")
        if not _has_valid_token(record, self.clock()):
            record = self._ensure_token(parent_email, expected_token=original_token, force_login=False)
        token = str(record.get("mail_access_token") or "")
        try:
            return self._new_client(access_token=token).fetch_latest_otp(
                after_ts=after_ts,
                max_wait=max_wait,
                poll_interval=poll_interval,
                settle_seconds=settle_seconds,
                recipient=target_recipient,
            )
        except MailComInvalidTokenError:
            # 只处理已验证的 401 + Bearer error=invalid_token；单次恢复后只重试原读。
            refreshed = self._ensure_token(parent_email, expected_token=token, force_login=True)
            fresh_token = str(refreshed.get("mail_access_token") or "")
            try:
                return self._new_client(access_token=fresh_token).fetch_latest_otp(
                    after_ts=after_ts,
                    max_wait=max_wait,
                    poll_interval=poll_interval,
                    settle_seconds=settle_seconds,
                    recipient=target_recipient,
                )
            except MailComInvalidTokenError as exc:
                raise MailComProviderError("mail.com AT 恢复后读取仍返回 invalid_token，已停止本次取码") from exc
            except MailComError as exc:
                raise MailComProviderError(f"mail.com AT 恢复后读取失败（{exc.error_type}）") from exc
        except MailComError as exc:
            if exc.error_type == "unauthorized":
                # 部分 mail.com 401 响应没有 Bearer invalid_token challenge，
                # 仍应立即刷新一次 Mailbox AT，而不是让轮询等到 timeout。
                refreshed = self._ensure_token(parent_email, expected_token=token, force_login=True)
                fresh_token = str(refreshed.get("mail_access_token") or "")
                try:
                    return self._new_client(access_token=fresh_token).fetch_latest_otp(
                        after_ts=after_ts,
                        max_wait=max_wait,
                        poll_interval=poll_interval,
                        settle_seconds=settle_seconds,
                        recipient=target_recipient,
                    )
                except MailComError as retry_exc:
                    raise MailComProviderError(f"mail.com AT 刷新后读取失败（{retry_exc.error_type}）") from retry_exc
            # 401 无 Bearer invalid_token、403、429、5xx、超时与风控 HTML 之外的
            # 普通错误继续保持原策略：不因网络/业务错误自动重新登录。
            db.record_mailcom_auth_error(parent_email, exc.error_type)
            raise MailComProviderError(f"mail.com 读取失败（{exc.error_type}），未自动重新登录") from exc


_DEFAULT_PROVIDER = MailComProvider()


def pick_account() -> MailComAccount:
    return _DEFAULT_PROVIDER.pick_account()


def get_account_context(email: str) -> MailComAccount | None:
    return _DEFAULT_PROVIDER.get_account_context(email)


def fetch_latest_otp(email: str, **kwargs: Any) -> str:
    return _DEFAULT_PROVIDER.fetch_latest_otp(email, **kwargs)


def release_account(email: str, status: str = "available", note: str | None = None) -> None:
    _DEFAULT_PROVIDER.release_account(email, status=status, note=note)


__all__ = [
    "MailComProviderError", "MailComProvider", "pick_account", "get_account_context",
    "fetch_latest_otp", "release_account",
]
