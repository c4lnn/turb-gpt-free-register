# -*- coding: utf-8 -*-
"""Masi CDK 双池持久化与进程内分配租约。"""
from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Iterable


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_POOL_PATH = _PROJECT_ROOT / "提链CDK池.json"
_LOCK = threading.RLock()
_LEASED_IDS: set[str] = set()

PROVIDER_MASI = "masi"
POOL_SELECTABLE = "selectable"
POOL_EXHAUSTED = "exhausted"
VALID_POOLS = {POOL_SELECTABLE, POOL_EXHAUSTED}
QUOTA_FIELDS = ("total_uses", "remaining_uses", "pending_uses", "available_uses")


class CdkPoolError(RuntimeError):
    pass


class CdkLeaseBusy(CdkPoolError):
    pass


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def fingerprint(cdk: str) -> str:
    return hashlib.sha256(str(cdk).strip().encode("utf-8")).hexdigest()


def mask_cdk(cdk: str) -> str:
    value = str(cdk or "").strip()
    if len(value) <= 8:
        return "*" * max(4, len(value))
    return f"{value[:5]}-****-{value[-4:]}"


def _is_enabled(row: dict) -> bool:
    value = row.get("enabled")
    return value if isinstance(value, bool) else True


def _load_rows() -> list[dict]:
    if not _POOL_PATH.exists():
        return []
    try:
        data = json.loads(_POOL_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    rows = data.get("items") if isinstance(data, dict) else data
    result: list[dict] = []
    for raw in rows or []:
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        row["enabled"] = _is_enabled(row)
        result.append(row)
    return result


def _save_rows(rows: list[dict]) -> None:
    _POOL_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "items": rows}
    tmp = _POOL_PATH.with_suffix(_POOL_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_POOL_PATH)


