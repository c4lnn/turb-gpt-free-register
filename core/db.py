# -*- coding: utf-8 -*-
"""
本地文件持久化层。

根目录文件分工：
    - 用于注册的邮箱.txt      仅保留可继续注册的邮箱素材
    - 注册成功的邮箱.txt      仅保存注册成功的邮箱素材，不追加 token
    - 注册成功的token.txt     每行只保存一个 access token
    - 用于注册的邮箱.json     Outlook 账号池完整状态
    - 注册成功的邮箱.json     注册成功账号完整状态
"""
import hashlib
import json
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

from config import email as _email_cfg
from core.sqlite_store import SQLiteRuntimeStore
from core.email_pool_status import (
    EMAIL_POOL_STATUSES,
    EMAIL_POOL_STATUS_SET,
    TERMINAL_EMAIL_POOL_STATUSES,
    can_transition,
    can_mark_used,
    canonical_status,
    is_manual_restorable,
    is_claimable,
    require_transition,
    status_counts,
    validate_status,
    LEGACY_EMAIL_POOL_STATUS_MAP,
)
from core.account_status_contracts import (
    build_account_status_contract,
    classify_plan_category,
    codex_auth_status,
    codex_operation_status,
    normalize_codex_auth_status,
    normalize_codex_operation_status,
    normalize_extract_link_status,
    extract_link_capabilities as contract_extract_link_capabilities,
)
from core.email_provider import VALID_EMAIL_SOURCES

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DATA_DIR = _PROJECT_ROOT
_LEGACY_DATA_DIR = _PROJECT_ROOT / "data"
_LOG_DIR = _PROJECT_ROOT / "注册日志"
_PLAN_CHECK_STALE_SECONDS = 120
_PLAN_CHECK_QUEUE_STALE_SECONDS = 1800
_PLAN_CHECK_ERROR_KINDS = frozenset({
    "network_timeout",
    "network_connection",
    "http_4xx",
    "http_5xx",
    "response_format",
    "local_token_expired",
})
_CHECKOUT_SESSION_STALE_SECONDS = 120
_CHECKOUT_SESSION_QUEUE_STALE_SECONDS = 1800

_OUTLOOK_JSON = _PROJECT_ROOT / "用于注册的邮箱.json"
_OUTLOOK_TXT = _PROJECT_ROOT / "用于注册的邮箱.txt"
_GENERIC_API_EMAIL_JSON = _PROJECT_ROOT / "用于注册的API邮箱.json"
_GENERIC_API_EMAIL_TXT = _PROJECT_ROOT / "用于注册的API邮箱.txt"
_ICLOUD_EMAIL_JSON = _PROJECT_ROOT / "用于注册的iCloud邮箱.json"
_EMAIL_POOL_LIFECYCLE_JSON = _PROJECT_ROOT / "email_pool_lifecycle.json"
_ACCOUNTS_JSON = _PROJECT_ROOT / "注册成功的邮箱.json"
_ACCOUNTS_TXT = _PROJECT_ROOT / "注册成功的邮箱.txt"
_TOKENS_TXT = _PROJECT_ROOT / "注册成功的token.txt"
_JOBS_JSON = _PROJECT_ROOT / "注册任务.json"
_RUNTIME_DB = _PROJECT_ROOT / "runtime.db"
_VIEWER_HTML = _PROJECT_ROOT / "accounts_viewer.html"
_CODEX_DIR = _PROJECT_ROOT / "codex_accounts"
# 导出状态单独存：{ "codex-邮箱-plan.json": {"exported_at": "...", "exported_count": N} }
# 不污染 CPA 兼容的原文件
_CODEX_EXPORT_STATE = _PROJECT_ROOT / "codex_导出状态.json"

_LEGACY_SQLITE = _LEGACY_DATA_DIR / "registrations.db"
_LEGACY_OUTLOOK_JSON = _LEGACY_DATA_DIR / "outlook_accounts.json"
_LEGACY_ACCOUNTS_JSON = _LEGACY_DATA_DIR / "registered_accounts.json"
_LEGACY_JOBS_JSON = _LEGACY_DATA_DIR / "registration_jobs.json"
_LOCK = threading.RLock()
_SQLITE_STORE = SQLiteRuntimeStore(_RUNTIME_DB)
_SQLITE_PATH_BINDINGS = {
    "_OUTLOOK_JSON": _OUTLOOK_JSON,
    "_GENERIC_API_EMAIL_JSON": _GENERIC_API_EMAIL_JSON,
    "_ICLOUD_EMAIL_JSON": _ICLOUD_EMAIL_JSON,
    "_ACCOUNTS_JSON": _ACCOUNTS_JSON,
    "_JOBS_JSON": _JOBS_JSON,
    "_DOMAIN_EMAIL_JSON": _PROJECT_ROOT / "用于注册的域名邮箱.json",
}


class EmailPoolLifecycleError(ValueError):
    """邮箱池生命周期操作的稳定冲突结果。"""

    def __init__(self, code: str, message: str | None = None, **details: Any):
        self.code = str(code)
        self.details = details
        super().__init__(message or self.code)


def _email_pool_lifecycle_path() -> Path:
    return _EMAIL_POOL_LIFECYCLE_JSON


def _load_email_pool_lifecycle() -> list[dict]:
    if _sqlite_enabled():
        rows = _SQLITE_STORE.load("email_pool_lifecycle")
    else:
        rows = _read_json(_email_pool_lifecycle_path(), [])
    return [dict(row) for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _save_email_pool_lifecycle(rows: list[dict]) -> None:
    if _sqlite_enabled():
        _SQLITE_STORE.replace_all("email_pool_lifecycle", rows)
    else:
        _write_json(_email_pool_lifecycle_path(), rows)


def list_email_pool_lifecycle(*, kind: str | None = None, key: str | None = None) -> list[dict]:
    with _LOCK:
        rows = _load_email_pool_lifecycle()
        if kind:
            rows = [row for row in rows if str(row.get("kind") or "") == str(kind)]
        if key:
            normalized = str(key).strip().casefold()
            rows = [row for row in rows if str(row.get("key") or "").casefold() == normalized]
        return [dict(row) for row in rows]


def _lifecycle_key(kind: str, value: str) -> str:
    return f"{str(kind or '').strip().casefold()}:{str(value or '').strip().casefold()}"


def _get_lifecycle_record_locked(kind: str, value: str) -> dict | None:
    key = _lifecycle_key(kind, value)
    rows = _load_email_pool_lifecycle()
    return next(
        (row for row in rows if _lifecycle_key(row.get("kind"), row.get("key")) == key),
        None,
    )


def _write_lifecycle_record_locked(
    kind: str,
    value: str,
    *,
    action: str,
    reason: str | None = None,
    parent_email: str | None = None,
    alias_email: str | None = None,
    generation: int | None = None,
    observed_at: str | None = None,
    actor: str | None = None,
    account_id: int | None = None,
) -> dict:
    rows = _load_email_pool_lifecycle()
    key = str(value or "").strip().casefold()
    now = _now()
    existing = next(
        (row for row in rows if _lifecycle_key(row.get("kind"), row.get("key")) == _lifecycle_key(kind, key)),
        None,
    )
    if existing is None:
        existing = {"id": _next_id(rows), "kind": str(kind), "key": key}
        rows.append(existing)
    existing.update({
        "action": str(action),
        "reason": str(reason or "")[:500] or None,
        "parent_email": str(parent_email or "").strip().casefold() or None,
        "alias_email": str(alias_email or "").strip().casefold() or None,
        "generation": int(generation) if generation is not None else existing.get("generation"),
        "deleted_at": existing.get("deleted_at") or (now if action in {"delete", "parent_delete", "alias_delete"} else None),
        "observed_at": str(observed_at or now),
        "actor": str(actor or "manual")[:80],
        "account_id": int(account_id) if account_id is not None else existing.get("account_id"),
        "updated_at": now,
    })
    _save_email_pool_lifecycle(rows)
    return dict(existing)


def _remove_lifecycle_record_locked(kind: str, value: str) -> bool:
    key = _lifecycle_key(kind, value)
    rows = _load_email_pool_lifecycle()
    remaining = [row for row in rows if _lifecycle_key(row.get("kind"), row.get("key")) != key]
    if len(remaining) == len(rows):
        return False
    _save_email_pool_lifecycle(remaining)
    return True


def _observe_lifecycle_record_locked(kind: str, value: str, *, observed_at: str | None = None) -> bool:
    record = _get_lifecycle_record_locked(kind, value)
    if record is None:
        return False
    rows = _load_email_pool_lifecycle()
    key = _lifecycle_key(kind, value)
    for row in rows:
        if _lifecycle_key(row.get("kind"), row.get("key")) == key:
            row["observed_at"] = str(observed_at or _now())
            row["updated_at"] = _now()
            _save_email_pool_lifecycle(rows)
            return True
    return False


def _sqlite_enabled() -> bool:
    backend = str(os.environ.get("RUNTIME_STORAGE_BACKEND") or "").strip().lower()
    bindings = _SQLITE_PATH_BINDINGS
    return backend == "sqlite" and _RUNTIME_DB.exists() and all(
        globals().get(name, expected) == expected
        for name, expected in bindings.items()
    )


def validate_runtime_storage() -> None:
    """启动时验证 SQLite；损坏时禁止静默降级到空文件数据。"""
    if _sqlite_enabled():
        _SQLITE_STORE.integrity_check()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _current_registration_job_id() -> int | None:
    """读取当前注册线程的任务 ID，避免模块导入阶段产生循环依赖。"""
    try:
        from core import registration_service

        value = getattr(registration_service._THREAD_CTX, "job_id", None)
        if value in (None, ""):
            return None
        return int(value)
    except (AttributeError, TypeError, ValueError):
        return None


def _normalize_pool_row(row: dict, *, missing_status: str = "disabled", alias: bool = False) -> dict:
    """在所有持久化入口规范化邮箱池状态和通用审计字段。"""
    raw_status = str(row.get("status") or "").strip().casefold()
    row["status"] = canonical_status(raw_status, missing=missing_status, unknown="disabled")
    if alias and raw_status == "active":
        row["status"] = "used" if row.get("registered_account_id") not in (None, "") else "available"
    if raw_status not in EMAIL_POOL_STATUS_SET and raw_status not in LEGACY_EMAIL_POOL_STATUS_MAP:
        row.setdefault(
            "status_migration_reason",
            "邮箱池 status 缺失或未知，已安全迁移为 disabled",
        )
    elif raw_status in LEGACY_EMAIL_POOL_STATUS_MAP:
        row.setdefault("status_migration_source", raw_status)
        row.setdefault(
            "status_migration_reason",
            f"历史状态 {raw_status} 已迁移为 {row['status']}",
        )
    row.setdefault("status_updated_at", row.get("updated_at") or row.get("imported_at"))
    row.setdefault("registration_job_id", None)
    row.setdefault("registration_started_at", None)
    row.setdefault("registration_completed_at", None)
    row.setdefault("failure_reason", None)
    row.setdefault("status_change_source", row.get("status_migration_source") or "legacy")
    row.setdefault("status_change_reason", row.get("status_migration_reason"))
    row.setdefault("manual_reactivated_from", None)
    row.setdefault("manual_reactivated_at", None)
    row.setdefault("manual_reactivated_by", None)
    row.setdefault("deleted_at", None)
    return row


def _mark_pool_row_used(row: dict, account_id: int | None = None) -> bool:
    """成功落库的内部状态边界。

    ``available -> used`` 只允许从明确的成功落库/已注册导入路径调用；
    失败和停用终态即使后来出现同名账号也不被复活。
    """
    current = canonical_status(row.get("status"), missing="disabled", unknown="disabled")
    if not can_mark_used(current):
        return False
    if current != "used":
        # can_transition 保持对外 API 的严格生命周期；这里是受控的账号
        # 落库边界，允许导入型 available 记录直接进入 used。
        _transition_pool_row(row, "used", force=True)
    if account_id is not None:
        row["registered_account_id"] = int(account_id)
    row["completed_at"] = row.get("completed_at") or _now()
    return True


def _normalize_pool_rows(rows: list[dict] | None, *, missing_status: str = "disabled", alias: bool = False) -> list[dict]:
    normalized: list[dict] = []
    for raw in rows or []:
        if not isinstance(raw, dict):
            continue
        normalized.append(_normalize_pool_row(dict(raw), missing_status=missing_status, alias=alias))
    return normalized


def _transition_pool_row(
    row: dict,
    target: str,
    *,
    note: str | None = None,
    job_id: int | None = None,
    force: bool = False,
    change_source: str | None = None,
    change_reason: str | None = None,
    reactivated_from: str | None = None,
) -> bool:
    """在内存行上执行统一状态迁移；force 仅供历史迁移使用。"""
    current = canonical_status(row.get("status"), missing="disabled", unknown="disabled")
    destination = validate_status(target)
    if not force and current != destination and not can_transition(current, destination):
        return False
    now = _now()
    row["status"] = destination
    row["status_updated_at"] = now
    previous_source = row.get("status_change_source")
    row["status_change_source"] = str(
        change_source or ("automatic" if previous_source in (None, "", "legacy") else previous_source)
    )[:80]
    if change_reason is not None:
        row["status_change_reason"] = str(change_reason)[:500] or None
    elif note is not None:
        row["status_change_reason"] = str(note)[:500] or None
    if reactivated_from is not None:
        row["manual_reactivated_from"] = str(reactivated_from)
        row["manual_reactivated_at"] = now
    if destination == "available":
        row["used_at"] = None
    else:
        row["used_at"] = row.get("used_at") or now
    if destination == "registering":
        row["registration_started_at"] = row.get("registration_started_at") or now
        row["registration_job_id"] = int(job_id) if job_id is not None else row.get("registration_job_id")
        row["registration_completed_at"] = None
    elif destination in TERMINAL_EMAIL_POOL_STATUSES:
        if job_id is not None:
            row["registration_job_id"] = int(job_id)
        row["registration_completed_at"] = now
    if note is not None:
        row["note"] = str(note)[:500]
    return True


def _pool_rows_for_email_locked(email: str) -> tuple[str | None, list[dict] | None, dict | None]:
    """返回邮箱所属池的 (kind, rows, row)，调用方必须持有 _LOCK。"""
    target = str(email or "").strip().casefold()
    if not target:
        return None, None, None
    pools = (
        ("outlook_emails", _load_outlook(), _find_by_email),
        ("generic_api_emails", _load_generic_api_emails(), _find_by_email),
        ("domain_emails", _load_domain_pool(), _find_domain_email),
        ("icloud_emails", _load_icloud_emails(), _find_by_email),
    )
    for kind, rows, finder in pools:
        row = finder(rows, target)
        if row is not None:
            return kind, rows, row
    return None, None, None


def _save_pool_rows(kind: str, rows: list[dict]) -> None:
    if kind == "outlook_emails":
        _save_outlook(rows)
    elif kind == "generic_api_emails":
        _save_generic_api_emails(rows)
    elif kind == "domain_emails":
        _save_domain_pool(rows)
    elif kind == "icloud_emails":
        _save_icloud_emails(rows)


def _active_registration_conflict_locked(kind: str, row: dict) -> str | None:
    """在统一锁内判断条目是否仍被注册任务/母号租约占用。"""
    state = canonical_status(row.get("status"), missing="disabled", unknown="disabled")
    if state == "registering":
        return "registration_busy"
    active_job_ids = {
        int(job.get("id") or 0)
        for job in _load_jobs()
        if str(job.get("status") or "") in {"pending", "running", "stopping"}
        and str(job.get("job_type") or "registration") == "registration"
        and str(job.get("id") or "").strip()
    }
    try:
        registration_job_id = int(row.get("registration_job_id"))
    except (TypeError, ValueError):
        registration_job_id = None
    if registration_job_id is not None and registration_job_id in active_job_ids:
        return "registration_busy"
    return None


def _pool_account_locked(email: str, row: dict | None = None) -> dict | None:
    target = str(email or "").strip().casefold()
    accounts = _load_accounts()
    account = _find_by_email(accounts, target)
    if account is not None:
        return account
    if row and row.get("registered_account_id") not in (None, ""):
        try:
            account_id = int(row.get("registered_account_id"))
        except (TypeError, ValueError):
            account_id = None
        if account_id is not None:
            return next((item for item in accounts if int(item.get("id") or 0) == account_id), None)
    return None


def restore_email_pool_entry(
    email: str,
    *,
    source: str = "manual",
    reason: str | None = None,
    actor: str | None = None,
    expected_status: str | None = None,
) -> dict:
    """唯一的人工/显式导入恢复边界，不放宽自动迁移规则。"""
    source = str(source or "manual").strip().casefold()
    if source not in {"manual", "import"}:
        raise EmailPoolLifecycleError("restore_source_invalid", "恢复来源非法")
    reason_text = str(reason or "").strip()
    if not reason_text:
        raise EmailPoolLifecycleError("restore_reason_required", "恢复原因不能为空")
    with _LOCK:
        kind, rows, row = _pool_rows_for_email_locked(email)
        if kind is None or rows is None or row is None:
            raise EmailPoolLifecycleError("email_not_found", "邮箱不存在")
        current = canonical_status(row.get("status"), missing="disabled", unknown="disabled")
        if expected_status is not None and current != canonical_status(expected_status):
            raise EmailPoolLifecycleError(
                "status_changed", "邮箱状态已变化", current_status=current,
            )
        if current == "registering" or _active_registration_conflict_locked(kind, row):
            raise EmailPoolLifecycleError("registration_busy", "邮箱仍有活动注册任务或租约")
        if not is_manual_restorable(current):
            raise EmailPoolLifecycleError("restore_not_allowed", "当前状态不允许人工恢复", current_status=current)
        now = _now()
        if not _transition_pool_row(
            row,
            "available",
            force=True,
            change_source=source,
            change_reason=reason_text,
            reactivated_from=current,
        ):
            raise EmailPoolLifecycleError("restore_failed", "邮箱恢复失败")
        row["manual_reactivated_from"] = current
        row["manual_reactivated_at"] = now
        row["manual_reactivated_by"] = str(actor or "manual")[:80]
        row["registration_job_id"] = None
        row["registration_started_at"] = None
        row["deleted_at"] = None
        row["note"] = reason_text[:500]
        _save_pool_rows(kind, rows)
        return {
            "email": row.get("alias_email") or row.get("email"),
            "source": source,
            "status": "available",
            "previous_status": current,
            "manual_reactivated_from": current,
            "manual_reactivated_at": now,
        }


def set_email_pool_status(
    email: str,
    status: str,
    *,
    source: str | None = None,
    reason: str | None = None,
    actor: str | None = None,
) -> dict:
    """统一人工状态 API；终态到 available 必须经过恢复边界。"""
    destination = validate_status(status)
    raw_reason = str(reason or "").strip()
    if destination == "available" and raw_reason == "":
        with _LOCK:
            _, _, existing_row = _pool_rows_for_email_locked(email)
            existing_state = canonical_status((existing_row or {}).get("status"), missing="disabled", unknown="disabled")
        if existing_row is not None and existing_state != "available":
            raise EmailPoolLifecycleError("restore_reason_required", "恢复邮箱必须填写原因")
    reason_text = (raw_reason or "手动修改邮箱池状态")[:500]
    with _LOCK:
        kind, rows, row = _pool_rows_for_email_locked(email)
        if kind is None or rows is None or row is None:
            raise EmailPoolLifecycleError("email_not_found", "邮箱不存在")
        if source and str(source).strip().casefold() not in {
            "outlook", "generic_api", "cloudflare_domain", "icloud",
        }:
            raise EmailPoolLifecycleError("source_invalid", "邮箱来源非法")
        normalized_source = str(source or "").strip().casefold()
        expected_kind = {
            "outlook": "outlook_emails",
            "generic_api": "generic_api_emails",
            "cloudflare_domain": "domain_emails",
            "icloud": "icloud_emails",
        }.get(normalized_source)
        if expected_kind and expected_kind != kind:
            raise EmailPoolLifecycleError("source_mismatch", "邮箱不属于指定来源", actual_source=kind)
        current = canonical_status(row.get("status"), missing="disabled", unknown="disabled")
    if destination == "available" and current != "available":
        return restore_email_pool_entry(
            email,
            source="manual",
            reason=reason_text,
            actor=actor,
            expected_status=current,
        )
    with _LOCK:
        kind, rows, row = _pool_rows_for_email_locked(email)
        if kind is None or rows is None or row is None:
            raise EmailPoolLifecycleError("email_not_found", "邮箱不存在")
        current = canonical_status(row.get("status"), missing="disabled", unknown="disabled")
        if current == destination:
            return {"email": row.get("alias_email") or row.get("email"), "status": current, "previous_status": current}
        if _active_registration_conflict_locked(kind, row):
            raise EmailPoolLifecycleError("registration_busy", "邮箱仍有活动注册任务或租约")
        if not _transition_pool_row(
            row,
            destination,
            note=reason_text,
            change_source="manual",
            change_reason=reason_text,
        ):
            raise EmailPoolLifecycleError(
                "status_transition_invalid",
                f"邮箱状态不可迁移: {current} -> {destination}",
                current_status=current,
                target_status=destination,
            )
        row["status_change_source"] = "manual"
        row["status_change_reason"] = reason_text
        if destination != "available":
            row["manual_reactivated_from"] = None
            row["manual_reactivated_at"] = None
        _save_pool_rows(kind, rows)
        return {
            "email": row.get("alias_email") or row.get("email"),
            "status": destination,
            "previous_status": current,
        }


def check_email_pool_delete(
    email: str,
    *,
    source: str | None = None,
    force: bool = False,
    reason: str | None = None,
) -> dict:
    """统一物理删除前置检查；调用方必须在同一锁内重新执行删除。"""
    with _LOCK:
        kind, rows, row = _pool_rows_for_email_locked(email)
        if kind is None or rows is None or row is None:
            raise EmailPoolLifecycleError("email_not_found", "邮箱不存在")
        expected_kind = {
            "outlook": "outlook_emails",
            "generic_api": "generic_api_emails",
            "cloudflare_domain": "domain_emails",
            "icloud": "icloud_emails",
        }.get(str(source or "").strip().casefold()) if source else None
        if expected_kind and expected_kind != kind:
            raise EmailPoolLifecycleError("source_mismatch", "邮箱不属于指定来源", actual_source=kind)
        conflict = _active_registration_conflict_locked(kind, row)
        if conflict:
            raise EmailPoolLifecycleError(conflict, "邮箱仍有活动注册任务或租约")
        account = _pool_account_locked(row.get("alias_email") or row.get("email"), row)
        if account is not None and not force:
            raise EmailPoolLifecycleError(
                "used_account_protected",
                "邮箱已关联注册账号，需 force=true 和删除原因",
                account_id=account.get("id"),
            )
        if account is not None and force and not str(reason or "").strip():
            raise EmailPoolLifecycleError("force_reason_required", "强制删除必须填写原因")
        return {
            "kind": kind,
            "email": row.get("alias_email") or row.get("email"),
            "status": canonical_status(row.get("status"), missing="disabled", unknown="disabled"),
            "account_id": account.get("id") if account else None,
            "force": bool(force),
        }


def delete_email_pool_entry(
    email: str,
    *,
    source: str | None = None,
    force: bool = False,
    reason: str | None = None,
    actor: str | None = None,
) -> dict:
    """按来源物理删除邮箱池条目，并保留高风险操作审计。"""
    with _LOCK:
        check = check_email_pool_delete(email, source=source, force=force, reason=reason)
        kind, rows, row = _pool_rows_for_email_locked(email)
        if kind is None or rows is None or row is None:
            raise EmailPoolLifecycleError("email_not_found", "邮箱不存在")
        target = str(row.get("alias_email") or row.get("email") or "").strip().casefold()
        remaining = [item for item in rows if item is not row and str(item.get("alias_email") or item.get("email") or "").strip().casefold() != target]
        _save_pool_rows(kind, remaining)
        audit = _write_lifecycle_record_locked(
            "email",
            target,
            action="delete",
            reason=reason or "用户永久删除邮箱池条目",
            actor=actor,
            account_id=check.get("account_id"),
        )
        return {"email": target, "source": source or kind, "deleted": True, "audit": audit, **check}


def mark_registration_success(email: str, account_id: int | None = None) -> bool:
    """把已生成本地账号的邮箱条目标记为 used，且不允许复活其它终态。"""
    with _LOCK:
        kind, rows, row = _pool_rows_for_email_locked(email)
        if row is None or rows is None or kind is None:
            return False
        current = canonical_status(row.get("status"), missing="disabled", unknown="disabled")
        if current in {"failed", "disabled"}:
            return False
        if current not in {"available", "registering", "used"}:
            return False
        if not _mark_pool_row_used(row, account_id=account_id):
            return False
        _save_pool_rows(kind, rows)
        return True


def mark_registration_failed(
    email: str,
    reason: str | None = None,
    *,
    stage: str | None = None,
    job_id: int | None = None,
) -> bool:
    """将未生成账号的已领取邮箱置为 failed；账号已存在时保持 used。"""
    with _LOCK:
        if _find_by_email(_load_accounts(), email) is not None:
            return mark_registration_success(email)
        kind, rows, row = _pool_rows_for_email_locked(email)
        if row is None or rows is None or kind is None:
            return False
        current = canonical_status(row.get("status"), missing="disabled", unknown="disabled")
        if current in {"failed", "disabled"}:
            return False
        note = str(reason or "注册失败")[:420]
        if stage:
            note = f"{stage}: {note}"[:500]
        if not _transition_pool_row(row, "failed", note=note, job_id=job_id):
            return False
        row["failure_reason"] = note
        _save_pool_rows(kind, rows)
        return True


def disable_registration_email(
    email: str,
    reason: str | None = None,
    *,
    job_id: int | None = None,
) -> bool:
    """将邮箱条目标记为 disabled；任何 disabled 条目都不可领取。"""
    with _LOCK:
        kind, rows, row = _pool_rows_for_email_locked(email)
        if row is None or rows is None or kind is None:
            return False
        note = str(reason or "邮箱已停用")[:500]
        if not _transition_pool_row(row, "disabled", note=note, job_id=job_id):
            return False
        row["failure_reason"] = note
        _save_pool_rows(kind, rows)
        return True


def migrate_email_pool_statuses() -> dict[str, int]:
    """幂等迁移旧邮箱池状态，并恢复启动时孤立的注册条目。

    迁移以不可复用为安全默认：没有账号且没有活动注册任务的旧 used 或
    registering 条目进入 failed，而不是重新暴露为 available。
    """
    with _LOCK:
        accounts = _load_accounts()
        account_emails = {
            str(row.get("email") or "").strip().casefold()
            for row in accounts
            if str(row.get("email") or "").strip()
        }
        jobs = _load_jobs()
        active_emails = {
            str(row.get("email") or "").strip().casefold()
            for row in jobs
            if str(row.get("status") or "") in {"pending", "running", "stopping"}
            and str(row.get("job_type") or "registration") == "registration"
            and str(row.get("email") or "").strip()
        }
        active_job_ids = {
            int(row.get("id") or 0)
            for row in jobs
            if str(row.get("status") or "") in {"pending", "running", "stopping"}
            and str(row.get("job_type") or "registration") == "registration"
            and row.get("id") not in (None, "")
        }
        changed = 0
        failed = 0
        normalized = 0

        def migrate_rows(kind: str, rows: list[dict], *, aliases: bool = False) -> None:
            nonlocal changed, failed, normalized
            dirty = False
            for row in rows:
                before = dict(row)
                email = str(row.get("alias_email") or row.get("email") or "").strip().casefold()
                raw_status = str(row.get("status") or "").strip().casefold()
                current = canonical_status(raw_status, missing="disabled", unknown="disabled")
                source_status = str(row.get("status_migration_source") or raw_status).strip().casefold()
                known_status = source_status in EMAIL_POOL_STATUS_SET or source_status in {
                    "active", "leased", "registered", "registration_failed", "deleted",
                }

                # 失败/停用是不可逆审计终态，不能因为后来发现同名账号或
                # 远端同步结果而改成 used/available。
                if current in {"failed", "disabled"}:
                    target = current
                elif source_status == "registered":
                    target = "used"
                elif not known_status:
                    target = "disabled"
                elif row.get("registered_account_id") not in (None, "") or email in account_emails:
                    target = "used"
                elif current in {"used", "registering"}:
                    job_id = row.get("registration_job_id")
                    try:
                        job_is_active = int(job_id) in active_job_ids
                    except (TypeError, ValueError):
                        job_is_active = False
                    target = "registering" if email in active_emails or job_is_active else "failed"
                else:
                    # available/active 且没有本地账号的记录继续可领取。
                    target = current

                _normalize_pool_row(row, missing_status="disabled", alias=aliases)
                if target != row.get("status"):
                    _transition_pool_row(
                        row,
                        target,
                        note="邮箱池状态启动迁移" if target in {"failed", "used", "registering"} else None,
                        force=True,
                    )
                if target == "failed" and current != "failed":
                    row["failure_reason"] = row.get("failure_reason") or "历史状态未确认成功，安全迁移为 failed"
                    failed += 1
                if row != before:
                    dirty = True
                    normalized += 1
            if dirty:
                _save_pool_rows(kind, rows)
                changed += 1

        migrate_rows("outlook_emails", _load_outlook())
        migrate_rows("generic_api_emails", _load_generic_api_emails())
        migrate_rows("domain_emails", _load_domain_pool())
        migrate_rows("icloud_emails", _load_icloud_emails())

        return {"stores_changed": changed, "rows_normalized": normalized, "rows_failed": failed}


def _ensure_storage() -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _LOG_DIR.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path, default: Any) -> Any:
    _ensure_storage()
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data: Any) -> None:
    _ensure_storage()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    json.loads(tmp.read_text(encoding="utf-8"))
    tmp.replace(path)


