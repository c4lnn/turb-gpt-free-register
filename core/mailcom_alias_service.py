# -*- coding: utf-8 -*-
"""mail.com 母号别名的创建、确认和删除生命周期。"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any, Callable

from core import db
from config import email as email_cfg
from core.mailcom_capacity import (
    CAPACITY_ACTIVE_FULL,
    CAPACITY_LIFETIME_FULL,
    CAPACITY_QUERY_UNKNOWN,
    CAPACITY_UNKNOWN,
    DEFAULT_HISTORY_TTL_SECONDS,
    DEFAULT_NEAR_LIMIT_REMAINING,
    MAX_LIFETIME_ALIASES,
    MailComCapacitySnapshot,
    aggregate_history_payload,
    aggregate_history_rows,
    capacity_status,
    lifetime_remaining,
    parse_snapshot_time,
    history_refresh_lock,
)
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
    MailComSettingsRemoteConflictError,
    canonical_email,
    is_active_deletable_address,
)


logger = logging.getLogger(__name__)
MAX_ACTIVE_ALIASES = 9
MAX_LIFETIME_ALIAS_COUNT = MAX_LIFETIME_ALIASES
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


class MailComAliasLifetimeCapacityError(MailComAliasError):
    def __init__(self, lifetime_count: int | None = None) -> None:
        super().__init__(
            f"mail.com 母号生命周期别名已达到 {MAX_LIFETIME_ALIASES} 个上限，当前任务已终止",
            error_type="lifetime_capacity_full",
        )
        self.lifetime_count = lifetime_count


def _key(email: str) -> str:
    return canonical_email(email)


def _mask(email: str) -> str:
    local, separator, domain = _key(email).partition("@")
    return f"{local[:1]}***@{domain}" if separator else "[redacted-email]"


def _parent_label(email: str) -> str:
    value = _key(email)
    local, separator, domain = value.partition("@")
    return f"{local[:1]}***@{domain}" if separator else "[invalid-email]"


def mother_alias_lock(parent_email: str) -> threading.Lock:
    """创建和删除共用的母号级锁，不与 mailbox AT 刷新锁复用。"""
    key = _key(parent_email)
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


def active_deletable_count(addresses: list[dict[str, Any]]) -> int:
    return sum(1 for item in addresses if isinstance(item, dict) and is_active_deletable_address(item))


def _configured_nonnegative(name: str, default: int) -> int:
    try:
        value = int(getattr(email_cfg, name, default))
    except (TypeError, ValueError):
        value = default
    return value if value >= 0 else default


def _capacity_status_for_runtime(
    active_count: int | None,
    lifetime_count: int | None,
    *,
    lifetime_limit: int = MAX_LIFETIME_ALIASES,
    complete: bool = True,
) -> str:
    return capacity_status(
        active_count,
        lifetime_count,
        lifetime_limit=lifetime_limit,
        near_limit_remaining=_configured_nonnegative("MAILCOM_LIFETIME_NEAR_LIMIT", DEFAULT_NEAR_LIMIT_REMAINING),
        complete=complete,
    )


def _history_method(client: Any) -> Callable[[], Any] | None:
    for name in ("history_snapshot", "get_history_snapshot", "list_address_history", "list_addresses_history"):
        method = getattr(client, name, None)
        if callable(method):
            return method
    return None


def _coerce_history_snapshot(value: Any) -> MailComCapacitySnapshot:
    if isinstance(value, MailComCapacitySnapshot):
        return value
    if isinstance(value, dict):
        # 允许测试替身返回公开字段字典，但不接受完整地址数组。
        if "mailaddresslist" in value:
            # 保留 next/totalCount 等完整性元数据，避免把分页响应误当成
            # 可用于创建决策的完整快照。
            return aggregate_history_payload(value)
        lifetime = value.get("remote_lifetime_alias_count", value.get("lifetime_alias_count"))
        active = value.get("remote_active_alias_count", value.get("active_alias_count"))
        return MailComCapacitySnapshot(
            lifetime_alias_count=None if lifetime is None else int(lifetime),
            active_alias_count=None if active is None else int(active),
            unknown_state_count=int(value.get("remote_history_unknown_count", value.get("unknown_state_count", 0)) or 0),
            complete=bool(value.get("complete", True)),
            lifetime_alias_limit=int(value.get("remote_lifetime_alias_limit", MAX_LIFETIME_ALIASES) or MAX_LIFETIME_ALIASES),
        )
    if isinstance(value, list):
        return aggregate_history_rows(value)
    raise ValueError("mail.com 历史容量结果格式无效")


def _snapshot_from_record(row: dict) -> MailComCapacitySnapshot | None:
    try:
        lifetime = row.get("remote_lifetime_alias_count")
        if lifetime is None:
            return None
        active = row.get("remote_active_alias_count")
        limit = int(row.get("remote_lifetime_alias_limit") or MAX_LIFETIME_ALIASES)
        unknown = int(row.get("remote_history_unknown_count") or 0)
    except (TypeError, ValueError):
        return None
    return MailComCapacitySnapshot(
        lifetime_alias_count=int(lifetime),
        active_alias_count=None if active is None else int(active),
        unknown_state_count=unknown,
        complete=str(row.get("remote_capacity_status") or "unknown") not in {"unknown", "capacity_unknown"},
        lifetime_alias_limit=limit,
        source="cached_history",
    )


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

    def _snapshot_needs_refresh(self, parent: dict, *, force: bool = False) -> bool:
        if force:
            return True
        count = parent.get("remote_lifetime_alias_count")
        synced_at = parse_snapshot_time(parent.get("remote_history_synced_at"))
        status = str(parent.get("remote_capacity_status") or "unknown").strip().casefold()
        if count is None or synced_at is None or status in {"unknown", "capacity_unknown"}:
            return True
        ttl = _configured_nonnegative("MAILCOM_LIFETIME_SNAPSHOT_TTL_SECONDS", DEFAULT_HISTORY_TTL_SECONDS)
        if time.time() - synced_at > ttl:
            return True
        try:
            remaining = max(0, int(parent.get("remote_lifetime_alias_limit") or MAX_LIFETIME_ALIASES) - int(count))
        except (TypeError, ValueError):
            return True
        near_limit = _configured_nonnegative("MAILCOM_LIFETIME_NEAR_LIMIT", DEFAULT_NEAR_LIMIT_REMAINING)
        return remaining <= near_limit

    def _refresh_history(
        self,
        client: MailComSettingsClient,
        parent_email: str,
        *,
        force: bool = True,
        parent: dict | None = None,
    ) -> MailComCapacitySnapshot:
        """执行一次全量历史 GET 并只把聚合结果写入本地。"""
        refresh_lock = history_refresh_lock(parent_email)
        # 强制刷新也要与已经在途的同母号请求合并，避免 409 与手动刷新并发时
        # 顺序发出两次全量 GET。只有在本调用实际等待过另一个刷新时才复用结果。
        waited_for_existing_refresh = not refresh_lock.acquire(blocking=False)
        if waited_for_existing_refresh:
            refresh_lock.acquire()
        try:
            if waited_for_existing_refresh:
                current = db.get_mailcom_internal_record(parent_email)
                cached = _snapshot_from_record(current) if current is not None else None
                if cached is not None and cached.complete:
                    return cached
            if not force:
                current = db.get_mailcom_internal_record(parent_email)
                if current is not None and not self._snapshot_needs_refresh(current):
                    cached = _snapshot_from_record(current)
                    if cached is not None:
                        return cached
            method = _history_method(client)
            try:
                if method is not None:
                    snapshot = _coerce_history_snapshot(method())
                else:
                    # 旧测试替身/第三方实现没有新方法时，用一次现有列表读取做兼容聚合。
                    snapshot = aggregate_history_rows(client.list_addresses())
            except MailComSettingsError as exc:
                db.update_mailcom_capacity_snapshot(parent_email, error=exc.error_type)
                raise MailComAliasError(
                    f"mail.com 历史容量查询失败（{exc.error_type}）",
                    error_type="capacity_unknown",
                ) from exc
            except (TypeError, ValueError) as exc:
                db.update_mailcom_capacity_snapshot(parent_email, error="protocol_error")
                raise MailComAliasError(
                    "mail.com 历史容量响应不完整，无法判断剩余容量",
                    error_type="capacity_unknown",
                ) from exc
            if not snapshot.complete:
                db.update_mailcom_capacity_snapshot(parent_email, snapshot, error="history_incomplete")
                raise MailComAliasError(
                    "mail.com 历史容量响应不完整，无法判断剩余容量",
                    error_type="capacity_unknown",
                )
            db.update_mailcom_capacity_snapshot(parent_email, snapshot)
            return snapshot
        finally:
            refresh_lock.release()

    def _capacity_before_create(
        self,
        client: MailComSettingsClient,
        parent: dict,
        *,
        force_history: bool = False,
    ) -> tuple[MailComCapacitySnapshot, list[dict[str, Any]] | None]:
        parent_email = canonical_email(parent.get("email"))
        # 队列中的只读刷新可能刚在等待期间完成；重新读取本地聚合值可避免重复全量 GET。
        effective_parent = db.get_mailcom_internal_record(parent_email) or parent
        if self._snapshot_needs_refresh(effective_parent, force=force_history):
            snapshot = self._refresh_history(client, parent_email, force=False, parent=parent)
            return snapshot, None
        try:
            rows = client.list_addresses()
        except MailComSettingsError as exc:
            raise MailComAliasError(
                f"mail.com 别名容量查询失败（{exc.error_type}）",
                error_type=exc.error_type,
            ) from exc
        active = active_deletable_count(rows)
        try:
            lifetime_count = int(effective_parent.get("remote_lifetime_alias_count"))
            lifetime_limit = int(effective_parent.get("remote_lifetime_alias_limit") or MAX_LIFETIME_ALIASES)
        except (TypeError, ValueError):
            lifetime_count = None
            lifetime_limit = MAX_LIFETIME_ALIASES
        return (
            MailComCapacitySnapshot(
                lifetime_alias_count=lifetime_count,
                active_alias_count=active,
                unknown_state_count=int(effective_parent.get("remote_history_unknown_count") or 0),
                complete=lifetime_count is not None,
                lifetime_alias_limit=lifetime_limit,
                source="active_check",
            ),
            rows,
        )

    def _classify_remote_conflict(
        self,
        client: MailComSettingsClient,
        parent: dict,
        original: Exception,
    ) -> str:
        parent_email = canonical_email(parent.get("email"))
        try:
            snapshot = self._refresh_history(client, parent_email, force=True, parent=parent)
        except MailComAliasError:
            logger.warning(
                "[MailComAlias] 409 后容量未知: parent=%s error_type=capacity_unknown",
                _parent_label(parent_email),
            )
            return "capacity_unknown"
        lifetime = snapshot.lifetime_alias_count
        active = snapshot.active_alias_count
        if lifetime is not None and lifetime >= MAX_LIFETIME_ALIASES:
            result = "lifetime_capacity_full"
        elif active is not None and active >= MAX_ACTIVE_ALIASES:
            result = "active_capacity_full"
        else:
            result = "remote_create_conflict"
        logger.warning(
            "[MailComAlias] 409 容量分类: parent=%s type=%s active=%s lifetime=%s",
            _parent_label(parent_email), result, active, lifetime,
        )
        return result

    def _post_create_calibration(
        self,
        client: MailComSettingsClient,
        parent_email: str,
        final_rows: list[dict[str, Any]],
    ) -> MailComCapacitySnapshot | None:
        """批次成功后最多一次历史校准；旧替身复用最终活动列表。"""
        method = _history_method(client)
        if method is None:
            try:
                snapshot = aggregate_history_rows(final_rows)
            except (TypeError, ValueError):
                db.update_mailcom_capacity_snapshot(parent_email, error="protocol_error")
                return None
        else:
            try:
                snapshot = _coerce_history_snapshot(method())
            except MailComSettingsError as exc:
                db.update_mailcom_capacity_snapshot(parent_email, error=exc.error_type)
                return None
            except (TypeError, ValueError):
                db.update_mailcom_capacity_snapshot(parent_email, error="protocol_error")
                return None
            if not snapshot.complete:
                db.update_mailcom_capacity_snapshot(parent_email, snapshot, error="history_incomplete")
                return None
        db.update_mailcom_capacity_snapshot(parent_email, snapshot)
        return snapshot

    def create_alias(self, parent_record: dict, *, job_id: int | None = None) -> dict:
        parent_email = canonical_email(parent_record.get("email"))
        if not parent_email:
            raise MailComAliasError("mail.com 母号不存在", error_type="parent_missing")
        with mother_alias_lock(parent_email):
            client = self._client_for_parent(parent_record)
            capacity, before = self._capacity_before_create(client, parent_record)
            count = int(capacity.active_alias_count or 0)
            remaining = lifetime_remaining(
                capacity.lifetime_alias_count,
                capacity.lifetime_alias_limit,
            )
            if remaining == 0:
                logger.warning(
                    "[MailComAlias] 生命周期容量已满: parent=%s lifetime=%s",
                    _parent_label(parent_email),
                    capacity.lifetime_alias_count,
                )
                raise MailComAliasLifetimeCapacityError(capacity.lifetime_alias_count)
            if count >= MAX_ACTIVE_ALIASES:
                logger.warning("[MailComAlias] 母号别名已满: parent=%s active=%s", _parent_label(parent_email), count)
                raise MailComAliasCapacityError(count)

            last_conflict: Exception | None = None
            for _ in range(self.max_attempts):
                try:
                    local_part = generate_alias_local_part()
                    domain = choose_alias_domain(domains=db.get_enabled_mailcom_alias_domains())
                except MailComAliasDomainError as exc:
                    raise MailComAliasError(str(exc), error_type="alias_domain_config") from exc
                alias_email = f"{local_part}@{domain}"
                try:
                    client.validate_address(alias_email)
                    client.create_address(alias_email)
                except MailComSettingsRemoteConflictError as exc:
                    classified = self._classify_remote_conflict(client, parent_record, exc)
                    raise MailComAliasError(
                        f"mail.com 别名创建冲突（{classified}）",
                        error_type=classified,
                    ) from exc
                except MailComSettingsConflictError as exc:
                    last_conflict = exc
                    continue
                except MailComSettingsError as exc:
                    if exc.error_type == "remote_create_conflict":
                        classified = self._classify_remote_conflict(client, parent_record, exc)
                        raise MailComAliasError(
                            f"mail.com 别名创建冲突（{classified}）",
                            error_type=classified,
                        ) from exc
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
                # 单别名任务也视为一个批次；真实 client 只追加一次全量校准。
                self._post_create_calibration(client, parent_email, after)
                logger.info("[MailComAlias] 已创建并确认别名: parent=%s alias=%s", _parent_label(parent_email), _mask(alias_email))
                return alias
            raise MailComAliasError(
                "mail.com 候选别名多次冲突或被拒绝，当前任务已终止",
                error_type="address_conflict",
            ) from last_conflict

    def sync_parent_snapshot(self, parent_record: dict) -> dict:
        """只读取远端活动地址并合并本地快照，不执行创建/校验。"""
        parent_email = canonical_email(parent_record.get("email"))
        if not parent_email:
            raise MailComAliasError("mail.com 母号不存在", error_type="parent_missing")
        with mother_alias_lock(parent_email):
            current_parent = db.get_mailcom_internal_record(parent_email)
            if not current_parent:
                raise MailComAliasError("mail.com 母号不存在", error_type="parent_missing")
            if str(current_parent.get("status") or "") == "disabled":
                raise MailComAliasError("mail.com 母号已停用", error_type="parent_disabled")
            if db.mailcom_parent_registration_busy(parent_email):
                raise MailComAliasError(
                    "mail.com 母号正在执行注册任务，暂不能同步 alias",
                    error_type="registration_busy",
                )
            client = self._client_for_parent(current_parent)
            before = db.mailcom_alias_summary(parent_email)
            before_keys = {
                str(row.get("alias_email") or "").casefold()
                for status in ("available", "registering", "used", "failed", "disabled")
                for row in db.list_mailcom_aliases(parent_email=parent_email, status=status, limit=10000)
            }
            try:
                remote_rows = client.list_addresses()
            except MailComSettingsError as exc:
                raise MailComAliasError(
                    f"mail.com 别名同步读取失败（{exc.error_type}）",
                    error_type=exc.error_type,
                ) from exc
            remote_aliases = [
                canonical_email(item.get("address"))
                for item in remote_rows
                if isinstance(item, dict)
                and is_active_deletable_address(item)
                and canonical_email(item.get("address")) != parent_email
            ]
            latest_parent = db.get_mailcom_internal_record(parent_email)
            if not latest_parent or str(latest_parent.get("status") or "") == "disabled":
                raise MailComAliasError("mail.com 母号已停用，放弃提交旧同步快照", error_type="parent_disabled")
            replaced = db.replace_mailcom_alias_snapshot(
                parent_email,
                remote_aliases,
                expected_generation=int(latest_parent.get("lifecycle_generation") or 1),
            )
            if replaced is None:
                raise MailComAliasError(
                    "mail.com 母号生命周期或注册租约已变化，本地快照未更新",
                    error_type="lifecycle_conflict",
                )
            after = db.mailcom_alias_summary(parent_email)
            after_keys = {
                str(row.get("alias_email") or "").casefold()
                for status in ("available", "registering", "used", "failed", "disabled")
                for row in db.list_mailcom_aliases(parent_email=parent_email, status=status, limit=10000)
            }
            active_count = len(remote_aliases)
            now = datetime.now().isoformat(timespec="seconds")
            result = {
                "action": "sync",
                "parent_email": parent_email,
                "remote_active_alias_count": active_count,
                "local_added_count": len(after_keys - before_keys),
                "local_disabled_count": max(0, int(after.get("disabled", 0)) - int(before.get("disabled", 0))),
                "local_alias_count": int(after.get("total", 0)),
                "snapshot_synced_at": now,
            }
            state = "ready" if active_count >= MAX_ACTIVE_ALIASES else "partial"
            db.update_mailcom_parent_sync(
                parent_email,
                sync_status=state,
                remote_active_alias_count=active_count,
                sync_action="sync",
                sync_result=result,
            )
            return {"ok": True, "status": state, **result}

    def sync_parent_aliases(self, parent_record: dict, *, target: int = MAX_ACTIVE_ALIASES) -> dict:
        """按活动与生命周期双重预算补齐母号别名。"""
        parent_email = canonical_email(parent_record.get("email"))
        if not parent_email:
            raise MailComAliasError("mail.com 母号不存在", error_type="parent_missing")
        target = max(0, min(MAX_ACTIVE_ALIASES, int(target)))
        with mother_alias_lock(parent_email):
            current_parent = db.get_mailcom_internal_record(parent_email)
            if not current_parent:
                raise MailComAliasError("mail.com 母号不存在", error_type="parent_missing")
            if str(current_parent.get("status") or "") == "disabled":
                raise MailComAliasError("mail.com 母号已停用", error_type="parent_disabled")
            if db.mailcom_parent_registration_busy(parent_email):
                raise MailComAliasError(
                    "mail.com 母号正在执行注册任务，暂不能同步 alias",
                    error_type="registration_busy",
                )
            client = self._client_for_parent(parent_record)
            try:
                capacity, initial_rows = self._capacity_before_create(client, parent_record)
            except MailComAliasError as exc:
                if exc.error_type == "capacity_unknown":
                    raise MailComAliasError(
                        f"mail.com 别名同步查询失败（{exc.error_type}）",
                        error_type=exc.error_type,
                    ) from exc
                raise
            initial_active_count = int(capacity.active_alias_count or 0)
            remaining = lifetime_remaining(capacity.lifetime_alias_count, capacity.lifetime_alias_limit)
            if remaining == 0:
                db.update_mailcom_capacity_snapshot(
                    parent_email,
                    active_count=initial_active_count,
                    lifetime_count=capacity.lifetime_alias_count,
                    status=CAPACITY_LIFETIME_FULL,
                )
                raise MailComAliasLifetimeCapacityError(capacity.lifetime_alias_count)
            active_gap = max(0, target - initial_active_count)
            create_opportunities = active_gap if remaining is None else min(active_gap, remaining)
            create_requests: list[str] = []
            created_candidates: list[str] = []
            validation_failures = 0
            conflict_type: str | None = None

            for _ in range(create_opportunities):
                candidate = ""
                for _validation_attempt in range(MAX_SYNC_VALIDATION_ATTEMPTS):
                    try:
                        local_part = generate_alias_local_part()
                        domain = choose_alias_domain(domains=db.get_enabled_mailcom_alias_domains())
                    except MailComAliasDomainError as exc:
                        raise MailComAliasError(str(exc), error_type="alias_domain_config") from exc
                    candidate = f"{local_part}@{domain}"
                    try:
                        client.validate_address(candidate)
                    except MailComSettingsError as exc:
                        validation_failures += 1
                        logger.warning(
                            "[MailComAlias] 候选校验失败: parent=%s type=%s",
                            _parent_label(parent_email),
                            exc.error_type,
                        )
                        candidate = ""
                        continue
                    break
                if not candidate:
                    continue

                try:
                    client.create_address(candidate)
                    create_requests.append(candidate)
                    created_candidates.append(candidate)
                except MailComSettingsRemoteConflictError as exc:
                    # 409 代表远端业务冲突：本批次立即停止，之后只做一次容量校准。
                    conflict_type = self._classify_remote_conflict(client, parent_record, exc)
                    break
                except MailComSettingsError as exc:
                    if exc.error_type == "remote_create_conflict":
                        conflict_type = self._classify_remote_conflict(client, parent_record, exc)
                        break
                    # 请求次数与确认成功数分开；失败 POST 不计入 created_count。
                    create_requests.append(candidate)
                    logger.warning(
                        "[MailComAlias] 创建请求未确认且不重试: parent=%s alias=%s type=%s",
                        _parent_label(parent_email),
                        _mask(candidate),
                        exc.error_type,
                    )

            if conflict_type is not None and not created_candidates:
                raise MailComAliasError(
                    f"mail.com 别名创建冲突（{conflict_type}）",
                    error_type=conflict_type,
                )

            try:
                final_rows = client.list_addresses()
            except MailComSettingsError as exc:
                raise MailComAliasError(
                    f"mail.com 别名最终列表查询失败（{exc.error_type}）",
                    error_type=exc.error_type,
                ) from exc

            latest_parent = db.get_mailcom_internal_record(parent_email)
            if not latest_parent or str(latest_parent.get("status") or "") == "disabled":
                raise MailComAliasError("mail.com 母号已停用，放弃提交旧同步快照", error_type="parent_disabled")

            final_aliases = [
                canonical_email(item.get("address"))
                for item in final_rows
                if isinstance(item, dict)
                and is_active_deletable_address(item)
                and canonical_email(item.get("address")) != parent_email
            ]
            replaced = db.replace_mailcom_alias_snapshot(
                parent_email,
                final_aliases,
                expected_generation=int(parent_record.get("lifecycle_generation") or 1),
            )
            if replaced is None:
                raise MailComAliasError(
                    "mail.com 母号注册租约在同步期间被占用，本地快照未更新",
                    error_type="registration_busy",
                )
            confirmed_candidates = [
                candidate for candidate in created_candidates
                if canonical_email(candidate) in set(final_aliases)
            ]
            if created_candidates and conflict_type is None:
                calibrated = self._post_create_calibration(client, parent_email, final_rows)
                if calibrated is not None:
                    capacity = calibrated
            elif conflict_type is None and initial_rows is not None:
                # 新鲜缓存 + 没有成功创建时，保持本地活动观测，不发历史 GET。
                db.update_mailcom_capacity_snapshot(
                    parent_email,
                    active_count=len(replaced),
                    lifetime_count=capacity.lifetime_alias_count,
                    lifetime_limit=capacity.lifetime_alias_limit,
                    unknown_state_count=capacity.unknown_state_count,
                    status=capacity_status(
                        len(replaced),
                        capacity.lifetime_alias_count,
                        lifetime_limit=capacity.lifetime_alias_limit,
                        near_limit_remaining=_configured_nonnegative(
                            "MAILCOM_LIFETIME_NEAR_LIMIT", DEFAULT_NEAR_LIMIT_REMAINING,
                        ),
                    ) if capacity.lifetime_alias_count is not None else CAPACITY_UNKNOWN,
                    update_history_time=False,
                )
            if conflict_type is not None:
                raise MailComAliasError(
                    f"mail.com 别名创建冲突（{conflict_type}）",
                    error_type=conflict_type,
                )
            return {
                "parent_email": parent_email,
                "remote_active_alias_count": len(replaced),
                "create_opportunity_count": create_opportunities,
                "create_request_count": len(create_requests),
                "create_requested_aliases": create_requests,
                "created_count": len(confirmed_candidates),
                "created_aliases": confirmed_candidates,
                "validation_failure_count": validation_failures,
                "remote_lifetime_alias_count": capacity.lifetime_alias_count,
                "remote_lifetime_alias_limit": capacity.lifetime_alias_limit,
                "remote_lifetime_remaining": lifetime_remaining(
                    capacity.lifetime_alias_count,
                    capacity.lifetime_alias_limit,
                ),
                "remote_capacity_status": _capacity_status_for_runtime(
                    len(replaced),
                    capacity.lifetime_alias_count,
                    lifetime_limit=capacity.lifetime_alias_limit,
                    complete=capacity.complete,
                ),
            }

    def delete_alias(self, alias_record: dict) -> bool:
        alias_email = canonical_email(alias_record.get("alias_email"))
        parent_email = canonical_email(alias_record.get("parent_email"))
        if not alias_email or not parent_email:
            raise MailComAliasError("mail.com 别名记录不完整", error_type="alias_record_invalid")
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
                # 兼容远端把“已不存在”作为 404/False 返回：回读一次地址列表，
                # 只有确认地址已不在列表中才视为幂等删除成功。
                try:
                    remaining = client.list_addresses()
                except MailComSettingsError as exc:
                    raise MailComAliasError(
                        f"mail.com 删除别名确认失败（{exc.error_type}）",
                        error_type=exc.error_type,
                    ) from exc
                if any(
                    isinstance(item, dict)
                    and canonical_email(item.get("address")) == alias_email
                    for item in remaining
                ):
                    raise MailComAliasError(
                        "mail.com 删除别名接口未确认成功",
                        error_type="delete_unconfirmed",
                    )
                deleted = True
            db.mark_mailcom_alias_deleted(alias_email)
            # 删除只释放活动槽位；生命周期累计数保持不变，且不因删除立即触发全量历史 GET。
            db.update_mailcom_capacity_snapshot(parent_email, local_active_delta=-1)
            logger.info("[MailComAlias] 已删除别名: parent=%s alias=%s", _parent_label(parent_email), _mask(alias_email))
            return True


_DEFAULT_SERVICE = MailComAliasService()


def create_alias(parent_record: dict, *, job_id: int | None = None) -> dict:
    return _DEFAULT_SERVICE.create_alias(parent_record, job_id=job_id)


def delete_alias(alias_record: dict) -> bool:
    return _DEFAULT_SERVICE.delete_alias(alias_record)


def sync_parent_aliases(parent_record: dict, *, target: int = MAX_ACTIVE_ALIASES) -> dict:
    return _DEFAULT_SERVICE.sync_parent_aliases(parent_record, target=target)


def sync_parent_snapshot(parent_record: dict) -> dict:
    return _DEFAULT_SERVICE.sync_parent_snapshot(parent_record)


__all__ = [
    "MAX_ACTIVE_ALIASES",
    "MAX_CREATE_ATTEMPTS",
    "MAX_LIFETIME_ALIAS_COUNT",
    "MAX_SYNC_VALIDATION_ATTEMPTS",
    "MailComAliasCapacityError",
    "MailComAliasError",
    "MailComAliasLifetimeCapacityError",
    "MailComAliasService",
    "active_deletable_count",
    "create_alias",
    "delete_alias",
    "mother_alias_lock",
    "sync_parent_aliases",
    "sync_parent_snapshot",
]