def _public_row(row: dict) -> dict:
    out = {
        "id": row.get("id"),
        "provider": row.get("provider") or PROVIDER_MASI,
        "fingerprint": str(row.get("fingerprint") or "")[:12],
        "masked_cdk": mask_cdk(row.get("cdk") or ""),
        "pool": row.get("pool") or POOL_SELECTABLE,
        "enabled": _is_enabled(row),
        "last_checked_at": row.get("last_checked_at"),
        "last_error": row.get("last_error"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "allocating": str(row.get("id") or "") in _LEASED_IDS,
    }
    for key in QUOTA_FIELDS:
        out[key] = row.get(key)
    return out


def list_cdks(*, provider: str = PROVIDER_MASI, pool: str | None = None) -> list[dict]:
    provider = str(provider or PROVIDER_MASI).strip().lower()
    if provider != PROVIDER_MASI:
        raise ValueError("当前仅支持 provider=masi 的 CDK 池")
    if pool is not None and pool not in VALID_POOLS:
        raise ValueError("pool 仅支持 selectable / exhausted")
    with _LOCK:
        rows = _load_rows()
        positions = {POOL_SELECTABLE: 0, POOL_EXHAUSTED: 0}
        result: list[dict] = []
        for row in rows:
            row_provider = row.get("provider") or PROVIDER_MASI
            row_pool = row.get("pool") or POOL_SELECTABLE
            if row_provider != provider:
                continue
            positions[row_pool] += 1
            if pool is not None and row_pool != pool:
                continue
            item = _public_row(row)
            item["position"] = positions[row_pool]
            result.append(item)
        return result


def pool_summary(*, provider: str = PROVIDER_MASI) -> dict:
    rows = list_cdks(provider=provider)
    selectable = [row for row in rows if row.get("pool") == POOL_SELECTABLE]
    exhausted = [row for row in rows if row.get("pool") == POOL_EXHAUSTED]
    enabled_selectable = [row for row in selectable if row.get("enabled")]
    enabled_exhausted = [row for row in exhausted if row.get("enabled")]
    return {
        "provider": provider,
        "selectable_count": len(selectable),
        "exhausted_count": len(exhausted),
        "allocating_count": sum(1 for row in selectable if row.get("allocating")),
        "total_available_uses": sum(int(row.get("available_uses") or 0) for row in selectable),
        "total_pending_uses": sum(int(row.get("pending_uses") or 0) for row in selectable),
        "enabled_selectable_count": len(enabled_selectable),
        "disabled_selectable_count": len(selectable) - len(enabled_selectable),
        "enabled_exhausted_count": len(enabled_exhausted),
        "disabled_exhausted_count": len(exhausted) - len(enabled_exhausted),
        "enabled_available_uses": sum(int(row.get("available_uses") or 0) for row in enabled_selectable),
    }


def import_cdks(text: str, *, provider: str = PROVIDER_MASI) -> dict:
    provider = str(provider or PROVIDER_MASI).strip().lower()
    if provider != PROVIDER_MASI:
        raise ValueError("当前仅支持 provider=masi 的 CDK 池")
    values: list[str] = []
    seen_input: set[str] = set()
    for raw in str(text or "").splitlines():
        value = raw.strip()
        if not value:
            continue
        fp = fingerprint(value)
        if fp in seen_input:
            continue
        seen_input.add(fp)
        values.append(value)
    now = _now()
    with _LOCK:
        rows = _load_rows()
        by_fp = {str(row.get("fingerprint") or ""): row for row in rows}
        added: list[dict] = []
        duplicates: list[dict] = []
        for value in values:
            fp = fingerprint(value)
            existing = by_fp.get(fp)
            if existing:
                duplicates.append(_public_row(existing))
                continue
            row = {
                "id": uuid.uuid4().hex,
                "provider": provider,
                "cdk": value,
                "fingerprint": fp,
                "pool": POOL_SELECTABLE,
                "enabled": True,
                "total_uses": None,
                "remaining_uses": None,
                "pending_uses": None,
                "available_uses": None,
                "last_checked_at": None,
                "last_error": None,
                "created_at": now,
                "updated_at": now,
            }
            rows.append(row)
            by_fp[fp] = row
            added.append(_public_row(row))
        if added:
            _save_rows(rows)
    return {
        "parsed_count": len(values),
        "added_count": len(added),
        "duplicate_count": len(duplicates),
        "added": added,
        "duplicates": duplicates,
        "refresh_ids": [row["id"] for row in added + duplicates],
    }


def set_enablement(
    *,
    enabled: bool,
    ids: Iterable[str] | None = None,
    pool: str | None = None,
    provider: str = PROVIDER_MASI,
) -> dict:
    if not isinstance(enabled, bool):
        raise ValueError("enabled 必须是布尔值")
    provider = str(provider or PROVIDER_MASI).strip().lower()
    if provider != PROVIDER_MASI:
        raise ValueError("当前仅支持 provider=masi 的 CDK 池")
    if (ids is None) == (pool is None):
        raise ValueError("ids 和 pool 必须且只能提供一个")
    if pool is not None and pool not in VALID_POOLS:
        raise ValueError("pool 仅支持 selectable / exhausted")

    requested_ids = list(dict.fromkeys(str(value).strip() for value in (ids or []) if str(value).strip()))
    requested_set = set(requested_ids)
    now = _now()
    with _LOCK:
        rows = _load_rows()
        matched_ids: list[str] = []
        changed_ids: list[str] = []
        for row in rows:
            if (row.get("provider") or PROVIDER_MASI) != provider:
                continue
            row_id = str(row.get("id") or "")
            row_pool = row.get("pool") or POOL_SELECTABLE
            if ids is not None and row_id not in requested_set:
                continue
            if pool is not None and row_pool != pool:
                continue
            matched_ids.append(row_id)
            if _is_enabled(row) == enabled and isinstance(row.get("enabled"), bool):
                continue
            row["enabled"] = enabled
            row["updated_at"] = now
            changed_ids.append(row_id)
        if changed_ids:
            _save_rows(rows)
        matched_set = set(matched_ids)
        not_found_ids = [value for value in requested_ids if value not in matched_set]
        return {
            "enabled": enabled,
            "matched_count": len(matched_ids),
            "changed_count": len(changed_ids),
            "unchanged_count": len(matched_ids) - len(changed_ids),
            "not_found_ids": not_found_ids,
        }


def get_secret(cdk_id: str) -> dict | None:
    with _LOCK:
        row = next((item for item in _load_rows() if str(item.get("id")) == str(cdk_id)), None)
        if not row:
            return None
        return {
            "id": row.get("id"),
            "provider": row.get("provider") or PROVIDER_MASI,
            "cdk": row.get("cdk") or "",
            "fingerprint": str(row.get("fingerprint") or "")[:12],
            "pool": row.get("pool") or POOL_SELECTABLE,
        }


def update_quota(cdk_id: str, quota: dict) -> dict:
    values: dict[str, int] = {}
    for key in QUOTA_FIELDS:
        value = quota.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"Masi CDK 额度字段 {key} 必须是整数")
        values[key] = value
    now = _now()
    with _LOCK:
        rows = _load_rows()
        row = next((item for item in rows if str(item.get("id")) == str(cdk_id)), None)
        if not row:
            raise KeyError("CDK 不存在")
        previous_pool = row.get("pool") or POOL_SELECTABLE
        target_pool = POOL_EXHAUSTED if values["remaining_uses"] == 0 else POOL_SELECTABLE
        row.update(values)
        row["pool"] = target_pool
        row["last_checked_at"] = now
        row["last_error"] = None
        row["updated_at"] = now
        requeued = previous_pool != target_pool or (
            target_pool == POOL_SELECTABLE
            and values["available_uses"] == 0
            and values["pending_uses"] > 0
        )
        if requeued:
            rows.remove(row)
            rows.append(row)
        _save_rows(rows)
        out = _public_row(row)
        out["previous_pool"] = previous_pool
        out["moved"] = previous_pool != row["pool"]
        out["requeued"] = requeued
        return out


