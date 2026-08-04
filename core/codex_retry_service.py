# -*- coding: utf-8 -*-
"""Codex 授权补跑服务，供账号页和注册任务队列共同使用。"""
import ctypes
import logging
import threading
import time
from pathlib import Path

from core import db, otp_operation_lock

logger = logging.getLogger(__name__)

_LOG_DIR = Path(__file__).resolve().parent.parent / "注册日志"
_RETRYING: set[str] = set()
_RETRYING_LOCK = threading.Lock()
_STOP_REQUESTED: set[str] = set()
_RUNNING_THREADS: dict[str, int] = {}
_RESERVED_AT: dict[str, float] = {}


class CodexRetryStopped(Exception):
    """用户手动停止 Codex 补跑。"""


class _ThreadIdFilter(logging.Filter):
    """仅接收指定 Python 线程 ID 的日志，避免同名补跑线程互相匹配。"""

    def __init__(self, thread_id: int):
        super().__init__()
        self.thread_id = int(thread_id)

    def filter(self, record: logging.LogRecord) -> bool:
        return int(record.thread) == self.thread_id


def _thread_alive(thread_id: int | None) -> bool:
    if not thread_id:
        return False
    try:
        tid = int(thread_id)
    except Exception:
        return False
    return any(getattr(t, "ident", None) == tid and t.is_alive() for t in threading.enumerate())


def _clear_state_locked(key: str, *, release_otp: bool = True) -> None:
    _RETRYING.discard(key)
    _RUNNING_THREADS.pop(key, None)
    _RESERVED_AT.pop(key, None)
    if release_otp:
        otp_operation_lock.release(key, "codex_retry")


def log_path(email: str) -> Path:
    safe = email.replace("/", "_").replace("\\", "_").replace(":", "_")
    return _LOG_DIR / f"codex-retry-{safe}.log"


def _remove_retry_log_handlers(root_logger: logging.Logger, path: Path) -> int:
    """清理同一补跑日志路径的残留 handler；同邮箱同一时刻只允许一个 worker。"""
    normalized = str(path.resolve())
    removed = 0
    for handler in list(root_logger.handlers):
        if getattr(handler, "_codex_retry_log_path", None) != normalized:
            continue
        root_logger.removeHandler(handler)
        handler.close()
        removed += 1
    return removed


def reserve(email: str) -> bool:
    """进程内防止同一账号被重复补跑。"""
    key = (email or "").strip().lower()
    if not key:
        return False
    if not otp_operation_lock.reserve(email, "codex_retry"):
        return False
    with _RETRYING_LOCK:
        if key in _RETRYING:
            thread_id = _RUNNING_THREADS.get(key)
            alive = _thread_alive(thread_id)
            age = time.time() - float(_RESERVED_AT.get(key) or 0)
            stop_req = key in _STOP_REQUESTED
            try:
                acc = db.get_account_by_email(email)
                status = str((acc or {}).get("codex_status") or "").lower()
            except Exception:
                status = ""
            # 只有旧线程已经退出才允许清理占位。状态虽已 stopped，但线程仍在阻塞时
            # 启动同邮箱新补跑会造成真实的重复授权和重复日志。
            if (not alive) and (status != "retrying" or age > 15 * 60):
                logger.warning(
                    "[Codex 补跑] 清理脏占位：email=%s status=%s thread_id=%s alive=%s stop_requested=%s age=%.1fs",
                    email, status or "-", thread_id or "-", alive, stop_req, age,
                )
                # reserve() 本轮刚取得共享邮箱锁；这里只清旧 Codex 状态，不能释放新锁。
                _clear_state_locked(key, release_otp=False)
            else:
                otp_operation_lock.release(email, "codex_retry")
                return False
        _STOP_REQUESTED.discard(key)
        _RUNNING_THREADS.pop(key, None)
        _RETRYING.add(key)
        _RESERVED_AT[key] = time.time()
        return True


def release(email: str) -> None:
    key = (email or "").strip().lower()
    with _RETRYING_LOCK:
        _clear_state_locked(key)
    otp_operation_lock.release(email, "codex_retry")


