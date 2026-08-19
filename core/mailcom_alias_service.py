# -*- coding: utf-8 -*-
"""mail.com 母号别名的创建、确认和删除生命周期。"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from core import db
from core.mailcom_alias_domains import (
    MailComAliasDomainError,
    choose_alias_domain,
    generate_alias_local_part,
)
from core.mailcom_settings_client import (
    MailComSettingsClient,
    MailComSettingsConfirmationError,
    MailComSettingsConflictError,
    MailComSettingsCredentialError,
    MailComSettingsError,
    canonical_email,
    is_active_deletable_address,
)


logger = logging.getLogger(__name__)
MAX_ACTIVE_ALIASES = 9
MAX_CREATE_ATTEMPTS = 5
MAX_SYNC_VALIDATION_ATTEMPTS = 3
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class MailComAliasError(RuntimeError):
    """别名生命周期的上层脱敏错误。"""

    def __init__(self, message: str, *, error_type: str = "alias_error") -> None:
        super().__init__(message)
        self.error_type = error_type


class MailComAliasCapacityError(MailComAliasError):
    def __init__(self, active_count: int) -> None:
        super().__init__(
            f"mail.com 母号活动别名已达到 {MAX_ACTIVE_ALIASES} 个上限，当前任务已终止",
            error_type="alias_capacity_full",
        )
        self.active_count = active_count


def _key(email: str) -> str:
    return canonical_email(email)


def _mask(email: str) -> str:
    local, separator, domain = _key(email).partition("@")
    return f"{local[:1]}***@{domain}" if separator else "[redacted-email]"


def mother_alias_lock(parent_email: str) -> threading.Lock:
    """创建和删除共用的母号级锁，不与 mailbox AT 刷新锁复用。"""
    key = _key(parent_email)
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


def active_deletable_count(addresses: list[dict[str, Any]]) -> int:
    return sum(1 for item in addresses if isinstance(item, dict) and is_active_deletable_address(item))


class MailComAliasService:
    def __init__(
        self,
        *,
        settings_client_factory: Callable[[], MailComSettingsClient] | None = None,
        max_attempts: int = MAX_CREATE_ATTEMPTS,
    ) -> None:
        self.settings_client_factory = settings_client_factory or MailComSettingsClient
        self.max_attempts = max(1, int(max_attempts))

    def _client_for_parent(self, parent: dict) -> MailComSettingsClient:
        email = canonical_email(parent.get("email"))
        password = str(parent.get("password") or "")
        if not email or not password:
            raise MailComAliasError("mail.com 母号缺少账号或密码", error_type="parent_credentials_missing")
        client = self.settings_client_factory()
        try:
            client.authenticate(email, password)
        except MailComSettingsCredentialError as exc:
            raise MailComAliasError("mail.com 母号 settings 登录无效或需要人工验证", error_type="invalid_credentials") from exc
        except MailComSettingsError as exc:
            raise MailComAliasError(
                f"mail.com settings 登录失败（{exc.error_type}）",
                error_type=exc.error_type,
            ) from exc
        return client

    def create_alias(self, parent_record: dict, *, job_id: int | None = None) -> dict:
        parent_email = canonical_email(parent_record.get("email"))
        if not parent_email:
            raise MailComAliasError("mail.com 母号不存在", error_type="parent_missing")
        with mother_alias_lock(parent_email):
            client = self._client_for_parent(parent_record)
            try:
                before = client.list_addresses()
            except MailComSettingsError as exc:
                raise MailComAliasError(
                    f"mail.com 别名容量查询失败（{exc.error_type}）",
                    error_type=exc.error_type,
                ) from exc
            count = active_deletable_count(before)
            if count >= MAX_ACTIVE_ALIASES:
                logger.warning("[MailComAlias] 母号别名已满: parent=%s active=%s", _mask(parent_email), count)
                raise MailComAliasCapacityError(count)

            last_conflict: Exception | None = None
            for _ in range(self.max_attempts):
                try:
                    local_part = generate_alias_local_part()
                    domain = choose_alias_domain()
                except MailComAliasDomainError as exc:
                    raise MailComAliasError(str(exc), error_type="alias_domain_config") from exc
                alias_email = f"{local_part}@{domain}"
                try:
                    client.validate_address(alias_email)
                    client.create_address(alias_email)
                except MailComSettingsConflictError as exc:
                    last_conflict = exc
                    continue
                except MailComSettingsError as exc:
                    raise MailComAliasError(
                        f"mail.com 别名创建失败（{exc.error_type}）",
                        error_type=exc.error_type,
                    ) from exc

                try:
                    after = client.list_addresses()
                except MailComSettingsError as exc:
                    raise MailComAliasError(
                        f"mail.com 别名创建后确认失败（{exc.error_type}）",
                        error_type=exc.error_type,
                    ) from exc
                confirmed = next(
                    (
                        item for item in after
                        if isinstance(item, dict) and is_active_deletable_address(item, alias_email)
                    ),
                    None,
                )
                if confirmed is None:
                    raise MailComAliasError(
                        "mail.com 别名创建后未在地址列表确认，当前任务已终止",
                        error_type="confirmation_failed",
                    )
                alias = db.create_mailcom_alias(
                    alias_email=alias_email,
                    parent_email=parent_email,
                    local_part=local_part,
                    domain=domain,
                    job_id=job_id,
                )
                logger.info("[MailComAlias] 已创建并确认别名: parent=%s alias=%s", _mask(parent_email), _mask(alias_email))
                return alias
            raise MailComAliasError(
                "mail.com 候选别名多次冲突或被拒绝，当前任务已终止",
                error_type="address_conflict",
            ) from last_conflict

    def sync_parent_aliases(self, parent_record: dict, *, target: int = MAX_ACTIVE_ALIASES) -> dict:
        """两阶段读取远端快照，并按首次缺口执行有限创建。"""
        parent_email = canonical_email(parent_record.get("email"))
        if not parent_email:
            raise MailComAliasError("mail.com 母号不存在", error_type="parent_missing")
        target = max(0, min(MAX_ACTIVE_ALIASES, int(target)))
        with mother_alias_lock(parent_email):
            if db.mailcom_parent_registration_busy(parent_email):
                raise MailComAliasError(
                    "mail.com 母号正在执行注册任务，暂不能同步 alias",
                    error_type="registration_busy",
                )
            client = self._client_for_parent(parent_record)
            try:
                initial_rows = client.list_addresses()
            except MailComSettingsError as exc:
                raise MailComAliasError(
                    f"mail.com 别名同步查询失败（{exc.error_type}）",
                    error_type=exc.error_type,
                ) from exc

            initial_active = [
                item for item in initial_rows
                if isinstance(item, dict) and is_active_deletable_address(item)
            ]
            create_opportunities = max(0, target - len(initial_active))
            create_requests: list[str] = []
            validation_failures = 0

            for _ in range(create_opportunities):
                candidate = ""
                for _validation_attempt in range(MAX_SYNC_VALIDATION_ATTEMPTS):
                    try:
                        local_part = generate_alias_local_part()
                        domain = choose_alias_domain()
                    except MailComAliasDomainError as exc:
                        raise MailComAliasError(str(exc), error_type="alias_domain_config") from exc
                    candidate = f"{local_part}@{domain}"
                    try:
                        client.validate_address(candidate)
                    except MailComSettingsError as exc:
                        validation_failures += 1
                        logger.warning(
                            "[MailComAlias] 候选校验失败: parent=%s type=%s",
                            _mask(parent_email),
                            exc.error_type,
                        )
                        candidate = ""
                        continue
                    break
                if not candidate:
                    continue

                create_requests.append(candidate)
                try:
                    client.create_address(candidate)
                except MailComSettingsError as exc:
                    logger.warning(
                        "[MailComAlias] 创建请求未确认且不重试: parent=%s alias=%s type=%s",
                        _mask(parent_email),
                        _mask(candidate),
                        exc.error_type,
                    )

            try:
                final_rows = client.list_addresses()
            except MailComSettingsError as exc:
                raise MailComAliasError(
                    f"mail.com 别名最终列表查询失败（{exc.error_type}）",
                    error_type=exc.error_type,
                ) from exc

            final_aliases = [
                canonical_email(item.get("address"))
                for item in final_rows
                if isinstance(item, dict)
                and is_active_deletable_address(item)
                and canonical_email(item.get("address")) != parent_email
            ]
            replaced = db.replace_mailcom_alias_snapshot(parent_email, final_aliases)
            if replaced is None:
                raise MailComAliasError(
                    "mail.com 母号注册租约在同步期间被占用，本地快照未更新",
                    error_type="registration_busy",
                )
            return {
                "parent_email": parent_email,
                "remote_active_alias_count": len(replaced),
                "create_opportunity_count": create_opportunities,
                "create_request_count": len(create_requests),
                "create_requested_aliases": create_requests,
                "created_count": len(create_requests),
                "created_aliases": create_requests,
                "validation_failure_count": validation_failures,
            }

    def delete_alias(self, alias_record: dict) -> bool:
        alias_email = canonical_email(alias_record.get("alias_email"))
        parent_email = canonical_email(alias_record.get("parent_email"))
        if not alias_email or not parent_email:
            raise MailComAliasError("mail.com 别名记录不完整", error_type="alias_record_invalid")
        if str(alias_record.get("status") or "").casefold() == "deleted":
            return True
        parent = db.get_mailcom_internal_record(parent_email)
        if not parent:
            raise MailComAliasError("mail.com 别名母号不存在", error_type="parent_missing")
        with mother_alias_lock(parent_email):
            client = self._client_for_parent(parent)
            try:
                deleted = client.delete_address(alias_email)
            except MailComSettingsError as exc:
                raise MailComAliasError(
                    f"mail.com 删除别名失败（{exc.error_type}）",
                    error_type=exc.error_type,
                ) from exc
            if not deleted:
                raise MailComAliasError(
                    "mail.com 删除别名接口未确认成功",
                    error_type="delete_unconfirmed",
                )
            db.mark_mailcom_alias_deleted(alias_email)
            logger.info("[MailComAlias] 已删除别名: parent=%s alias=%s", _mask(parent_email), _mask(alias_email))
            return True


_DEFAULT_SERVICE = MailComAliasService()


def create_alias(parent_record: dict, *, job_id: int | None = None) -> dict:
    return _DEFAULT_SERVICE.create_alias(parent_record, job_id=job_id)


def delete_alias(alias_record: dict) -> bool:
    return _DEFAULT_SERVICE.delete_alias(alias_record)


def sync_parent_aliases(parent_record: dict, *, target: int = MAX_ACTIVE_ALIASES) -> dict:
    return _DEFAULT_SERVICE.sync_parent_aliases(parent_record, target=target)


__all__ = [
    "MAX_ACTIVE_ALIASES",
    "MAX_CREATE_ATTEMPTS",
    "MAX_SYNC_VALIDATION_ATTEMPTS",
    "MailComAliasCapacityError",
    "MailComAliasError",
    "MailComAliasService",
    "active_deletable_count",
    "create_alias",
    "delete_alias",
    "mother_alias_lock",
    "sync_parent_aliases",
]
