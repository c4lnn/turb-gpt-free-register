# -*- coding: utf-8 -*-
"""Checkout Session 类型检测的独立后台队列。"""
from __future__ import annotations

import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

from core import db
from core.chatgpt_checkout import (
    CheckoutSettings,
    check_checkout_session,
    checkout_settings_from_config,
    normalize_token,
    public_checkout_result,
    validate_checkout_settings,
)

logger = logging.getLogger(__name__)


def _initial_settings() -> CheckoutSettings:
    try:
        return checkout_settings_from_config()
    except Exception:
        return CheckoutSettings()


_STARTUP_SETTINGS = _initial_settings()
_WORKERS = max(1, min(16, int(_STARTUP_SETTINGS.workers)))
_QUEUE_LIMIT = max(_WORKERS, min(5000, int(_STARTUP_SETTINGS.queue_limit)))
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="checkout-session")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_RATE_LOCK = threading.Lock()
_NEXT_REQUEST_AT = 0.0


def _release_slot_if_cancelled(future) -> None:
    """补偿 shutdown(cancel_futures=True) 取消的、尚未进入 worker 的任务。"""
    if not future.cancelled():
        return
    try:
        _QUEUE_SLOTS.release()
    except ValueError:
        # 防御异常的重复回调，不让解释器退出路径再抛出错误。
        pass


def _wait_for_rate_slot(settings: CheckoutSettings) -> None:
    """按入队时固化的限速配置错开初始请求。"""
    global _NEXT_REQUEST_AT
    with _RATE_LOCK:
        now = time.monotonic()
        scheduled = max(now, _NEXT_REQUEST_AT)
        if settings.jitter:
            scheduled += random.uniform(0.0, settings.jitter)
        _NEXT_REQUEST_AT = scheduled + settings.min_interval
    wait_seconds = scheduled - now
    if wait_seconds > 0:
        time.sleep(wait_seconds)


def _error_result(message: str, *, code: str = "queue_error") -> dict[str, Any]:
    safe = " ".join(str(message or "").replace("\x00", " ").split())[:240]
    return {
        "ok": False,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "error_code": code,
        "error_message": safe,
        "error": safe,
        "retryable": False,
    }


def _run_checkout_session_check(
    *,
    account_id: int,
    email: str,
    access_token: str,
    trigger: str,
    settings: CheckoutSettings,
) -> dict[str, Any]:
    try:
        if not db.mark_account_checkout_session_running(account_id):
            result = _error_result("账号已删除或 Checkout 检测状态已被重置", code="state_error")
            db.update_account_checkout_session(account_id, result)
            return result

        _wait_for_rate_slot(settings)
        result = check_checkout_session(access_token, settings=settings)
        db.update_account_checkout_session(account_id, result)
        safe = public_checkout_result(result)
        if result.get("ok"):
            logger.info(
                "[Checkout] 检测成功: id=%s type=%s status=%s attempts=%s trigger=%s",
                account_id,
                result.get("checkout_session_type") or "unknown",
                result.get("http_status"),
                result.get("attempt_count"),
                trigger,
            )
        else:
            logger.warning(
                "[Checkout] 检测失败: id=%s status=%s code=%s attempts=%s trigger=%s message=%s",
                account_id,
                safe.get("http_status"),
                safe.get("error_code") or "-",
                safe.get("attempt_count"),
                trigger,
                safe.get("error_message") or "未知错误",
            )
        return result
    except Exception as exc:
        # 未知异常文本可能包含请求库拼出的 URL 或认证信息，只记录异常类型。
        result = _error_result(f"{type(exc).__name__}: 后台任务异常", code="worker_error")
        try:
            db.update_account_checkout_session(account_id, result)
        except Exception:
            logger.error("[Checkout] 写入异常状态失败: id=%s error_type=%s", account_id, type(exc).__name__)
        # 不记录异常 traceback：底层异常文本可能携带 URL、代理或认证片段。
        logger.error(
            "[Checkout] 后台任务异常: id=%s email=%s error_type=%s",
            account_id,
            email,
            type(exc).__name__,
        )
        return result
    finally:
        _QUEUE_SLOTS.release()


def enqueue_checkout_session_check(
    *,
    account_id: int,
    email: str,
    access_token: str,
    trigger: str = "manual",
) -> dict[str, Any]:
    """把单账号 Checkout 检测加入独立队列。

    代理、国家、货币和重试参数在这里读取并固化；调用方不得从请求体覆盖代理。
    """
    account_id = int(account_id)
    email = str(email or "").strip()
    token = normalize_token(access_token)
    if not token:
        return {"accepted": False, "busy": False, "config_error": False, "error": "账号缺少 access_token"}

    try:
        settings = checkout_settings_from_config()
        validate_checkout_settings(settings, require_request_values=True)
    except Exception as exc:
        return {
            "accepted": False,
            "busy": False,
            "config_error": True,
            "error": "Checkout 配置错误: " + " ".join(str(exc).split())[:240],
        }

    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {
            "accepted": False,
            "busy": False,
            "queue_full": True,
            "error": "Checkout 检测队列已满，请稍后重试",
        }

    trigger = str(trigger or "manual")
    if not db.claim_account_checkout_session(acc_id=account_id, trigger=trigger):
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": True, "error": "该账号正在进行 Checkout 检测"}

    try:
        future = _EXECUTOR.submit(
            _run_checkout_session_check,
            account_id=account_id,
            email=email,
            access_token=token,
            trigger=trigger,
            settings=settings,
        )
        future.add_done_callback(_release_slot_if_cancelled)
    except Exception as exc:
        _QUEUE_SLOTS.release()
        result = _error_result(f"Checkout 检测入队失败: {type(exc).__name__}")
        try:
            db.update_account_checkout_session(account_id, result)
        except Exception:
            logger.error("[Checkout] 写入入队失败状态失败: id=%s error_type=%s", account_id, type(exc).__name__)
        return {"accepted": False, "busy": False, "queue_error": True, "error": result["error"]}

    return {
        "accepted": True,
        "busy": False,
        "account_id": account_id,
        "email": email,
        "status": "queued",
        "trigger": trigger,
    }


def queue_settings() -> dict[str, Any]:
    """返回不含代理凭据的独立队列配置。"""
    current = _initial_settings()
    return {
        "workers": _WORKERS,
        "queue_limit": _QUEUE_LIMIT,
        "min_interval": current.min_interval,
        "jitter": current.jitter,
    }


def shutdown(wait: bool = False) -> None:
    """测试/进程退出时关闭独立线程池。"""
    _EXECUTOR.shutdown(wait=wait, cancel_futures=True)


# 兼容更直观的调用命名。
enqueue_account_checkout_session = enqueue_checkout_session_check
enqueue_account_checkout_check = enqueue_checkout_session_check