def record_query_error(cdk_id: str, error: str) -> dict:
    with _LOCK:
        rows = _load_rows()
        row = next((item for item in rows if str(item.get("id")) == str(cdk_id)), None)
        if not row:
            raise KeyError("CDK 不存在")
        row["last_error"] = str(error or "额度查询失败")[:500]
        row["updated_at"] = _now()
        _save_rows(rows)
        return _public_row(row)


def delete_cdk(cdk_id: str, *, active_ids: Iterable[str] = ()) -> bool:
    active = {str(value) for value in active_ids}
    if str(cdk_id) in active or str(cdk_id) in _LEASED_IDS:
        raise CdkLeaseBusy("该 CDK 正被活动提链任务使用，不能删除")
    with _LOCK:
        rows = _load_rows()
        kept = [row for row in rows if str(row.get("id")) != str(cdk_id)]
        if len(kept) == len(rows):
            return False
        _save_rows(kept)
        return True


def lease_next(*, exclude_ids: Iterable[str] = ()) -> dict | None:
    excluded = {str(value) for value in exclude_ids}
    with _LOCK:
        rows = _load_rows()
        for row in rows:
            cdk_id = str(row.get("id") or "")
            if (
                (row.get("provider") or PROVIDER_MASI) == PROVIDER_MASI
                and (row.get("pool") or POOL_SELECTABLE) == POOL_SELECTABLE
                and _is_enabled(row)
                and cdk_id not in excluded
                and cdk_id not in _LEASED_IDS
            ):
                _LEASED_IDS.add(cdk_id)
                return {
                    "id": cdk_id,
                    "cdk": row.get("cdk") or "",
                    "fingerprint": str(row.get("fingerprint") or "")[:12],
                }
    return None


def lease_by_id(cdk_id: str) -> dict:
    with _LOCK:
        if str(cdk_id) in _LEASED_IDS:
            raise CdkLeaseBusy("该 CDK 正在分配或刷新")
        row = next((item for item in _load_rows() if str(item.get("id")) == str(cdk_id)), None)
        if not row:
            raise KeyError("CDK 不存在")
        _LEASED_IDS.add(str(cdk_id))
        return {"id": str(cdk_id), "cdk": row.get("cdk") or "", "fingerprint": str(row.get("fingerprint") or "")[:12]}


def release_lease(cdk_id: str, *, move_to_tail: bool = True) -> None:
    with _LOCK:
        _LEASED_IDS.discard(str(cdk_id))
        if not move_to_tail:
            return
        rows = _load_rows()
        index = next((idx for idx, row in enumerate(rows) if str(row.get("id")) == str(cdk_id)), None)
        if index is None:
            return
        row = rows.pop(index)
        rows.append(row)
        _save_rows(rows)


def leased_count() -> int:
    with _LOCK:
        return len(_LEASED_IDS)


def reset_runtime_leases() -> None:
    """仅供进程启动恢复和测试使用；租约不持久化。"""
    with _LOCK:
        _LEASED_IDS.clear()
