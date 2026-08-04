# -*- coding: utf-8 -*-
"""可路由的 Plus 试用提链后台队列。"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlsplit

from config import extract_link as cfg
from core import db, masi_cdk_pool
from core.extract_link_providers import (
    LegacyExtractProvider,
    MasiKakaoProvider,
    ProviderError,
    extract_error_message,
)

logger = logging.getLogger(__name__)

SUPPORTED_LINK_TYPES = {"pix", "upi", "kakao_pay", "ideal"}
PROVIDER_CAPABILITIES = {
    "legacy": {"link_types": SUPPORTED_LINK_TYPES, "update_modes": {"sse"}},
    "masi": {"link_types": {"kakao_pay"}, "update_modes": {"poll"}},
}


class CdkPoolExhausted(RuntimeError):
    pass


class CdkPoolUnavailable(RuntimeError):
    pass


class CdkPoolDisabled(RuntimeError):
    pass


class MasiJobFailed(RuntimeError):
    pass


def _runtime_setting(name: str, default=None):
    try:
        from config.env_loader import load_env
        load_env(override=True)
    except Exception:
        pass
    raw = os.getenv(name)
    if raw is not None and str(raw).strip() != "":
        return str(raw).strip()
    return getattr(cfg, name, default)


def _int_setting(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(float(_runtime_setting(name, default) or default))
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


def _float_setting(name: str, default: float, lower: float, upper: float) -> float:
    try:
        value = float(_runtime_setting(name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


_WORKERS = _int_setting("EXTRACT_LINK_WORKERS", 3, 1, 16)
_QUEUE_LIMIT = _int_setting("EXTRACT_LINK_QUEUE_LIMIT", 500, _WORKERS, 5000)
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="extract-link")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)


def queue_settings() -> dict:
    return {"workers": _WORKERS, "queue_limit": _QUEUE_LIMIT}


def provider_capabilities() -> dict:
    return {
        provider: {
            "link_types": sorted(values["link_types"]),
            "update_modes": sorted(values["update_modes"]),
        }
        for provider, values in PROVIDER_CAPABILITIES.items()
    }


def validate_route_combination(*, link_type: str, provider: str, update_mode: str) -> dict:
    selected_provider = str(provider or "").strip().lower()
    selected_type = str(link_type or "").strip().lower()
    selected_mode = str(update_mode or "").strip().lower()
    caps = PROVIDER_CAPABILITIES.get(selected_provider)
    if not caps:
        raise ValueError(f"提链 provider 无效: {selected_provider}; 支持值={sorted(PROVIDER_CAPABILITIES)}")
    if selected_type not in caps["link_types"] or selected_mode not in caps["update_modes"]:
        raise ValueError(
            "提链路由组合不受支持: "
            f"provider={selected_provider}, link_type={selected_type}, update_mode={selected_mode}; "
            f"支持 link_types={sorted(caps['link_types'])}, update_modes={sorted(caps['update_modes'])}"
        )
    return {"provider": selected_provider, "link_type": selected_type, "update_mode": selected_mode}


def validate_extract_proxy(proxy: str | None) -> str:
    value = str(proxy or "").strip()
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("EXTRACT_LINK_PROXY 不是合法的代理 URL") from exc
    if parsed.scheme.lower() not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname:
        raise ValueError("EXTRACT_LINK_PROXY 仅支持 http/https/socks5/socks5h URL")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("EXTRACT_LINK_PROXY 不得包含路径、查询参数或片段")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("EXTRACT_LINK_PROXY 端口必须在 1-65535 范围内")
    return value


def resolve_route(
    *,
    link_type: str | None = None,
    provider: str | None = None,
    update_mode: str | None = None,
    cdk: str | None = None,
) -> dict:
    selected_provider = str(provider or _runtime_setting("EXTRACT_LINK_PROVIDER", "legacy") or "legacy").strip().lower()
    selected_type = str(link_type or _runtime_setting("EXTRACT_LINK_TYPE", "pix") or "pix").strip().lower()
    selected_mode = str(update_mode or _runtime_setting("EXTRACT_LINK_UPDATE_MODE", "sse") or "sse").strip().lower()
    route = {
        **validate_route_combination(link_type=selected_type, provider=selected_provider, update_mode=selected_mode),
        "request_timeout": _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300),
        "wait_timeout": _int_setting("EXTRACT_LINK_EVENT_TIMEOUT", 180, 30, 900),
        "proxy": validate_extract_proxy(_runtime_setting("EXTRACT_LINK_PROXY", "")),
    }
    if selected_provider == "legacy":
        base = str(_runtime_setting("EXTRACT_LINK_API_BASE", "") or "").strip().rstrip("/")
        code = str(cdk or _runtime_setting("EXTRACT_LINK_CDK", "") or "").strip()
        if not base:
            raise ValueError("EXTRACT_LINK_API_BASE 为空")
        if not code:
            raise ValueError("EXTRACT_LINK_CDK/CDK 为空")
        route.update({"base_url": base, "cdk": code})
    else:
        base = str(_runtime_setting("MASI_KAKAO_API_BASE", "https://masi.cc.cd") or "").strip().rstrip("/")
        if not base:
            raise ValueError("MASI_KAKAO_API_BASE 为空")
        route["base_url"] = base
    return route


def _sanitize_error(error, *secrets: str) -> str:
    text = f"{type(error).__name__}: {error}" if isinstance(error, Exception) else str(error or "")
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), "***")
    text = re.sub(r"([?&](?:cdk|code|token|access_token)=)[^&\s]+", r"\1***", text, flags=re.I)
    return text[:500]


def _legacy_provider(route: dict, *, session=None) -> LegacyExtractProvider:
    return LegacyExtractProvider(
        base_url=route["base_url"],
        cdk=route["cdk"],
        timeout=route["request_timeout"],
        event_timeout=route["wait_timeout"],
        proxy=route.get("proxy"),
        session=session,
    )


def _masi_provider(route: dict | None = None, *, session=None) -> MasiKakaoProvider:
    route = route or resolve_route(link_type="kakao_pay", provider="masi", update_mode="poll")
    return MasiKakaoProvider(
        base_url=route["base_url"], timeout=route["request_timeout"], proxy=route.get("proxy"), session=session,
    )


def query_cdk(*, cdk: str | None = None, provider: str | None = None) -> dict:
    selected = str(provider or _runtime_setting("EXTRACT_LINK_PROVIDER", "legacy") or "legacy").strip().lower()
    if selected == "masi":
        return {"provider": "masi", "pool": masi_cdk_pool.pool_summary()}
    route = resolve_route(provider="legacy", update_mode="sse", cdk=cdk)
    adapter = _legacy_provider(route)
    try:
        return adapter.query_quota()
    finally:
        adapter.close()


def _query_masi_quota_with_retry(adapter: MasiKakaoProvider, *, cdk: str) -> dict:
    attempts = _int_setting("MASI_CDK_QUERY_MAX_ATTEMPTS", 3, 1, 10)
    delay = _float_setting("MASI_CDK_QUERY_RETRY_DELAY", 2.0, 0.0, 30.0)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return adapter.query_quota(cdk=cdk)
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(delay)
    assert last_error is not None
    raise last_error


def refresh_masi_cdk(cdk_id: str, *, session=None, route: dict | None = None) -> dict:
    lease = masi_cdk_pool.lease_by_id(cdk_id)
    adapter = _masi_provider(route, session=session)
    try:
        quota = _query_masi_quota_with_retry(adapter, cdk=lease["cdk"])
        return {"ok": True, "item": masi_cdk_pool.update_quota(cdk_id, quota)}
    except Exception as exc:
        error = _sanitize_error(exc, lease.get("cdk"))
        return {"ok": False, "item": masi_cdk_pool.record_query_error(cdk_id, error), "error": error}
    finally:
        adapter.close()
        masi_cdk_pool.release_lease(cdk_id, move_to_tail=False)


def refresh_masi_cdks(*, ids: list[str] | None = None, pool: str | None = None) -> dict:
    if ids is None:
        rows = masi_cdk_pool.list_cdks(pool=pool)
        ids = [str(row["id"]) for row in rows]
    unique_ids = list(dict.fromkeys(str(value) for value in ids if str(value).strip()))
    workers = _int_setting("MASI_CDK_REFRESH_WORKERS", 4, 1, 16)
    route = resolve_route(link_type="kakao_pay", provider="masi", update_mode="poll")
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="masi-cdk-refresh") as executor:
        futures = {executor.submit(refresh_masi_cdk, cdk_id, route=route): cdk_id for cdk_id in unique_ids}
        for future in as_completed(futures):
            cdk_id = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({"ok": False, "id": cdk_id, "error": _sanitize_error(exc)})
    moved_to_exhausted = sum(
        1 for result in results
        if result.get("ok") and result.get("item", {}).get("moved") and result.get("item", {}).get("pool") == masi_cdk_pool.POOL_EXHAUSTED
    )
    moved_to_selectable = sum(
        1 for result in results
        if result.get("ok") and result.get("item", {}).get("moved") and result.get("item", {}).get("pool") == masi_cdk_pool.POOL_SELECTABLE
    )
    return {
        "total": len(unique_ids),
        "success_count": sum(1 for result in results if result.get("ok")),
        "failed_count": sum(1 for result in results if not result.get("ok")),
        "moved_to_exhausted": moved_to_exhausted,
        "moved_to_selectable": moved_to_selectable,
        "results": results,
    }


def import_masi_cdks(text: str, *, refresh_quota: bool = False) -> dict:
    imported = masi_cdk_pool.import_cdks(text)
    refresh_ids = imported.pop("refresh_ids")
    refreshed = refresh_masi_cdks(ids=refresh_ids) if refresh_quota and refresh_ids else {
        "total": 0, "success_count": 0, "failed_count": 0,
        "moved_to_exhausted": 0, "moved_to_selectable": 0, "results": [],
    }
    return {**imported, "refresh_requested": refresh_quota, "refresh": refreshed}


def _select_masi_cdk_and_create(adapter: MasiKakaoProvider, *, access_token: str) -> tuple[dict, dict]:
    deadline = time.monotonic() + _int_setting("MASI_CDK_SELECTION_TIMEOUT", 180, 1, 1800)
    wait = _float_setting("EXTRACT_LINK_POLL_INTERVAL", 2.5, 0.1, 60.0)
    saw_query_error = False
    saw_waitable = False
    while True:
        seen: set[str] = set()
        while True:
            lease = masi_cdk_pool.lease_next(exclude_ids=seen)
            if not lease:
                break
            cdk_id = lease["id"]
            seen.add(cdk_id)
            try:
                try:
                    quota = _query_masi_quota_with_retry(adapter, cdk=lease["cdk"])
                    item = masi_cdk_pool.update_quota(cdk_id, quota)
                except Exception as exc:
                    saw_query_error = True
                    masi_cdk_pool.record_query_error(cdk_id, _sanitize_error(exc, lease["cdk"]))
                    masi_cdk_pool.release_lease(cdk_id)
                    continue

                if item["pool"] == masi_cdk_pool.POOL_EXHAUSTED:
                    masi_cdk_pool.release_lease(cdk_id)
                    continue
                if int(item.get("available_uses") or 0) <= 0:
                    saw_waitable = True
                    masi_cdk_pool.release_lease(cdk_id)
                    continue

                try:
                    job = adapter.create_job(cdk=lease["cdk"], access_token=access_token)
                except Exception:
                    masi_cdk_pool.release_lease(cdk_id)
                    raise
                return job, lease
            except Exception:
                if cdk_id in {row["id"] for row in masi_cdk_pool.list_cdks() if row.get("allocating")}:
                    masi_cdk_pool.release_lease(cdk_id)
                raise

        summary = masi_cdk_pool.pool_summary()
        if summary["selectable_count"] == 0 and summary["allocating_count"] == 0:
            raise CdkPoolExhausted("Masi CDK 池额度已用完，请导入可用 CDK")
        if summary["enabled_selectable_count"] == 0:
            raise CdkPoolDisabled("Masi 可选池没有已启用的 CDK")
        if time.monotonic() >= deadline:
            if saw_query_error and not saw_waitable:
                raise CdkPoolUnavailable("Masi CDK 额度查询暂时失败，请稍后重试")
            raise CdkPoolUnavailable("Masi CDK 当前无可用次数，仍有任务占用额度")
        time.sleep(wait)


def _normalize_success_result(payload: dict) -> dict:
    result = dict(payload or {})
    if not str(result.get("long_url") or "").strip() and str(result.get("copy_paste") or "").strip():
        result["long_url"] = str(result["copy_paste"]).strip()
    if not str(result.get("long_url") or "").strip():
        raise RuntimeError("提链服务成功结果缺少 long_url")
    return result


def _run_legacy(*, account_id: int, access_token: str, route: dict) -> dict:
    adapter = _legacy_provider(route)
    logs: list[str] = []
    last_event = None
    try:
        job = adapter.create_job(access_token=access_token, link_type=route["link_type"])
        job_id = str(job["job_id"])
        db.update_account_extract(account_id, {
            "ok": False, "status": "running", "job_id": job_id,
            "link_type": route["link_type"], "provider": "legacy", "update_mode": "sse",
            "message": "提链任务已创建，等待 SSE 结果", "cdk_remaining": job.get("cdk_remaining"),
        })
        for event, data in adapter.iter_events(job_id=job_id):
            last_event = {"event": event, "data": data}
            if event == "log":
                message = str((data or {}).get("message") or "")[:300]
                if message:
                    logs.append(message)
                    db.update_account_extract(account_id, {"ok": False, "status": "running", "message": message})
            elif event == "result":
                result = (data or {}).get("result") if isinstance(data, dict) else None
                return {"ok": True, "status": "success", "job_id": job_id, "result": _normalize_success_result(result or {}), "logs": logs}
            elif event == "error":
                raise RuntimeError(extract_error_message(data) or "提链任务失败")
            elif event == "done":
                break
        reason = extract_error_message((last_event or {}).get("data"))
        raise RuntimeError(f"提链事件流结束但未返回 result{': ' + reason if reason else ''}")
    finally:
        adapter.close()


def _poll_masi_job(
    *,
    account_id: int,
    adapter: MasiKakaoProvider,
    cdk: str,
    job_id: str,
    route: dict,
    attempt_no: int = 1,
    max_attempts: int = 1,
) -> dict:
    deadline = time.monotonic() + int(route["wait_timeout"])
    interval = _float_setting("EXTRACT_LINK_POLL_INTERVAL", 2.5, 0.1, 60.0)
    max_errors = _int_setting("EXTRACT_LINK_POLL_MAX_ERRORS", 3, 1, 20)
    consecutive_errors = 0
    last_status = "queued"
    while time.monotonic() < deadline:
        try:
            job = adapter.get_job(cdk=cdk, job_id=job_id)
            consecutive_errors = 0
        except ProviderError as exc:
            if not exc.retryable:
                raise
            consecutive_errors += 1
            if consecutive_errors >= max_errors:
                raise RuntimeError(f"Masi Job 连续查询失败 {consecutive_errors} 次: {exc}") from exc
            db.update_account_extract(account_id, {
                "ok": False, "status": "running",
                "message": (
                    f"Masi Job 第 {attempt_no}/{max_attempts} 次查询暂时失败，"
                    f"准备重试（{consecutive_errors}/{max_errors}）"
                ),
            })
            time.sleep(interval)
            continue

        status = str(job.get("status") or "").strip().lower()
        last_status = status
        if status in {"queued", "running"}:
            detail = str(job.get("message") or ("排队中" if status == "queued" else "提炼中"))
            message = f"Masi Job 第 {attempt_no}/{max_attempts} 次：{detail}"[:300]
            db.update_account_extract(account_id, {"ok": False, "status": "running", "message": message})
            time.sleep(interval)
            continue
        if status == "completed":
            output = job.get("output") if isinstance(job.get("output"), dict) else {}
            return {"ok": True, "status": "success", "job_id": job_id, "result": _normalize_success_result(output)}
        if status == "failed":
            raise MasiJobFailed(extract_error_message(job) or "Masi Job 提炼失败")
        if status == "canceled":
            return {"ok": False, "status": "canceled", "job_id": job_id, "message": "Masi Job 已取消", "error": "Masi Job 已取消"}
        raise RuntimeError(f"Masi Job 返回未知状态: {status or '-'}")

    raise TimeoutError(f"Masi Job 等待超时: job={job_id}, last_status={last_status}")


def _run_masi(*, account_id: int, access_token: str, route: dict) -> dict:
    adapter = _masi_provider(route)
    lease: dict | None = None
    max_attempts = 3
    try:
        for attempt_no in range(1, max_attempts + 1):
            lease = None
            job, lease = _select_masi_cdk_and_create(adapter, access_token=access_token)
            job_id = str(job["job_id"])
            db.update_account_extract(account_id, {
                "ok": False, "status": "running", "job_id": job_id,
                "link_type": "kakao_pay", "provider": "masi", "update_mode": "poll",
                "cdk_id": lease["id"], "cdk_fingerprint": lease["fingerprint"],
                "message": f"Masi Job 第 {attempt_no}/{max_attempts} 次已创建，等待主动轮询结果",
            })
            masi_cdk_pool.release_lease(lease["id"], move_to_tail=False)
            bound = masi_cdk_pool.get_secret(lease["id"])
            if not bound:
                raise RuntimeError("Masi Job 绑定的 CDK 已不存在")
            try:
                return _poll_masi_job(
                    account_id=account_id,
                    adapter=adapter,
                    cdk=bound["cdk"],
                    job_id=job_id,
                    route=route,
                    attempt_no=attempt_no,
                    max_attempts=max_attempts,
                )
            except MasiJobFailed as exc:
                if attempt_no >= max_attempts:
                    raise
                db.update_account_extract(account_id, {
                    "ok": False,
                    "status": "running",
                    "message": (
                        f"Masi Job 第 {attempt_no}/{max_attempts} 次失败：{exc}；"
                        "准备重新提交"
                    )[:300],
                })
                time.sleep(_float_setting("EXTRACT_LINK_POLL_INTERVAL", 2.5, 0.1, 60.0))
        raise AssertionError("Masi Job 尝试循环意外结束")
    finally:
        if lease and lease["id"] in {row["id"] for row in masi_cdk_pool.list_cdks() if row.get("allocating")}:
            masi_cdk_pool.release_lease(lease["id"], move_to_tail=False)
        adapter.close()


def _run_extract(*, account_id: int, email: str, access_token: str, route: dict, trigger: str) -> dict:
    try:
        if not db.mark_account_extract_running(account_id):
            return {"ok": False, "error": "账号已删除或提链状态已被重置"}
        if route["provider"] == "legacy":
            result = _run_legacy(account_id=account_id, access_token=access_token, route=route)
        else:
            result = _run_masi(account_id=account_id, access_token=access_token, route=route)
        result.update({
            "link_type": route["link_type"],
            "provider": route["provider"],
            "update_mode": route["update_mode"],
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        })
        db.update_account_extract(account_id, result)
        if result.get("ok"):
            logger.info("[提链] 成功: %s provider=%s type=%s mode=%s job=%s", email, route["provider"], route["link_type"], route["update_mode"], result.get("job_id"))
        return result
    except Exception as exc:
        secrets = [access_token, str(route.get("cdk") or ""), str(route.get("proxy") or "")]
        reason = _sanitize_error(exc, *secrets)
        result = {
            "ok": False, "status": "failed", "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": reason, "message": reason,
            "link_type": route.get("link_type"), "provider": route.get("provider"), "update_mode": route.get("update_mode"),
        }
        try:
            db.update_account_extract(account_id, result)
        except Exception:
            logger.exception("[提链] 写入失败状态异常: account_id=%s", account_id)
        logger.error("[提链] 失败: %s provider=%s error=%s", email, route.get("provider"), reason)
        return result
    finally:
        _QUEUE_SLOTS.release()


def _run_existing_masi_extract(
    *,
    account_id: int,
    email: str,
    job_id: str,
    cdk: str,
    route: dict,
) -> dict:
    adapter = None
    try:
        adapter = _masi_provider(route)
        if not db.mark_account_extract_running(account_id):
            return {"ok": False, "error": "账号已删除或恢复轮询状态已被重置"}
        result = _poll_masi_job(
            account_id=account_id,
            adapter=adapter,
            cdk=cdk,
            job_id=job_id,
            route=route,
        )
        result.update({
            "link_type": "kakao_pay",
            "provider": "masi",
            "update_mode": "poll",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        })
        db.update_account_extract(account_id, result)
        if result.get("ok"):
            logger.info("[提链] 恢复轮询成功: %s provider=masi job=%s", email, job_id)
        return result
    except Exception as exc:
        reason = _sanitize_error(exc, cdk, str(route.get("proxy") or ""))
        result = {
            "ok": False,
            "status": "failed",
            "job_id": job_id,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": reason,
            "message": reason,
            "link_type": "kakao_pay",
            "provider": "masi",
            "update_mode": "poll",
        }
        db.update_account_extract(account_id, result)
        logger.error("[提链] 恢复轮询失败: %s provider=masi job=%s error=%s", email, job_id, reason)
        return result
    finally:
        if adapter is not None:
            adapter.close()
        _QUEUE_SLOTS.release()


def enqueue_account_extract(
    *,
    account_id: int,
    email: str,
    access_token: str,
    trigger: str = "manual",
    link_type: str | None = None,
    cdk: str | None = None,
    provider: str | None = None,
    update_mode: str | None = None,
) -> dict:
    route = resolve_route(link_type=link_type, provider=provider, update_mode=update_mode, cdk=cdk)
    if route["provider"] == "masi" and masi_cdk_pool.pool_summary()["enabled_selectable_count"] == 0:
        raise CdkPoolDisabled("Masi 可选池没有已启用的 CDK")
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "error": "提链队列已满"}
    try:
        if not db.claim_account_extract(
            account_id,
            trigger=trigger,
            link_type=route["link_type"],
            provider=route["provider"],
            update_mode=route["update_mode"],
        ):
            _QUEUE_SLOTS.release()
            return {"accepted": False, "busy": True, "error": "该账号正在提链中"}
        future = _EXECUTOR.submit(
            _run_extract,
            account_id=account_id,
            email=email,
            access_token=access_token,
            route=route,
            trigger=trigger,
        )
        return {
            "accepted": True,
            "busy": False,
            "future": future,
            "link_type": route["link_type"],
            "provider": route["provider"],
            "update_mode": route["update_mode"],
        }
    except Exception:
        _QUEUE_SLOTS.release()
        raise


def enqueue_existing_masi_job_poll(*, account_id: int, email: str, trigger: str = "manual_resume") -> dict:
    account = db.get_account(account_id)
    if not account:
        raise RuntimeError("账号不存在")
    if str(account.get("extract_link_provider") or "").lower() != "masi":
        raise RuntimeError("仅 Masi 提链任务支持恢复轮询")
    job_id = str(account.get("extract_link_job_id") or "").strip()
    cdk_id = str(account.get("extract_link_cdk_id") or "").strip()
    if not job_id or not cdk_id:
        raise RuntimeError("缺少原 Masi job_id 或 CDK 绑定")
    bound = masi_cdk_pool.get_secret(cdk_id)
    if not bound or not str(bound.get("cdk") or "").strip():
        raise RuntimeError("原 Masi Job 绑定的 CDK 已不存在")
    saved_fingerprint = str(account.get("extract_link_cdk_fingerprint") or "").strip()
    if saved_fingerprint and saved_fingerprint != str(bound.get("fingerprint") or "").strip():
        raise RuntimeError("原 Masi Job 的 CDK 指纹不匹配")
    route = resolve_route(link_type="kakao_pay", provider="masi", update_mode="poll")
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "error": "提链队列已满"}
    try:
        if not db.claim_account_extract_resume(account_id, trigger=trigger):
            _QUEUE_SLOTS.release()
            return {"accepted": False, "busy": True, "error": "该账号正在提链或没有可恢复的 Masi Job"}
        future = _EXECUTOR.submit(
            _run_existing_masi_extract,
            account_id=account_id,
            email=email,
            job_id=job_id,
            cdk=bound["cdk"],
            route=route,
        )
        return {"accepted": True, "busy": False, "future": future, "job_id": job_id}
    except Exception:
        _QUEUE_SLOTS.release()
        raise