def _write_text_atomic(path: Path, text: str) -> None:
    _ensure_storage()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.read_text(encoding="utf-8")
    tmp.replace(path)


def _next_id(items: list[dict]) -> int:
    ids = [int(item.get("id") or 0) for item in items]
    return (max(ids) if ids else 0) + 1


def _outlook_line(row: dict) -> str:
    return "----".join([
        row.get("email") or "",
        row.get("password") or "",
        row.get("client_id") or "",
        row.get("refresh_token") or "",
    ])


def _generic_api_email_line(row: dict) -> str:
    return "----".join([
        row.get("email") or "",
        row.get("code_url") or "",
    ])


def _extract_registration_password(row: dict) -> str:
    """从账号记录提取 ChatGPT 登录密码（registration_password）。

    优先从 extra_json.registration_password 读取，回退到顶层 registration_password；
    读取失败或未设置时返回空字符串。
    """
    extra_raw = row.get("extra_json")
    if isinstance(extra_raw, str) and extra_raw.strip():
        try:
            extra = json.loads(extra_raw)
        except Exception:
            extra = {}
    elif isinstance(extra_raw, dict):
        extra = extra_raw
    else:
        extra = {}
    return str(extra.get("registration_password") or row.get("registration_password") or "").strip()


def _account_line(row: dict) -> str:
    base = row.get("original_email_line") or row.get("email") or ""
    token = row.get("access_token") or ""
    gpt_password = _extract_registration_password(row) or "未设置"
    totp = row.get("totp_secret") or ""
    parts = [base, token, gpt_password]
    if totp:
        parts.append(totp)
    return "----".join(parts)


def _registered_email_line(row: dict) -> str:
    """生成注册成功邮箱 TXT 的行内容；token 由注册成功的token.txt 单独保存。"""
    return row.get("original_email_line") or row.get("email") or ""


def _sync_outlook_txt(rows: list[dict]) -> None:
    available_rows = [r for r in rows if r.get("status") == "available"]
    lines = [_outlook_line(r) for r in sorted(available_rows, key=lambda x: int(x.get("id") or 0))]
    _write_text_atomic(_OUTLOOK_TXT, "\n".join(lines) + ("\n" if lines else ""))


def _sync_generic_api_email_txt(rows: list[dict]) -> None:
    available_rows = [r for r in rows if r.get("status") == "available"]
    lines = [_generic_api_email_line(r) for r in sorted(available_rows, key=lambda x: int(x.get("id") or 0))]
    _write_text_atomic(_GENERIC_API_EMAIL_TXT, "\n".join(lines) + ("\n" if lines else ""))


def _sync_accounts_txt(rows: list[dict]) -> None:
    lines = [_registered_email_line(r) for r in sorted(rows, key=lambda x: int(x.get("id") or 0))]
    _write_text_atomic(_ACCOUNTS_TXT, "\n".join(lines) + ("\n" if lines else ""))


def _sync_tokens_txt(rows: list[dict]) -> None:
    tokens = [
        r.get("access_token") or ""
        for r in sorted(rows, key=lambda x: int(x.get("id") or 0))
        if r.get("access_token")
    ]
    _write_text_atomic(_TOKENS_TXT, "\n".join(tokens) + ("\n" if tokens else ""))


def _viewer_snapshot(outlook_rows: list[dict], account_rows: list[dict]) -> dict:
    account_by_email = {
        (a.get("email") or "").lower(): a
        for a in account_rows
    }
    return {
        "generated_at": _now(),
        "accounts": [
            _decorate_account(r, include_checkout_session_id=False)
            for r in sorted(account_rows, key=lambda x: int(x.get("id") or 0), reverse=True)
        ],
        "outlook": [
            _decorate_outlook(r, account_by_email)
            for r in sorted(outlook_rows, key=lambda x: int(x.get("id") or 0), reverse=True)
        ],
        "summary": {
            "accounts": len(account_rows),
            "outlook_total": len(outlook_rows),
            **{f"outlook_{status}": sum(1 for r in outlook_rows if r.get("status") == status) for status in EMAIL_POOL_STATUSES},
        },
    }