def is_retrying(email: str) -> bool:
    with _RETRYING_LOCK:
        return (email or "").strip().lower() in _RETRYING


def is_stop_requested(email: str) -> bool:
    with _RETRYING_LOCK:
        return (email or "").strip().lower() in _STOP_REQUESTED


def check_stop_requested(email: str) -> None:
    if is_stop_requested(email):
        raise CodexRetryStopped("用户手动停止 Codex 补跑")


def _async_raise(thread_id: int, exc_type: type[BaseException]) -> bool:
    """向指定 Python 线程注入异常，用于尽快中断阻塞中的补跑流程。"""
    if not thread_id:
        return False
    res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_long(thread_id),
        ctypes.py_object(exc_type),
    )
    if res == 0:
        return False
    if res != 1:
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_long(thread_id), None)
        return False
    return True


def request_stop(email: str) -> dict:
    """请求停止单个 Codex 补跑。运行中会注入停止异常；排队中会在启动前退出。"""
    key = (email or "").strip().lower()
    if not key:
        return {"ok": False, "error": "email 为空", "status": 400}
    with _RETRYING_LOCK:
        retrying = key in _RETRYING
        thread_id = _RUNNING_THREADS.get(key)
        _STOP_REQUESTED.add(key)
    if not retrying:
        db.update_account_codex_status(email, "stopped", "用户手动停止（未发现运行中的补跑）")
        return {"ok": True, "message": "未发现运行中的补跑，已标记为已停止", "state": "stopped", "running": False}

    injected = bool(thread_id and _async_raise(int(thread_id), CodexRetryStopped))
    db.update_account_codex_status(email, "stopped", "用户手动停止 Codex 补跑")
    # 如果没有可注入的存活线程，立即释放进程内占位，避免 UI 显示已停止但再次补跑仍 409。
    with _RETRYING_LOCK:
        if not _thread_alive(thread_id):
            _clear_state_locked(key)
    if injected:
        # 异常注入通常会很快让线程进入 finally/release。若仍阻塞，必须保留占位，
        # 防止同邮箱新旧补跑并发；旧线程退出时 finally 会正常 release。
        def _delayed_release() -> None:
            time.sleep(5)
            with _RETRYING_LOCK:
                if key in _RETRYING and key in _STOP_REQUESTED:
                    try:
                        acc = db.get_account_by_email(email)
                        status = str((acc or {}).get("codex_status") or "").lower()
                    except Exception:
                        status = ""
                    if status == "stopped" and not _thread_alive(thread_id):
                        logger.warning("[Codex 补跑] 停止后延迟释放占位：email=%s thread_id=%s", email, thread_id or "-")
                        _clear_state_locked(key)
                    elif status == "stopped":
                        logger.warning("[Codex 补跑] 停止后线程仍在退出，保留占位：email=%s thread_id=%s", email, thread_id or "-")

        threading.Thread(target=_delayed_release, name=f"codex-stop-release-{key}", daemon=True).start()
    try:
        p = log_path(email)
        p.parent.mkdir(parents=True, exist_ok=True)
        from datetime import datetime as _dt
        with p.open("a", encoding="utf-8") as f:
            f.write(f"{_dt.now().strftime('%H:%M:%S')} [WARNING] [Codex 补跑] 用户手动停止，已发送停止信号 injected={injected}\n")
    except Exception:
        logger.exception("写入 Codex 停止日志失败")
    return {"ok": True, "message": "已发送停止信号", "state": "stopped", "running": True, "injected": injected}


