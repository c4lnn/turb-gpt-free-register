# -*- coding: utf-8 -*-
"""mail.com 地址设置协议客户端。

此模块和 mailbox AT 读取 client 分离。settings Bearer token 只按母号在当前
进程短时缓存；别名请求仍在每个 client 自己的临时登录 session 中执行，绝不
持久化 Cookie、sid 或其他 settings 认证材料。
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import threading
import time
from typing import Any, Callable, Mapping
from urllib.parse import quote
from uuid import uuid4

from config import email as _email_cfg
from core.mailcom_client import MailComClient, MailComCredentialError, MailComError
from core.mailcom_capacity import (
    MAX_LIFETIME_ALIASES,
    MailComCapacitySnapshot,
    aggregate_history_payload,
)
from core.mailcom_protocol import redact_mapping, safe_request_diagnostic


SETTINGS_API = "https://settings-cats.mail.com"
SETTINGS_ORIGIN = "https://mailset-root.mail.com"
SETTINGS_UI_APP = "mailcom.mailset-compose/1.0.6"
DEFAULT_TIMEOUT = 30
SETTINGS_TOKEN_REFRESH_SKEW_SECONDS = 60

logger = logging.getLogger(__name__)


class MailComSettingsError(RuntimeError):
    """地址设置请求失败，错误信息不携带认证材料或响应正文。"""

    def __init__(
        self,
        message: str,
        *,
        error_type: str = "settings_error",
        diagnostic: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.diagnostic = diagnostic or {}


class MailComSettingsCredentialError(MailComSettingsError):
    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__(message, error_type="invalid_credentials", **kwargs)


class MailComSettingsConflictError(MailComSettingsError):
    def __init__(self, message: str = "mail.com 别名地址不可创建", **kwargs: Any) -> None:
        super().__init__(message, error_type="address_conflict", **kwargs)


class MailComSettingsRemoteConflictError(MailComSettingsError):
    """创建端点返回 HTTP 409 的远端业务冲突。

    该错误与候选地址的 412 冲突严格分开，调用方可据此触发一次容量校准。
    """

    def __init__(self, message: str = "mail.com 别名创建遭遇远端业务冲突", **kwargs: Any) -> None:
        super().__init__(message, error_type="remote_create_conflict", **kwargs)


class MailComSettingsConfirmationError(MailComSettingsError):
    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__(message, error_type="confirmation_failed", **kwargs)


def canonical_email(value: object) -> str:
    return str(value or "").strip().casefold()


@dataclass(frozen=True)
class _SettingsTokenCacheEntry:
    """仅保存 settings Bearer token 及其过期时间，绝不保存会话状态。"""

    access_token: str
    expires_at: float


_SETTINGS_TOKEN_CACHE: dict[str, _SettingsTokenCacheEntry] = {}
_SETTINGS_TOKEN_LOCKS: dict[str, threading.Lock] = {}
_SETTINGS_TOKEN_LOCKS_GUARD = threading.Lock()


def _settings_token_lock(parent_email: str) -> threading.Lock:
    """settings token 刷新锁，独立于 mailbox AT 与别名生命周期锁。"""
    key = canonical_email(parent_email)
    with _SETTINGS_TOKEN_LOCKS_GUARD:
        return _SETTINGS_TOKEN_LOCKS.setdefault(key, threading.Lock())


def is_active_deletable_address(item: Mapping[str, Any], address: str | None = None) -> bool:
    """判断地址列表项是否是已激活且可删除的别名。"""
    if str(item.get("state") or "").casefold() != "active":
        return False
    if item.get("deletable") is not True:
        return False
    if address is None:
        return True
    return canonical_email(item.get("address")) == canonical_email(address)


class MailComSettingsClient:
    """可注入 session 的 settings client，便于使用脱敏 HAR fixture 做契约测试。"""

    def __init__(
        self,
        session: Any | None = None,
        *,
        timeout: int | None = None,
        login_client_factory: Callable[..., MailComClient] | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.session = session
        self.timeout = int(
            timeout
            if timeout is not None
            else getattr(_email_cfg, "MAILCOM_REQUEST_TIMEOUT", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT
        )
        self.login_client_factory = login_client_factory
        self._now = now or time.time
        self._authenticated = session is not None
        self._settings_access_token = ""
        self._settings_cache_entry: _SettingsTokenCacheEntry | None = None
        self._parent_email = ""
        self._password = ""

    def _headers(self, accept: str, content_type: str | None = None) -> dict[str, str]:
        headers = {
            "Accept": accept,
            "Origin": SETTINGS_ORIGIN,
            "Referer": SETTINGS_ORIGIN + "/",
            "X-Request-ID": str(uuid4()),
            "X-UI-App": SETTINGS_UI_APP,
        }
        if content_type:
            headers["Content-Type"] = content_type
        if self._settings_access_token:
            headers["Authorization"] = f"Bearer {self._settings_access_token}"
        return headers

    def _cache_entry_is_fresh(self, entry: _SettingsTokenCacheEntry | None) -> bool:
        return bool(
            entry
            and entry.access_token
            and entry.expires_at - float(self._now()) > SETTINGS_TOKEN_REFRESH_SKEW_SECONDS
        )

    def _new_transport_session(self) -> Any:
        """为命中缓存的 client 创建新的临时 HTTP session。"""
        factory = self.login_client_factory or MailComClient
        client = factory()
        session = getattr(client, "session", None)
        if session is None:
            raise MailComSettingsError("mail.com settings 没有返回临时会话", error_type="auth_session_missing")
        return session

    def _set_authenticated(self, entry: _SettingsTokenCacheEntry, session: Any) -> None:
        self.session = session
        self._settings_access_token = entry.access_token
        self._settings_cache_entry = entry
        self._authenticated = True

    def _login_and_bootstrap(self) -> tuple[_SettingsTokenCacheEntry, Any]:
        factory = self.login_client_factory or MailComClient
        try:
            client = factory()
            client.login(self._parent_email, self._password)
            token = client.bootstrap_settings_session()
        except MailComCredentialError as exc:
            raise MailComSettingsCredentialError("mail.com settings 登录被拒绝或需要人工验证") from exc
        except MailComError as exc:
            raise MailComSettingsError(
                f"mail.com settings 登录失败（{exc.error_type}）",
                error_type=exc.error_type,
                diagnostic=redact_mapping(exc.diagnostic),
            ) from exc
        session = getattr(client, "session", None)
        if session is None:
            raise MailComSettingsError("mail.com settings 登录没有返回临时会话", error_type="auth_session_missing")
        access_token = str(getattr(token, "access_token", "") or "").strip()
        if not access_token:
            raise MailComSettingsError("mail.com settings 登录没有返回临时访问令牌", error_type="auth_token_missing")
        try:
            expires_at = float(getattr(token, "expires_at"))
        except (TypeError, ValueError, AttributeError) as exc:
            raise MailComSettingsError(
                "mail.com settings 登录没有返回有效访问令牌过期时间",
                error_type="auth_token_expiry_missing",
            ) from exc
        if expires_at <= float(self._now()):
            raise MailComSettingsError(
                "mail.com settings 登录返回的访问令牌已过期",
                error_type="auth_token_expired",
            )
        return _SettingsTokenCacheEntry(access_token=access_token, expires_at=expires_at), session

    def _attach_cached_token(self, entry: _SettingsTokenCacheEntry) -> None:
        self._set_authenticated(entry, self._new_transport_session())

    def _refresh_settings_token_after_unauthorized(
        self,
        observed_token: str,
        *,
        observed_entry: _SettingsTokenCacheEntry | None = None,
    ) -> None:
        """在 401 后串行刷新；同一旧 token 只会触发一次登录。"""
        if not self._parent_email or not self._password:
            raise MailComSettingsCredentialError("mail.com settings 缺少母号登录上下文，不能刷新访问令牌")
        with _settings_token_lock(self._parent_email):
            cached = _SETTINGS_TOKEN_CACHE.get(self._parent_email)
            if (
                self._cache_entry_is_fresh(cached)
                and cached is not None
                and cached is not observed_entry
                and (observed_entry is not None or cached.access_token != observed_token)
            ):
                self._attach_cached_token(cached)
                logger.info("[MailComSettings] stage=settings_token_cache action=reuse_after_401")
                return
            if cached is observed_entry or (observed_entry is None and cached is not None and cached.access_token == observed_token):
                _SETTINGS_TOKEN_CACHE.pop(self._parent_email, None)
            logger.info("[MailComSettings] stage=settings_token_cache action=refresh_after_401")
            entry, session = self._login_and_bootstrap()
            _SETTINGS_TOKEN_CACHE[self._parent_email] = entry
            self._set_authenticated(entry, session)

    def _can_refresh_after_unauthorized(self) -> bool:
        return bool(self._parent_email and self._password)

    def authenticate(self, email: str, password: str) -> None:
        """复用未临期 settings token；登录会话仍只存在于当前 client。"""
        self._parent_email = canonical_email(email)
        self._password = str(password or "")
        if not self._parent_email or not self._password:
            raise MailComSettingsCredentialError("mail.com settings 账号或密码为空/格式无效")
        if self._authenticated:
            return
        cached = _SETTINGS_TOKEN_CACHE.get(self._parent_email)
        if self._cache_entry_is_fresh(cached):
            self._attach_cached_token(cached)
            logger.info("[MailComSettings] stage=settings_token_cache action=hit")
            return
        with _settings_token_lock(self._parent_email):
            cached = _SETTINGS_TOKEN_CACHE.get(self._parent_email)
            if self._cache_entry_is_fresh(cached):
                self._attach_cached_token(cached)
                logger.info("[MailComSettings] stage=settings_token_cache action=hit_after_lock")
                return
            logger.info("[MailComSettings] stage=settings_token_cache action=miss")
            entry, session = self._login_and_bootstrap()
            _SETTINGS_TOKEN_CACHE[self._parent_email] = entry
            self._set_authenticated(entry, session)

    def _request(
        self,
        method: str,
        path: str,
        *,
        endpoint: str,
        expected: set[int],
        headers: Mapping[str, Any],
        **kwargs: Any,
    ) -> Any:
        if self.session is None:
            raise MailComSettingsCredentialError("mail.com settings 会话尚未登录")
        logger.info("[MailComSettings] stage=%s action=request", endpoint)
        try:
            request = getattr(self.session, method.lower())
            response = request(SETTINGS_API + path, headers=dict(headers), timeout=self.timeout, **kwargs)
        except Exception as exc:
            logger.warning("[MailComSettings] stage=%s action=error error_type=network_error", endpoint)
            raise MailComSettingsError(
                f"mail.com settings {endpoint} 网络请求失败（{type(exc).__name__}）",
                error_type="network_error",
                diagnostic=safe_request_diagnostic(endpoint=endpoint, error_type="network_error"),
            ) from exc
        try:
            status = int(getattr(response, "status_code", 0) or 0)
        except (TypeError, ValueError):
            status = 0
        if status in expected:
            logger.info("[MailComSettings] stage=%s action=success status=%s", endpoint, status)
            return response
        if status in {401, 403}:
            error_type = "unauthorized" if status == 401 else "forbidden_or_risk"
        elif status == 429:
            error_type = "rate_limited"
        elif status >= 500:
            error_type = "upstream_error"
        else:
            error_type = "http_error"
        logger.warning(
            "[MailComSettings] stage=%s action=error status=%s error_type=%s",
            endpoint,
            status,
            error_type,
        )
        raise MailComSettingsError(
            f"mail.com settings {endpoint} 请求失败（HTTP {status}，{error_type}）",
            error_type=error_type,
            diagnostic=safe_request_diagnostic(
                endpoint=endpoint,
                status=status,
                headers=getattr(response, "headers", {}),
                error_type=error_type,
            ),
        )

    @staticmethod
    def _json(response: Any, endpoint: str) -> Any:
        try:
            return response.json()
        except (TypeError, ValueError) as exc:
            logger.warning("[MailComSettings] stage=%s action=error error_type=invalid_json", endpoint)
            raise MailComSettingsError(
                f"mail.com settings {endpoint} 响应不是 JSON",
                error_type="invalid_json",
                diagnostic=safe_request_diagnostic(
                    endpoint=endpoint,
                    status=getattr(response, "status_code", None),
                    headers=getattr(response, "headers", {}),
                    error_type="invalid_json",
                ),
            ) from exc

    def _list_addresses_once(self) -> list[dict[str, Any]]:
        media = "application/vnd.ui.trinity.mailaddress.list-v5+json"
        response = self._request(
            "GET",
            "/mailaccount/primary/emailAddresses",
            endpoint="email_addresses",
            expected={200},
            params={"absoluteURI": "false", "q.state.in": "ACTIVE", "q.type.in": "MANAGED,DOMAIN_HOSTING"},
            headers=self._headers(media, media),
        )
        payload = self._json(response, "email_addresses")
        rows = payload.get("mailaddresslist") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or any(not isinstance(item, dict) for item in rows):
            logger.warning("[MailComSettings] stage=email_addresses action=error error_type=protocol_error")
            raise MailComSettingsError("mail.com settings 地址列表缺少 mailaddresslist", error_type="protocol_error")
        normalized: list[dict[str, Any]] = []
        for item in rows:
            address = canonical_email(item.get("address"))
            state = item.get("state")
            deletable = item.get("deletable")
            if "@" not in address or not isinstance(state, str) or not state.strip() or not isinstance(deletable, bool):
                logger.warning("[MailComSettings] stage=email_addresses action=error error_type=protocol_error")
                raise MailComSettingsError(
                    "mail.com settings 地址列表缺少 address/state/deletable 协议字段",
                    error_type="protocol_error",
                )
            row = dict(item)
            row["address"] = address
            row["state"] = state.strip().upper()
            normalized.append(row)
        return normalized

    def list_addresses(self) -> list[dict[str, Any]]:
        """地址列表 GET 在 token 401 后只刷新并重试一次。"""
        try:
            return self._list_addresses_once()
        except MailComSettingsError as exc:
            if exc.error_type != "unauthorized" or not self._can_refresh_after_unauthorized():
                raise
            self._refresh_settings_token_after_unauthorized(
                self._settings_access_token,
                observed_entry=self._settings_cache_entry,
            )
            return self._list_addresses_once()

    def _history_snapshot_once(self) -> MailComCapacitySnapshot:
        """读取不带 ``q.state.in`` 的全量地址并立即聚合。"""
        media = "application/vnd.ui.trinity.mailaddress.list-v5+json"
        response = self._request(
            "GET",
            "/mailaccount/primary/emailAddresses",
            endpoint="email_addresses_history",
            expected={200},
            params={"absoluteURI": "false", "q.type.in": "MANAGED,DOMAIN_HOSTING"},
            headers=self._headers(media, media),
        )
        payload = self._json(response, "email_addresses_history")
        try:
            snapshot = aggregate_history_payload(
                payload,
                lifetime_limit=MAX_LIFETIME_ALIASES,
            )
        except ValueError as exc:
            logger.warning(
                "[MailComSettings] stage=email_addresses_history action=error error_type=protocol_error"
            )
            raise MailComSettingsError(
                "mail.com settings 历史地址响应协议不完整",
                error_type="protocol_error",
                diagnostic=safe_request_diagnostic(
                    endpoint="email_addresses_history",
                    status=getattr(response, "status_code", None),
                    headers=getattr(response, "headers", {}),
                    error_type="protocol_error",
                ),
            ) from exc
        if not snapshot.complete:
            logger.warning(
                "[MailComSettings] stage=email_addresses_history action=error error_type=protocol_incomplete"
            )
        return snapshot

    def history_snapshot(self) -> MailComCapacitySnapshot:
        """历史容量 GET 在 token 401 后只刷新并重试一次。"""
        try:
            return self._history_snapshot_once()
        except MailComSettingsError as exc:
            if exc.error_type != "unauthorized" or not self._can_refresh_after_unauthorized():
                raise
            self._refresh_settings_token_after_unauthorized(
                self._settings_access_token,
                observed_entry=self._settings_cache_entry,
            )
            return self._history_snapshot_once()

    # 这些名称用于调用方/契约测试的语义化别名；都只返回聚合对象，不暴露地址行。
    def get_history_snapshot(self) -> MailComCapacitySnapshot:
        return self.history_snapshot()

    def list_address_history(self) -> MailComCapacitySnapshot:
        return self.history_snapshot()

    def list_addresses_history(self) -> MailComCapacitySnapshot:
        return self.history_snapshot()

    def _validate_address_once(self, candidate: str) -> None:
        response = self._request(
            "POST",
            "/mailaccount/emailAddressValidations",
            endpoint="email_address_validation",
            expected={200},
            params={"absoluteURI": "false"},
            json=[candidate],
            headers=self._headers(
                "application/vnd.ui.trinity.email-address-validation-response+json",
                "application/vnd.ui.trinity.email-address-validation-request+json",
            ),
        )
        payload = self._json(response, "email_address_validation")
        if isinstance(payload, dict) and payload.get("valid") is False:
            logger.warning("[MailComSettings] stage=email_address_validation action=error error_type=address_conflict")
            raise MailComSettingsConflictError("mail.com 候选别名未通过服务端校验")

    def validate_address(self, address: str) -> None:
        candidate = canonical_email(address)
        if "@" not in candidate:
            raise MailComSettingsConflictError("mail.com 候选别名格式无效")
        try:
            self._validate_address_once(candidate)
        except MailComSettingsError as exc:
            if exc.error_type != "unauthorized" or not self._can_refresh_after_unauthorized():
                raise
            self._refresh_settings_token_after_unauthorized(
                self._settings_access_token,
                observed_entry=self._settings_cache_entry,
            )
            self._validate_address_once(candidate)

    def _create_address_once(self, candidate: str) -> None:
        media = "application/vnd.ui.trinity.minimalmailaddress-v3+json"
        response = self._request(
            "POST",
            "/mailaccount/primary/emailAddresses",
            endpoint="email_address_create",
            expected={201, 409, 412},
            params={"absoluteURI": "false"},
            json={
                "address": candidate,
                "deletable": True,
                "pgpEnabled": False,
                "defaultSenderAddress": False,
                "defaultReceiverAddress": False,
                "state": "ACTIVE",
            },
            headers=self._headers(media, media),
        )
        status = int(getattr(response, "status_code", 0) or 0)
        if status == 409:
            logger.warning(
                "[MailComSettings] stage=email_address_create action=error status=409 error_type=remote_create_conflict"
            )
            raise MailComSettingsRemoteConflictError(
                diagnostic=safe_request_diagnostic(
                    endpoint="email_address_create",
                    status=409,
                    headers=getattr(response, "headers", {}),
                    error_type="remote_create_conflict",
                )
            )
        if status == 412:
            logger.warning("[MailComSettings] stage=email_address_create action=error status=412 error_type=address_conflict")
            raise MailComSettingsConflictError(
                "mail.com 拒绝创建候选别名",
                diagnostic=safe_request_diagnostic(
                    endpoint="email_address_create",
                    status=412,
                    headers=getattr(response, "headers", {}),
                    error_type="address_conflict",
                ),
            )

    def create_address(self, address: str) -> None:
        """创建 401 后不重放 POST，只刷新并回读确认最终状态。"""
        candidate = canonical_email(address)
        try:
            self._create_address_once(candidate)
            return
        except MailComSettingsError as exc:
            if exc.error_type != "unauthorized" or not self._can_refresh_after_unauthorized():
                raise
        self._refresh_settings_token_after_unauthorized(
            self._settings_access_token,
            observed_entry=self._settings_cache_entry,
        )
        addresses = self.list_addresses()
        if any(is_active_deletable_address(item, candidate) for item in addresses):
            logger.info("[MailComSettings] stage=email_address_create action=confirmed_after_401")
            return
        logger.warning("[MailComSettings] stage=email_address_create action=confirmation_failed_after_401")
        raise MailComSettingsConfirmationError(
            "mail.com 别名创建返回 401，刷新后地址列表未确认该别名",
        )

    def _delete_address_once(self, candidate: str) -> bool:
        encoded = quote(candidate, safe="")
        response = self._request(
            "POST",
            f"/mailaccount/primary/emailAddressesRemovals/{encoded}/removals",
            endpoint="email_address_delete",
            expected={204, 404},
            params={"absoluteURI": "false"},
            headers=self._headers("text/plain;charset=UTF-8", "text/plain;charset=UTF-8"),
        )
        return int(getattr(response, "status_code", 0) or 0) == 204

    def delete_address(self, address: str) -> bool:
        """删除 401 后不重放 POST，只刷新并回读确认最终状态。"""
        candidate = canonical_email(address)
        try:
            return self._delete_address_once(candidate)
        except MailComSettingsError as exc:
            if exc.error_type != "unauthorized" or not self._can_refresh_after_unauthorized():
                raise
        self._refresh_settings_token_after_unauthorized(
            self._settings_access_token,
            observed_entry=self._settings_cache_entry,
        )
        addresses = self.list_addresses()
        if not any(canonical_email(item.get("address")) == candidate for item in addresses):
            logger.info("[MailComSettings] stage=email_address_delete action=confirmed_after_401")
            return True
        logger.warning("[MailComSettings] stage=email_address_delete action=confirmation_failed_after_401")
        raise MailComSettingsConfirmationError(
            "mail.com 别名删除返回 401，刷新后地址列表仍存在该别名",
        )


__all__ = [
    "DEFAULT_TIMEOUT",
    "MailComSettingsClient",
    "MailComSettingsConfirmationError",
    "MailComSettingsConflictError",
    "MailComSettingsCredentialError",
    "MailComSettingsError",
    "MailComSettingsRemoteConflictError",
    "SETTINGS_API",
    "SETTINGS_TOKEN_REFRESH_SKEW_SECONDS",
    "canonical_email",
    "is_active_deletable_address",
]
