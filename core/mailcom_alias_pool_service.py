# -*- coding: utf-8 -*-
"""mail.com 母号 alias 同步/补齐后台队列。"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from core import db
from core.mailcom_alias_service import MAX_ACTIVE_ALIASES, MailComAliasError, delete_alias, sync_parent_aliases


logger = logging.getLogger(__name__)
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mailcom-alias-sync")
_PENDING: set[str] = set()
_PENDING_LOCK = threading.Lock()


def _key(email: str) -> str:
    return str(email or "").strip().casefold()


def _mask(email: str) -> str:
    local, separator, domain = _key(email).partition("@")
    return f"{local[:1]}***@{domain}" if separator else "[redacted-email]"


def sync_parent_now(
    parent_email: str,
    *,
    sync_fn: Callable[[dict], dict] | None = None,
) -> dict:
    parent = db.get_mailcom_internal_record(parent_email)
    if not parent:
        return {"ok": False, "error": "parent_missing"}
    db.update_mailcom_parent_sync(parent_email, sync_status="syncing")
    try:
        result = (sync_fn or sync_parent_aliases)(parent)
    except MailComAliasError as exc:
        db.update_mailcom_parent_sync(parent_email, sync_status="failed", error=exc.error_type)
        logger.warning("[MailComAliasPool] 同步失败: parent=%s type=%s", _mask(parent_email), exc.error_type)
        return {"ok": False, "error": exc.error_type, "status": "failed"}
    except Exception as exc:  # pragma: no cover - 后台边界
        db.update_mailcom_parent_sync(parent_email, sync_status="failed", error=type(exc).__name__)
        logger.exception("[MailComAliasPool] 同步异常: parent=%s", _mask(parent_email))
        return {"ok": False, "error": type(exc).__name__, "status": "failed"}
    count = int(result.get("remote_active_alias_count") or 0)
    state = "ready" if count >= MAX_ACTIVE_ALIASES else "partial"
    db.update_mailcom_parent_sync(
        parent_email,
        sync_status=state,
        remote_active_alias_count=count,
    )
    return {"ok": True, "status": state, **result}


def _run(parent_email: str) -> None:
    try:
        sync_parent_now(parent_email)
    finally:
        with _PENDING_LOCK:
            _PENDING.discard(_key(parent_email))


def enqueue_parent_sync(parent_email: str) -> dict:
    key = _key(parent_email)
    if "@" not in key:
        return {"accepted": False, "error": "parent_invalid"}
    with _PENDING_LOCK:
        if key in _PENDING:
            return {"accepted": False, "busy": True, "parent_email": key}
        if db.get_mailcom_internal_record(key) is None:
            return {"accepted": False, "error": "parent_missing"}
        _PENDING.add(key)
    db.update_mailcom_parent_sync(key, sync_status="queued")
    try:
        _EXECUTOR.submit(_run, key)
    except Exception:
        with _PENDING_LOCK:
            _PENDING.discard(key)
        db.update_mailcom_parent_sync(key, sync_status="failed", error="queue_submit_failed")
        raise
    return {"accepted": True, "busy": False, "parent_email": key}


def delete_alias_now(alias_email: str) -> dict:
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
    return {"ok": True, "deleted": True}


def queue_state() -> dict:
    with _PENDING_LOCK:
        return {"pending": len(_PENDING), "parents": sorted(_PENDING)}


__all__ = ["delete_alias_now", "enqueue_parent_sync", "queue_state", "sync_parent_now"]