def run_worker(
    email: str,
    *,
    batch_label: str | None = None,
    clear_log: bool = True,
    target_log_path: str | Path | None = None,
) -> dict:
    """执行一次 Codex 补跑。调用前必须先 reserve，结束时会自动 release。"""
    fh: logging.FileHandler | None = None
    root_logger = logging.getLogger()
    result: dict = {"status": "failed", "ok": False, "message": "Codex 补跑未返回结果"}
    key = (email or "").strip().lower()
    try:
        with _RETRYING_LOCK:
            _RUNNING_THREADS[key] = threading.get_ident()
            _RESERVED_AT[key] = time.time()
        check_stop_requested(email)

        from core.codex_oauth import run_codex_oauth

        path = Path(target_log_path) if target_log_path else log_path(email)
        path.parent.mkdir(parents=True, exist_ok=True)
        if clear_log:
            path.write_text("", encoding="utf-8")

        thread_id = threading.get_ident()
        removed_handlers = _remove_retry_log_handlers(root_logger, path)
        if removed_handlers:
            logger.warning("[Codex 补跑] 已清理 %s 个残留日志 handler：%s", removed_handlers, path.name)
        fh = logging.FileHandler(str(path), encoding="utf-8")
        fh._codex_retry_log_path = str(path.resolve())
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
        ))
        fh.addFilter(_ThreadIdFilter(thread_id))
        root_logger.addHandler(fh)

        try:
            import config as config_pkg
            config_pkg.reload_all()
            from config import codex as codex_cfg
            from config import roxybrowser as roxy_cfg
            logger.info(
                "[Codex 补跑] 已热加载配置：CODEX_OAUTH_DRIVER=%s ROXY_OPEN_HEADLESS=%s ROXY_KEEP_BROWSER_OPEN=%s",
                getattr(codex_cfg, "CODEX_OAUTH_DRIVER", ""),
                getattr(roxy_cfg, "ROXY_OPEN_HEADLESS", ""),
                getattr(roxy_cfg, "ROXY_KEEP_BROWSER_OPEN", ""),
            )
        except Exception as exc:
            logger.warning("[Codex 补跑] 配置热加载失败，将继续使用当前内存配置：%s: %s", type(exc).__name__, exc)

        if batch_label:
            logger.info("[Codex 补跑] 批量任务：%s", batch_label)
        logger.info("[Codex 补跑] 开始：%s", email)
        logger.info("[Codex 补跑] 阶段说明：获取授权地址 → 登录邮箱 → 邮箱 OTP → 手机验证 → 捕获 callback → 提交/保存凭证")
        check_stop_requested(email)
        result = run_codex_oauth(email, force=True)
        check_stop_requested(email)
        logger.info(
            "[Codex 补跑] 结果：status=%s ok=%s file=%s callback=%s",
            result.get("status"), result.get("ok"), result.get("file_path"), result.get("callback_url"),
        )
        result_status = result.get("status", "failed")
        if result.get("ok"):
            db.update_account_codex_status(email, "success", None)
            logger.info("[Codex 补跑] %s 成功", email)
        elif result_status == "deactivated":
            db.update_account_codex_status(email, "deactivated", result.get("message"))
            logger.warning("[Codex 补跑] %s 账号已废: %s", email, result.get("message"))
        else:
            db.update_account_codex_status(email, result_status, result.get("message"))
            logger.warning("[Codex 补跑] %s 失败: %s", email, result.get("message"))
        return result
    except CodexRetryStopped as exc:
        result = {"status": "stopped", "ok": False, "message": str(exc) or "用户手动停止 Codex 补跑"}
        db.update_account_codex_status(email, "stopped", result["message"])
        logger.warning("[Codex 补跑] %s 已停止: %s", email, result["message"])
        return result
    except Exception as exc:
        if is_stop_requested(email):
            result = {"status": "stopped", "ok": False, "message": "用户手动停止 Codex 补跑"}
            db.update_account_codex_status(email, "stopped", result["message"])
            logger.warning("[Codex 补跑] %s 已停止", email)
            return result
        result = {"status": "failed", "ok": False, "message": f"{type(exc).__name__}: {exc}"}
        db.update_account_codex_status(email, "failed", result["message"])
        logger.exception("[Codex 补跑] %s 异常", email)
        logger.error("[Codex 补跑] 已结束：异常失败")
        return result
    finally:
        try:
            logger.info("[Codex 补跑] 结束：%s", email)
            if fh is not None:
                root_logger.removeHandler(fh)
                fh.close()
        finally:
            release(email)
            with _RETRYING_LOCK:
                if key:
                    _STOP_REQUESTED.discard(key)
