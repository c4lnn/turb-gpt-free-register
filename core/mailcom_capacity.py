# -*- coding: utf-8 -*-
"""mail.com 别名容量的纯计算与协议聚合 helper。

本模块不执行网络请求，也不保存地址正文；调用方只应把聚合结果写入
``mailcom_emails`` 的容量字段。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import threading
from typing import Any, Mapping


MAX_ACTIVE_ALIASES = 9
MAX_LIFETIME_ALIASES = 99
DEFAULT_HISTORY_TTL_SECONDS = 12 * 60 * 60
DEFAULT_NEAR_LIMIT_REMAINING = 9

_HISTORY_LOCKS: dict[str, threading.Lock] = {}
_HISTORY_LOCKS_GUARD = threading.Lock()

CAPACITY_UNKNOWN = "unknown"
CAPACITY_NORMAL = "normal"
CAPACITY_NEAR_LIMIT = "near_limit"
CAPACITY_ACTIVE_FULL = "active_full"
CAPACITY_LIFETIME_FULL = "lifetime_full"
CAPACITY_QUERY_UNKNOWN = "capacity_unknown"


@dataclass(frozen=True)
class MailComCapacitySnapshot:
    """不含远端地址正文的历史容量聚合结果。"""

    lifetime_alias_count: int | None
    active_alias_count: int | None
    unknown_state_count: int = 0
    complete: bool = True
    lifetime_alias_limit: int = MAX_LIFETIME_ALIASES
    source: str = "remote_history"

    @property
    def lifetime_remaining(self) -> int | None:
        if self.lifetime_alias_count is None:
            return None
        return max(0, int(self.lifetime_alias_limit) - int(self.lifetime_alias_count))

    # 简短字段别名便于业务层和外部契约测试按“active/lifetime count”读取。
    @property
    def active_count(self) -> int | None:
        return self.active_alias_count

    @property
    def lifetime_count(self) -> int | None:
        return self.lifetime_alias_count

    @property
    def remaining(self) -> int | None:
        return self.lifetime_remaining

    @property
    def status(self) -> str:
        return capacity_status(
            self.active_alias_count,
            self.lifetime_alias_count,
            lifetime_limit=self.lifetime_alias_limit,
            complete=self.complete,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "remote_active_alias_count": self.active_alias_count,
            "remote_lifetime_alias_count": self.lifetime_alias_count,
            "remote_lifetime_alias_limit": self.lifetime_alias_limit,
            "remote_lifetime_remaining": self.lifetime_remaining,
            "remote_history_unknown_count": max(0, int(self.unknown_state_count or 0)),
            "remote_capacity_status": self.status,
        }


def capacity_status(
    active_count: int | None,
    lifetime_count: int | None,
    *,
    lifetime_limit: int = MAX_LIFETIME_ALIASES,
    active_limit: int = MAX_ACTIVE_ALIASES,
    near_limit_remaining: int = DEFAULT_NEAR_LIMIT_REMAINING,
    complete: bool = True,
) -> str:
    """按固定优先级计算容量状态。

    ``complete=False`` 必须先返回未知，避免不完整列表被当成有剩余容量。
    """
    if not complete or lifetime_count is None:
        return CAPACITY_QUERY_UNKNOWN if not complete else CAPACITY_UNKNOWN
    try:
        lifetime = max(0, int(lifetime_count))
        limit = max(1, int(lifetime_limit))
        active = None if active_count is None else max(0, int(active_count))
        near = max(0, int(near_limit_remaining))
    except (TypeError, ValueError):
        return CAPACITY_UNKNOWN
    if lifetime >= limit:
        return CAPACITY_LIFETIME_FULL
    if active is not None and active >= max(1, int(active_limit)):
        return CAPACITY_ACTIVE_FULL
    remaining = max(0, limit - lifetime)
    if 0 < remaining <= near:
        return CAPACITY_NEAR_LIMIT
    return CAPACITY_NORMAL


def lifetime_remaining(lifetime_count: int | None, lifetime_limit: int = MAX_LIFETIME_ALIASES) -> int | None:
    if lifetime_count is None:
        return None
    try:
        return max(0, int(lifetime_limit) - int(lifetime_count))
    except (TypeError, ValueError):
        return None


def _has_next_page(payload: Mapping[str, Any]) -> bool:
    """识别显式分页信号；没有信号的当前 mail.com 响应视为完整快照。"""
    def present(value: Any) -> bool:
        if value in (None, "", False, 0):
            return False
        if isinstance(value, (list, tuple, dict, set)) and not value:
            return False
        return True

    for key in ("hasMore", "has_more", "more", "isTruncated", "truncated"):
        if payload.get(key) is True:
            return True
    for key in ("next", "nextPage", "next_page", "nextURI", "nextUri"):
        value = payload.get(key)
        if present(value):
            return True
    links = payload.get("_links")
    if isinstance(links, Mapping):
        for key in ("next", "nextPage", "next_page"):
            value = links.get(key)
            if present(value):
                return True
    # 只有服务端明确报告总量且本页不足时才判定不完整，避免把普通链接元数据误判。
    rows = payload.get("mailaddresslist")
    if isinstance(rows, list):
        for key in ("totalCount", "total", "total_count", "count"):
            value = payload.get(key)
            try:
                if value is not None and int(value) > len(rows):
                    return True
            except (TypeError, ValueError):
                continue
    return False


def aggregate_history_payload(
    payload: Any,
    *,
    lifetime_limit: int = MAX_LIFETIME_ALIASES,
) -> MailComCapacitySnapshot:
    """校验并聚合全量 settings 响应。

    缺少必要字段、行不是对象或存在无法确认的分页时抛出 ``ValueError``；
    未知的地址状态本身不丢弃记录，而是计入生命周期并增加 unknown 计数。
    """
    if not isinstance(payload, Mapping):
        raise ValueError("mail.com settings 历史响应不是对象")
    rows = payload.get("mailaddresslist")
    if not isinstance(rows, list) or any(not isinstance(item, Mapping) for item in rows):
        raise ValueError("mail.com settings 历史响应缺少 mailaddresslist")
    if _has_next_page(payload):
        return MailComCapacitySnapshot(
            lifetime_alias_count=None,
            active_alias_count=None,
            unknown_state_count=0,
            complete=False,
            lifetime_alias_limit=lifetime_limit,
        )
    lifetime = 0
    active = 0
    unknown = 0
    for item in rows:
        address = str(item.get("address") or "").strip()
        state = item.get("state")
        deletable = item.get("deletable")
        if "@" not in address or not isinstance(state, str) or not state.strip() or not isinstance(deletable, bool):
            raise ValueError("mail.com settings 历史地址缺少 address/state/deletable")
        if not deletable:
            continue
        lifetime += 1
        normalized_state = state.strip().casefold()
        if normalized_state == "active":
            active += 1
        elif normalized_state not in {"inactive", "deleted", "pending", "removing"}:
            unknown += 1
    return MailComCapacitySnapshot(
        lifetime_alias_count=min(max(0, lifetime), max(1, int(lifetime_limit))),
        active_alias_count=active,
        unknown_state_count=unknown,
        complete=True,
        lifetime_alias_limit=max(1, int(lifetime_limit)),
    )


def aggregate_history_rows(
    rows: Any,
    *,
    lifetime_limit: int = MAX_LIFETIME_ALIASES,
) -> MailComCapacitySnapshot:
    """兼容注入式 client 的已解析行列表。"""
    return aggregate_history_payload({"mailaddresslist": rows}, lifetime_limit=lifetime_limit)


# 兼容更直观的调用名；返回值仍是同一个聚合 dataclass。
MailComHistorySnapshot = MailComCapacitySnapshot
aggregate_mailcom_history = aggregate_history_payload
aggregate_mailcom_address_history = aggregate_history_payload


def parse_snapshot_time(value: Any) -> float | None:
    """解析本地 ISO 时间，用于 TTL 判断；无法解析时按未知处理。"""
    if not value:
        return None
    try:
        text = str(value).strip()
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            # db._now() 使用本地无时区 ISO；按当前进程本地时区解释，避免东八区
            # 快照刚写入就被误判为已过期。
            parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo or timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def history_refresh_lock(parent_email: str) -> threading.Lock:
    """返回母号级历史 GET 去重锁，与创建锁保持独立。"""
    key = str(parent_email or "").strip().casefold()
    with _HISTORY_LOCKS_GUARD:
        return _HISTORY_LOCKS.setdefault(key, threading.Lock())


__all__ = [
    "CAPACITY_ACTIVE_FULL",
    "CAPACITY_LIFETIME_FULL",
    "CAPACITY_NEAR_LIMIT",
    "CAPACITY_NORMAL",
    "CAPACITY_QUERY_UNKNOWN",
    "CAPACITY_UNKNOWN",
    "DEFAULT_HISTORY_TTL_SECONDS",
    "DEFAULT_NEAR_LIMIT_REMAINING",
    "MAX_ACTIVE_ALIASES",
    "MAX_LIFETIME_ALIASES",
    "MailComCapacitySnapshot",
    "MailComHistorySnapshot",
    "aggregate_mailcom_address_history",
    "aggregate_mailcom_history",
    "aggregate_history_payload",
    "aggregate_history_rows",
    "capacity_status",
    "lifetime_remaining",
    "parse_snapshot_time",
    "history_refresh_lock",
]
