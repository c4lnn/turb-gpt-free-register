# -*- coding: utf-8 -*-
"""邮箱池统一状态契约。

状态值由持久化层、provider 和 WebUI 共同使用。这里不读写文件，避免把
状态规则复制到各个邮箱来源后再次发生漂移。
"""
from __future__ import annotations

from typing import Any


EMAIL_POOL_STATUSES = (
    "available",
    "registering",
    "used",
    "failed",
    "disabled",
)
EMAIL_POOL_STATUS_SET = frozenset(EMAIL_POOL_STATUSES)
CLAIMABLE_EMAIL_POOL_STATUS = "available"
TERMINAL_EMAIL_POOL_STATUSES = frozenset({"used", "failed", "disabled"})
MANUAL_RESTORABLE_EMAIL_POOL_STATUSES = frozenset({"disabled", "failed", "used"})

# mail.com 别名和旧接口曾经公开过这些值；读取时允许兼容，写入时只保存规范值。
LEGACY_EMAIL_POOL_STATUS_MAP = {
    "leased": "registering",
    "registered": "used",
    "registration_failed": "failed",
    "deleted": "disabled",
    "active": "available",
}


class EmailPoolStatusError(ValueError):
    """邮箱池状态值或迁移不符合统一契约。"""


def canonical_status(value: Any, *, missing: str = "disabled", unknown: str = "disabled") -> str:
    """返回规范状态；未知值使用安全的不可领取状态。"""
    raw = str(value or "").strip().casefold()
    if not raw:
        raw = str(missing or "").strip().casefold()
    mapped = LEGACY_EMAIL_POOL_STATUS_MAP.get(raw, raw)
    if mapped in EMAIL_POOL_STATUS_SET:
        return mapped
    fallback = str(unknown or "disabled").strip().casefold()
    if fallback not in EMAIL_POOL_STATUS_SET:
        fallback = "disabled"
    return fallback


def validate_status(value: Any) -> str:
    """校验 API/写入端传入的状态，只接受五种规范值。"""
    raw = str(value or "").strip().casefold()
    if raw not in EMAIL_POOL_STATUS_SET:
        raise EmailPoolStatusError(
            f"邮箱池 status 非法，仅支持: {', '.join(EMAIL_POOL_STATUSES)}"
        )
    return raw


def is_claimable(value: Any) -> bool:
    """只有规范的 available 状态允许进入领取队列。"""
    return canonical_status(value, missing="disabled", unknown="disabled") == CLAIMABLE_EMAIL_POOL_STATUS


def can_mark_used(value: Any) -> bool:
    """判断成功落库是否可以把条目标记为 ``used``。

    导入已注册账号时可能没有经历 ``registering``，因此这里允许
    ``available -> used`` 这一内部成功边界；失败和停用终态永远不允许被
    成功导入或旧线程复活。
    """
    return canonical_status(value, missing="disabled", unknown="disabled") in {
        "available", "registering", "used",
    }


def is_terminal(value: Any) -> bool:
    return canonical_status(value, missing="disabled", unknown="disabled") in TERMINAL_EMAIL_POOL_STATUSES


def is_manual_restorable(value: Any) -> bool:
    """判断条目是否允许通过明确的人工/导入恢复边界复活。"""
    return canonical_status(value, missing="disabled", unknown="disabled") in MANUAL_RESTORABLE_EMAIL_POOL_STATUSES


def can_transition(current: Any, target: Any) -> bool:
    """判断状态迁移是否允许，禁止失败条目被复活。"""
    source = canonical_status(current, missing="disabled", unknown="disabled")
    destination = validate_status(target)
    if source == destination:
        return True
    allowed = {
        "available": {"registering", "failed", "disabled"},
        "registering": {"used", "failed", "disabled"},
        "used": {"disabled"},
        "failed": {"disabled"},
        "disabled": set(),
    }
    return destination in allowed.get(source, set())


def require_transition(current: Any, target: Any) -> str:
    """校验并返回目标规范状态。"""
    destination = validate_status(target)
    if not can_transition(current, destination):
        source = canonical_status(current, missing="disabled", unknown="disabled")
        raise EmailPoolStatusError(f"邮箱池状态不可迁移: {source} -> {destination}")
    return destination


def status_counts(rows: list[dict] | None) -> dict[str, int]:
    """生成稳定的五态统计；未知/空状态按 disabled 计数。"""
    out = {status: 0 for status in EMAIL_POOL_STATUSES}
    for row in rows or []:
        out[canonical_status((row or {}).get("status"), missing="disabled", unknown="disabled")] += 1
    out["total"] = sum(out.values())
    return out


__all__ = [
    "CLAIMABLE_EMAIL_POOL_STATUS",
    "EMAIL_POOL_STATUSES",
    "EMAIL_POOL_STATUS_SET",
    "EmailPoolStatusError",
    "LEGACY_EMAIL_POOL_STATUS_MAP",
    "MANUAL_RESTORABLE_EMAIL_POOL_STATUSES",
    "TERMINAL_EMAIL_POOL_STATUSES",
    "can_transition",
    "can_mark_used",
    "canonical_status",
    "is_claimable",
    "is_manual_restorable",
    "is_terminal",
    "require_transition",
    "status_counts",
    "validate_status",
]
