# -*- coding: utf-8 -*-
"""按邮箱协调会消费 OTP 的后台操作。"""
from __future__ import annotations

import threading

_LOCK = threading.Lock()
_OWNERS: dict[str, str] = {}


def normalize_email(email: str) -> str:
    return str(email or "").strip().casefold()


def reserve(email: str, operation: str) -> bool:
    key = normalize_email(email)
    owner = str(operation or "").strip()
    if not key or not owner:
        return False
    with _LOCK:
        if key in _OWNERS:
            return False
        _OWNERS[key] = owner
        return True


def release(email: str, operation: str | None = None) -> None:
    key = normalize_email(email)
    with _LOCK:
        if operation is None or _OWNERS.get(key) == str(operation):
            _OWNERS.pop(key, None)


def owner(email: str) -> str | None:
    with _LOCK:
        return _OWNERS.get(normalize_email(email))


def clear() -> None:
    """仅供启动恢复和隔离测试清理进程内状态。"""
    with _LOCK:
        _OWNERS.clear()