def _render_static_viewer(outlook_rows: list[dict] | None = None, account_rows: list[dict] | None = None) -> Path:
    """生成可直接双击打开的静态账号查看页。"""
    outlook_rows = _load_outlook() if outlook_rows is None else outlook_rows
    account_rows = _load_accounts() if account_rows is None else account_rows
    snapshot = _viewer_snapshot(outlook_rows, account_rows)
    data_json = json.dumps(snapshot, ensure_ascii=False).replace("</", "<\\/")
    title = escape(f"账号查看器 - {snapshot['generated_at']}")
    html_text = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <style>
    * {{ box-sizing: border-box; }}
    :root {{
      --bg: #eef3f8;
      --surface: #ffffff;
      --soft: #f7f9fc;
      --text: #172033;
      --muted: #667085;
      --line: #d9e2ec;
      --blue: #2563eb;
      --green: #16803c;
      --red: #c2413a;
      --amber: #b7791f;
    }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    header {{
      padding: 22px 28px;
      background: #101827;
      color: #fff;
      display: flex;
      justify-content: space-between;
      gap: 20px;
      align-items: center;
      flex-wrap: wrap;
    }}
    h1, h2, p {{ margin: 0; }}
    h1 {{ font-size: 28px; }}
    .meta {{ margin-top: 6px; color: #b8c7d9; font-size: 13px; }}
    .stats {{ display: flex; gap: 10px; flex-wrap: wrap; }}
    .stat {{
      min-width: 116px;
      padding: 10px 12px;
      border: 1px solid rgba(255,255,255,.16);
      border-radius: 8px;
      background: rgba(255,255,255,.08);
    }}
    .stat span {{ display: block; color: #b8c7d9; font-size: 12px; }}
    .stat strong {{ display: block; margin-top: 4px; font-size: 18px; }}
    main {{ width: min(1500px, calc(100vw - 32px)); margin: 16px auto 30px; display: grid; gap: 16px; }}
    .toolbar, section {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      box-shadow: 0 8px 22px rgba(15,23,42,.06);
    }}
    .toolbar {{ padding: 14px; display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; }}
    .search {{ min-width: min(520px, 100%); flex: 1; }}
    input {{
      width: 100%;
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 0 12px;
      font: inherit;
    }}
    .buttons {{ display: flex; gap: 8px; flex-wrap: wrap; }}
    button {{
      min-height: 32px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      padding: 0 12px;
      font-weight: 700;
      cursor: pointer;
    }}
    button:hover {{ background: var(--soft); }}
    button.primary {{ border-color: var(--blue); background: var(--blue); color: #fff; }}
    button.good {{ border-color: #2f855a; background: #edf8f1; color: #166534; }}
    button:disabled {{ color: #98a2b3; cursor: not-allowed; background: #f2f4f7; }}
    .head {{ padding: 14px 16px; border-bottom: 1px solid var(--line); background: var(--soft); }}
    .head p {{ margin-top: 4px; color: var(--muted); font-size: 12px; }}
    .table-wrap {{ overflow: auto; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ padding: 10px 12px; border-bottom: 1px solid #edf1f5; text-align: left; white-space: nowrap; vertical-align: middle; }}
    th {{ position: sticky; top: 0; background: #fbfcfe; color: #475467; z-index: 1; font-size: 12px; }}
    tr:hover td {{ background: #fbfdff; }}
    .main-cell {{ font-weight: 700; }}
    .sub-cell {{ margin-top: 3px; color: var(--muted); font-size: 12px; }}
    .mono {{ font-family: ui-monospace, "JetBrains Mono", Consolas, monospace; font-size: 12px; }}
    .muted {{ color: var(--muted); }}
    .pill {{ display: inline-flex; min-width: 48px; justify-content: center; padding: 3px 8px; border-radius: 999px; font-size: 12px; font-weight: 700; }}
    .status-available {{ color: var(--blue); background: #eef4ff; }}
    .status-used {{ color: #475467; background: #f2f4f7; }}
    .status-registering {{ color: var(--amber); background: #fff7e6; }}
    .status-failed {{ color: var(--red); background: #fff0ef; }}
    .status-disabled {{ color: #475467; background: #f2f4f7; }}
    .actions {{ display: flex; gap: 8px; flex-wrap: wrap; }}
    #toast {{
      position: fixed;
      right: 18px;
      bottom: 18px;
      padding: 10px 14px;
      border-radius: 8px;
      background: #101827;
      color: #fff;
      box-shadow: 0 14px 30px rgba(15,23,42,.24);
      opacity: 0;
      transform: translateY(8px);
      pointer-events: none;
      transition: opacity .18s ease, transform .18s ease;
    }}
    #toast.show {{ opacity: 1; transform: translateY(0); }}
    @media (max-width: 820px) {{
      header {{ align-items: flex-start; }}
      .stats {{ width: 100%; }}
      .stat {{ flex: 1; }}
    }}
  </style>
</head>
<body>
<header>
  <div>
    <h1>账号查看器</h1>
    <p class="meta">静态快照，无需启动 Web Server。生成时间：<span id="generated"></span></p>
  </div>
  <div class="stats">
    <div class="stat"><span>已完成</span><strong id="statAccounts">0</strong></div>
    <div class="stat"><span>邮箱总数</span><strong id="statOutlook">0</strong></div>
    <div class="stat"><span>可用邮箱</span><strong id="statAvailable">0</strong></div>
  </div>
</header>
<main>
  <div class="toolbar">
    <div class="search"><input id="q" placeholder="搜索邮箱、token、clientId、状态"></div>
    <div class="buttons">
      <button class="primary" id="copyAllTokens">复制全部 Token</button>
      <button class="good" id="copyAllLines">复制全部整行</button>
      <button id="copyAllEmails">复制全部邮箱素材</button>
    </div>
  </div>
  <section>
    <div class="head">
      <h2>已完成账号</h2>
      <p>整行格式：邮箱----密码----clientId----邮箱刷新令牌----accessToken----totpSecret（如有）</p>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>ID</th><th>邮箱</th><th>来源</th><th>AT</th><th>备注</th><th>2FA</th><th>创建时间</th><th>操作</th></tr></thead>
        <tbody id="accountsBody"></tbody>
      </table>
    </div>
  </section>
  <section>
    <div class="head">
      <h2>邮箱素材库</h2>
      <p>原始格式：邮箱----密码----clientId----邮箱刷新令牌；注册完成后可直接复制对应 Token 或整行。</p>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>邮箱</th><th>状态</th><th>AT</th><th>导入时间</th><th>已用时间</th><th>操作</th></tr></thead>
        <tbody id="outlookBody"></tbody>
      </table>
    </div>
  </section>
</main>
<div id="toast"></div>
<script id="snapshot" type="application/json">{data_json}</script>
<script>
const SNAPSHOT = JSON.parse(document.getElementById('snapshot').textContent);
const $ = (s) => document.querySelector(s);
let copySeq = 0;
const copyStore = new Map();

function fmt(v) {{ return v == null || v === '' ? '-' : String(v); }}
function esc(v) {{
  return fmt(v).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}}
function short(v, n = 34) {{
  const s = v || '';
  return s.length > n ? `${{s.slice(0, n)}}...` : s;
}}
function copyId(v) {{
  if (!v) return '';
  const id = `c${{++copySeq}}`;
  copyStore.set(id, v);
  return id;
}}
function btn(label, value, cls = '') {{
  const id = copyId(value);
  return `<button class="${{cls}}" data-copy-id="${{id}}" ${{id ? '' : 'disabled'}}>${{label}}</button>`;
}}
function pill(status) {{
  const map = {{ available: '可用', registering: '注册中', used: '已用', failed: '失败', disabled: '已停用' }};
  const label = map[status] || status || '-';
  return `<span class="pill status-${{esc(status)}}">${{esc(label)}}</span>`;
}}
function showToast(text) {{
  const toast = $('#toast');
  toast.textContent = text;
  toast.classList.add('show');
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove('show'), 1400);
}}
async function copyText(text) {{
  if (!text) return;
  if (navigator.clipboard && window.isSecureContext) {{
    await navigator.clipboard.writeText(text);
  }} else {{
    const area = document.createElement('textarea');
    area.value = text;
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    document.execCommand('copy');
    area.remove();
  }}
  showToast('已复制');
}}
function haystack(row) {{
  return Object.values(row).join('\\n').toLowerCase();
}}
function render() {{
  copyStore.clear();
  copySeq = 0;
  const q = $('#q').value.trim().toLowerCase();
  const accounts = SNAPSHOT.accounts.filter((r) => !q || haystack(r).includes(q));
  const outlook = SNAPSHOT.outlook.filter((r) => !q || haystack(r).includes(q));
  $('#generated').textContent = SNAPSHOT.generated_at;
  $('#statAccounts').textContent = SNAPSHOT.summary.accounts;
  $('#statOutlook').textContent = SNAPSHOT.summary.outlook_total;
  $('#statAvailable').textContent = SNAPSHOT.summary.outlook_available;
  $('#accountsBody').innerHTML = accounts.map((r) => `
    <tr>
      <td class="muted">#${{esc(r.id)}}</td>
      <td><div class="main-cell">${{esc(r.email)}}</div><div class="sub-cell">${{esc(r.user_name || '-')}}</div></td>
      <td>${{esc(r.email_source || '-')}}</td>
      <td><span class="mono">${{esc(short(r.access_token || '', 42))}}</span></td>
      <td title="${{esc(r.note || '')}}">${{r.note ? esc(short(r.note, 60)) : '<span class="muted">-</span>'}}</td>
      <td>${{r.totp_secret ? '已启用' : '<span class="muted">未启用</span>'}}</td>
      <td class="muted">${{esc(r.created_at || '-')}}</td>
      <td class="actions">${{btn('复制AT', r.access_token, 'primary')}} ${{btn('复制整行', r.copy_line, 'good')}}</td>
    </tr>`).join('');
  $('#outlookBody').innerHTML = outlook.map((r) => `
    <tr>
      <td><div class="main-cell">${{esc(r.email)}}</div><div class="sub-cell mono">${{esc(short(r.copy_line, 76))}}</div></td>
      <td>${{pill(r.status)}}</td>
      <td><span class="mono">${{esc(short(r.access_token || '', 36) || '未生成')}}</span></td>
      <td class="muted">${{esc(r.imported_at || r.created_at || '-')}}</td>
      <td class="muted">${{esc(r.used_at || '-')}}</td>
      <td class="actions">${{btn('复制邮箱', r.copy_line)}} ${{btn('复制AT', r.access_token, 'primary')}} ${{btn('复制整行', r.account_copy_line, 'good')}}</td>
    </tr>`).join('');
}}
document.addEventListener('click', (e) => {{
  const target = e.target.closest('[data-copy-id]');
  if (!target) return;
  copyText(copyStore.get(target.dataset.copyId));
}});
$('#q').addEventListener('input', render);
$('#copyAllTokens').addEventListener('click', () => copyText(SNAPSHOT.accounts.map((r) => r.access_token).filter(Boolean).join('\\n')));
$('#copyAllLines').addEventListener('click', () => copyText(SNAPSHOT.accounts.map((r) => r.copy_line).filter(Boolean).join('\\n')));
$('#copyAllEmails').addEventListener('click', () => copyText(SNAPSHOT.outlook.map((r) => r.copy_line).filter(Boolean).join('\\n')));
render();
</script>
</body>
</html>
"""
    tmp = _VIEWER_HTML.with_suffix(".html.tmp")
    tmp.write_text(html_text, encoding="utf-8")
    try:
        tmp.replace(_VIEWER_HTML)
        return _VIEWER_HTML
    except PermissionError:
        # Windows 下如果目标 HTML 正被浏览器或编辑器短暂占用，原子替换可能失败。
        # 先尝试直接覆盖；仍失败时写一个时间戳快照，避免注册流程被查看页刷新阻断。
        try:
            _VIEWER_HTML.write_text(html_text, encoding="utf-8")
            try:
                tmp.unlink()
            except OSError:
                pass
            return _VIEWER_HTML
        except PermissionError:
            fallback = _DATA_DIR / f"accounts_viewer_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
            fallback.write_text(html_text, encoding="utf-8")
            try:
                tmp.unlink()
            except OSError:
                pass
            return fallback


def _load_outlook() -> list[dict]:
    if _sqlite_enabled():
        rows = _SQLITE_STORE.load("outlook_emails")
        return _normalize_pool_rows(rows, missing_status="disabled")
    rows = _read_json(_OUTLOOK_JSON, None)
    if not isinstance(rows, list):
        rows = _read_json(_LEGACY_OUTLOOK_JSON, [])
    return _normalize_pool_rows(rows if isinstance(rows, list) else [], missing_status="disabled")


def _save_outlook(rows: list[dict]) -> None:
    if _sqlite_enabled():
        _SQLITE_STORE.replace_all("outlook_emails", rows)
    else:
        _write_json(_OUTLOOK_JSON, rows)
    _sync_outlook_txt(rows)
    _render_static_viewer(outlook_rows=rows)


def _load_generic_api_emails() -> list[dict]:
    if _sqlite_enabled():
        rows = _SQLITE_STORE.load("generic_api_emails")
        return _normalize_pool_rows(rows, missing_status="disabled")
    rows = _read_json(_GENERIC_API_EMAIL_JSON, [])
    return _normalize_pool_rows(rows if isinstance(rows, list) else [], missing_status="disabled")


def _save_generic_api_emails(rows: list[dict]) -> None:
    for row in rows:
        row["copy_line"] = _generic_api_email_line(row)
    if _sqlite_enabled():
        _SQLITE_STORE.replace_all("generic_api_emails", rows)
    else:
        _write_json(_GENERIC_API_EMAIL_JSON, rows)
    _sync_generic_api_email_txt(rows)


def _load_accounts() -> list[dict]:
    if _sqlite_enabled():
        return _SQLITE_STORE.load("accounts")
    rows = _read_json(_ACCOUNTS_JSON, None)
    if not isinstance(rows, list):
        rows = _read_json(_LEGACY_ACCOUNTS_JSON, [])
    return rows if isinstance(rows, list) else []


def _save_accounts(rows: list[dict]) -> None:
    for row in rows:
        row["copy_line"] = _account_line(row)
    if _sqlite_enabled():
        _SQLITE_STORE.replace_all("accounts", rows)
    else:
        _write_json(_ACCOUNTS_JSON, rows)
    _sync_accounts_txt(rows)
    _sync_tokens_txt(rows)
    _render_static_viewer(account_rows=rows)


def _load_jobs() -> list[dict]:
    if _sqlite_enabled():
        return _SQLITE_STORE.load("jobs")
    rows = _read_json(_JOBS_JSON, None)
    if not isinstance(rows, list):
        rows = _read_json(_LEGACY_JOBS_JSON, [])
    return rows if isinstance(rows, list) else []


def _save_jobs(rows: list[dict]) -> None:
    if _sqlite_enabled():
        _SQLITE_STORE.replace_all("jobs", rows)
    else:
        _write_json(_JOBS_JSON, rows)


def _save_accounts_with_pool(accounts: list[dict], pool_kind: str, pool_rows: list[dict]) -> None:
    """在 SQLite 中原子提交账号与邮箱池；旧存储保持原有兼容路径。"""
    if not _sqlite_enabled():
        _save_accounts(accounts)
        if pool_kind == "outlook_emails":
            _save_outlook(pool_rows)
        elif pool_kind == "generic_api_emails":
            _save_generic_api_emails(pool_rows)
        elif pool_kind == "icloud_emails":
            _save_icloud_emails(pool_rows)
        return
    for row in accounts:
        row["copy_line"] = _account_line(row)
    if pool_kind == "generic_api_emails":
        for row in pool_rows:
            row["copy_line"] = _generic_api_email_line(row)
    _SQLITE_STORE.replace_many({"accounts": accounts, pool_kind: pool_rows})
    _sync_accounts_txt(accounts)
    _sync_tokens_txt(accounts)
    if pool_kind == "outlook_emails":
        _sync_outlook_txt(pool_rows)
        _render_static_viewer(outlook_rows=pool_rows, account_rows=accounts)
    elif pool_kind == "generic_api_emails":
        _sync_generic_api_email_txt(pool_rows)


def _find_by_email(rows: list[dict], email: str) -> dict | None:
    target = (email or "").lower()
    return next((r for r in rows if (r.get("email") or "").lower() == target), None)


def _plan_check_error_kind_for_row(row: dict) -> str | None:
    """返回套餐失败分类，并兼容没有新字段的历史记录。"""
    try:
        http_status = int(row.get("plan_check_http_status"))
    except (TypeError, ValueError):
        http_status = None
    if http_status is not None:
        if 400 <= http_status < 500:
            return "http_4xx"
        if 500 <= http_status < 600:
            return "http_5xx"

    current = str(row.get("plan_check_error_kind") or "").strip()
    if current in _PLAN_CHECK_ERROR_KINDS:
        return current

    error = str(row.get("plan_check_error") or "")
    lowered = error.lower()
    if error.strip() in {
        # 兼容历史版本生成的旧文案，以及当前简化后的新文案。
        "AT已过期/失效，请手动查活刷新",
        "本地判断：AT已过期/失效，请手动查活刷新",
        "AT已失效，请手动查活刷新",
        "本地AT已失效，请手动查活刷新",
    }:
        return "local_token_expired"
    if re.search(r"\bhttp\s*4\d{2}\b", lowered):
        return "http_4xx"
    if re.search(r"\bhttp\s*5\d{2}\b", lowered):
        return "http_5xx"
    if "timeout" in lowered or "timed out" in lowered or "超时" in error:
        return "network_timeout"
    if any(marker in error for marker in ("响应不是 JSON", "响应缺少", "未找到可解析的账号条目")):
        return "response_format"
    if any(marker in lowered for marker in ("invalid json", "jsondecodeerror")):
        return "response_format"
    if any(
        marker in lowered
        for marker in (
            "connectionerror",
            "proxyerror",
            "sslerror",
            "connection refused",
            "connection reset",
            "connect failed",
        )
    ):
        return "network_connection"
    return None


def _decorate_account(row: dict, *, include_checkout_session_id: bool = True) -> dict:
    out = {
        key: value
        for key, value in dict(row).items()
        if not (
            str(key).lower().startswith("codex_agent_")
            or str(key).lower() in {"agent_identity", "agent_runtime_id", "agent_private_key"}
        )
    }
    if not include_checkout_session_id:
        # 完整 Checkout Session ID 只允许留在服务端账号 JSON 和内部任务边界。
        out.pop("checkout_session_id", None)
        # 兼容旧记录：公共装饰路径不携带可能包含响应 secret 的原始诊断。
        out.pop("checkout_check_result_json", None)
        for key in (
            "checkout_check_error_code",
            "checkout_check_error_message",
            "checkout_check_error",
            "checkout_check_message",
            "checkout_check_proxy_used",
        ):
            if key in out:
                out[key] = _safe_checkout_diagnostic_text(out.get(key))
        for key in (
            "client_secret",
            "customer_session",
            "customer_session_secret",
            "publishable_key",
            "response",
            "payload",
        ):
            out.pop(key, None)
    out["note"] = out.get("note") or ""
    out["note_updated_at"] = out.get("note_updated_at") or ""
    plan_status = out.get("plan_check_status")
    if plan_status in {"queued", "running"}:
        try:
            stamp_key = "plan_check_queued_at" if plan_status == "queued" else "plan_check_started_at"
            stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if plan_status == "queued" else _PLAN_CHECK_STALE_SECONDS
            started_at = datetime.fromisoformat(str(out.get(stamp_key) or ""))
            if (datetime.now() - started_at).total_seconds() >= stale_after:
                out["plan_check_status"] = "failed"
                out["plan_check_error"] = "上次套餐查询状态已超时，可重新查询"
                out["plan_check_stale"] = True
        except (TypeError, ValueError):
            out["plan_check_status"] = "failed"
            out["plan_check_error"] = "上次套餐查询状态异常，可重新查询"
            out["plan_check_stale"] = True
    checkout_status = out.get("checkout_check_status")
    if checkout_status in {"queued", "running"}:
        try:
            stamp_key = "checkout_check_queued_at" if checkout_status == "queued" else "checkout_check_started_at"
            stale_after = (
                _CHECKOUT_SESSION_QUEUE_STALE_SECONDS
                if checkout_status == "queued"
                else _CHECKOUT_SESSION_STALE_SECONDS
            )
            started_at = datetime.fromisoformat(str(out.get(stamp_key) or ""))
            if (datetime.now() - started_at).total_seconds() >= stale_after:
                out["checkout_check_status"] = "failed"
                out["checkout_check_error_message"] = "上次 Checkout 检测状态已超时，可重新检测"
                out["checkout_check_error"] = out["checkout_check_error_message"]
                out["checkout_check_ok"] = False
                out["checkout_check_stale"] = True
        except (TypeError, ValueError):
            out["checkout_check_status"] = "failed"
            out["checkout_check_error_message"] = "上次 Checkout 检测状态异常，可重新检测"
            out["checkout_check_error"] = out["checkout_check_error_message"]
            out["checkout_check_ok"] = False
            out["checkout_check_stale"] = True
    if out.get("plan_check_status") == "failed":
        error_kind = _plan_check_error_kind_for_row(out)
        if error_kind:
            out["plan_check_error_kind"] = error_kind
        else:
            out.pop("plan_check_error_kind", None)
    out["extract_link_resumable"] = account_extract_resumable(out)
    out.update(build_account_status_contract(out))
    out["copy_line"] = _account_line(out)
    return out


def _account_matches_plan_filter(row: dict, plan_filter: str | None = None) -> bool:
    """按规范套餐类别过滤；旧值仅在兼容期映射到规范类别。"""
    f = str(plan_filter or "").strip().lower()
    if not f or f in {"all", "any"}:
        return True
    category = classify_plan_category(row)
    if f in {"free_trial_eligible", "free_no_trial", "paid", "unknown"}:
        return category == f
    # 兼容旧前端参数：free 仍表示所有已确认的 Free 分类，plus 表示 paid。
    if f == "free":
        return category in {"free_trial_eligible", "free_no_trial"}
    if f == "plus":
        return category == "paid"
    return False


def _account_matches_codex_filter(row: dict, status_filter: str | None = None) -> bool:
    """按 Codex 授权/操作/查活维度兼容旧筛选参数。"""
    value = str(status_filter or "").strip().lower()
    if not value or value in {"all", "any"}:
        return True
    if value in {"retrying", "running"}:
        return codex_operation_status(row) == "running"
    if value in {"stopped", "cancelled", "canceled"}:
        return codex_operation_status(row) == "canceled"
    if value == "deactivated":
        return str(row.get("live_check_status") or "").strip().lower() == "deactivated"
    return codex_auth_status(row) == normalize_codex_auth_status(value)


def _account_matches_checkout_type_filter(row: dict, checkout_type_filter: str | None = None) -> bool:
    """账号 Checkout Session 类型过滤；none 表示尚未保存类型。"""
    f = str(checkout_type_filter or "").strip().lower()
    if not f or f in {"all", "any"}:
        return True
    current = str(row.get("checkout_session_type") or "").strip().lower()
    if f in {"none", "empty", "未检测"}:
        return not current
    return current == f


def _account_email_source_code(row: dict | None) -> str:
    """返回账号来源的规范 code；缺失或历史未知值归入 unknown。"""
    raw = str((row or {}).get("email_source") or "").strip().lower()
    return raw if raw in set(VALID_EMAIL_SOURCES) else "unknown"


def _account_matches_email_source_filter(row: dict, email_source_filter: str | None = None) -> bool:
    """按邮箱来源 code 过滤，unknown 覆盖缺失和未识别的原始值。"""
    value = str(email_source_filter or "").strip().lower()
    if not value or value in {"all", "any"}:
        return True
    return _account_email_source_code(row) == value


def _decorate_outlook(row: dict, account_by_email: dict[str, dict] | None = None) -> dict:
    out = dict(row)
    out["copy_line"] = _outlook_line(out)
    account = None
    if account_by_email is not None:
        account = account_by_email.get((out.get("email") or "").lower())
    if account:
        out["registered_account_id"] = account.get("id")
        out["access_token"] = account.get("access_token")
        out["access_token_preview"] = (
            (account.get("access_token") or "")[:40] + "..."
            if account.get("access_token")
            else ""
        )
        out["account_copy_line"] = _account_line(account)
        out["totp_secret"] = account.get("totp_secret")
    return out


def _decorate_generic_api_email(row: dict, account_by_email: dict[str, dict] | None = None) -> dict:
    out = dict(row)
    out["copy_line"] = _generic_api_email_line(out)
    out["password"] = out.get("password") or ""
    out["client_id"] = out.get("client_id") or ""
    out["refresh_token"] = out.get("refresh_token") or ""
    account = None
    if account_by_email is not None:
        account = account_by_email.get((out.get("email") or "").lower())
    if account:
        out["registered_account_id"] = account.get("id")
        out["access_token"] = account.get("access_token")
        out["access_token_preview"] = (
            (account.get("access_token") or "")[:40] + "..."
            if account.get("access_token")
            else ""
        )
        out["account_copy_line"] = _account_line(account)
        out["totp_secret"] = account.get("totp_secret")
    return out


def _get_conn() -> None:
    """兼容旧入口：初始化文件存储目录。"""
    _ensure_storage()
    return None


def _row_to_dict(row: dict | None) -> dict | None:
    return dict(row) if row is not None else None


# ============================================================
# registered_accounts
# ============================================================

def insert_account(
    *,
    email: str,
    access_token: str,
    totp_secret: str | None = None,
    user_id: str | None = None,
    user_name: str | None = None,
    plan_type: str | None = None,
    expires_at: str | None = None,
    device_id: str | None = None,
    proxy_used: str | None = None,
    email_source: str | None = None,
    extra: dict | None = None,
    codex_status: str | None = None,   # success / failed / skipped / missing
    codex_error: str | None = None,    # 失败原因（仅 codex_status=failed 时有意义）
) -> int:
    """插入或更新注册成功账号，返回本地文件中的 id。"""
    with _LOCK:
        accounts = _load_accounts()
        outlook_rows = _load_outlook()
        generic_rows = _load_generic_api_emails()
        domain_rows = _load_domain_pool()
        icloud_rows = _load_icloud_emails()
        existing = _find_by_email(accounts, email)
        outlook_row = _find_by_email(outlook_rows, email)
        generic_row = _find_by_email(generic_rows, email)
        domain_row = _find_domain_email(domain_rows, email)
        icloud_row = _find_by_email(icloud_rows, email)
        extra_json = json.dumps(extra, ensure_ascii=False) if extra else None

        if existing is None:
            row_id = _next_id(accounts)
            row = {
                "id": row_id,
                "email": email,
                "created_at": _now(),
            }
            accounts.append(row)
        else:
            row = existing
            row_id = int(row["id"])

        row.update({
            "access_token": access_token,
            "totp_secret": totp_secret if totp_secret is not None else row.get("totp_secret"),
            "user_id": user_id if user_id is not None else row.get("user_id"),
            "user_name": user_name if user_name is not None else row.get("user_name"),
            "plan_type": plan_type if plan_type is not None else row.get("plan_type"),
            "expires_at": expires_at if expires_at is not None else row.get("expires_at"),
            "device_id": device_id if device_id is not None else row.get("device_id"),
            "proxy_used": proxy_used if proxy_used is not None else row.get("proxy_used"),
            "email_source": email_source if email_source is not None else row.get("email_source"),
            "extra_json": extra_json if extra_json is not None else row.get("extra_json"),
            "codex_status": codex_status if codex_status is not None else row.get("codex_status"),
            "codex_error": codex_error if codex_error is not None else row.get("codex_error"),
            "updated_at": _now(),
        })

        if outlook_row:
            row["password"] = outlook_row.get("password")
            row["client_id"] = outlook_row.get("client_id")
            row["refresh_token"] = outlook_row.get("refresh_token")
            row["original_email_line"] = _outlook_line(outlook_row)
            _mark_pool_row_used(outlook_row, account_id=row_id)
            outlook_row["access_token"] = access_token
            outlook_row["completed_at"] = _now()
            if totp_secret:
                outlook_row["totp_secret"] = totp_secret

        # 其它持久化邮箱池也必须在账号落库的同一生命周期边界变为 used。
        for pool_row in (generic_row, domain_row, icloud_row):
            if pool_row is None:
                continue
            _mark_pool_row_used(pool_row, account_id=row_id)
            pool_row["completed_at"] = _now()
            if access_token:
                pool_row["access_token"] = access_token

        row["copy_line"] = _account_line(row)
        changed_pools = {
            "outlook_emails": outlook_rows if outlook_row else None,
            "generic_api_emails": generic_rows if generic_row else None,
            "domain_emails": domain_rows if domain_row else None,
            "icloud_emails": icloud_rows if icloud_row else None,
        }
        changed_pools = {kind: rows for kind, rows in changed_pools.items() if rows is not None}
        if _sqlite_enabled():
            payload = {"accounts": accounts, **changed_pools}
            _SQLITE_STORE.replace_many(payload)
            _sync_accounts_txt(accounts)
            _sync_tokens_txt(accounts)
            if "outlook_emails" in changed_pools:
                _sync_outlook_txt(outlook_rows)
                _render_static_viewer(outlook_rows=outlook_rows, account_rows=accounts)
            if "generic_api_emails" in changed_pools:
                _sync_generic_api_email_txt(generic_rows)
        else:
            _save_accounts(accounts)
            for kind, rows in changed_pools.items():
                _save_pool_rows(kind, rows)
        return row_id


def _parse_extra_json(value: object) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except Exception:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def update_account_codex_status(email: str, codex_status: str, codex_error: str | None = None) -> bool:
    """
    单独更新某账号的 codex_status / codex_error（手动补跑 Codex 时用）。
    返回是否找到该账号。
    """
    with _LOCK:
        accounts = _load_accounts()
        row = _find_by_email(accounts, email)
        if row is None:
            return False
        raw_status = str(codex_status or "").strip().lower()
        if raw_status in {"retrying", "stopped", "cancelled", "canceled"}:
            row["codex_operation_status"] = normalize_codex_operation_status(raw_status)
            row["codex_operation_error"] = codex_error
            if row["codex_operation_status"] == "running":
                row["codex_operation_started_at"] = _now()
            else:
                row["codex_operation_completed_at"] = _now()
        elif raw_status == "deactivated":
            row["live_check_status"] = "deactivated"
            row["live_check_error"] = codex_error or "账号已删除/停用/封禁"
            row["codex_auth_status"] = normalize_codex_auth_status(row.get("codex_auth_status"))
        else:
            # legacy 字段仅投影授权事实，不能再承载补跑过程。
            row["codex_status"] = codex_status
            row["codex_auth_status"] = normalize_codex_auth_status(raw_status)
            row["codex_operation_status"] = "success" if raw_status == "success" else "idle"
            row["codex_error"] = codex_error
            row["codex_operation_error"] = codex_error if raw_status == "failed" else None
            row["codex_operation_completed_at"] = _now()
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def recover_interrupted_codex_operations() -> int:
    """将重启前遗留的 Codex 补跑活动状态安全收敛为失败。

    补跑线程状态只存在于进程内，重启后不能把 queued/running 误报为仍在执行；
    该恢复只改变操作维度，不改变授权事实或 legacy 原始字段。
    """
    with _LOCK:
        accounts = _load_accounts()
        now = _now()
        recovered = 0
        for row in accounts:
            raw = str(row.get("codex_status") or "").strip().lower()
            operation = str(row.get("codex_operation_status") or "").strip().lower()
            if not operation and raw in {"retrying", "queued", "running"}:
                operation = "running" if raw in {"retrying", "running"} else "queued"
            if operation not in {"queued", "running"}:
                continue
            row["codex_operation_status"] = "failed"
            row["codex_operation_error"] = "WebUI 重启导致 Codex 补跑中断，请重新补跑"
            row["codex_operation_completed_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(accounts)
        return recovered


def migrate_account_status_contracts() -> dict[str, int]:
    """幂等迁移账号规范状态字段；适用于当前 JSON 或 SQLite 后端。"""
    with _LOCK:
        accounts = _load_accounts()
        changed = unknown = 0
        for row in accounts:
            before = dict(row)
            legacy = str(row.get("codex_status") or "").strip().lower()
            if legacy and not row.get("codex_status_legacy_raw"):
                row["codex_status_legacy_raw"] = legacy
            if not str(row.get("codex_auth_status") or "").strip():
                row["codex_auth_status"] = normalize_codex_auth_status(legacy)
            if not str(row.get("codex_operation_status") or "").strip():
                row["codex_operation_status"] = (
                    normalize_codex_operation_status(legacy)
                    if legacy in {"retrying", "stopped", "cancelled", "canceled"}
                    else "idle"
                )
            if legacy == "deactivated" and not str(row.get("live_check_status") or "").strip():
                row["live_check_status"] = "deactivated"
            if row.get("codex_auth_status") == "unknown":
                unknown += 1
            if row != before:
                row["status_contract_migrated_at"] = _now()
                changed += 1
        if changed:
            _save_accounts(accounts)
        return {"changed": changed, "unknown": unknown, "total": len(accounts)}


def claim_account_plan_check(
    acc_id: int | None = None,
    email: str | None = None,
    trigger: str = "manual",
) -> bool:
    """原子占用账号的套餐查询；已有未超时查询时返回 False。"""
    with _LOCK:
        accounts = _load_accounts()
        target_email = (email or "").lower()
        row = next((
            r for r in accounts
            if (acc_id is not None and int(r.get("id") or 0) == int(acc_id))
            or (target_email and (r.get("email") or "").lower() == target_email)
        ), None)
        if row is None:
            return False

        current_status = row.get("plan_check_status")
        if current_status in {"queued", "running"}:
            try:
                stamp_key = "plan_check_queued_at" if current_status == "queued" else "plan_check_started_at"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if current_status == "queued" else _PLAN_CHECK_STALE_SECONDS
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (datetime.now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass

        now = _now()
        row["plan_check_status"] = "queued"
        row["plan_check_trigger"] = str(trigger or "manual")
        row["plan_check_queued_at"] = now
        row["plan_check_updated_at"] = now
        row["plan_check_started_at"] = None
        row["plan_check_completed_at"] = None
        row["plan_check_error"] = None
        row["plan_check_error_kind"] = None
        row["updated_at"] = now
        _save_accounts(accounts)
        return True


def mark_account_plan_check_running(acc_id: int) -> bool:
    """把已排队的套餐查询标记为执行中。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("plan_check_status") not in {"queued", "running"}:
            return False
        now = _now()
        row["plan_check_status"] = "running"
        row["plan_check_started_at"] = now
        row["plan_check_updated_at"] = now
        row["plan_check_error"] = None
        row["plan_check_error_kind"] = None
        row["updated_at"] = now
        _save_accounts(accounts)
        return True


def recover_interrupted_plan_checks() -> int:
    """服务启动时把上次进程遗留的内存队列状态恢复为可重试失败。"""
    with _LOCK:
        accounts = _load_accounts()
        recovered = 0
        now = _now()
        for row in accounts:
            if row.get("plan_check_status") not in {"queued", "running"}:
                continue
            row["plan_check_status"] = "failed"
            row["plan_check_ok"] = False
            row["plan_check_error"] = "WebUI 重启导致套餐查询中断，请重新查询"
            row["plan_check_error_kind"] = None
            row["plan_check_completed_at"] = now
            row["plan_check_updated_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(accounts)
        return recovered


def update_account_plan_check(acc_id: int | None = None, email: str | None = None, result: dict | None = None) -> bool:
    """更新账号套餐/Plus 试用资格查询结果。"""
    result = result or {}
    with _LOCK:
        accounts = _load_accounts()
        target_email = (email or "").lower()
        row = next((
            r for r in accounts
            if (acc_id is not None and int(r.get("id") or 0) == int(acc_id))
            or (target_email and (r.get("email") or "").lower() == target_email)
        ), None)
        if row is None:
            return False

        ok = bool(result.get("ok"))
        now = _now()
        row["plan_check_status"] = "success" if ok else "failed"
        row["plan_check_ok"] = ok
        row["plan_checked_at"] = result.get("checked_at") or now
        row["plan_check_completed_at"] = now
        row["plan_check_updated_at"] = now
        row["plan_check_http_status"] = result.get("http_status")
        row["plan_check_error"] = None if ok else result.get("error")
        error_kind = str(result.get("plan_check_error_kind") or "").strip()
        row["plan_check_error_kind"] = error_kind if error_kind in _PLAN_CHECK_ERROR_KINDS else None
        # 这个标志表达本次响应是否足够完整，不能由 plus_trial_eligible 的
        # 普通布尔值替代；失败查询或非布尔资格必须明确写为不可判定。
        plus_trial_eligible = result.get("plus_trial_eligible")
        trial_eligibility_known = bool(
            ok
            and result.get("trial_eligibility_known") is True
            and (plus_trial_eligible is True or plus_trial_eligible is False)
        )
        row["trial_eligibility_known"] = trial_eligibility_known

        if result.get("account_id"):
            row["account_id"] = result.get("account_id")
        # 查询失败只更新本次错误和网络信息，不覆盖上一次成功拿到的套餐、
        # 试用资格、优惠及有效期，避免临时网络故障把真实权益清空。
        if ok:
            if result.get("current_plan_type"):
                row["current_plan_type"] = result.get("current_plan_type")
                row["plan_type"] = result.get("current_plan_type")
            if result.get("subscription_plan") is not None:
                row["subscription_plan"] = result.get("subscription_plan")
            if result.get("has_active_subscription") is not None:
                row["has_active_subscription"] = bool(result.get("has_active_subscription"))
            if result.get("expires_at") is not None:
                row["plan_expires_at"] = result.get("expires_at")
            if result.get("renews_at") is not None:
                row["plan_renews_at"] = result.get("renews_at")
            if result.get("cancels_at") is not None:
                row["plan_cancels_at"] = result.get("cancels_at")
            if result.get("billing_period") is not None:
                row["billing_period"] = result.get("billing_period")
            if result.get("billing_currency") is not None:
                row["billing_currency"] = result.get("billing_currency")
            if result.get("is_delinquent") is not None:
                row["is_delinquent"] = bool(result.get("is_delinquent"))
            for _k in (
                "discount_type",
                "discount_amount",
                "discount_duration_num_periods",
                "discount_expires_at",
                "discount_cancellation_policy",
                "discount_promo_campaign_id",
                "last_purchase_origin_platform",
                "last_will_renew",
            ):
                if result.get(_k) is not None:
                    row[_k] = result.get(_k)

            row["plus_trial_eligible"] = (
                plus_trial_eligible if trial_eligibility_known else None
            )
            row["plus_trial_campaign_id"] = result.get("plus_trial_campaign_id")
            row["plus_trial_title"] = result.get("plus_trial_title")
            row["plus_trial_discount_percentage"] = result.get("plus_trial_discount_percentage")
            row["plus_trial_duration_num_periods"] = result.get("plus_trial_duration_num_periods")
            row["plus_trial_duration_period"] = result.get("plus_trial_duration_period")
            row["eligible_offer_ids"] = result.get("eligible_offer_ids") or []
            row["plan_last_success_at"] = result.get("checked_at") or _now()
            row["plan_last_success_result_json"] = json.dumps(result, ensure_ascii=False)
        row["plan_check_proxy_mode"] = result.get("proxy_mode")
        row["plan_check_network_route"] = result.get("network_route")
        row["plan_check_proxy_used"] = result.get("proxy_used")
        row["plan_check_proxy_fallback_reason"] = result.get("proxy_fallback_reason")
        row["token_expired"] = result.get("token_expired")
        row["token_expires_at"] = result.get("token_expires_at")
        row["needs_live_check"] = result.get("needs_live_check")
        row["plan_check_result_json"] = json.dumps(result, ensure_ascii=False)
        row["updated_at"] = now
        _save_accounts(accounts)
        return True


def claim_account_totp_setup(acc_id: int, trigger: str = "manual") -> bool:
    """原子占用账号 2FA 设置任务；已有未超时任务时返回 False。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        current_status = row.get("totp_setup_status")
        if current_status in {"queued", "running"}:
            try:
                stamp_key = "totp_setup_queued_at" if current_status == "queued" else "totp_setup_started_at"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if current_status == "queued" else _PLAN_CHECK_STALE_SECONDS
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (datetime.now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass
        now = _now()
        row["totp_setup_status"] = "queued"
        row["totp_setup_ok"] = False
        row["totp_setup_trigger"] = str(trigger or "manual")
        row["totp_setup_queued_at"] = now
        row["totp_setup_started_at"] = None
        row["totp_setup_completed_at"] = None
        row["totp_setup_error"] = None
        row["updated_at"] = now
        _save_accounts(accounts)
        return True


def mark_account_totp_setup_running(acc_id: int) -> bool:
    """把 2FA 设置任务标记为运行中。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("totp_setup_status") not in {"queued", "running"}:
            return False
        now = _now()
        row["totp_setup_status"] = "running"
        row["totp_setup_started_at"] = now
        row["totp_setup_error"] = None
        row["updated_at"] = now
        _save_accounts(accounts)
        return True


def update_account_totp_secret(acc_id: int, result: dict | None = None) -> bool:
    """更新账号 2FA/TOTP 设置结果。"""
    result = result or {}
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        status = str(result.get("status") or ("success" if result.get("ok") else "failed"))
        ok = bool(result.get("ok")) and status == "success"
        row["totp_setup_status"] = status
        row["totp_setup_ok"] = ok
        row["totp_setup_checked_at"] = result.get("checked_at") or _now()
        if status in {"success", "failed", "stopped"}:
            row["totp_setup_completed_at"] = _now()
        row["totp_setup_error"] = None if ok or status == "running" else result.get("error")
        secret = str(result.get("totp_secret") or "").strip()
        if ok and secret:
            row["totp_secret"] = secret
        if result.get("message") is not None:
            row["totp_setup_message"] = result.get("message")
        row["copy_line"] = _account_line(row)
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def recover_interrupted_totp_setups() -> int:
    """服务启动时恢复上次进程中断的 2FA 设置状态。"""
    with _LOCK:
        accounts = _load_accounts()
        recovered = 0
        now = _now()
        for row in accounts:
            if row.get("totp_setup_status") not in {"queued", "running"}:
                continue
            row["totp_setup_status"] = "failed"
            row["totp_setup_ok"] = False
            row["totp_setup_error"] = "WebUI 重启导致 2FA 设置中断，请重新开启"
            row["totp_setup_completed_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(accounts)
        return recovered


def claim_account_checkout_session(acc_id: int, trigger: str = "manual") -> bool:
    """原子占用账号的 Checkout Session 检测任务。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False

        current_status = row.get("checkout_check_status")
        if current_status in {"queued", "running"}:
            try:
                stamp_key = "checkout_check_queued_at" if current_status == "queued" else "checkout_check_started_at"
                stale_after = (
                    _CHECKOUT_SESSION_QUEUE_STALE_SECONDS
                    if current_status == "queued"
                    else _CHECKOUT_SESSION_STALE_SECONDS
                )
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (datetime.now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass

        now = _now()
        row["checkout_check_status"] = "queued"
        row["checkout_check_ok"] = False
        row["checkout_check_trigger"] = str(trigger or "manual")
        row["checkout_check_queued_at"] = now
        row["checkout_check_started_at"] = None
        row["checkout_check_completed_at"] = None
        row["checkout_check_updated_at"] = now
        row["checkout_check_error_code"] = None
        row["checkout_check_error_message"] = None
        row["checkout_check_error"] = None
        row["checkout_check_message"] = "已入队"
        row["updated_at"] = now
        _save_accounts(accounts)
        return True


def mark_account_checkout_session_running(acc_id: int) -> bool:
    """把已排队的 Checkout 检测标记为运行中。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("checkout_check_status") not in {"queued", "running"}:
            return False
        now = _now()
        row["checkout_check_status"] = "running"
        row["checkout_check_started_at"] = now
        row["checkout_check_updated_at"] = now
        row["checkout_check_error_code"] = None
        row["checkout_check_error_message"] = None
        row["checkout_check_error"] = None
        row["checkout_check_message"] = "正在创建初始 Checkout Session"
        row["updated_at"] = now
        _save_accounts(accounts)
        return True


def _safe_checkout_diagnostic_text(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).replace("\x00", " ").split())[:240]
    text = re.sub(r"(?i)((?:[a-z][a-z0-9+.-]*://)?)([^/@\s:]+):([^/@\s]+)@", r"\1[redacted]@", text)
    text = re.sub(r"(?i)(?:oaics_|cs_)[A-Za-z0-9._~-]+", "[redacted-session]", text)
    text = re.sub(
        r"(?ix)([\"']?(?:client_secret|customer_session[^\s\"'=:\\]*|publishable_key|authorization)[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^,;\s}]+)",
        r"\1[redacted]",
        text,
    )
    text = re.sub(
        r"(?i)(?:client_secret|customer_session(?:_secret)?|publishable_key)[A-Za-z0-9._~-]*",
        "[redacted-secret]",
        text,
    )
    return text or None


def update_account_checkout_session(acc_id: int, result: dict | None = None) -> bool:
    """写入 Checkout 检测终态；失败不得清空上次成功 ID/type。"""
    result = result or {}
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False

        ok = bool(result.get("ok")) and bool(result.get("checkout_session_id"))
        now = _now()
        status = "success" if ok else "failed"
        row["checkout_check_status"] = status
        row["checkout_check_ok"] = ok
        row["checkout_check_completed_at"] = now
        row["checkout_check_updated_at"] = now
        row["checkout_check_http_status"] = result.get("http_status")
        row["checkout_check_error_code"] = None if ok else _safe_checkout_diagnostic_text(result.get("error_code"))
        message = None if ok else _safe_checkout_diagnostic_text(
            result.get("error_message") or result.get("error")
        )
        row["checkout_check_error_message"] = message
        row["checkout_check_error"] = message
        row["checkout_check_message"] = "检测成功" if ok else (message or "Checkout 检测失败")
        row["checkout_check_attempt_count"] = result.get("attempt_count")
        row["checkout_check_max_attempts"] = result.get("max_attempts")
        row["checkout_check_request_timeout"] = result.get("request_timeout")
        row["checkout_check_network_route"] = result.get("network_route")
        row["checkout_check_proxy_mode"] = result.get("proxy_mode")
        row["checkout_check_proxy_used"] = _safe_checkout_diagnostic_text(result.get("proxy_used"))
        row["checkout_check_retryable"] = bool(result.get("retryable"))
        row["checkout_check_content_type"] = _safe_checkout_diagnostic_text(result.get("content_type"))
        row["checkout_check_response_bytes"] = result.get("response_bytes")
        row["checkout_check_retry_after_seconds"] = result.get("retry_after_seconds")
        row["checkout_check_checked_at"] = result.get("checked_at") or now

        if ok:
            # 完整 ID 只写入账号 JSON；任何装饰/列表路径都会主动移除它。
            row["checkout_session_id"] = str(result.get("checkout_session_id") or "").strip()
            session_type = str(result.get("checkout_session_type") or "").strip().lower()
            row["checkout_session_type"] = session_type if session_type in {
                "oaics", "cs_live", "other_cs", "unknown",
            } else "unknown"
            row["checkout_session_last_success_at"] = result.get("checked_at") or now

        # 仅保存白名单诊断，禁止把原始响应、AT、完整代理或完整 ID 序列化到结果字段。
        diagnostic = {
            key: row.get(key)
            for key in (
                "checkout_check_status",
                "checkout_check_ok",
                "checkout_check_http_status",
                "checkout_check_error_code",
                "checkout_check_error_message",
                "checkout_check_attempt_count",
                "checkout_check_max_attempts",
                "checkout_check_network_route",
                "checkout_check_proxy_mode",
                "checkout_check_proxy_used",
                "checkout_check_response_bytes",
                "checkout_check_content_type",
            )
            if row.get(key) is not None
        }
        row["checkout_check_result_json"] = json.dumps(diagnostic, ensure_ascii=False)
        row["updated_at"] = now
        _save_accounts(accounts)
        return True


def recover_interrupted_checkout_sessions() -> int:
    """服务启动时恢复遗留任务，不自动重新发送可能有副作用的 POST。"""
    with _LOCK:
        accounts = _load_accounts()
        recovered = 0
        now = _now()
        for row in accounts:
            if row.get("checkout_check_status") not in {"queued", "running"}:
                continue
            row["checkout_check_status"] = "failed"
            row["checkout_check_ok"] = False
            row["checkout_check_error_code"] = "interrupted"
            row["checkout_check_error_message"] = (
                "WebUI 重启导致 Checkout 检测中断，服务端可能已创建 Session，请确认后再重新检测"
            )
            row["checkout_check_error"] = row["checkout_check_error_message"]
            row["checkout_check_message"] = row["checkout_check_error_message"]
            row["checkout_check_completed_at"] = now
            row["checkout_check_updated_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(accounts)
        return recovered


# 兼容更短的内部命名，便于队列/API 测试和后续调用方复用。
claim_account_checkout_check = claim_account_checkout_session
mark_account_checkout_check_running = mark_account_checkout_session_running
update_account_checkout_check = update_account_checkout_session
recover_interrupted_checkout_checks = recover_interrupted_checkout_sessions


def claim_account_extract(
    acc_id: int,
    trigger: str = "manual",
    link_type: str = "pix",
    provider: str = "legacy",
    update_mode: str = "sse",
) -> bool:
    """原子占用账号提链任务；已有未超时任务时返回 False。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        current_status = row.get("extract_link_status")
        if current_status in {"queued", "running"}:
            try:
                stamp_key = "extract_link_queued_at" if current_status == "queued" else "extract_link_started_at"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if current_status == "queued" else _PLAN_CHECK_STALE_SECONDS
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (datetime.now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass
        now = _now()
        row["extract_link_status"] = "queued"
        row["extract_link_ok"] = False
        row["extract_link_trigger"] = str(trigger or "manual")
        row["extract_link_type"] = str(link_type or "pix").lower()
        row["extract_link_provider"] = str(provider or "legacy").lower()
        row["extract_link_update_mode"] = str(update_mode or "sse").lower()
        row["extract_link_queued_at"] = now
        row["extract_link_started_at"] = None
        row["extract_link_completed_at"] = None
        row["extract_link_error"] = None
        row["extract_link_message"] = "已入队"
        for key in (
            "extract_link_job_id", "extract_link_cdk_id", "extract_link_cdk_fingerprint",
            "extract_link_long_url", "extract_link_copy_paste", "extract_link_image_url_png",
            "extract_link_image_url_svg", "extract_link_payment_method",
            "extract_link_payment_link_type", "extract_link_expires_at",
            "extract_link_result_json", "extract_link_cdk_remaining",
        ):
            row[key] = None
        row["updated_at"] = now
        _save_accounts(accounts)
        return True


_EXTRACT_RESUME_ERROR_MARKERS = (
    "Masi Job 等待超时",
    "Masi Job 连续查询失败",
    "WebUI 重启导致提链任务中断",
)


def _account_extract_resumable_legacy(row: dict | None) -> bool:
    row = row or {}
    error = str(row.get("extract_link_error") or "")
    return bool(
        normalize_extract_link_status(row.get("extract_link_status")) == "failed"
        and str(row.get("extract_link_provider") or "").lower() == "masi"
        and str(row.get("extract_link_job_id") or "").strip()
        and str(row.get("extract_link_cdk_id") or "").strip()
        and any(marker in error for marker in _EXTRACT_RESUME_ERROR_MARKERS)
    )


def account_extract_capabilities(row: dict | None) -> dict[str, bool]:
    row = row or {}
    return contract_extract_link_capabilities(
        row.get("extract_link_status"),
        resumable=_account_extract_resumable_legacy(row),
        has_access_token=bool(str(row.get("access_token") or "").strip()),
    )


def account_extract_resumable(row: dict | None) -> bool:
    return account_extract_capabilities(row)["resumable"]


def claim_account_extract_resume(acc_id: int, trigger: str = "manual_resume") -> bool:
    """原子占用已有 Masi Job 的恢复轮询；保留原 job/CDK 绑定。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if not account_extract_resumable(row):
            return False
        now = _now()
        row["extract_link_status"] = "queued"
        row["extract_link_ok"] = False
        row["extract_link_trigger"] = str(trigger or "manual_resume")
        row["extract_link_queued_at"] = now
        row["extract_link_started_at"] = None
        row["extract_link_completed_at"] = None
        row["extract_link_error"] = None
        row["extract_link_message"] = "已入队恢复原 Masi Job 轮询"
        row["updated_at"] = now
        _save_accounts(accounts)
        return True


def mark_account_extract_running(acc_id: int) -> bool:
    """把提链任务标记为运行中。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("extract_link_status") not in {"queued", "running"}:
            return False
        row["extract_link_status"] = "running"
        row["extract_link_started_at"] = _now()
        row["extract_link_error"] = None
        row["extract_link_message"] = "任务运行中"
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def update_account_extract(acc_id: int, result: dict | None = None) -> bool:
    """更新账号提链任务结果/进度。"""
    result = result or {}
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        status = normalize_extract_link_status(
            result.get("status") or ("success" if result.get("ok") else "failed")
        )
        ok = bool(result.get("ok")) and status == "success"
        row["extract_link_status"] = status
        row["extract_link_ok"] = ok
        row["extract_link_checked_at"] = result.get("checked_at") or _now()
        if status in {"success", "failed", "canceled"}:
            row["extract_link_completed_at"] = _now()
        row["extract_link_error"] = None if ok or status == "running" else result.get("error")
        if result.get("message") is not None:
            row["extract_link_message"] = result.get("message")
        if result.get("job_id") is not None:
            row["extract_link_job_id"] = result.get("job_id")
        if result.get("link_type") is not None:
            row["extract_link_type"] = result.get("link_type")
        if result.get("provider") is not None:
            row["extract_link_provider"] = result.get("provider")
        if result.get("update_mode") is not None:
            row["extract_link_update_mode"] = result.get("update_mode")
        if result.get("cdk_id") is not None:
            row["extract_link_cdk_id"] = result.get("cdk_id")
        if result.get("cdk_fingerprint") is not None:
            row["extract_link_cdk_fingerprint"] = result.get("cdk_fingerprint")
        if result.get("cdk_remaining") is not None:
            row["extract_link_cdk_remaining"] = result.get("cdk_remaining")
        payload = result.get("result") if isinstance(result.get("result"), dict) else {}
        if payload:
            row["extract_link_long_url"] = payload.get("long_url")
            row["extract_link_copy_paste"] = payload.get("copy_paste")
            row["extract_link_image_url_png"] = payload.get("image_url_png")
            row["extract_link_image_url_svg"] = payload.get("image_url_svg")
            row["extract_link_payment_method"] = payload.get("payment_method")
            row["extract_link_payment_link_type"] = payload.get("payment_link_type")
            row["extract_link_expires_at"] = payload.get("expires_at")
            if payload.get("cdk_remaining") is not None:
                row["extract_link_cdk_remaining"] = payload.get("cdk_remaining")
            row["extract_link_result_json"] = json.dumps(payload, ensure_ascii=False)
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def recover_interrupted_extract_links() -> int:
    """服务启动时恢复上次进程中断的提链状态。"""
    with _LOCK:
        accounts = _load_accounts()
        recovered = 0
        now = _now()
        for row in accounts:
            if row.get("extract_link_status") not in {"queued", "running"}:
                continue
            row["extract_link_status"] = "failed"
            row["extract_link_ok"] = False
            row["extract_link_error"] = "WebUI 重启导致提链任务中断，请重新提链"
            row["extract_link_completed_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(accounts)
        return recovered


def active_extract_cdk_ids() -> set[str]:
    """返回仍被排队/运行中 Masi Job 绑定的 CDK 内部 ID。"""
    with _LOCK:
        return {
            str(row.get("extract_link_cdk_id"))
            for row in _load_accounts()
            if row.get("extract_link_status") in {"queued", "running"}
            and str(row.get("extract_link_cdk_id") or "").strip()
        }


def _account_matches_query(row: dict, q: str | None) -> bool:
    q = str(q or "").strip().lower()
    if not q:
        return True
    try:
        return q in "\n".join(str(v) for v in row.values()).lower()
    except Exception:
        return False


def _parse_iso_dt(value: str | None, end_of_day: bool = False) -> datetime | None:
    """宽松解析 ISO 日期/时间字符串；支持 YYYY-MM-DD 或完整 ISO；解析失败返回 None。

    end_of_day=True 时，纯日期（YYYY-MM-DD）按当天 23:59:59.999999 解析，
    用于 date_to 过滤（保证包含截止当天）；完整时间串原样返回。
    """
    if not value:
        return None
    text = str(value).strip()
    try:
        if len(text) == 10 and text[4] == "-":
            if end_of_day:
                return datetime.fromisoformat(text + "T23:59:59.999999")
            return datetime.fromisoformat(text + "T00:00:00")
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _filtered_decorated_accounts(
    archived: str | bool | None = False,
    email_source_filter: str | None = None,
    plan_filter: str | None = None,
    q: str | None = None,
    codex_status_filter: str | None = None,
    codex_auth_status_filter: str | None = None,
    codex_operation_status_filter: str | None = None,
    live_check_status_filter: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    checkout_type_filter: str | None = None,
) -> list[dict]:
    rows = _load_accounts()
    if archived in (True, "1", "true", "yes", "only"):
        rows = [r for r in rows if bool(r.get("archived"))]
    elif archived in ("all", "include"):
        pass
    else:
        rows = [r for r in rows if not bool(r.get("archived"))]
    rows = [r for r in rows if _account_matches_email_source_filter(r, email_source_filter)]
    # Checkout 的 none 需要区分“原始字段缺失”和显式 unknown，必须在装饰器补默认值前筛选。
    rows = [r for r in rows if _account_matches_checkout_type_filter(r, checkout_type_filter)]
    decorated = [_decorate_account(r, include_checkout_session_id=False) for r in rows]
    decorated = [r for r in decorated if _account_matches_plan_filter(r, plan_filter)]
    codex_status_filter = str(codex_status_filter or "").strip().lower()
    if codex_status_filter:
        decorated = [r for r in decorated if _account_matches_codex_filter(r, codex_status_filter)]
    auth_filter = str(codex_auth_status_filter or "").strip().lower()
    if auth_filter:
        decorated = [r for r in decorated if r.get("codex_auth_status") == normalize_codex_auth_status(auth_filter)]
    operation_filter = str(codex_operation_status_filter or "").strip().lower()
    if operation_filter:
        decorated = [r for r in decorated if r.get("codex_operation_status") == normalize_codex_operation_status(operation_filter)]
    live_filter = str(live_check_status_filter or "").strip().lower()
    if live_filter:
        decorated = [r for r in decorated if r.get("live_check_status") == live_filter]
    decorated = [r for r in decorated if _account_matches_query(r, q)]
    # 按创建时间筛选（date_from/date_to 为 ISO 字符串或 YYYY-MM-DD）
    if date_from or date_to:
        d_from = _parse_iso_dt(date_from)
        d_to = _parse_iso_dt(date_to, end_of_day=True)
        if d_from or d_to:
            filtered = []
            for r in decorated:
                ct = _parse_iso_dt(str(r.get("created_at") or ""))
                if ct is None:
                    continue
                if d_from and ct < d_from:
                    continue
                if d_to and ct > d_to:
                    continue
                filtered.append(r)
            decorated = filtered
    return sorted(decorated, key=lambda x: int(x.get("id") or 0), reverse=True)


def list_account_plan_check_statuses(
    limit: int = 5000,
    offset: int = 0,
    archived: str | bool | None = False,
    email_source_filter: str | None = None,
    plan_filter: str | None = None,
    q: str | None = None,
    codex_status_filter: str | None = None,
    codex_auth_status_filter: str | None = None,
    codex_operation_status_filter: str | None = None,
    live_check_status_filter: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    checkout_type_filter: str | None = None,
) -> dict:
    """返回不含 Token/邮箱密码的套餐查询轻量状态快照。"""
    fields = (
        "id", "email", "archived",
        "email_source",
        "plan_type", "current_plan_type", "plus_trial_eligible", "trial_eligibility_known",
        "plan_check_status", "plan_check_ok", "plan_check_error", "plan_check_error_kind", "plan_check_http_status",
        "plan_check_trigger", "plan_check_queued_at", "plan_check_started_at",
        "plan_check_completed_at", "plan_check_updated_at", "plan_checked_at", "plan_last_success_at",
        "plan_check_network_route", "plan_check_proxy_used", "plan_check_proxy_fallback_reason",
        "checkout_check_status", "checkout_check_ok", "checkout_check_trigger",
        "checkout_check_queued_at", "checkout_check_started_at", "checkout_check_completed_at",
        "checkout_check_updated_at", "checkout_check_checked_at", "checkout_check_http_status",
        "checkout_check_error_code", "checkout_check_error_message", "checkout_check_error",
        "checkout_check_message", "checkout_check_attempt_count", "checkout_check_max_attempts",
        "checkout_check_request_timeout", "checkout_check_network_route", "checkout_check_proxy_mode",
        "checkout_check_proxy_used", "checkout_check_retryable", "checkout_check_content_type",
        "checkout_check_response_bytes", "checkout_check_retry_after_seconds",
        "checkout_session_type", "checkout_session_last_success_at",
        "live_check_device_id", "live_check_proxy_used", "live_check_fingerprint_text",
        "expires_at", "plan_expires_at", "plan_renews_at", "renews_at",
        "billing_period", "billing_currency", "discount_amount", "discount_type",
        "discount_expires_at", "discount_promo_campaign_id",
        "token_expired", "token_expires_at", "needs_live_check",
        "extract_link_status", "extract_link_ok", "extract_link_type",
        "extract_link_provider", "extract_link_update_mode", "extract_link_cdk_fingerprint",
        "extract_link_message", "extract_link_error",
        "extract_link_long_url", "extract_link_copy_paste",
        "extract_link_image_url_png", "extract_link_image_url_svg",
        "extract_link_expires_at",
        "codex_status", "codex_error",
        "codex_auth_status", "codex_operation_status", "codex_capabilities",
        "plan_category_code", "plan_query_status", "plan_capabilities",
        "extract_link_capabilities", "live_check_status", "live_check_ok", "live_check_error", "live_checked_at",
        "live_check_capabilities", "checkout_query_status", "checkout_capabilities",
    )
    with _LOCK:
        all_rows = _filtered_decorated_accounts(
            archived=archived,
            email_source_filter=email_source_filter,
            plan_filter=plan_filter,
            q=q,
            codex_status_filter=codex_status_filter,
            codex_auth_status_filter=codex_auth_status_filter,
            codex_operation_status_filter=codex_operation_status_filter,
            live_check_status_filter=live_check_status_filter,
            date_from=date_from,
            date_to=date_to,
            checkout_type_filter=checkout_type_filter,
        )
        total = len(all_rows)
        limit = max(1, int(limit))
        offset = max(0, int(offset or 0))
        rows = all_rows[offset: offset + limit]
        items = []
        for row in rows:
            item = {"id": row.get("id"), "email": row.get("email")}
            for key in fields:
                value = row.get(key)
                if key in ("id", "email"):
                    continue
                if value is not None and value != "":
                    item[key] = value
            plan = str(row.get("current_plan_type") or row.get("plan_type") or "").lower()
            if not any(x in plan for x in ("plus", "pro", "team", "go")):
                for expire_key in ("expires_at", "plan_expires_at", "plan_renews_at", "renews_at"):
                    item.pop(expire_key, None)
            item["has_access_token"] = bool(str(row.get("access_token") or "").strip())
            items.append(item)
        latest = max((str(row.get("updated_at") or "") for row in all_rows), default="")
        # updated_at 目前只有秒级精度；一次快速查询可能在同一秒内完成
        # queued -> running -> success/failed，导致 revision 不变，前端跳过合并状态，
        # 页面就会一直停在“查询中”。把轻量状态本身纳入签名，保证状态变化可被轮询发现。
        revision_payload = json.dumps(
            [
                {
                    "id": row.get("id"),
                    "email_source": row.get("email_source"),
                    "updated_at": row.get("updated_at"),
                    "plan_check_status": row.get("plan_check_status"),
                    "plan_check_ok": row.get("plan_check_ok"),
                    "plan_check_error": row.get("plan_check_error"),
                    "plan_check_error_kind": _plan_check_error_kind_for_row(row),
                    "plan_check_http_status": row.get("plan_check_http_status"),
                    "plan_check_updated_at": row.get("plan_check_updated_at"),
                    "checkout_check_status": row.get("checkout_check_status"),
                    "checkout_check_ok": row.get("checkout_check_ok"),
                    "checkout_check_error_code": row.get("checkout_check_error_code"),
                    "checkout_check_error_message": row.get("checkout_check_error_message"),
                    "checkout_check_updated_at": row.get("checkout_check_updated_at"),
                    "checkout_check_http_status": row.get("checkout_check_http_status"),
                    "checkout_check_attempt_count": row.get("checkout_check_attempt_count"),
                    "checkout_check_network_route": row.get("checkout_check_network_route"),
                    "checkout_check_proxy_used": row.get("checkout_check_proxy_used"),
                    "checkout_session_type": row.get("checkout_session_type"),
                    "checkout_session_last_success_at": row.get("checkout_session_last_success_at"),
                    "current_plan_type": row.get("current_plan_type"),
                    "plan_type": row.get("plan_type"),
                    "plus_trial_eligible": row.get("plus_trial_eligible"),
                    "trial_eligibility_known": row.get("trial_eligibility_known"),
                    "live_check_status": row.get("live_check_status"),
                    "live_check_ok": row.get("live_check_ok"),
                    "live_check_error": row.get("live_check_error"),
                    "live_checked_at": row.get("live_checked_at"),
                    "extract_link_status": row.get("extract_link_status"),
                    "codex_status": row.get("codex_status"),
                }
                for row in all_rows
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        revision_sig = hashlib.sha1(revision_payload.encode("utf-8")).hexdigest()[:12]
        return {"items": items, "total": total, "offset": offset, "limit": limit, "revision": f"{total}:{latest}:{revision_sig}"}


def list_accounts(
    limit: int = 500,
    offset: int = 0,
    archived: str | bool | None = False,
    email_source_filter: str | None = None,
    plan_filter: str | None = None,
    q: str | None = None,
    codex_status_filter: str | None = None,
    codex_auth_status_filter: str | None = None,
    codex_operation_status_filter: str | None = None,
    live_check_status_filter: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    checkout_type_filter: str | None = None,
) -> list[dict]:
    with _LOCK:
        rows = _filtered_decorated_accounts(
            archived=archived,
            email_source_filter=email_source_filter,
            plan_filter=plan_filter,
            q=q,
            codex_status_filter=codex_status_filter,
            codex_auth_status_filter=codex_auth_status_filter,
            codex_operation_status_filter=codex_operation_status_filter,
            live_check_status_filter=live_check_status_filter,
            date_from=date_from,
            date_to=date_to,
            checkout_type_filter=checkout_type_filter,
        )
        return rows[max(0, int(offset or 0)): max(0, int(offset or 0)) + max(1, int(limit))]


def list_accounts_page(
    limit: int = 50,
    offset: int = 0,
    archived: str | bool | None = False,
    email_source_filter: str | None = None,
    plan_filter: str | None = None,
    q: str | None = None,
    codex_status_filter: str | None = None,
    codex_auth_status_filter: str | None = None,
    codex_operation_status_filter: str | None = None,
    live_check_status_filter: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    checkout_type_filter: str | None = None,
) -> dict:
    with _LOCK:
        rows = _filtered_decorated_accounts(
            archived=archived,
            email_source_filter=email_source_filter,
            plan_filter=plan_filter,
            q=q,
            codex_status_filter=codex_status_filter,
            codex_auth_status_filter=codex_auth_status_filter,
            codex_operation_status_filter=codex_operation_status_filter,
            live_check_status_filter=live_check_status_filter,
            date_from=date_from,
            date_to=date_to,
            checkout_type_filter=checkout_type_filter,
        )
        total = len(rows)
        limit = max(1, int(limit))
        offset = max(0, int(offset or 0))
        items = rows[offset: offset + limit]
        latest = max((str(row.get("updated_at") or "") for row in rows), default="")
        return {"items": items, "total": total, "offset": offset, "limit": limit, "revision": f"{total}:{latest}"}


def get_account(acc_id: int, *, include_checkout_session_id: bool = True) -> dict | None:
    with _LOCK:
        row = next((r for r in _load_accounts() if int(r.get("id") or 0) == int(acc_id)), None)
        return _decorate_account(row, include_checkout_session_id=include_checkout_session_id) if row else None


def get_retry_account_snapshot() -> dict[str, dict[int | str, dict]]:
    """Load only the non-sensitive account fields needed by job retry decisions."""
    with _LOCK:
        by_id: dict[int, dict] = {}
        by_email: dict[str, dict] = {}
        for row in _load_accounts():
            account = {
                "id": row.get("id"),
                "email": row.get("email"),
                "codex_status": row.get("codex_status"),
                "codex_auth_status": row.get("codex_auth_status"),
                "live_check_status": row.get("live_check_status"),
            }
            try:
                account_id = int(row.get("id") or 0)
            except (TypeError, ValueError):
                account_id = 0
            if account_id:
                by_id[account_id] = account
            email = str(row.get("email") or "").strip().lower()
            if email:
                by_email[email] = account
        return {"by_id": by_id, "by_email": by_email}


def get_account_by_email(email: str) -> dict | None:
    with _LOCK:
        row = _find_by_email(_load_accounts(), email)
        return _decorate_account(row) if row else None


def update_account_note(acc_id: int, note: str) -> bool:
    """更新单个已注册账号备注。note 为空字符串时表示清空备注。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        now = _now()
        row["note"] = str(note or "")
        row["note_updated_at"] = now
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def update_account_liveness(acc_id: int, result: dict | None = None) -> bool:
    """写回账号查活结果；成功时同步刷新最新 access_token 和账号基础信息。"""
    result = result or {}
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False

        now = _now()
        ok = bool(result.get("ok"))
        status = str(result.get("status") or ("live" if ok else "failed"))
        if not str(row.get("codex_auth_status") or "").strip():
            row["codex_auth_status"] = normalize_codex_auth_status(row.get("codex_status"))
        row["live_check_status"] = status
        row["live_check_ok"] = ok
        row["live_checked_at"] = result.get("checked_at") or now
        row["live_check_error"] = None if ok else result.get("error")
        row["updated_at"] = now

        if ok:
            token = str(result.get("access_token") or "").strip()
            if token:
                row["access_token"] = token
            session = result.get("session") or {}
            user = session.get("user") or {}
            account = session.get("account") or {}
            if user.get("id"):
                row["user_id"] = user.get("id")
            if user.get("name") is not None:
                row["user_name"] = user.get("name")
            if account.get("planType"):
                row["plan_type"] = account.get("planType")
            if session.get("expires"):
                row["expires_at"] = session.get("expires")
            if result.get("device_id"):
                row["device_id"] = result.get("device_id")
            row["live_check_device_id"] = result.get("device_id") or row.get("live_check_device_id")
            row["live_check_proxy_used"] = result.get("proxy_used") or row.get("live_check_proxy_used")
            row["live_check_fingerprint_text"] = result.get("fingerprint_text") or row.get("live_check_fingerprint_text")
            if result.get("fingerprint"):
                row["live_check_fingerprint"] = result.get("fingerprint")
            row["live_check_error"] = None

        row["copy_line"] = _account_line(row)
        _save_accounts(rows)
        return True


def claim_account_live_check(acc_id: int, trigger: str = "manual") -> bool:
    """原子占用账号查活任务；已有 queued/running 时返回 False。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        if row.get("live_check_status") in {"queued", "running"}:
            try:
                stamp_key = "live_check_queued_at" if row.get("live_check_status") == "queued" else "live_check_started_at"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if row.get("live_check_status") == "queued" else _PLAN_CHECK_STALE_SECONDS
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (datetime.now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass
        now = _now()
        row["live_check_status"] = "queued"
        row["live_check_ok"] = False
        row["live_check_trigger"] = str(trigger or "manual")
        row["live_check_queued_at"] = now
        row["live_check_started_at"] = None
        row["live_checked_at"] = None
        row["live_check_error"] = None
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def recover_interrupted_live_checks() -> int:
    """服务启动时恢复上次进程中断的查活状态，避免 queued/running 卡死。"""
    with _LOCK:
        rows = _load_accounts()
        recovered = 0
        now = _now()
        for row in rows:
            if row.get("live_check_status") not in {"queued", "running"}:
                continue
            row["live_check_status"] = "failed"
            row["live_check_ok"] = False
            row["live_check_error"] = "WebUI 重启或任务异常中断，请重新查活"
            row["live_checked_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(rows)
        return recovered


def mark_account_live_check_running(acc_id: int) -> bool:
    """把账号查活任务标记为运行中。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("live_check_status") not in {"queued", "running"}:
            return False
        now = _now()
        row["live_check_status"] = "running"
        row["live_check_started_at"] = now
        row["live_check_error"] = None
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def update_accounts_note(account_ids: list[int] | None, note: str) -> tuple[list[dict], list[dict]]:
    """
    批量更新已注册账号备注。
    返回 (updated, skipped)，updated/skipped 元素含 id/email。
    """
    ids = {int(x) for x in (account_ids or []) if str(x).strip().lstrip("-").isdigit()}
    updated: list[dict] = []
    skipped: list[dict] = []
    with _LOCK:
        rows = _load_accounts()
        seen_ids: set[int] = set()
        now = _now()
        text = str(note or "")
        for row in rows:
            row_id = int(row.get("id") or 0)
            if row_id not in ids:
                continue
            row["note"] = text
            row["note_updated_at"] = now
            row["updated_at"] = now
            updated.append({"id": row_id, "email": row.get("email"), "note": text, "note_updated_at": now})
            seen_ids.add(row_id)
        for item in ids - seen_ids:
            skipped.append({"id": item, "reason": "账号不存在"})
        if updated:
            _save_accounts(rows)
    return updated, skipped


def archive_account(acc_id: int, archived: bool = True) -> bool:
    """归档/取消归档单个已注册账号。归档不会删除 token，只影响默认账号列表查询。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        now = _now()
        row["archived"] = bool(archived)
        row["archived_at"] = now if archived else None
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def archive_accounts(account_ids: list[int] | None, archived: bool = True) -> tuple[list[dict], list[dict]]:
    """批量归档/取消归档账号。返回 (updated, skipped)。"""
    ids = {int(x) for x in (account_ids or []) if str(x).strip().lstrip("-").isdigit()}
    updated: list[dict] = []
    skipped: list[dict] = []
    with _LOCK:
        rows = _load_accounts()
        seen_ids: set[int] = set()
        now = _now()
        for row in rows:
            row_id = int(row.get("id") or 0)
            if row_id not in ids:
                continue
            row["archived"] = bool(archived)
            row["archived_at"] = now if archived else None
            row["updated_at"] = now
            updated.append({"id": row_id, "email": row.get("email"), "archived": bool(archived), "archived_at": row.get("archived_at")})
            seen_ids.add(row_id)
        for item in ids - seen_ids:
            skipped.append({"id": item, "reason": "账号不存在"})
        if updated:
            _save_accounts(rows)
    return updated, skipped


def count_accounts() -> int:
    with _LOCK:
        return len(_load_accounts())


def delete_account(acc_id: int | None = None, email: str | None = None) -> bool:
    """删除一个已注册账号记录，并同步刷新 注册成功的邮箱.txt / token.txt / 静态查看页。"""
    with _LOCK:
        rows = _load_accounts()
        target_email = (email or "").lower()
        new_rows = []
        deleted = False
        for row in rows:
            match_id = acc_id is not None and int(row.get("id") or 0) == int(acc_id)
            match_email = bool(target_email) and (row.get("email") or "").lower() == target_email
            if match_id or match_email:
                deleted = True
                continue
            new_rows.append(row)
        if not deleted:
            return False
        _save_accounts(new_rows)
        return True


def delete_accounts(account_ids: list[int] | None = None, emails: list[str] | None = None) -> tuple[list[dict], list[dict]]:
    """
    批量删除已注册账号。
    返回 (deleted, skipped)，deleted 元素含 id/email。
    """
    ids = {int(x) for x in (account_ids or []) if str(x).strip().isdigit()}
    email_set = {(e or "").lower() for e in (emails or []) if e}
    deleted: list[dict] = []
    skipped: list[dict] = []
    with _LOCK:
        rows = _load_accounts()
        new_rows = []
        seen_ids: set[int] = set()
        seen_emails: set[str] = set()
        for row in rows:
            row_id = int(row.get("id") or 0)
            row_email = (row.get("email") or "").lower()
            if row_id in ids or row_email in email_set:
                deleted.append({"id": row_id, "email": row.get("email")})
                seen_ids.add(row_id)
                seen_emails.add(row_email)
                continue
            new_rows.append(row)
        for item in ids - seen_ids:
            skipped.append({"id": item, "reason": "账号不存在"})
        for item in email_set - seen_emails:
            skipped.append({"email": item, "reason": "账号不存在"})
        if deleted:
            _save_accounts(new_rows)
    return deleted, skipped


# ============================================================
# outlook_pool
# ============================================================

def import_outlook_accounts(records: list[dict], *, reactivate_existing: bool = False) -> tuple[int, int]:
    """
    批量导入 Outlook 账号。
    records 元素：{email, password, client_id, refresh_token}
    返回 (新增数, 跳过数)。
    """
    with _LOCK:
        rows = _load_outlook()
        inserted = skipped = 0
        for raw in records:
            email = (raw.get("email") or "").strip()
            if not email:
                skipped += 1
                continue
            existing = _find_by_email(rows, email)
            if existing:
                if not reactivate_existing:
                    skipped += 1
                    continue
                previous_status = canonical_status(existing.get("status"), missing="disabled", unknown="disabled")
                try:
                    restore_email_pool_entry(
                        email,
                        source="import",
                        reason="显式重新导入并恢复邮箱池条目",
                        expected_status=previous_status,
                    )
                except EmailPoolLifecycleError:
                    skipped += 1
                    continue
                existing["status"] = "available"
                existing["status_change_source"] = "import"
                existing["status_change_reason"] = "显式重新导入并恢复邮箱池条目"
                existing["manual_reactivated_from"] = previous_status
                existing["manual_reactivated_at"] = _now()
                existing["password"] = (raw.get("password") or "").strip()
                existing["client_id"] = (raw.get("client_id") or raw.get("clientId") or "").strip()
                existing["refresh_token"] = (raw.get("refresh_token") or raw.get("refreshToken") or "").strip()
                inserted += 1
                continue
            row = {
                "id": _next_id(rows),
                "email": email,
                "password": (raw.get("password") or "").strip(),
                "client_id": (raw.get("client_id") or raw.get("clientId") or "").strip(),
                "refresh_token": (raw.get("refresh_token") or raw.get("refreshToken") or "").strip(),
                "status": "available",
                "status_change_source": "import",
                "status_change_reason": "导入邮箱池素材",
                "used_at": None,
                "note": None,
                "imported_at": _now(),
            }
            row["copy_line"] = _outlook_line(row)
            rows.append(row)
            inserted += 1
        _save_outlook(rows)
        return inserted, skipped


def normalize_registered_icloud_import_record(raw: dict) -> tuple[dict | None, str | None]:
    """校验 iCloud 已注册账号导入行；仅解析 JWT，不验证远端有效性。"""
    from core.chatgpt_plan import normalize_token, token_claims

    email = str((raw or {}).get("email") or "").strip()
    access_token = normalize_token(str((raw or {}).get("access_token") or (raw or {}).get("token") or ""))
    if not email or "@" not in email:
        return None, "邮箱格式无效"
    if not access_token:
        return None, "缺少 AT"

    claim_email = str(token_claims(access_token).get("email") or "").strip()
    if claim_email and claim_email.casefold() != email.casefold():
        return None, "AT 邮箱与导入邮箱不一致"

    normalized = dict(raw)
    normalized["email"] = email
    normalized["access_token"] = access_token
    return normalized, None


def import_registered_email_accounts(records: list[dict], source: str | None) -> tuple[int, int]:
    """
    把邮箱素材直接导入为“已注册成功账号”，用于跳过注册、直接在账号页补跑 Codex 授权。

    source:
      - outlook: records 元素 {email,password,client_id,refresh_token[,access_token,totp_secret]}
      - generic_api: records 元素 {email,code_url[,access_token,totp_secret]}
      - icloud: records 元素 {email,access_token}

    返回 (新增账号数, 跳过数)。已存在账号会跳过；邮箱池中已存在的素材会复用并标记 used。
    """
    source = (source or "").strip().lower()
    if source not in ("outlook", "generic_api", "icloud"):
        raise ValueError("source 必须显式传入 outlook / generic_api / icloud")

    with _LOCK:
        accounts = _load_accounts()
        outlook_rows = _load_outlook()
        generic_rows = _load_generic_api_emails()
        icloud_rows = _load_icloud_emails()
        inserted = skipped = 0

        for raw in records:
            if source == "icloud":
                normalized, _reason = normalize_registered_icloud_import_record(raw)
                if normalized is None:
                    skipped += 1
                    continue
                raw = normalized
            email = (raw.get("email") or "").strip()
            if not email:
                skipped += 1
                continue
            if _find_by_email(accounts, email):
                skipped += 1
                continue

            now = _now()
            original_line = email
            pool_row = None
            pool_terminal = False

            if source == "generic_api":
                code_url = (raw.get("code_url") or raw.get("url") or "").strip()
                if not code_url:
                    skipped += 1
                    continue
                pool_row = _find_by_email(generic_rows, email)
                if pool_row is None:
                    pool_row = {
                        "id": _next_id(generic_rows),
                        "email": email,
                        "code_url": code_url,
                        "status": "used",
                        "used_at": now,
                        "note": "导入为已注册账号，用于 Codex 授权",
                        "imported_at": now,
                    }
                    generic_rows.append(pool_row)
                else:
                    pool_row["code_url"] = code_url or pool_row.get("code_url")
                pool_terminal = canonical_status(pool_row.get("status"), missing="disabled", unknown="disabled") in {"failed", "disabled"}
                if not pool_terminal:
                    _mark_pool_row_used(pool_row)
                    pool_row["used_at"] = pool_row.get("used_at") or now
                    pool_row["completed_at"] = pool_row.get("completed_at") or now
                    pool_row["note"] = pool_row.get("note") or "导入为已注册账号，用于 Codex 授权"
                pool_row["copy_line"] = _generic_api_email_line(pool_row)
                original_line = _generic_api_email_line(pool_row)
            elif source == "outlook":
                password = (raw.get("password") or "").strip()
                client_id = (raw.get("client_id") or raw.get("clientId") or "").strip()
                refresh_token = (raw.get("refresh_token") or raw.get("refreshToken") or "").strip()
                if not (password and client_id and refresh_token):
                    skipped += 1
                    continue
                pool_row = _find_by_email(outlook_rows, email)
                if pool_row is None:
                    pool_row = {
                        "id": _next_id(outlook_rows),
                        "email": email,
                        "password": password,
                        "client_id": client_id,
                        "refresh_token": refresh_token,
                        "status": "used",
                        "used_at": now,
                        "note": "导入为已注册账号，用于 Codex 授权",
                        "imported_at": now,
                    }
                    outlook_rows.append(pool_row)
                else:
                    pool_row["password"] = password or pool_row.get("password")
                    pool_row["client_id"] = client_id or pool_row.get("client_id")
                    pool_row["refresh_token"] = refresh_token or pool_row.get("refresh_token")
                pool_terminal = canonical_status(pool_row.get("status"), missing="disabled", unknown="disabled") in {"failed", "disabled"}
                if not pool_terminal:
                    _mark_pool_row_used(pool_row)
                    pool_row["used_at"] = pool_row.get("used_at") or now
                    pool_row["completed_at"] = pool_row.get("completed_at") or now
                    pool_row["note"] = pool_row.get("note") or "导入为已注册账号，用于 Codex 授权"
                pool_row["copy_line"] = _outlook_line(pool_row)
                original_line = _outlook_line(pool_row)
            else:
                pool_row = _find_by_email(icloud_rows, email)
                if pool_row is None:
                    pool_row = {
                        "id": _next_id(icloud_rows),
                        "email": email,
                        "status": "used",
                        "used_at": now,
                        "note": "导入为已注册账号，用于邮箱 OTP",
                        "imported_at": now,
                    }
                    icloud_rows.append(pool_row)
                pool_terminal = canonical_status(pool_row.get("status"), missing="disabled", unknown="disabled") in {"failed", "disabled"}
                if not pool_terminal:
                    _mark_pool_row_used(pool_row)
                    pool_row["used_at"] = pool_row.get("used_at") or now
                    pool_row["completed_at"] = pool_row.get("completed_at") or now
                    pool_row["note"] = pool_row.get("note") or "导入为已注册账号，用于邮箱 OTP"

            row_id = _next_id(accounts)
            access_token = (raw.get("access_token") or raw.get("token") or "").strip()
            totp_secret = (raw.get("totp_secret") or raw.get("totp") or "").strip() or None
            account = {
                "id": row_id,
                "email": email,
                "created_at": now,
                "access_token": access_token,
                "totp_secret": totp_secret,
                "user_id": raw.get("user_id"),
                "user_name": raw.get("user_name") if source == "icloud" else raw.get("user_name") or "Imported Account",
                "plan_type": raw.get("plan_type"),
                "expires_at": raw.get("expires_at"),
                "device_id": raw.get("device_id"),
                "proxy_used": raw.get("proxy_used"),
                "email_source": source,
                "extra_json": json.dumps({"imported_registered": True}, ensure_ascii=False),
                "codex_status": raw.get("codex_status") or "",
                "codex_error": raw.get("codex_error"),
                "updated_at": now,
                "original_email_line": original_line,
            }
            if source == "outlook":
                account["password"] = pool_row.get("password")
                account["client_id"] = pool_row.get("client_id")
                account["refresh_token"] = pool_row.get("refresh_token")
            account["copy_line"] = _account_line(account)
            accounts.append(account)

            if not pool_terminal:
                pool_row["registered_account_id"] = row_id
            pool_row["access_token"] = access_token
            if totp_secret:
                pool_row["totp_secret"] = totp_secret
            inserted += 1

        if source == "outlook":
            _save_accounts_with_pool(accounts, "outlook_emails", outlook_rows)
        elif source == "generic_api":
            _save_accounts_with_pool(accounts, "generic_api_emails", generic_rows)
        else:
            _save_accounts_with_pool(accounts, "icloud_emails", icloud_rows)
        return inserted, skipped


def claim_next_outlook(job_id: int | None = None) -> dict | None:
    """原子领取一个可用 Outlook 账号并标记为 registering。"""
    job_id = _current_registration_job_id() if job_id is None else int(job_id)
    with _LOCK:
        rows = sorted(_load_outlook(), key=lambda x: int(x.get("id") or 0))
        row = next((r for r in rows if r.get("status") == "available"), None)
        if row is None:
            return None
        if not _transition_pool_row(row, "registering", job_id=job_id):
            return None
        row["note"] = None
        _save_outlook(rows)
        return _decorate_outlook(row)


def release_outlook(email: str, status: str = "failed", note: str | None = None) -> bool:
    """更新 Outlook 状态；失败回收不得把已领取条目改回 available。"""
    status = validate_status(status)
    with _LOCK:
        rows = _load_outlook()
        row = _find_by_email(rows, email)
        if row is None:
            return False
        if not _transition_pool_row(row, status, note=note):
            raise ValueError(f"邮箱状态不可迁移: {row.get('status')} -> {status}")
        _save_outlook(rows)
        return True


def release_unconsumed_outlook(email: str, note: str | None = None) -> bool:
    """将未生成本地账号的已领取 Outlook 邮箱置为 failed，永久不复用。"""
    with _LOCK:
        if _find_by_email(_load_accounts(), email) is not None:
            return False
        rows = _load_outlook()
        row = _find_by_email(rows, email)
        if row is None or row.get("status") in {"failed", "disabled", "used"}:
            return False
        if not _transition_pool_row(row, "failed", note=note):
            return False
        _save_outlook(rows)
        return True


def delete_outlook(email: str, *, physical: bool = False, force: bool = False, reason: str | None = None) -> bool:
    """兼容旧调用；新删除 API 通过统一前置检查物理移除。"""
    if physical:
        delete_email_pool_entry(email, source="outlook", force=force, reason=reason)
        return True
    with _LOCK:
        rows = _load_outlook()
        row = _find_by_email(rows, email)
        if row is None:
            return False
        if not _transition_pool_row(row, "disabled", note="用户删除邮箱池条目"):
            return False
        row["deleted_at"] = _now()
        _save_outlook(rows)
        return True


def list_outlook_pool(status: str | None = None, limit: int = 500) -> list[dict]:
    with _LOCK:
        account_by_email = {
            (a.get("email") or "").lower(): a
            for a in _load_accounts()
        }
        rows = _load_outlook()
        if status:
            status = validate_status(status)
            rows = [r for r in rows if r.get("status") == status]
        rows = sorted(rows, key=lambda x: int(x.get("id") or 0), reverse=True)
        return [_decorate_outlook(r, account_by_email) for r in rows[:limit]]


def outlook_pool_summary() -> dict:
    with _LOCK:
        return status_counts(_load_outlook())


def get_outlook_by_email(email: str) -> dict | None:
    with _LOCK:
        row = _find_by_email(_load_outlook(), email)
        return _decorate_outlook(row) if row else None


# ============================================================
# generic_api email pool
# ============================================================

def import_generic_api_emails(records: list[dict], *, reactivate_existing: bool = False) -> tuple[int, int]:
    """
    批量导入通用 API 取码邮箱。
    records 元素：{email, code_url}
    返回 (新增数, 跳过数)。
    """
    with _LOCK:
        rows = _load_generic_api_emails()
        inserted = skipped = 0
        for raw in records:
            email = (raw.get("email") or "").strip()
            code_url = (raw.get("code_url") or raw.get("url") or "").strip()
            if not email or not code_url:
                skipped += 1
                continue
            existing = _find_by_email(rows, email)
            if existing:
                if not reactivate_existing:
                    skipped += 1
                    continue
                try:
                    restore_email_pool_entry(
                        email,
                        source="import",
                        reason="显式重新导入并恢复邮箱池条目",
                        expected_status=existing.get("status"),
                    )
                except EmailPoolLifecycleError:
                    skipped += 1
                    continue
                existing["status"] = "available"
                existing["status_change_source"] = "import"
                existing["status_change_reason"] = "显式重新导入并恢复邮箱池条目"
                existing["manual_reactivated_at"] = _now()
                existing["code_url"] = code_url
                inserted += 1
                continue
            row = {
                "id": _next_id(rows),
                "email": email,
                "code_url": code_url,
                "status": "available",
                "status_change_source": "import",
                "status_change_reason": "导入邮箱池素材",
                "used_at": None,
                "note": None,
                "imported_at": _now(),
            }
            row["copy_line"] = _generic_api_email_line(row)
            rows.append(row)
            inserted += 1
        _save_generic_api_emails(rows)
        return inserted, skipped


def claim_next_generic_api_email(job_id: int | None = None) -> dict | None:
    """原子领取一个可用通用 API 邮箱并标记为 registering。"""
    job_id = _current_registration_job_id() if job_id is None else int(job_id)
    with _LOCK:
        rows = sorted(_load_generic_api_emails(), key=lambda x: int(x.get("id") or 0))
        row = next((r for r in rows if r.get("status") == "available"), None)
        if row is None:
            return None
        if not _transition_pool_row(row, "registering", job_id=job_id):
            return None
        row["note"] = None
        _save_generic_api_emails(rows)
        return _decorate_generic_api_email(row)


def release_generic_api_email(email: str, status: str = "failed", note: str | None = None) -> bool:
    """更新通用 API 邮箱状态；失败条目不可回收到 available。"""
    status = validate_status(status)
    with _LOCK:
        rows = _load_generic_api_emails()
        row = _find_by_email(rows, email)
        if row is None:
            return False
        if not _transition_pool_row(row, status, note=note):
            raise ValueError(f"邮箱状态不可迁移: {row.get('status')} -> {status}")
        _save_generic_api_emails(rows)
        return True


def release_unconsumed_generic_api_email(email: str, note: str | None = None) -> bool:
    """将未生成本地账号的已领取通用 API 邮箱置为 failed。"""
    with _LOCK:
        if _find_by_email(_load_accounts(), email) is not None:
            return False
        rows = _load_generic_api_emails()
        row = _find_by_email(rows, email)
        if row is None or row.get("status") in {"failed", "disabled", "used"}:
            return False
        if not _transition_pool_row(row, "failed", note=note):
            return False
        _save_generic_api_emails(rows)
        return True


def delete_generic_api_email(email: str, *, physical: bool = False, force: bool = False, reason: str | None = None) -> bool:
    """兼容旧调用；新删除 API 通过统一前置检查物理移除。"""
    if physical:
        delete_email_pool_entry(email, source="generic_api", force=force, reason=reason)
        return True
    with _LOCK:
        rows = _load_generic_api_emails()
        row = _find_by_email(rows, email)
        if row is None:
            return False
        if not _transition_pool_row(row, "disabled", note="用户删除邮箱池条目"):
            return False
        row["deleted_at"] = _now()
        _save_generic_api_emails(rows)
        return True


def list_generic_api_email_pool(status: str | None = None, limit: int = 500) -> list[dict]:
    with _LOCK:
        account_by_email = {
            (a.get("email") or "").lower(): a
            for a in _load_accounts()
        }
        rows = _load_generic_api_emails()
        if status:
            status = validate_status(status)
            rows = [r for r in rows if r.get("status") == status]
        rows = sorted(rows, key=lambda x: int(x.get("id") or 0), reverse=True)
        return [_decorate_generic_api_email(r, account_by_email) for r in rows[:limit]]


def generic_api_email_pool_summary() -> dict:
    with _LOCK:
        return status_counts(_load_generic_api_emails())


def get_generic_api_email_by_email(email: str) -> dict | None:
    with _LOCK:
        row = _find_by_email(_load_generic_api_emails(), email)
        return _decorate_generic_api_email(row) if row else None


# ============================================================
# Codex 授权账号（来自 codex_accounts/codex-邮箱-plan.json）
# ============================================================

def _load_codex_export_state() -> dict:
    """读导出状态映射 {filename: {exported_at, exported_count}}。不存在返回 {}。"""
    data = _read_json(_CODEX_EXPORT_STATE, {})
    return data if isinstance(data, dict) else {}


def _save_codex_export_state(state: dict) -> None:
    _write_json(_CODEX_EXPORT_STATE, state)


def list_codex_accounts(archived: str | bool | None = "0", date_from: str | None = None, date_to: str | None = None) -> list[dict]:
    """
    扫 codex_accounts/ 目录，每个 codex-*.json 是一条 CPA 兼容凭证。
    返回带元信息的列表（含导出状态、文件大小、token 预览等）。
    archived: '0'=仅未归档（默认）/ 'only'=仅归档 / 'all'=全部；
    date_from/date_to 按文件修改时间（mtime）筛选（ISO 或 YYYY-MM-DD）。
    """
    with _LOCK:
        out = []
        if not _CODEX_DIR.exists():
            return out
        export_state = _load_codex_export_state()
        d_from = _parse_iso_dt(date_from)
        d_to = _parse_iso_dt(date_to, end_of_day=True)
        for path in sorted(_CODEX_DIR.glob("codex-*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                content = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            fname = path.name
            es = export_state.get(fname) or {}
            rec_archived = bool(es.get("archived"))
            if archived in (True, "1", "true", "yes", "only"):
                if not rec_archived:
                    continue
            elif archived in ("all", "include"):
                pass
            else:
                if rec_archived:
                    continue
            mtime_dt = datetime.fromtimestamp(path.stat().st_mtime)
            if d_from and mtime_dt < d_from:
                continue
            if d_to and mtime_dt > d_to:
                continue
            # 从文件名抽 email 和 plan：codex-{email}.json 或 codex-{email}-{plan}.json
            stem = path.stem  # codex-邮箱-plan
            without_prefix = stem[len("codex-"):] if stem.startswith("codex-") else stem
            # plan 可能为空。简单做法：直接读 JSON 里的 email（更准），文件名只做 fallback
            email = content.get("email") or ""
            if not email:
                # JSON 里 email 为空（旧 bug 产物），从文件名兜底
                # 文件名格式 codex-{email}-{plan}.json，email 里可能有 - 但是常见邮箱不会有
                # 简单做法：去掉末尾 -plan（如 -free / -plus / -team），剩下的当 email
                parts = without_prefix.rsplit("-", 1)
                if len(parts) == 2 and parts[1].lower() in ("free", "plus", "team", "pro", "enterprise"):
                    email = parts[0]
                else:
                    email = without_prefix
            # 推断 plan
            plan = ""
            if "-" in without_prefix:
                tail = without_prefix.rsplit("-", 1)[-1].lower()
                if tail in ("free", "plus", "team", "pro", "enterprise"):
                    plan = tail
            out.append({
                "filename": fname,
                "path": str(path),
                "email": email,
                "plan": plan,
                "account_id": content.get("account_id", ""),
                "type": content.get("type", "codex"),
                "last_refresh": content.get("last_refresh", ""),
                "expired": content.get("expired", ""),
                "access_token_preview": (content.get("access_token", "") or "")[:32],
                "size": path.stat().st_size,
                "mtime": mtime_dt.isoformat(timespec="seconds"),
                "exported_at": es.get("exported_at"),
                "exported_count": es.get("exported_count", 0),
                "archived": rec_archived,
                "archived_at": es.get("archived_at"),
            })
        return out


def archive_codex(filename: str, archived: bool = True) -> dict | None:
    """归档/取消归档一条 Codex 授权凭证（状态记录在导出状态文件）。不存在返回 None。"""
    with _LOCK:
        if not filename.startswith("codex-") or not filename.endswith(".json"):
            raise ValueError(f"非法文件名: {filename}")
        if "/" in filename or "\\" in filename or ".." in filename:
            raise ValueError(f"非法文件名: {filename}")
        path = _CODEX_DIR / filename
        if not path.exists() or not path.is_file():
            return None
        state = _load_codex_export_state()
        rec = state.get(filename) or {}
        rec["archived"] = bool(archived)
        rec["archived_at"] = _now() if archived else None
        state[filename] = rec
        _save_codex_export_state(state)
        return rec


def read_codex_credential(filename: str) -> tuple[str, str]:
    """
    读取一个 codex-*.json 文件原始内容。
    Returns: (content_string, filename)
    抛 ValueError：文件名不合法（防目录穿越）/ 不存在。
    """
    with _LOCK:
        # 防注入：只允许 codex-*.json 模式，不允许路径分隔符
        if not filename.startswith("codex-") or not filename.endswith(".json"):
            raise ValueError(f"非法文件名: {filename}")
        if "/" in filename or "\\" in filename or ".." in filename:
            raise ValueError(f"非法文件名: {filename}")
        path = _CODEX_DIR / filename
        if not path.exists() or not path.is_file():
            raise ValueError(f"文件不存在: {filename}")
        return path.read_text(encoding="utf-8"), filename


def mark_codex_exported(filename: str) -> dict:
    """
    标记某个 codex 凭证已导出（导出计数 +1，记录最近导出时间）。
    Returns: 该 filename 当前的导出状态记录。
    """
    with _LOCK:
        state = _load_codex_export_state()
        rec = state.get(filename) or {"exported_count": 0}
        rec["exported_count"] = int(rec.get("exported_count", 0)) + 1
        rec["exported_at"] = _now()
        state[filename] = rec
        _save_codex_export_state(state)
        return rec


def reset_codex_exported(filename: str) -> None:
    """清掉某个 codex 凭证的导出状态（用户想重置时用）。"""
    with _LOCK:
        state = _load_codex_export_state()
        if filename in state:
            del state[filename]
            _save_codex_export_state(state)


def delete_codex_credential(filename: str) -> bool:
    """删除一个本地 codex-*.json 凭证文件，并清理导出状态。"""
    with _LOCK:
        if not filename.startswith("codex-") or not filename.endswith(".json"):
            raise ValueError(f"非法文件名: {filename}")
        if "/" in filename or "\\" in filename or ".." in filename:
            raise ValueError(f"非法文件名: {filename}")
        path = _CODEX_DIR / filename
        if not path.exists() or not path.is_file():
            return False
        path.unlink()
        state = _load_codex_export_state()
        if filename in state:
            del state[filename]
            _save_codex_export_state(state)
        return True


def codex_accounts_summary() -> dict:
    """codex 账号汇总：总数 / 已导出 / 未导出。"""
    with _LOCK:
        rows = list_codex_accounts()
        total = len(rows)
        exported = sum(1 for r in rows if r.get("exported_count", 0) > 0)
        return {
            "total": total,
            "exported": exported,
            "pending": total - exported,
        }


# ============================================================
# registration_jobs
# ============================================================

def _new_job_row(
    rows: list[dict],
    *,
    email_source: str,
    job_type: str = "registration",
    parent_job_id: int | None = None,
    root_job_id: int | None = None,
    retry_attempt: int = 0,
    retry_action: str | None = None,
    email: str | None = None,
    account_id: int | None = None,
) -> dict:
    job_uuid = str(uuid.uuid4())
    log_file = str(_LOG_DIR / f"{job_uuid}.log")
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    return {
        "id": _next_id(rows),
        "job_uuid": job_uuid,
        "job_type": job_type,
        "parent_job_id": parent_job_id,
        "root_job_id": root_job_id,
        "retry_attempt": int(retry_attempt or 0),
        "retry_action": retry_action,
        "email_source": email_source,
        "email": email,
        "status": "pending",
        "error_message": None,
        "log_file": log_file,
        "started_at": None,
        "completed_at": None,
        "account_id": account_id,
        "network_traffic": None,
        "created_at": _now(),
    }


def create_job(
    email_source: str,
    *,
    job_type: str = "registration",
    email: str | None = None,
    account_id: int | None = None,
) -> dict:
    """创建一个首次执行的 pending 任务。"""
    with _LOCK:
        rows = _load_jobs()
        row = _new_job_row(
            rows,
            email_source=email_source,
            job_type=job_type,
            email=email,
            account_id=account_id,
        )
        rows.append(row)
        _save_jobs(rows)
        return dict(row)


def create_retry_job(
    source_job_id: int,
    *,
    job_type: str,
    email_source: str,
    email: str | None = None,
    account_id: int | None = None,
) -> tuple[dict, bool]:
    """原子创建重试子任务；同一任务链已有活跃任务时直接复用。"""
    with _LOCK:
        rows = _load_jobs()
        source = next((r for r in rows if int(r.get("id") or 0) == int(source_job_id)), None)
        if source is None:
            raise LookupError("任务不存在")
        if source.get("status") not in ("failed", "stopped", "cancelled"):
            raise ValueError(f"当前状态不支持重试：{source.get('status')}")

        root_id = int(source.get("root_job_id") or source.get("id"))
        active_states = {"pending", "running", "stopping"}
        active = next((
            r for r in rows
            if int(r.get("id") or 0) != int(source_job_id)
            and int(r.get("root_job_id") or 0) == root_id
            and r.get("status") in active_states
        ), None)
        if active is not None:
            if active.get("job_type", "registration") != job_type:
                raise ValueError(f"已有其他类型重试任务 #{active.get('id')} 在排队或运行中")
            return dict(active), False

        attempts = [
            int(r.get("retry_attempt") or 0)
            for r in rows
            if int(r.get("id") or 0) == root_id or int(r.get("root_job_id") or 0) == root_id
        ]
        row = _new_job_row(
            rows,
            email_source=email_source,
            job_type=job_type,
            parent_job_id=int(source_job_id),
            root_job_id=root_id,
            retry_attempt=(max(attempts) if attempts else 0) + 1,
            retry_action=("codex" if job_type == "codex_retry" else "registration"),
            email=email,
            account_id=account_id,
        )
        rows.append(row)
        _save_jobs(rows)
        return dict(row), True


def update_job(
    job_id: int,
    *,
    status: str | None = None,
    email: str | None = None,
    error: str | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
    account_id: int | None = None,
    network_traffic: dict | None = None,
) -> None:
    with _LOCK:
        rows = _load_jobs()
        row = next((r for r in rows if int(r.get("id") or 0) == int(job_id)), None)
        if row is None:
            return
        if status is not None:
            row["status"] = status
        if email is not None:
            row["email"] = email
        if error is not None:
            row["error_message"] = error
        if started_at is not None:
            row["started_at"] = started_at
        if completed_at is not None:
            row["completed_at"] = completed_at
        if account_id is not None:
            row["account_id"] = account_id
        if network_traffic is not None:
            row["network_traffic"] = dict(network_traffic)
        _save_jobs(rows)


def list_jobs(limit: int = 100) -> list[dict]:
    with _LOCK:
        rows = sorted(_load_jobs(), key=lambda x: int(x.get("id") or 0), reverse=True)
        return [dict(r) for r in rows[:limit]]


def get_job(job_id: int) -> dict | None:
    with _LOCK:
        row = next((r for r in _load_jobs() if int(r.get("id") or 0) == int(job_id)), None)
        return dict(row) if row else None


def get_successful_retry_for_job(job_id: int) -> dict | None:
    """返回同一任务链中已成功的其他重试任务，用于保留原任务历史状态并阻止重复重试。"""
    with _LOCK:
        rows = _load_jobs()
        source = next((r for r in rows if int(r.get("id") or 0) == int(job_id)), None)
        if source is None:
            return None
        root_id = int(source.get("root_job_id") or source.get("id") or 0)
        matches = [
            r for r in rows
            if int(r.get("id") or 0) != int(job_id)
            and int(r.get("root_job_id") or 0) == root_id
            and r.get("status") == "success"
        ]
        if not matches:
            return None
        return dict(max(matches, key=lambda r: int(r.get("id") or 0)))


def delete_job(job_id: int, *, delete_log: bool = True, allow_running: bool = False) -> bool:
    """
    删除一个注册任务记录；默认同时删除该任务日志文件。返回是否删除到记录。
    默认不删除 running 任务，避免后台线程仍在执行但前端记录消失。
    """
    with _LOCK:
        rows = _load_jobs()
        idx = next((i for i, r in enumerate(rows) if int(r.get("id") or 0) == int(job_id)), None)
        if idx is None:
            return False
        if not allow_running and rows[idx].get("status") in ("running", "stopping"):
            return False
        row = rows.pop(idx)
        _save_jobs(rows)

    if delete_log:
        log_file = row.get("log_file")
        if log_file:
            try:
                Path(log_file).unlink(missing_ok=True)
            except Exception:
                pass
    return True


# ============================================================
# 迁移与路径
# ============================================================

def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _migrate_legacy_sqlite() -> dict:
    summary = {"sqlite_accounts_imported": 0, "sqlite_outlook_imported": 0, "sqlite_outlook_skipped": 0}
    if not _LEGACY_SQLITE.exists():
        return summary
    try:
        conn = sqlite3.connect(str(_LEGACY_SQLITE))
        conn.row_factory = sqlite3.Row
        if _table_exists(conn, "outlook_pool"):
            records = []
            statuses = []
            for row in conn.execute("SELECT * FROM outlook_pool").fetchall():
                records.append({
                    "email": row["email"],
                    "password": row["password"],
                    "client_id": row["client_id"],
                    "refresh_token": row["refresh_token"],
                })
                statuses.append({
                    "email": row["email"],
                    "status": row["status"],
                    "note": row["note"],
                })
            ins, skip = import_outlook_accounts(records)
            for item in statuses:
                legacy_status = canonical_status(
                    item.get("status"),
                    missing="disabled",
                    unknown="disabled",
                )
                if legacy_status != "available":
                    try:
                        release_outlook(item["email"], status=legacy_status, note=item.get("note"))
                    except ValueError:
                        # 旧快照可能把已消费条目标为 available；安全地保留为
                        # failed，避免迁移阶段把它重新暴露给注册任务。
                        release_outlook(item["email"], status="failed", note=item.get("note"))
            summary["sqlite_outlook_imported"] += ins
            summary["sqlite_outlook_skipped"] += skip
        if _table_exists(conn, "registered_accounts"):
            for row in conn.execute("SELECT * FROM registered_accounts").fetchall():
                insert_account(
                    email=row["email"],
                    access_token=row["access_token"],
                    totp_secret=row["totp_secret"],
                    user_id=row["user_id"],
                    user_name=row["user_name"],
                    plan_type=row["plan_type"],
                    expires_at=row["expires_at"],
                    device_id=row["device_id"],
                    proxy_used=row["proxy_used"],
                    email_source=row["email_source"],
                    extra=json.loads(row["extra_json"]) if row["extra_json"] else None,
                )
                summary["sqlite_accounts_imported"] += 1
        conn.close()
    except Exception as exc:
        summary["sqlite_error"] = f"{type(exc).__name__}: {exc}"
    return summary


def migrate_legacy_files() -> dict:
    """
    把历史 SQLite、accounts/*.json、outlook_accounts.txt、outlook_accounts_used.json
    迁移到当前 JSON/TXT 文件存储。多次调用是幂等的。
    """
    summary = {
        "accounts_imported": 0,
        "outlook_imported": 0,
        "outlook_skipped": 0,
    }
    summary.update(_migrate_legacy_sqlite())

    accounts_dir = _PROJECT_ROOT / "accounts"
    if accounts_dir.exists():
        for jf in accounts_dir.glob("*.json"):
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
                if not data.get("email") or not data.get("access_token"):
                    continue
                extra = data.get("extra") or {}
                user = extra.get("user") or {}
                account = extra.get("account") or {}
                insert_account(
                    email=data["email"],
                    access_token=data["access_token"],
                    totp_secret=data.get("totp_secret"),
                    user_id=user.get("id"),
                    user_name=user.get("name"),
                    plan_type=account.get("planType"),
                    expires_at=extra.get("expires"),
                    device_id=extra.get("device_id"),
                    extra=extra,
                )
                summary["accounts_imported"] += 1
            except Exception:
                continue

    for txt in (_PROJECT_ROOT / "outlook_accounts.txt", _OUTLOOK_TXT):
        if txt.exists():
            records = []
            for line in txt.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("----")
                # 支持 4 段或 6 段格式
                if len(parts) == 4:
                    email, password, client_id, refresh_token = (p.strip() for p in parts)
                elif len(parts) == 6:
                    email, password, client_id, refresh_token, _, _ = (p.strip() for p in parts)
                else:
                    continue
                records.append({
                    "email": email,
                    "password": password,
                    "client_id": client_id,
                    "refresh_token": refresh_token,
                })
            ins, skip = import_outlook_accounts(records)
            summary["outlook_imported"] += ins
            summary["outlook_skipped"] += skip

    used = _PROJECT_ROOT / "outlook_accounts_used.json"
    if used.exists():
        try:
            emails = json.loads(used.read_text(encoding="utf-8"))
            for email in emails:
                release_outlook(email, status="used")
        except Exception:
            pass

    return summary


def db_path() -> Path:
    """兼容旧名称，返回当前文件存储目录。"""
    return _DATA_DIR


def storage_paths() -> dict:
    return {
        "runtime_db": str(_RUNTIME_DB),
        "outlook_json": str(_OUTLOOK_JSON),
        "outlook_txt": str(_OUTLOOK_TXT),
        "accounts_json": str(_ACCOUNTS_JSON),
        "accounts_txt": str(_ACCOUNTS_TXT),
        "tokens_txt": str(_TOKENS_TXT),
        "viewer_html": str(_VIEWER_HTML),
        "jobs_json": str(_JOBS_JSON),
        "email_pool_lifecycle_json": str(_EMAIL_POOL_LIFECYCLE_JSON),
        "logs_dir": str(_LOG_DIR),
    }


def export_runtime_snapshots() -> dict[str, str]:
    """从 SQLite 权威源原子重建兼容 JSON/TXT/HTML 快照。"""
    if not _sqlite_enabled():
        raise RuntimeError("SQLite 运行时存储尚未启用")
    with _LOCK:
        accounts = _load_accounts()
        jobs = _load_jobs()
        outlook = _load_outlook()
        generic = _load_generic_api_emails()
        icloud = _load_icloud_emails()
        domain = _load_domain_pool()
        lifecycle = _load_email_pool_lifecycle()
        _write_json(_ACCOUNTS_JSON, accounts)
        _write_json(_JOBS_JSON, jobs)
        _write_json(_OUTLOOK_JSON, outlook)
        _write_json(_GENERIC_API_EMAIL_JSON, generic)
        _write_json(_ICLOUD_EMAIL_JSON, icloud)
        _write_json(_DOMAIN_EMAIL_JSON, domain)
        _write_json(_EMAIL_POOL_LIFECYCLE_JSON, lifecycle)
        _sync_outlook_txt(outlook)
        _sync_generic_api_email_txt(generic)
        _sync_accounts_txt(accounts)
        _sync_tokens_txt(accounts)
        viewer = _render_static_viewer(outlook_rows=outlook, account_rows=accounts)
        return {"viewer_html": str(viewer), "runtime_db": str(_RUNTIME_DB)}


def create_runtime_backup(directory: str | Path | None = None, *, keep: int = 7) -> Path:
    """创建经完整性校验的 SQLite 备份并滚动保留。"""
    if not _sqlite_enabled():
        raise RuntimeError("SQLite 运行时存储尚未启用")
    backup_dir = Path(directory) if directory else (_PROJECT_ROOT / "runtime_backups")
    backup_dir.mkdir(parents=True, exist_ok=True)
    probe = backup_dir / ".write-probe"
    probe.write_text("ok", encoding="ascii")
    probe.unlink()
    destination = backup_dir / f"runtime-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    _SQLITE_STORE.backup(destination)
    backups = sorted(backup_dir.glob("runtime-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for expired in backups[max(1, int(keep)):]:
        expired.unlink()
    return destination


def refresh_static_viewer() -> Path:
    """手动刷新静态查看器，返回 HTML 路径。"""
    with _LOCK:
        outlook_rows = _load_outlook()
        account_rows = _load_accounts()
        _sync_outlook_txt(outlook_rows)
        _sync_accounts_txt(account_rows)
        _sync_tokens_txt(account_rows)
        return _render_static_viewer(outlook_rows=outlook_rows, account_rows=account_rows)


# ============================================================
# Domain email pool（Cloudflare 域名邮箱跟踪）
# ============================================================

_DOMAIN_EMAIL_JSON = _PROJECT_ROOT / "用于注册的域名邮箱.json"


def _load_domain_pool() -> list[dict]:
    if _sqlite_enabled():
        rows = _SQLITE_STORE.load("domain_emails")
        return _normalize_pool_rows(rows, missing_status="disabled")
    rows = _read_json(_DOMAIN_EMAIL_JSON, [])
    return _normalize_pool_rows(rows if isinstance(rows, list) else [], missing_status="disabled")


def _save_domain_pool(rows: list[dict]) -> None:
    if _sqlite_enabled():
        _SQLITE_STORE.replace_all("domain_emails", rows)
    else:
        _write_json(_DOMAIN_EMAIL_JSON, rows)


def _find_domain_email(rows: list[dict], email: str) -> dict | None:
    target = (email or "").lower()
    return next((r for r in rows if (r.get("email") or "").lower() == target), None)


def claim_next_domain_email(
    email: str,
    job_id: int | None = None,
    *,
    reactivate_existing: bool = False,
) -> dict | None:
    """记录并领取一个新的域名邮箱地址（标记为 registering）。"""
    job_id = _current_registration_job_id() if job_id is None else int(job_id)
    with _LOCK:
        rows = _load_domain_pool()
        if _find_domain_email(rows, email):
            row = _find_domain_email(rows, email)
            if row and row.get("status") == "available":
                if not _transition_pool_row(row, "registering", job_id=job_id):
                    raise ValueError("域名邮箱领取状态迁移失败")
                _save_domain_pool(rows)
            elif row is not None and reactivate_existing:
                previous_status = canonical_status(row.get("status"), missing="disabled", unknown="disabled")
                try:
                    restore_email_pool_entry(
                        email,
                        source="import",
                        reason="显式重新导入并恢复域名邮箱",
                        expected_status=previous_status,
                    )
                except EmailPoolLifecycleError:
                    return None
                row["status"] = "available"
                if not _transition_pool_row(row, "registering", job_id=job_id):
                    return None
                _save_domain_pool(rows)
            elif row is not None:
                return None
            return row
        row = {
            "id": _next_id(rows),
            "email": email,
            "status": "available",
            "used_at": None,
            "note": None,
            "created_at": _now(),
        }
        _normalize_pool_row(row)
        if not _transition_pool_row(row, "registering", job_id=job_id):
            raise RuntimeError("域名邮箱初始化领取失败")
        rows.append(row)
        _save_domain_pool(rows)
        return dict(row)


def release_domain_email(email: str, status: str = "failed", note: str | None = None) -> bool:
    """更新域名邮箱状态。"""
    status = validate_status(status)
    with _LOCK:
        rows = _load_domain_pool()
        row = _find_domain_email(rows, email)
        if row is None:
            return False
        if not _transition_pool_row(row, status, note=note):
            raise ValueError(f"邮箱状态不可迁移: {row.get('status')} -> {status}")
        _save_domain_pool(rows)
        return True


def release_unconsumed_domain_email(email: str, note: str | None = None) -> bool:
    """将未生成本地账号的域名邮箱置为 failed。"""
    with _LOCK:
        if _find_by_email(_load_accounts(), email) is not None:
            return False
        rows = _load_domain_pool()
        row = _find_domain_email(rows, email)
        if row is None or row.get("status") in {"failed", "disabled", "used"}:
            return False
        if not _transition_pool_row(row, "failed", note=note):
            return False
        _save_domain_pool(rows)
        return True


def get_domain_email_by_email(email: str) -> dict | None:
    with _LOCK:
        row = _find_domain_email(_load_domain_pool(), email)
        return dict(row) if row else None


def list_domain_email_pool(status: str | None = None, limit: int = 500) -> list[dict]:
    with _LOCK:
        rows = sorted(_load_domain_pool(), key=lambda x: int(x.get("id") or 0), reverse=True)
        if status:
            status = validate_status(status)
            rows = [r for r in rows if r.get("status") == status]
        return [dict(r) for r in rows[:limit]]


def domain_email_pool_summary() -> dict:
    with _LOCK:
        return status_counts(_load_domain_pool())


def delete_domain_email(email: str, *, physical: bool = False, force: bool = False, reason: str | None = None) -> bool:
    """兼容旧调用；新删除 API 通过统一前置检查物理移除。"""
    if physical:
        delete_email_pool_entry(email, source="cloudflare_domain", force=force, reason=reason)
        return True
    with _LOCK:
        rows = _load_domain_pool()
        row = _find_domain_email(rows, email)
        if row is None:
            return False
        if not _transition_pool_row(row, "disabled", note="用户删除邮箱池条目"):
            return False
        row["deleted_at"] = _now()
        _save_domain_pool(rows)
        return True


# ============================================================
# iCloud 隐私邮箱池（导入地址，QQ IMAP 收信）
# ============================================================

def _load_icloud_emails() -> list[dict]:
    if _sqlite_enabled():
        rows = _SQLITE_STORE.load("icloud_emails")
        return _normalize_pool_rows(rows, missing_status="disabled")
    rows = _read_json(_ICLOUD_EMAIL_JSON, [])
    return _normalize_pool_rows(rows if isinstance(rows, list) else [], missing_status="disabled")

def _save_icloud_emails(rows: list[dict]) -> None:
    if _sqlite_enabled():
        _SQLITE_STORE.replace_all("icloud_emails", rows)
    else:
        _write_json(_ICLOUD_EMAIL_JSON, rows)

def import_icloud_emails(records: list[dict], *, reactivate_existing: bool = False) -> tuple[int, int]:
    with _LOCK:
        rows = _load_icloud_emails()
        inserted = skipped = 0
        for raw in records:
            email = (str(raw.get("email") or "") if isinstance(raw, dict) else str(raw or "")).strip()
            if not email or "@" not in email:
                skipped += 1
                continue
            existing = _find_by_email(rows, email)
            if existing:
                if not reactivate_existing:
                    skipped += 1
                    continue
                try:
                    restore_email_pool_entry(
                        email,
                        source="import",
                        reason="显式重新导入并恢复邮箱池条目",
                        expected_status=existing.get("status"),
                    )
                except EmailPoolLifecycleError:
                    skipped += 1
                    continue
                existing["status"] = "available"
                existing["status_change_source"] = "import"
                existing["status_change_reason"] = "显式重新导入并恢复邮箱池条目"
                existing["manual_reactivated_at"] = _now()
                inserted += 1
                continue
            rows.append({
                "id": _next_id(rows), "email": email, "status": "available",
                "status_change_source": "import",
                "status_change_reason": "导入邮箱池素材",
                "used_at": None, "note": None, "imported_at": _now(),
            })
            inserted += 1
        _save_icloud_emails(rows)
        return inserted, skipped

def claim_next_icloud_email(job_id: int | None = None) -> dict | None:
    job_id = _current_registration_job_id() if job_id is None else int(job_id)
    with _LOCK:
        rows = sorted(_load_icloud_emails(), key=lambda x: int(x.get("id") or 0))
        row = next((r for r in rows if r.get("status") == "available"), None)
        if row is None:
            return None
        if not _transition_pool_row(row, "registering", job_id=job_id):
            return None
        row["note"] = None
        _save_icloud_emails(rows)
        return dict(row)

def release_icloud_email(email: str, status: str = "failed", note: str | None = None) -> bool:
    status = validate_status(status)
    with _LOCK:
        rows = _load_icloud_emails()
        row = _find_by_email(rows, email)
        if row is None:
            return False
        if not _transition_pool_row(row, status, note=note):
            raise ValueError(f"邮箱状态不可迁移: {row.get('status')} -> {status}")
        _save_icloud_emails(rows)
        return True

def release_unconsumed_icloud_email(email: str, note: str | None = None) -> bool:
    with _LOCK:
        if _find_by_email(_load_accounts(), email) is not None:
            return False
        rows = _load_icloud_emails()
        row = _find_by_email(rows, email)
        if row is None or row.get("status") in {"failed", "disabled", "used"}:
            return False
        if not _transition_pool_row(row, "failed", note=note):
            return False
        _save_icloud_emails(rows)
        return True

def delete_icloud_email(email: str, *, physical: bool = False, force: bool = False, reason: str | None = None) -> bool:
    """兼容旧调用；新删除 API 通过统一前置检查物理移除。"""
    if physical:
        delete_email_pool_entry(email, source="icloud", force=force, reason=reason)
        return True
    with _LOCK:
        rows = _load_icloud_emails()
        row = _find_by_email(rows, email)
        if row is None:
            return False
        if not _transition_pool_row(row, "disabled", note="用户删除邮箱池条目"):
            return False
        row["deleted_at"] = _now()
        _save_icloud_emails(rows)
        return True

def list_icloud_email_pool(status: str | None = None, limit: int = 500) -> list[dict]:
    with _LOCK:
        rows = _load_icloud_emails()
        if status:
            status = validate_status(status)
            rows = [r for r in rows if r.get("status") == status]
        rows = sorted(rows, key=lambda x: int(x.get("id") or 0), reverse=True)
        return [dict(r) for r in rows[:limit]]

def icloud_email_pool_summary() -> dict:
    with _LOCK:
        return status_counts(_load_icloud_emails())

def get_icloud_email_by_email(email: str) -> dict | None:
    with _LOCK:
        row = _find_by_email(_load_icloud_emails(), email)
        return dict(row) if row else None


