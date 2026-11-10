# -*- coding: utf-8 -*-
"""任务执行上下文：线程局部任务 ID、进度快照与停止信号。

注册、邮箱、短信、登录各链路只在"当前线程属于哪个任务、是否已被要求停止"
这一点上耦合，因此这层状态归 infra，不归任何业务域。

本模块除标准库外不依赖任何模块；数据库查询在函数内完成，避免把持久化层
的加载顺序带进导入期。
"""
from __future__ import annotations

import threading
import time


class StopRequested(RuntimeError):
    """用户手动停止任务。"""


_STOP_EVENTS: dict[int, threading.Event] = {}
_ACTIVE_JOBS: set[int] = set()
_STOP_LOCK = threading.Lock()
_CTX = threading.local()


def activate_job(job_id: int) -> None:
    """把当前线程绑定到任务，并登记停止位。"""
    _CTX.job_id = int(job_id)
    _CTX.progress = {}
    with _STOP_LOCK:
        _STOP_EVENTS.setdefault(int(job_id), threading.Event())
        _ACTIVE_JOBS.add(int(job_id))


def deactivate_job(job_id: int) -> None:
    """解除线程绑定并清理停止位。"""
    with _STOP_LOCK:
        _STOP_EVENTS.pop(int(job_id), None)
        _ACTIVE_JOBS.discard(int(job_id))
    for attr in ("job_id", "progress"):
        try:
            delattr(_CTX, attr)
        except AttributeError:
            pass


def current_job_id() -> int | None:
    """当前线程绑定的任务 ID；未绑定返回 None。"""
    value = getattr(_CTX, "job_id", None)
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def progress() -> dict:
    """当前任务的进度快照；未绑定时为空字典。"""
    return getattr(_CTX, "progress", {})


def record_progress(snapshot: dict) -> None:
    """记录当前任务进度，供异常与停止收口复用同一事实。"""
    if getattr(_CTX, "job_id", None) is not None:
        _CTX.progress = snapshot


def is_current_account_task() -> bool:
    """当前任务是否为"已有账号登录"而非新号注册。"""
    return getattr(_CTX, "progress", {}).get("account_kind") == "existing"


def is_stop_requested(job_id: int | None = None) -> bool:
    """停止位已置，或任务在库里已进入停止/取消状态。"""
    if job_id is None:
        job_id = current_job_id()
    if not job_id:
        return False
    with _STOP_LOCK:
        event = _STOP_EVENTS.get(int(job_id))
        if event is not None and event.is_set():
            return True
    from core.infra import db

    job = db.get_job(int(job_id))
    return bool(job and job.get("status") in ("stopping", "stopped", "cancelled"))


def check_stop_requested() -> None:
    """检查点：已请求停止则抛 StopRequested。"""
    job_id = current_job_id()
    if is_stop_requested(job_id):
        raise StopRequested(f"任务 #{job_id} 已被用户手动停止")


def request_stop(job_id: int) -> bool:
    """置停止位；返回当前是否存在可响应的活动实例。"""
    with _STOP_LOCK:
        active = int(job_id) in _ACTIVE_JOBS
        event = _STOP_EVENTS.get(int(job_id)) if active else None
        if event is not None:
            event.set()
        return bool(active and event is not None)


def discard_stop_state(job_id: int) -> None:
    """清理停止位；用于实例不存在的残留状态。"""
    with _STOP_LOCK:
        _STOP_EVENTS.pop(int(job_id), None)
        _ACTIVE_JOBS.discard(int(job_id))


def interruptible_sleep(seconds: float, interval: float = 0.25) -> None:
    """分段等待，使手动停止无需等完整退避结束。"""
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        check_stop_requested()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(max(0.01, interval), remaining))


__all__ = [
    "StopRequested", "activate_job", "check_stop_requested", "current_job_id",
    "deactivate_job", "discard_stop_state", "interruptible_sleep",
    "is_current_account_task", "is_stop_requested", "progress", "record_progress",
    "request_stop",
]
