# -*- coding: utf-8 -*-
"""mail.com 母号 alias 同步/补齐后台队列。"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from core import db
from core.mailcom_alias_service import (
    MAX_ACTIVE_ALIASES,
    MailComAliasError,
    MailComAliasLifetimeCapacityError,
    delete_alias,
    sync_parent_aliases,
    sync_parent_snapshot,
)
from core.mailcom_capacity import (
    CAPACITY_ACTIVE_FULL,
    CAPACITY_LIFETIME_FULL,
    CAPACITY_QUERY_UNKNOWN,
    CAPACITY_UNKNOWN,
    MAX_LIFETIME_ALIASES,
    MailComCapacitySnapshot,
    capacity_status,
)
from core.mailcom_settings_client import MailComSettingsError, MailComSettingsClient


logger = logging.getLogger(__name__)
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mailcom-alias-sync")
_PENDING: set[str] = set()
_PENDING_LOCK = threading.Lock()
_SNAPSHOT_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mailcom-alias-snapshot")
_SNAPSHOT_PENDING: set[str] = set()
_SNAPSHOT_PENDING_LOCK = threading.Lock()
_HISTORY_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mailcom-history-refresh")
_HISTORY_PENDING: set[str] = set()
_HISTORY_PENDING_LOCK = threading.Lock()


def _key(email: str) -> str:
    return str(email or "").strip().casefold()


def _mask(email: str) -> str:
    """日志使用母号脱敏标识，不记录完整邮箱。"""
    value = _key(email)
    local, separator, domain = value.partition("@")
    return f"{local[:1]}***@{domain}" if separator else "[invalid-email]"


def sync_parent_now(
    parent_email: str,
    *,
    sync_fn: Callable[[dict], dict] | None = None,
) -> dict:
    parent = db.get_mailcom_internal_record(parent_email)
    if not parent:
        return {"ok": False, "action": "replenish", "error": "parent_missing"}
    if str(parent.get("status") or "") == "disabled":
        return {"ok": False, "action": "replenish", "error": "parent_disabled", "status": "blocked"}
    db.update_mailcom_parent_sync(parent_email, sync_status="syncing", sync_action="replenish")
    try:
        result = (sync_fn or sync_parent_aliases)(parent)
    except MailComAliasError as exc:
        capacity_state = {
            "lifetime_capacity_full": CAPACITY_LIFETIME_FULL,
            "active_capacity_full": CAPACITY_ACTIVE_FULL,
            "capacity_unknown": CAPACITY_QUERY_UNKNOWN,
        }.get(exc.error_type)
        db.update_mailcom_parent_sync(
            parent_email,
            sync_status="failed",
            sync_action="replenish",
            remote_capacity_status=capacity_state,
            error=exc.error_type,
        )
        logger.warning("[MailComAliasPool] 同步失败: parent=%s type=%s", _mask(parent_email), exc.error_type)
        return {"ok": False, "action": "replenish", "error": exc.error_type, "status": "failed"}
    except Exception as exc:  # pragma: no cover - 后台边界
        db.update_mailcom_parent_sync(parent_email, sync_status="failed", sync_action="replenish", error=type(exc).__name__)
        logger.exception("[MailComAliasPool] 同步异常: parent=%s", _mask(parent_email))
        return {"ok": False, "action": "replenish", "error": type(exc).__name__, "status": "failed"}
    count = int(result.get("remote_active_alias_count") or 0)
    state = "ready" if count >= MAX_ACTIVE_ALIASES else "partial"
    if db.get_mailcom_internal_record(parent_email) is None or str(
        (db.get_mailcom_internal_record(parent_email) or {}).get("status") or ""
    ) == "disabled":
        return {"ok": False, "action": "replenish", "error": "parent_disabled", "status": "blocked"}
    db.update_mailcom_parent_sync(
        parent_email,
        sync_status=state,
        sync_action="replenish",
        sync_result=result,
        remote_active_alias_count=count,
        remote_lifetime_alias_count=result.get("remote_lifetime_alias_count"),
        remote_lifetime_alias_limit=result.get("remote_lifetime_alias_limit"),
        remote_capacity_status=result.get("remote_capacity_status")
        or (
            capacity_status(
                count,
                result.get("remote_lifetime_alias_count"),
                lifetime_limit=int(result.get("remote_lifetime_alias_limit") or MAX_LIFETIME_ALIASES),
            )
            if result.get("remote_lifetime_alias_count") is not None
            else None
        ),
        remote_history_unknown_count=result.get("remote_history_unknown_count"),
        remote_history_error=result.get("remote_history_error"),
    )
    return {"ok": True, "action": "replenish", "status": state, **result}


def sync_parent_snapshot_now(
    parent_email: str,
    *,
    sync_fn: Callable[[dict], dict] | None = None,
) -> dict:
    """只同步远端活动地址快照，不创建或校验新别名。"""
    key = _key(parent_email)
    parent = db.get_mailcom_internal_record(key)
    if not parent:
        return {"ok": False, "action": "sync", "error": "parent_missing"}
    if str(parent.get("status") or "") == "disabled":
        return {"ok": False, "action": "sync", "error": "parent_disabled", "status": "blocked"}
    db.update_mailcom_parent_sync(key, sync_status="syncing", sync_action="sync")
    try:
        result = (sync_fn or sync_parent_snapshot)(parent)
    except MailComAliasError as exc:
        db.update_mailcom_parent_sync(
            key,
            sync_status="failed",
            sync_action="sync",
            error=exc.error_type,
        )
        logger.warning("[MailComAliasSnapshot] 同步失败: parent=%s type=%s", _mask(key), exc.error_type)
        return {"ok": False, "action": "sync", "error": exc.error_type, "status": "failed"}
    except Exception as exc:  # pragma: no cover - 后台边界
        db.update_mailcom_parent_sync(key, sync_status="failed", sync_action="sync", error=type(exc).__name__)
        logger.exception("[MailComAliasSnapshot] 同步异常: parent=%s", _mask(key))
        return {"ok": False, "action": "sync", "error": type(exc).__name__, "status": "failed"}
    count = int(result.get("remote_active_alias_count") or 0)
    state = "ready" if count >= MAX_ACTIVE_ALIASES else "partial"
    if db.get_mailcom_internal_record(key) is None or str(
        (db.get_mailcom_internal_record(key) or {}).get("status") or ""
    ) == "disabled":
        return {"ok": False, "action": "sync", "error": "parent_disabled", "status": "blocked"}
    db.update_mailcom_parent_sync(
        key,
        sync_status=state,
        sync_action="sync",
        remote_active_alias_count=count,
        sync_result=result,
    )
    return {"ok": True, "action": "sync", "status": state, **result}


def _run(parent_email: str) -> None:
    try:
        sync_parent_now(parent_email)
    finally:
        with _PENDING_LOCK:
            _PENDING.discard(_key(parent_email))


def _run_snapshot(parent_email: str) -> None:
    try:
        sync_parent_snapshot_now(parent_email)
    finally:
        with _SNAPSHOT_PENDING_LOCK:
            _SNAPSHOT_PENDING.discard(_key(parent_email))


def enqueue_parent_sync(parent_email: str) -> dict:
    key = _key(parent_email)
    if "@" not in key:
        return {"accepted": False, "action": "replenish", "error": "parent_invalid"}
    with _PENDING_LOCK:
        with _SNAPSHOT_PENDING_LOCK:
            if key in _PENDING or key in _SNAPSHOT_PENDING:
                return {"accepted": False, "action": "replenish", "busy": True, "parent_email": key}
            parent = db.get_mailcom_internal_record(key)
            if parent is None:
                return {"accepted": False, "action": "replenish", "error": "parent_missing"}
            if str(parent.get("status") or "") == "disabled":
                return {"accepted": False, "action": "replenish", "error": "parent_disabled", "parent_email": key}
            _PENDING.add(key)
    db.update_mailcom_parent_sync(key, sync_status="queued", sync_action="replenish")
    try:
        _EXECUTOR.submit(_run, key)
    except Exception:
        with _PENDING_LOCK:
            _PENDING.discard(key)
        db.update_mailcom_parent_sync(key, sync_status="failed", sync_action="replenish", error="queue_submit_failed")
        raise
    return {"accepted": True, "action": "replenish", "busy": False, "parent_email": key}


def enqueue_parent_replenish(parent_email: str) -> dict:
    """显式提交当前的同步并补齐流程。"""
    return enqueue_parent_sync(parent_email)


def enqueue_parent_snapshot_sync(parent_email: str) -> dict:
    """提交只同步远端活动地址快照的任务。"""
    key = _key(parent_email)
    if "@" not in key:
        return {"accepted": False, "action": "sync", "error": "parent_invalid", "parent_email": key}
    with _PENDING_LOCK:
        with _SNAPSHOT_PENDING_LOCK:
            if key in _PENDING or key in _SNAPSHOT_PENDING:
                return {"accepted": False, "action": "sync", "busy": True, "parent_email": key}
            parent = db.get_mailcom_internal_record(key)
            if parent is None:
                return {"accepted": False, "action": "sync", "error": "parent_missing", "parent_email": key}
            if str(parent.get("status") or "") == "disabled":
                return {"accepted": False, "action": "sync", "error": "parent_disabled", "parent_email": key}
            _SNAPSHOT_PENDING.add(key)
    db.update_mailcom_parent_sync(key, sync_status="queued", sync_action="sync")
    try:
        _SNAPSHOT_EXECUTOR.submit(_run_snapshot, key)
    except Exception:
        with _SNAPSHOT_PENDING_LOCK:
            _SNAPSHOT_PENDING.discard(key)
        db.update_mailcom_parent_sync(key, sync_status="failed", sync_action="sync", error="queue_submit_failed")
        raise
    return {"accepted": True, "action": "sync", "busy": False, "parent_email": key}


def refresh_parent_history_now(
    parent_email: str,
    *,
    refresh_fn: Callable[[dict], dict] | None = None,
) -> dict:
    """同步执行一次只读历史刷新；不创建/删除远端地址。"""
    key = _key(parent_email)
    parent = db.get_mailcom_internal_record(key)
    if not parent:
        return {"ok": False, "error": "parent_missing", "status": "failed"}
    if str(parent.get("status") or "") == "disabled":
        return {"ok": False, "error": "parent_disabled", "status": "blocked", "parent_email": key}
    try:
        if refresh_fn is not None:
            result = refresh_fn(parent) or {}
            snapshot_value = result.get("snapshot")
            if snapshot_value is None and any(
                key in result
                for key in ("remote_lifetime_alias_count", "lifetime_alias_count", "remote_active_alias_count", "active_alias_count")
            ):
                snapshot_value = result
            if snapshot_value is not None:
                snapshot = snapshot_value
                if isinstance(snapshot, dict):
                    snapshot = MailComCapacitySnapshot(
                        lifetime_alias_count=snapshot.get("remote_lifetime_alias_count", snapshot.get("lifetime_alias_count")),
                        active_alias_count=snapshot.get("remote_active_alias_count", snapshot.get("active_alias_count")),
                        unknown_state_count=int(snapshot.get("remote_history_unknown_count", snapshot.get("unknown_state_count", 0)) or 0),
                        complete=bool(snapshot.get("complete", True)),
                        lifetime_alias_limit=int(snapshot.get("remote_lifetime_alias_limit", MAX_LIFETIME_ALIASES) or MAX_LIFETIME_ALIASES),
                    )
                db.update_mailcom_capacity_snapshot(key, snapshot)
            return {"ok": True, "status": "refreshed", **result}
        # 复用 service 的协议/脱敏边界；这里只调用 history_snapshot 或兼容 GET。
        from core.mailcom_alias_service import MailComAliasService

        service = MailComAliasService()
        client = service._client_for_parent(parent)
        snapshot = service._refresh_history(client, key, force=True, parent=parent)
        return {
            "ok": True,
            "status": "refreshed",
            "parent_email": key,
            **snapshot.as_dict(),
        }
    except MailComAliasError as exc:
        # _refresh_history 已保留旧值并标记 capacity_unknown；这里仅返回稳定错误类型。
        db.mark_mailcom_capacity_unknown(key, exc.error_type)
        logger.warning("[MailComHistory] 刷新失败: parent=%s type=%s", _mask(key), exc.error_type)
        return {"ok": False, "status": "failed", "error": exc.error_type, "parent_email": key}
    except MailComSettingsError as exc:
        db.mark_mailcom_capacity_unknown(key, exc.error_type)
        logger.warning("[MailComHistory] 刷新失败: parent=%s type=%s", _mask(key), exc.error_type)
        return {"ok": False, "status": "failed", "error": exc.error_type, "parent_email": key}
    except Exception as exc:  # pragma: no cover - 后台边界
        db.mark_mailcom_capacity_unknown(key, type(exc).__name__)
        logger.exception("[MailComHistory] 刷新异常: parent=%s", _mask(key))
        return {"ok": False, "status": "failed", "error": type(exc).__name__, "parent_email": key}


def _run_history(parent_email: str) -> None:
    try:
        refresh_parent_history_now(parent_email)
    finally:
        with _HISTORY_PENDING_LOCK:
            _HISTORY_PENDING.discard(_key(parent_email))


def enqueue_parent_history_refresh(parent_email: str) -> dict:
    """按母号去重提交只读历史刷新任务。"""
    key = _key(parent_email)
    if "@" not in key:
        return {"accepted": False, "busy": False, "error": "parent_invalid", "parent_email": key}
    parent = db.get_mailcom_internal_record(key)
    if parent is None:
        return {"accepted": False, "busy": False, "error": "parent_missing", "parent_email": key}
    if str(parent.get("status") or "") == "disabled":
        return {"accepted": False, "busy": False, "error": "parent_disabled", "parent_email": key}
    with _HISTORY_PENDING_LOCK:
        if key in _HISTORY_PENDING:
            return {"accepted": False, "busy": True, "parent_email": key}
        _HISTORY_PENDING.add(key)
    try:
        _HISTORY_EXECUTOR.submit(_run_history, key)
    except Exception:
        with _HISTORY_PENDING_LOCK:
            _HISTORY_PENDING.discard(key)
        db.mark_mailcom_capacity_unknown(key, "queue_submit_failed")
        raise
    return {"accepted": True, "busy": False, "parent_email": key}


def delete_alias_now(alias_email: str, *, force: bool = False, reason: str | None = None) -> dict:
    alias = db.get_mailcom_alias_internal(alias_email)
    if not alias:
        return {"ok": False, "error": "alias_missing", "status": 404}
    if db.mailcom_alias_is_leased(alias_email):
        return {"ok": False, "error": "alias_leased", "status": 409}
    try:
        confirmed = bool(delete_alias(alias))
    except MailComAliasError as exc:
        db.mark_mailcom_alias_cleanup_pending(alias_email, exc.error_type)
        return {"ok": False, "error": exc.error_type, "status": 502}
    if not confirmed:
        db.mark_mailcom_alias_cleanup_pending(alias_email, "删除接口未确认")
        return {"ok": False, "error": "delete_unconfirmed", "status": 502}
    try:
        deleted = db.delete_mailcom_alias_entry(
            alias_email,
            reason=reason or "母号管理远端确认后删除别名",
            actor="manual",
            force=force,
        )
    except db.EmailPoolLifecycleError as exc:
        return {"ok": False, "error": exc.code, "status": 409, **exc.details}
    return {"ok": True, **deleted}


def queue_state() -> dict:
    with _PENDING_LOCK:
        with _SNAPSHOT_PENDING_LOCK:
            with _HISTORY_PENDING_LOCK:
                return {
                    "pending": len(_PENDING),
                    "parents": sorted(_PENDING),
                    "replenish_pending": len(_PENDING),
                    "replenish_parents": sorted(_PENDING),
                    "sync_pending": len(_SNAPSHOT_PENDING),
                    "sync_parents": sorted(_SNAPSHOT_PENDING),
                    "history_pending": len(_HISTORY_PENDING),
                    "history_parents": sorted(_HISTORY_PENDING),
                }


__all__ = [
    "delete_alias_now",
    "enqueue_parent_history_refresh",
    "enqueue_parent_replenish",
    "enqueue_parent_snapshot_sync",
    "enqueue_parent_sync",
    "queue_state",
    "refresh_parent_history_now",
    "sync_parent_now",
    "sync_parent_snapshot_now",
]
