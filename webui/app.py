# -*- coding: utf-8 -*-
"""
Flask 本地控制台。

复用现有后端：
    core.db                     —— 账号 / 邮箱池 / 任务的文件持久化与查询
    core.registration_service   —— 线程池批量注册 + 任务日志
    webui.config_editor         —— 安全读写 config/*.py

所有接口返回 JSON；前端是单文件 templates/index.html（原生 JS + fetch）。
默认绑定 127.0.0.1，仅本地访问。
"""
import logging
import binascii
import json
import re
import threading
import time
import uuid

from flask import Flask, Response, jsonify, render_template, request
import pyotp

from core import codex_retry_service, db, plan_check_service, checkout_session_service, extract_link_service, live_check_service, masi_cdk_pool
from webui.auth import init_auth, register_auth_routes
from core import registration_service as svc
from core.email_pool_status import EMAIL_POOL_STATUSES, validate_status, EmailPoolStatusError
from core.account_status_contracts import build_account_status_contract, mailcom_cleanup_capabilities
from core.account_status_contracts import (
    CODEX_AUTH_STATUSES, CODEX_OPERATION_STATUSES, LIVE_CHECK_STATUSES, PLAN_CATEGORY_CODES,
)
from webui import config_editor

logger = logging.getLogger(__name__)

def _pool_source_arg(default: str = "outlook") -> str:
    src = (request.args.get("source") or "").strip()
    if not src and request.method == "POST":
        data = request.get_json(silent=True) or {}
        src = (data.get("source") or data.get("type") or "").strip()
    return src if src in ("all", "outlook", "generic_api", "cloudflare_domain", "icloud", "mailcom") else default


def _pool_status_arg(raw: str | None) -> tuple[str | None, str | None]:
    """校验邮箱池查询状态；空值表示不过滤，非法值返回错误文本。"""
    value = str(raw or "").strip()
    if not value:
        return None, None
    try:
        return validate_status(value), None
    except EmailPoolStatusError as exc:
        return None, str(exc)


def _account_status_filter_args() -> tuple[dict[str, str], str | None]:
    plan = str(request.args.get("plan_category") or request.args.get("plan") or "").strip().lower()
    auth = str(request.args.get("codex_auth_status") or "").strip().lower()
    operation = str(request.args.get("codex_operation_status") or "").strip().lower()
    live = str(request.args.get("live_check_status") or "").strip().lower()
    legacy = str(request.args.get("codex_status") or "").strip().lower()
    if plan and plan not in set(PLAN_CATEGORY_CODES) | {"all", "any", "free", "plus"}:
        return {}, "plan_category 非法"
    if auth and auth not in CODEX_AUTH_STATUSES:
        return {}, "codex_auth_status 非法"
    if operation and operation not in CODEX_OPERATION_STATUSES:
        return {}, "codex_operation_status 非法"
    if live and live not in LIVE_CHECK_STATUSES:
        return {}, "live_check_status 非法"
    if legacy and legacy not in set(CODEX_AUTH_STATUSES) | {"retrying", "stopped", "cancelled", "canceled", "deactivated"}:
        return {}, "codex_status 非法"
    if legacy and any((auth, operation, live)):
        return {}, "codex_status 不能与新状态筛选参数同时提交"
    return {
        "plan_filter": plan,
        "codex_auth_status_filter": auth,
        "codex_operation_status_filter": operation,
        "live_check_status_filter": live,
        "codex_status_filter": legacy,
    }, None


def _lifecycle_error_response(exc: db.EmailPoolLifecycleError):
    status_code = 404 if exc.code in {"email_not_found", "parent_missing", "alias_missing"} else 400 if exc.code in {
        "source_invalid", "restore_source_invalid", "force_reason_required",
    } else 409
    return jsonify({"ok": False, "error": exc.code, "message": str(exc), **exc.details}), status_code


def _with_pool_source(rows: list[dict], source: str) -> list[dict]:
    out = []
    for r in rows:
        x = dict(r)
        x["source"] = source
        if not x.get("copy_line"):
            x["copy_line"] = x.get("email") or ""
        out.append(x)
    return out




def _matches_query(row: dict, q: str | None) -> bool:
    q = str(q or "").strip().lower()
    if not q:
        return True
    try:
        return q in "\n".join(str(v) for v in row.values()).lower()
    except Exception:
        return False


def _matches_email_query(row: dict, q: str | None) -> bool:
    q = str(q or "").strip().lower()
    if not q:
        return True
    return q in str(row.get("email") or "").lower()


def _paginate_items(items: list[dict], *, page: int, page_size: int) -> dict:
    page = max(1, int(page or 1))
    page_size = max(1, min(500, int(page_size or 50)))
    total = len(items)
    offset = (page - 1) * page_size
    return {
        "ok": True,
        "items": items[offset:offset + page_size],
        "total": total,
        "page": page,
        "page_size": page_size,
        "offset": offset,
        "limit": page_size,
    }


def _safe_twofa_summary(row: dict) -> dict:
    """从账号 extra 中提取可公开展示的 2FA 状态，不返回 Secret/Token/正文。"""
    raw = row.get("extra_json")
    if isinstance(raw, dict):
        extra = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = {}
        extra = parsed if isinstance(parsed, dict) else {}
    else:
        extra = {}
    twofa = extra.get("twofa") if isinstance(extra.get("twofa"), dict) else {}
    allowed = {"enabled", "failed", "disabled", "already_enabled"}
    status = str(twofa.get("status") or "").strip().lower()
    if str(row.get("totp_secret") or "").strip():
        # 已持久化 Secret 是账号可用状态的权威证据，不能被旧的失败摘要覆盖。
        status = "enabled"
    elif status not in allowed:
        status = ""
    out = {"twofa_status": status} if status else {}
    error = twofa.get("error") if isinstance(twofa.get("error"), dict) else {}
    def safe_identifier(value: object, limit: int) -> str:
        text = str(value or "").strip().lower()[:limit]
        return text if re.fullmatch(r"[a-z0-9][a-z0-9_.:-]*", text) else ""

    stage = safe_identifier(error.get("stage"), 40)
    code = safe_identifier(error.get("code"), 80)
    if stage:
        out["twofa_stage"] = stage
    if code:
        out["twofa_error_code"] = code
    return out


def _compact_account_for_list(row: dict) -> dict:
    """账号列表轻量对象：只返回当前表格渲染和按钮判断必需字段。

    原则：
    - 不返回完整 Token / Token 预览 / TOTP Secret 等敏感凭证。
    - 时间戳、错误原因、提链详情等只在前端确实要展示时返回；空值不返回。
    - 复制/下载敏感内容时再通过 /secret 接口按需读取。
    """
    out = {
        "id": row.get("id"),
        "email": row.get("email"),
        "has_access_token": bool(str(row.get("access_token") or "").strip()),
        "totp_enabled": bool(row.get("totp_secret")),
    }
    out.update(_safe_twofa_summary(row))

    # 这些是列表固定列直接展示字段。
    for key in (
        "user_name", "email_source", "note", "archived", "created_at",
        "plan_type", "current_plan_type", "plus_trial_eligible", "trial_eligibility_known",
        "plan_check_status", "codex_status",
        "checkout_check_status", "checkout_session_type",
    ):
        if key in row:
            out[key] = row.get(key)

    if row.get("plan_check_status") in ("queued", "running") or row.get("plan_check_ok") is False:
        out["plan_check_ok"] = row.get("plan_check_ok")

    # 下面字段仅在有值时返回，避免每行堆满 null/空字符串/内部状态。
    optional_keys = (
        # 套餐展示补充：付费到期/折扣/失败原因。
        "plan_check_error", "plan_check_error_kind", "plan_check_http_status", "plan_checked_at", "plan_check_updated_at", "plan_last_success_at",
        "plan_expires_at", "plan_renews_at", "renews_at",
        "billing_period", "billing_currency", "discount_amount", "discount_type",
        "discount_expires_at", "discount_promo_campaign_id",
        "token_expired", "token_expires_at",
        # Checkout 类型检测仅返回状态/脱敏诊断，不返回完整 Session ID。
        "checkout_check_ok", "checkout_check_trigger", "checkout_check_queued_at",
        "checkout_check_started_at", "checkout_check_completed_at", "checkout_check_updated_at",
        "checkout_check_checked_at", "checkout_check_http_status", "checkout_check_error_code",
        "checkout_check_error_message", "checkout_check_error", "checkout_check_message",
        "checkout_check_attempt_count", "checkout_check_max_attempts", "checkout_check_request_timeout",
        "checkout_check_network_route", "checkout_check_proxy_mode", "checkout_check_proxy_used",
        "checkout_check_retryable", "checkout_check_content_type", "checkout_check_response_bytes",
        "checkout_check_retry_after_seconds", "checkout_session_last_success_at",
        # 查活状态。
        "live_check_status", "live_check_error", "live_checked_at",
        "live_check_device_id", "live_check_proxy_used", "live_check_fingerprint_text",
        # 提链成功/失败时才需要。
        "extract_link_status", "extract_link_type", "extract_link_message", "extract_link_error",
        "extract_link_job_id",
        "extract_link_provider", "extract_link_update_mode", "extract_link_cdk_fingerprint",
        "extract_link_long_url", "extract_link_copy_paste", "extract_link_image_url_png",
        "extract_link_image_url_svg", "extract_link_expires_at",
        # Codex 状态提示。
        "codex_error",
    )
    for key in optional_keys:
        value = row.get(key)
        if value is not None and value != "":
            out[key] = value
    contract_row = dict(row)
    contract_row["extract_link_resumable"] = db.account_extract_resumable(row)
    # 兼容现有 WebUI；规范能力同时通过 extract_link_capabilities 返回。
    out["extract_link_resumable"] = contract_row["extract_link_resumable"]
    out.update(build_account_status_contract(contract_row))
    out["extract_link_capabilities"] = db.account_extract_capabilities(row)
    plan = str(row.get("current_plan_type") or row.get("plan_type") or "").lower()
    if any(x in plan for x in ("plus", "pro", "team", "go")):
        expire = row.get("expires_at")
        if expire:
            out["expires_at"] = expire
    return out


_ACCOUNT_COPY_FIELDS = frozenset({
    "access_token",
    "email_access_token",
    "email_access_token_totp",
})


def _normalize_account_totp_secret(value: object) -> str:
    return "".join(str(value or "").split()).upper().rstrip("=")


def _account_copy_result(row: dict, field: str) -> dict:
    """按账号界面导出格式生成值，并保留退化/跳过原因。"""
    field = (field or "").strip()
    if field not in _ACCOUNT_COPY_FIELDS:
        raise ValueError("不支持的账号复制格式")

    access_token = str(row.get("access_token") or "").strip()
    if not access_token:
        return {
            "value": "",
            "fallback": False,
            "fallback_reason": "",
            "skipped": True,
            "skip_reason": "missing_access_token",
        }

    if field == "access_token":
        return {
            "value": access_token,
            "fallback": False,
            "fallback_reason": "",
            "skipped": False,
            "skip_reason": "",
        }

    email = str(row.get("email") or "").strip()
    if not email:
        return {
            "value": "",
            "fallback": False,
            "fallback_reason": "",
            "skipped": True,
            "skip_reason": "missing_email",
        }

    base = f"{email}----{access_token}"
    if field == "email_access_token":
        return {
            "value": base,
            "fallback": False,
            "fallback_reason": "",
            "skipped": False,
            "skip_reason": "",
        }

    totp_secret = _normalize_account_totp_secret(row.get("totp_secret"))
    if totp_secret:
        return {
            "value": f"{base}----{totp_secret}",
            "fallback": False,
            "fallback_reason": "",
            "skipped": False,
            "skip_reason": "",
        }
    return {
        "value": base,
        "fallback": True,
        "fallback_reason": "missing_totp_secret",
        "skipped": False,
        "skip_reason": "",
    }


def _account_secret_value(row: dict, field: str) -> str:
    field = (field or "").strip()
    if field in _ACCOUNT_COPY_FIELDS:
        return _account_copy_result(row, field)["value"]
    if field == "copy_line":
        return str(row.get("copy_line") or "")
    raise ValueError(
        "field 仅支持 access_token/email_access_token/email_access_token_totp/copy_line"
    )


def _account_secret_result(row: dict, field: str) -> dict:
    field = (field or "").strip()
    if field in _ACCOUNT_COPY_FIELDS:
        return _account_copy_result(row, field)
    value = _account_secret_value(row, field)
    return {
        "value": value,
        "fallback": False,
        "fallback_reason": "",
        "skipped": False,
        "skip_reason": "",
    }


def _no_store_json(payload: dict, status: int = 200):
    response = jsonify(payload)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


def _safe_checkout_queue_result(result: dict) -> dict:
    """过滤 Checkout 入队结果中的服务端敏感字段。"""
    return {
        key: value
        for key, value in result.items()
        if key not in {"proxy", "access_token", "checkout_session_id"}
    }


def _compact_job_for_list(row: dict) -> dict:
    """注册任务列表轻量对象：只返回表格展示和按钮判断需要的字段。"""
    out = {
        "id": row.get("id"),
        "status": row.get("status"),
    }
    for key in (
        "job_type", "account_id", "parent_job_id", "retry_attempt", "email", "started_at", "completed_at",
        "display_status", "retryable", "retry_action", "retry_label",
        "manual_otp_required",
    ):
        value = row.get(key)
        if value is not None and value != "" and value is not False:
            out[key] = value
    err = str(row.get("error_message") or "").strip()
    if err:
        # 列表只需要摘要；完整错误和堆栈看“任务日志”。
        out["error_message"] = err[:240] + ("…" if len(err) > 240 else "")
    return out


def _job_status_counts(rows: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    counts["active"] = sum(int(counts.get(s, 0) or 0) for s in ("pending", "running", "stopping"))
    return counts


def _enrich_job_rows(rows: list[dict], manual_otp_required: bool) -> None:
    terminal_statuses = {"failed", "stopped", "cancelled"}
    needs_account_snapshot = any(str(row.get("status") or "") in terminal_statuses for row in rows)
    account_snapshot = db.get_retry_account_snapshot() if needs_account_snapshot else None
    for row in rows:
        row["manual_otp_required"] = manual_otp_required
        row.update(svc.get_retry_info(row, account_snapshot=account_snapshot))

def create_app(auth_code: str | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates")
    _prepared_downloads: dict[str, dict] = {}

    def _put_prepared_download(content: bytes, filename: str, mimetype: str = "application/zip") -> str:
        now = time.time()
        # 顺手清理 10 分钟前的临时下载，避免内存堆积。
        for k, v in list(_prepared_downloads.items()):
            if now - float(v.get("created_at") or 0) > 600:
                _prepared_downloads.pop(k, None)
        download_id = uuid.uuid4().hex
        _prepared_downloads[download_id] = {
            "content": bytes(content),
            "filename": filename,
            "mimetype": mimetype,
            "created_at": now,
        }
        return download_id

    @app.get("/api/downloads/<download_id>")
    def api_prepared_download(download_id: str):
        item = _prepared_downloads.pop(str(download_id or ""), None)
        if not item:
            return jsonify({"ok": False, "error": "下载已过期或不存在，请重新生成"}), 404
        content = item.get("content") or b""
        filename = item.get("filename") or "download.zip"
        mimetype = item.get("mimetype") or "application/octet-stream"
        return Response(
            content,
            mimetype=mimetype,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(content)),
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
                "X-Download-Options": "noopen",
            },
        )

    init_auth(app, auth_code=auth_code)
    register_auth_routes(app)
    db.validate_runtime_storage()
    migrated_email_pool = db.migrate_email_pool_statuses()
    if migrated_email_pool.get("rows_normalized"):
        logger.warning("已迁移邮箱池状态: %s", migrated_email_pool)
    migrated_account_statuses = db.migrate_account_status_contracts()
    if migrated_account_statuses.get("changed"):
        logger.warning("已迁移账号状态契约: %s", migrated_account_statuses)
    recovered_codex_operations = db.recover_interrupted_codex_operations()
    if recovered_codex_operations:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的 Codex 补跑状态", recovered_codex_operations)
    recovered_plan_checks = db.recover_interrupted_plan_checks()
    if recovered_plan_checks:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的套餐查询状态", recovered_plan_checks)
    recovered_checkout_sessions = db.recover_interrupted_checkout_sessions()
    if recovered_checkout_sessions:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的 Checkout 检测状态", recovered_checkout_sessions)
    recovered_extract_links = db.recover_interrupted_extract_links()
    if recovered_extract_links:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的提链状态", recovered_extract_links)
    recovered_live_checks = db.recover_interrupted_live_checks()
    if recovered_live_checks:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的查活状态", recovered_live_checks)
    recovered_mailcom = db.recover_interrupted_mailcom_state()
    if recovered_mailcom.get("sync") or recovered_mailcom.get("lease"):
        logger.warning("已恢复 mail.com 状态: %s", recovered_mailcom)
    # ----------------------------------------------------------
    # 页面
    # ----------------------------------------------------------
    @app.get("/")
    def index():
        return render_template("index.html")

    # ----------------------------------------------------------
    # 统计概览
    # ----------------------------------------------------------
    @app.get("/api/summary")
    def api_summary():
        from config import email as _email_cfg
        from core.email_provider import parse_email_sources
        pool = {"total": 0, **{status: 0 for status in EMAIL_POOL_STATUSES}}
        for src in parse_email_sources(_email_cfg.EMAIL_SOURCE):
            # GPTMail/MailNest/CloudMail 地址按需生成，不属于本地邮箱池。
            if src in ("gptmail", "mailnest", "cloudmail", "cloudflare"):
                continue
            one = (
                db.generic_api_email_pool_summary() if src == "generic_api"
                else db.domain_email_pool_summary() if src == "cloudflare_domain"
                else db.icloud_email_pool_summary() if src == "icloud"
                else db.mailcom_pool_summary() if src == "mailcom"
                else db.outlook_pool_summary()
            )
            for k in pool:
                pool[k] += int(one.get(k, 0) or 0)
        domain_pool = db.domain_email_pool_summary()
        return jsonify({
            "accounts": db.count_accounts(),
            "outlook_total": pool.get("total", 0),
            "outlook_available": pool.get("available", 0),
            "outlook_registering": pool.get("registering", 0),
            "outlook_used": pool.get("used", 0),
            "outlook_failed": pool.get("failed", 0),
            "outlook_disabled": pool.get("disabled", 0),
            "domain_total": domain_pool.get("total", 0),
            "domain_available": domain_pool.get("available", 0),
            "domain_registering": domain_pool.get("registering", 0),
            "domain_used": domain_pool.get("used", 0),
            "domain_failed": domain_pool.get("failed", 0),
            "domain_disabled": domain_pool.get("disabled", 0),
            "email_pool": pool,
        })

    # ----------------------------------------------------------
    # 已注册账号
    # ----------------------------------------------------------
    @app.get("/api/accounts")
    def api_accounts():
        limit = request.args.get("limit", default=500, type=int)
        archived = str(request.args.get("archived", default="0") or "0").lower()
        status_filters, filter_error = _account_status_filter_args()
        if filter_error:
            return jsonify({"ok": False, "error": filter_error}), 400
        checkout_type_filter = str(request.args.get("checkout_type", default="") or "").lower()
        q = str(request.args.get("q", default="") or "").strip()
        date_from = str(request.args.get("date_from", default="") or "").strip() or None
        date_to = str(request.args.get("date_to", default="") or "").strip() or None
        # 新分页接口：传 page/page_size 或 paged=1 时返回 {items,total,page,page_size,...}
        paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        if paged or page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            offset = (page - 1) * page_size
            result = db.list_accounts_page(
                limit=page_size,
                offset=offset,
                archived=archived,
                **status_filters,
                q=q,
                date_from=date_from,
                date_to=date_to,
                checkout_type_filter=checkout_type_filter,
            )
            result["items"] = [_compact_account_for_list(r) for r in (result.get("items") or [])]
            result.update({"ok": True, "page": page, "page_size": page_size, "compact": True})
            return jsonify(result)
        rows = db.list_accounts(
            limit=limit,
            archived=archived,
            **status_filters,
            q=q,
            date_from=date_from,
            date_to=date_to,
            checkout_type_filter=checkout_type_filter,
        )
        return jsonify([_compact_account_for_list(row) for row in rows])

    @app.get("/api/accounts/plan-check-status")
    def api_account_plan_check_status():
        """套餐查询轻量状态，不返回 Token、邮箱密码等敏感字段。"""
        limit = request.args.get("limit", default=5000, type=int)
        archived = str(request.args.get("archived", default="0") or "0").lower()
        status_filters, filter_error = _account_status_filter_args()
        if filter_error:
            return jsonify({"ok": False, "error": filter_error}), 400
        checkout_type_filter = str(request.args.get("checkout_type", default="") or "").lower()
        q = str(request.args.get("q", default="") or "").strip()
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        if page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            offset = (page - 1) * page_size
            snapshot = db.list_account_plan_check_statuses(
                limit=page_size,
                offset=offset,
                archived=archived,
                **status_filters,
                q=q,
                checkout_type_filter=checkout_type_filter,
            )
            snapshot.update({"page": page, "page_size": page_size})
        else:
            snapshot = db.list_account_plan_check_statuses(
                limit=max(1, min(5000, limit)),
                archived=archived,
                **status_filters,
                q=q,
                checkout_type_filter=checkout_type_filter,
            )
        snapshot["queue"] = plan_check_service.queue_settings()
        snapshot["checkout_queue"] = checkout_session_service.queue_settings()
        return jsonify(snapshot)


    @app.post("/api/accounts/check-checkout-session")
    @app.post("/api/accounts/check-checkout")
    def api_account_check_checkout_session():
        """把单账号初始 Checkout Session 类型检测加入独立队列。"""
        data = request.get_json(silent=True) or {}
        acc_id = data.get("account_id") or data.get("id")
        email = str(data.get("email") or "").strip()
        acc = None
        if acc_id is not None:
            try:
                acc = db.get_account(int(acc_id))
            except (TypeError, ValueError):
                acc = None
        if acc is None and email:
            acc = db.get_account_by_email(email)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        token = str(acc.get("access_token") or "").strip()
        if not token:
            return jsonify({"ok": False, "error": "该账号没有 access_token"}), 400
        queued = checkout_session_service.enqueue_checkout_session_check(
            account_id=int(acc.get("id")),
            email=str(acc.get("email") or ""),
            access_token=token,
            trigger="manual",
        )
        safe = _safe_checkout_queue_result(queued)
        if safe.get("busy"):
            return jsonify({"ok": False, **safe}), 409
        if not safe.get("accepted"):
            status = 400 if safe.get("config_error") else 503
            return jsonify({"ok": False, **safe}), status
        return jsonify({"ok": True, "started": True, **safe}), 202


    @app.post("/api/accounts/check-checkout-session-bulk")
    @app.post("/api/accounts/check-checkout-bulk")
    def api_accounts_check_checkout_session_bulk():
        """批量把 Checkout Session 类型检测加入独立队列。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多查询 500 个账号"}), 400

        items = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            token = str(acc.get("access_token") or "").strip()
            if not token:
                skipped.append({"id": acc_id, "email": acc.get("email"), "reason": "缺少 access_token"})
                continue
            items.append((acc, token))

        started = []
        busy = []
        failed = []
        for acc, token in items:
            acc_id = int(acc.get("id"))
            queued = checkout_session_service.enqueue_checkout_session_check(
                account_id=acc_id,
                email=acc.get("email") or "",
                access_token=token,
                trigger="manual_bulk",
            )
            safe = _safe_checkout_queue_result(queued)
            item = {"id": acc_id, "email": acc.get("email"), **safe}
            if safe.get("accepted"):
                started.append(item)
            elif safe.get("busy"):
                busy.append(item)
            else:
                failed.append(item)

        return jsonify({
            "ok": True,
            "started": started,
            "started_count": len(started),
            "busy": busy,
            "busy_count": len(busy),
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "skipped_count": len(skipped),
            "queue": checkout_session_service.queue_settings(),
        }), 202


    @app.get("/api/accounts/<int:acc_id>/secret")
    def api_account_secret(acc_id: int):
        """按需读取单账号敏感值，避免账号列表一次性下发完整 Token/整行。"""
        field = str(request.args.get("field") or "").strip()
        acc = db.get_account(acc_id)
        if not acc:
            return _no_store_json({"ok": False, "error": "账号不存在"}, 404)
        try:
            result = _account_secret_result(acc, field)
        except ValueError as exc:
            return _no_store_json({"ok": False, "error": str(exc)}, 400)
        return _no_store_json({
            "ok": True,
            "id": acc_id,
            "field": field,
            "value": result["value"],
            "fallback": result["fallback"],
            "fallback_reason": result["fallback_reason"],
            "skipped": result["skipped"],
            "skip_reason": result["skip_reason"],
        })

    @app.get("/api/accounts/<int:acc_id>/totp-code")
    def api_account_totp_code(acc_id: int):
        """按需生成当前 TOTP；只返回验证码，不返回账号 Secret。"""
        acc = db.get_account(acc_id)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404

        secret = "".join(str(acc.get("totp_secret") or "").split()).upper().rstrip("=")
        if not secret:
            return jsonify({"ok": False, "error": "该账号没有已保存的 2FA Secret"}), 400

        now = time.time()
        try:
            totp = pyotp.TOTP(secret)
            code = totp.at(now)
        except (binascii.Error, TypeError, ValueError):
            return jsonify({"ok": False, "error": "该账号保存的 2FA Secret 无效"}), 422

        period = int(totp.interval or 30)
        remaining_seconds = max(1, period - (int(now) % period))
        response = jsonify({
            "ok": True,
            "id": acc_id,
            "code": code,
            "period_seconds": period,
            "remaining_seconds": remaining_seconds,
        })
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response

    @app.post("/api/accounts/secret-bulk")
    def api_accounts_secret_bulk():
        """按需批量读取账号敏感值。Body {account_ids:[...], field}."""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        field = str(data.get("field") or "").strip()
        if not isinstance(ids, list) or not ids:
            return _no_store_json({"ok": False, "error": "account_ids 必须是非空数组"}, 400)
        if len(ids) > 5000:
            return _no_store_json({"ok": False, "error": "单次最多读取 5000 个账号"}, 400)
        values = []
        skipped = []
        fallback_count = 0
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            try:
                result = _account_secret_result(acc, field)
            except ValueError as exc:
                return _no_store_json({"ok": False, "error": str(exc)}, 400)
            if result["value"]:
                item = {"id": acc_id, "email": acc.get("email"), "value": result["value"]}
                if result["fallback"]:
                    item["fallback"] = True
                    item["fallback_reason"] = result["fallback_reason"]
                    fallback_count += 1
                values.append(item)
            else:
                skipped.append({
                    "id": acc_id,
                    "email": acc.get("email"),
                    "reason": result["skip_reason"] or "值为空",
                })
        return _no_store_json({
            "ok": True,
            "field": field,
            "values": values,
            "count": len(values),
            "fallback_count": fallback_count,
            "skipped": skipped,
            "skipped_count": len(skipped),
        })

    @app.post("/api/accounts/<int:acc_id>/archive")
    def api_account_archive(acc_id: int):
        """归档/取消归档一个账号。Body {archived: true|false}。"""
        data = request.get_json(silent=True) or {}
        archived = bool(data.get("archived", True))
        updated = db.archive_account(acc_id=acc_id, archived=archived)
        if not updated:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        return jsonify({"ok": True, "updated": True, "id": acc_id, "archived": archived})

    @app.post("/api/accounts/archive-bulk")
    def api_accounts_archive_bulk():
        """批量归档/取消归档账号。Body {account_ids:[...], archived:true|false}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        archived = bool(data.get("archived", True))
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多归档 5000 个账号"}), 400
        account_ids = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            account_ids.append(acc_id)
        updated, db_skipped = db.archive_accounts(account_ids=account_ids, archived=archived)
        skipped.extend(db_skipped)
        return jsonify({"ok": True, "updated": updated, "updated_count": len(updated), "archived": archived, "skipped": skipped})

    @app.post("/api/accounts/<int:acc_id>/delete")
    def api_account_delete(acc_id: int):
        """删除一个已注册账号记录。只删除本地保存的账号/token记录，不改邮箱池状态。"""
        deleted = db.delete_account(acc_id=acc_id)
        if not deleted:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        return jsonify({"ok": True, "deleted": True})

    @app.post("/api/accounts/delete-bulk")
    def api_accounts_delete_bulk():
        """批量删除已注册账号记录。Body {account_ids: [...]} 或 {ids: [...]}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多删除 5000 个账号"}), 400
        account_ids = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            account_ids.append(acc_id)
        deleted, db_skipped = db.delete_accounts(account_ids=account_ids)
        skipped.extend(db_skipped)
        return jsonify({
            "ok": True,
            "deleted": deleted,
            "deleted_count": len(deleted),
            "skipped": skipped,
        })

    @app.post("/api/accounts/<int:acc_id>/note")
    def api_account_note(acc_id: int):
        """更新单个已注册账号备注。Body {note: "..."}，空字符串表示清空。"""
        data = request.get_json(silent=True) or {}
        note = str(data.get("note") or "")
        if len(note) > 2000:
            return jsonify({"ok": False, "error": "备注最多 2000 个字符"}), 400
        updated = db.update_account_note(acc_id=acc_id, note=note)
        if not updated:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        return jsonify({"ok": True, "updated": True, "id": acc_id, "note": note})

    @app.post("/api/accounts/note-bulk")
    def api_accounts_note_bulk():
        """批量更新已注册账号备注。Body {account_ids: [...], note: "..."}，空字符串表示清空。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        note = str(data.get("note") or "")
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多备注 5000 个账号"}), 400
        if len(note) > 2000:
            return jsonify({"ok": False, "error": "备注最多 2000 个字符"}), 400

        account_ids = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            account_ids.append(acc_id)
        updated, db_skipped = db.update_accounts_note(account_ids=account_ids, note=note)
        skipped.extend(db_skipped)
        return jsonify({
            "ok": True,
            "updated": updated,
            "updated_count": len(updated),
            "skipped": skipped,
            "skipped_count": len(skipped),
        })

    @app.post("/api/accounts/check-live-bulk")
    def api_accounts_check_live_bulk():
        """批量查活：加入后台队列；协议 BrowserSession 指纹环境重新登录并刷新最新 AT。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多查活 500 个账号"}), 400

        account_ids: list[int] = []
        skipped: list[dict] = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            account_ids.append(acc_id)

        accounts = []
        for acc_id in account_ids:
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = str(acc.get("email") or "").strip()
            if not email:
                skipped.append({"id": acc_id, "reason": "邮箱为空"})
                continue
            accounts.append(acc)

        started = []
        busy_count = 0
        failed = []
        for acc in accounts:
            acc_id = int(acc.get("id") or 0)
            email = str(acc.get("email") or "")
            queued = live_check_service.enqueue_account_live_check(
                account_id=acc_id,
                email=email,
                trigger="manual",
                # 查活按“查套餐”同一套网络选路：
                # PLAN_CHECK_PROXY_MODE / PLAN_CHECK_PROXY / PROXY_POOL。
                # 不复用账号注册时的 proxy_used，避免旧注册出口被 CF 403 后一直失败。
                proxy=None,
            )
            if queued.get("accepted"):
                started.append({"id": acc_id, "email": email, "status": "queued"})
            elif queued.get("busy"):
                busy_count += 1
                skipped.append({"id": acc_id, "email": email, "reason": queued.get("error") or "正在查活"})
            else:
                failed.append({"id": acc_id, "email": email, "error": queued.get("error") or "入队失败"})

        return jsonify({
            "ok": True,
            "message": f"已入队 {len(started)} 个查活任务",
            "started": started,
            "started_count": len(started),
            "busy_count": busy_count,
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "queue": live_check_service.queue_settings(),
        }), 202


    @app.post("/api/accounts/check-plan")
    def api_account_check_plan():
        """把单账号套餐查询加入后台队列。Body {account_id|email, proxy?, timezone_offset_min?}"""
        data = request.get_json(silent=True) or {}
        acc_id = data.get("account_id") or data.get("id")
        email = (data.get("email") or "").strip()
        acc = None
        if acc_id is not None:
            try:
                acc = db.get_account(int(acc_id))
            except Exception:
                acc = None
        if acc is None and email:
            acc = db.get_account_by_email(email)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        token = (acc.get("access_token") or "").strip()
        if not token:
            return jsonify({"ok": False, "error": "该账号没有 access_token"}), 400
        account_id = int(acc.get("id"))
        queued = plan_check_service.enqueue_account_plan_check(
            account_id=account_id,
            email=acc.get("email") or "",
            access_token=token,
            trigger="manual",
            proxy=data.get("proxy") if "proxy" in data else None,
            timezone_offset_min=str(data.get("timezone_offset_min") or "-"),
        )
        if queued.get("busy"):
            return jsonify({"ok": False, **queued}), 409
        if not queued.get("accepted"):
            return jsonify({"ok": False, **queued}), 503
        return jsonify({"ok": True, "started": True, **queued}), 202

    @app.post("/api/accounts/check-plan-bulk")
    def api_accounts_check_plan_bulk():
        """批量把套餐查询加入统一后台队列。Body {account_ids:[...], proxy?, timezone_offset_min?}"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多查询 500 个账号"}), 400
        # 与单账号查询保持一致：未传时使用独立网络策略。
        proxy = data.get("proxy") if "proxy" in data else None
        timezone_offset_min = str(data.get("timezone_offset_min") or "-")

        items = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except Exception:
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            if not (acc.get("access_token") or "").strip():
                skipped.append({"id": acc_id, "email": acc.get("email"), "reason": "缺少 access_token"})
                continue
            items.append(acc)

        started = []
        busy = []
        failed = []
        for acc in items:
            queued = plan_check_service.enqueue_account_plan_check(
                account_id=int(acc.get("id")),
                email=acc.get("email") or "",
                access_token=acc.get("access_token") or "",
                trigger="manual_bulk",
                proxy=proxy,
                timezone_offset_min=timezone_offset_min,
            )
            item = {"id": acc.get("id"), "email": acc.get("email"), **queued}
            if queued.get("accepted"):
                started.append(item)
            elif queued.get("busy"):
                busy.append(item)
            else:
                failed.append(item)
        return jsonify({
            "ok": True,
            "started": started,
            "started_count": len(started),
            "busy": busy,
            "busy_count": len(busy),
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "skipped_count": len(skipped),
        }), 202

    @app.get("/api/extract-link/cdk")
    def api_extract_link_cdk():
        """查询当前配置或传入 CDK 的剩余次数。"""
        code = (request.args.get("code") or "").strip() or None
        provider = (request.args.get("provider") or "").strip() or None
        try:
            return jsonify({"ok": True, **extract_link_service.query_cdk(cdk=code, provider=provider)})
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    @app.get("/api/extract-link/capabilities")
    def api_extract_link_capabilities():
        return jsonify({"ok": True, "providers": extract_link_service.provider_capabilities()})

    @app.get("/api/extract-link/cdks")
    def api_extract_link_cdks():
        provider = (request.args.get("provider") or "masi").strip().lower()
        pool = (request.args.get("pool") or "").strip().lower() or None
        try:
            page = int(request.args.get("page") or 1)
            page_size = int(request.args.get("page_size") or 10)
            if page < 1:
                raise ValueError("page 必须大于等于 1")
            if page_size < 1 or page_size > 100:
                raise ValueError("page_size 必须在 1 到 100 之间")
            items = masi_cdk_pool.list_cdks(provider=provider, pool=pool)
            total = len(items)
            total_pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, total_pages)
            start = (page - 1) * page_size
            return jsonify({
                "ok": True,
                "items": items[start:start + page_size],
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": total_pages,
                "summary": masi_cdk_pool.pool_summary(provider=provider),
            })
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    @app.post("/api/extract-link/cdks/import")
    def api_extract_link_cdks_import():
        data = request.get_json(silent=True) or {}
        text = str(data.get("text") or data.get("cdks") or "")
        refresh_quota = data.get("refresh_quota", False)
        if not text.strip():
            return jsonify({"ok": False, "error": "请提供一行一个的 CDK"}), 400
        if not isinstance(refresh_quota, bool):
            return jsonify({"ok": False, "error": "refresh_quota 必须是布尔值"}), 400
        try:
            result = extract_link_service.import_masi_cdks(text, refresh_quota=refresh_quota)
            return jsonify({"ok": True, **result}), 200
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    @app.post("/api/extract-link/cdks/<cdk_id>/refresh")
    def api_extract_link_cdk_refresh(cdk_id: str):
        try:
            result = extract_link_service.refresh_masi_cdk(cdk_id)
            return jsonify(result), 200 if result.get("ok") else 502
        except KeyError:
            return jsonify({"ok": False, "error": "CDK 不存在"}), 404
        except masi_cdk_pool.CdkLeaseBusy as exc:
            return jsonify({"ok": False, "error": str(exc)}), 409
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    @app.post("/api/extract-link/cdks/refresh")
    def api_extract_link_cdks_refresh():
        data = request.get_json(silent=True) or {}
        ids = data.get("ids")
        pool = str(data.get("pool") or "").strip().lower() or None
        if ids is not None and not isinstance(ids, list):
            return jsonify({"ok": False, "error": "ids 必须是数组"}), 400
        try:
            result = extract_link_service.refresh_masi_cdks(ids=ids, pool=pool)
            return jsonify({"ok": True, **result})
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    @app.post("/api/extract-link/cdks/enablement")
    def api_extract_link_cdks_enablement():
        data = request.get_json(silent=True) or {}
        enabled = data.get("enabled")
        scope = str(data.get("scope") or "").strip().lower()
        if not isinstance(enabled, bool):
            return jsonify({"ok": False, "error": "enabled 必须是布尔值"}), 400
        if scope not in {"ids", "pool"}:
            return jsonify({"ok": False, "error": "scope 仅支持 ids / pool"}), 400
        ids = data.get("ids")
        pool = str(data.get("pool") or "").strip().lower() or None
        if scope == "ids":
            if not isinstance(ids, list) or not ids:
                return jsonify({"ok": False, "error": "scope=ids 时 ids 必须是非空数组"}), 400
            if len(ids) > 100:
                return jsonify({"ok": False, "error": "单次最多修改 100 条 CDK"}), 400
            if any(not isinstance(value, str) or not value.strip() for value in ids):
                return jsonify({"ok": False, "error": "ids 中的每一项必须是非空字符串"}), 400
            if pool is not None:
                return jsonify({"ok": False, "error": "scope=ids 时不得提供 pool"}), 400
        else:
            if ids is not None:
                return jsonify({"ok": False, "error": "scope=pool 时不得提供 ids"}), 400
            if pool not in masi_cdk_pool.VALID_POOLS:
                return jsonify({"ok": False, "error": "scope=pool 时 pool 仅支持 selectable / exhausted"}), 400
        try:
            result = masi_cdk_pool.set_enablement(
                enabled=enabled,
                ids=ids if scope == "ids" else None,
                pool=pool if scope == "pool" else None,
            )
            return jsonify({"ok": True, **result})
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    @app.delete("/api/extract-link/cdks/<cdk_id>")
    def api_extract_link_cdk_delete(cdk_id: str):
        try:
            deleted = masi_cdk_pool.delete_cdk(cdk_id, active_ids=db.active_extract_cdk_ids())
            if not deleted:
                return jsonify({"ok": False, "error": "CDK 不存在"}), 404
            return jsonify({"ok": True, "deleted": True, "id": cdk_id})
        except masi_cdk_pool.CdkLeaseBusy as exc:
            return jsonify({"ok": False, "error": str(exc)}), 409
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    def _is_extract_eligible(acc: dict) -> bool:
        return bool(build_account_status_contract(acc)["plan_capabilities"]["is_eligible"])

    @app.post("/api/accounts/extract-link")
    def api_account_extract_link():
        """单账号提链。Body {account_id|id, link_type?, provider?, update_mode?, cdk?}。"""
        data = request.get_json(silent=True) or {}
        acc_id = data.get("account_id") or data.get("id")
        try:
            acc = db.get_account(int(acc_id))
        except Exception:
            acc = None
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        if not _is_extract_eligible(acc):
            return jsonify({"ok": False, "error": "仅支持 free(可Plus试用) 账号提链；请先查询套餐确认资格"}), 400
        token = (acc.get("access_token") or "").strip()
        if not token:
            return jsonify({"ok": False, "error": "该账号没有 access_token"}), 400
        try:
            queued = extract_link_service.enqueue_account_extract(
                account_id=int(acc.get("id")),
                email=acc.get("email") or "",
                access_token=token,
                trigger="manual",
                link_type=data.get("link_type"),
                cdk=data.get("cdk"),
                provider=data.get("provider"),
                update_mode=data.get("update_mode"),
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400
        if queued.get("busy"):
            return jsonify({"ok": False, **queued}), 409
        if not queued.get("accepted"):
            return jsonify({"ok": False, **queued}), 503
        return jsonify({"ok": True, "started": True, **{k: v for k, v in queued.items() if k != "future"}}), 202

    @app.post("/api/accounts/extract-link-bulk")
    def api_accounts_extract_link_bulk():
        """批量提链。Body {account_ids:[...], link_type?, provider?, update_mode?, cdk?}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多提链 500 个账号"}), 400

        try:
            extract_link_service.resolve_route(
                link_type=data.get("link_type"),
                provider=data.get("provider"),
                update_mode=data.get("update_mode"),
                cdk=data.get("cdk"),
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

        started = []
        busy = []
        failed = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except Exception:
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = acc.get("email")
            if not _is_extract_eligible(acc):
                skipped.append({"id": acc_id, "email": email, "reason": "不是 free(可Plus试用)"})
                continue
            token = (acc.get("access_token") or "").strip()
            if not token:
                skipped.append({"id": acc_id, "email": email, "reason": "缺少 access_token"})
                continue
            try:
                queued = extract_link_service.enqueue_account_extract(
                    account_id=acc_id,
                    email=email or "",
                    access_token=token,
                    trigger="manual_bulk",
                    link_type=data.get("link_type"),
                    cdk=data.get("cdk"),
                    provider=data.get("provider"),
                    update_mode=data.get("update_mode"),
                )
            except Exception as exc:
                failed.append({"id": acc_id, "email": email, "error": f"{type(exc).__name__}: {exc}"})
                continue
            item = {"id": acc_id, "email": email, **{k: v for k, v in queued.items() if k != "future"}}
            if queued.get("accepted"):
                started.append(item)
            elif queued.get("busy"):
                busy.append(item)
            else:
                failed.append(item)
        return jsonify({
            "ok": True,
            "started": started,
            "started_count": len(started),
            "busy": busy,
            "busy_count": len(busy),
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "skipped_count": len(skipped),
        }), 202

    @app.post("/api/accounts/extract-link-resume")
    def api_account_extract_link_resume():
        """恢复轮询账号已保存的 Masi Job；不会创建新 Job。"""
        data = request.get_json(silent=True) or {}
        acc_id = data.get("account_id") or data.get("id")
        try:
            acc = db.get_account(int(acc_id))
        except Exception:
            acc = None
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        try:
            queued = extract_link_service.enqueue_existing_masi_job_poll(
                account_id=int(acc["id"]),
                email=acc.get("email") or "",
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400
        if queued.get("busy"):
            return jsonify({"ok": False, **queued}), 409
        if not queued.get("accepted"):
            return jsonify({"ok": False, **queued}), 503
        return jsonify({"ok": True, "started": True, "message": "已恢复原 Masi Job 轮询"}), 202

    @app.post("/api/accounts/download-cpa-bulk")
    def api_accounts_download_cpa_bulk():
        """
        从账号列表选中的账号直接到 CPA auth-files 下载 Codex CPA JSON，并打包为 ZIP。
        Body: {"account_ids": [1,2,...]} 或 {"ids": [...]}
        """
        import io
        import json as _json
        import zipfile
        from datetime import datetime as _dt
        from core.codex_oauth import download_cpa_codex_auth_text, list_cpa_codex_auth_files

        data = request.get_json(silent=True) or {}
        if not data and request.form:
            ids_text = (request.form.get("account_ids") or request.form.get("ids") or "").strip()
            try:
                ids = _json.loads(ids_text) if ids_text else []
            except Exception:
                ids = [x.strip() for x in ids_text.split(",") if x.strip()]
        else:
            ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 1000:
            return jsonify({"ok": False, "error": "单次最多下载 1000 个账号"}), 400

        try:
            cpa_files = list_cpa_codex_auth_files()
        except Exception as exc:
            return jsonify({"ok": False, "error": f"读取 CPA auth-files 失败: {type(exc).__name__}: {exc}"}), 502

        def _match_cpa_file(email: str, local_filename: str = "") -> dict | None:
            """在已缓存的 CPA 文件列表中匹配，避免每个账号都重新请求 auth-files。"""
            email_l = str(email or "").strip().lower()
            local_name_l = str(local_filename or "").strip().lower()
            local_stem_l = local_name_l[:-5] if local_name_l.endswith(".json") else local_name_l

            def score(item: dict) -> int:
                name_l = str(item.get("name") or "").lower()
                item_email_l = str(item.get("email") or "").lower()
                s = 0
                if local_name_l and name_l == local_name_l:
                    s = max(s, 100)
                if local_stem_l and name_l.startswith(local_stem_l):
                    s = max(s, 80)
                if email_l and item_email_l == email_l:
                    s = max(s, 70)
                if email_l and email_l in name_l:
                    s = max(s, 60)
                if local_stem_l.endswith("-cpa-callback"):
                    base = local_stem_l[:-len("-cpa-callback")]
                    if base and name_l.startswith(base + "-"):
                        s = max(s, 75)
                return s

            ranked = sorted(((score(item), item) for item in cpa_files), key=lambda x: x[0], reverse=True)
            return ranked[0][1] if ranked and ranked[0][0] > 0 else None

        # 建立 email -> 本地 codex 文件名索引；有本地文件名时传给 CPA 匹配逻辑可提升命中率。
        local_by_email: dict[str, str] = {}
        try:
            for item in db.list_codex_accounts():
                email_key = str(item.get("email") or "").strip().lower()
                fname = str(item.get("filename") or "").strip()
                if email_key and fname and email_key not in local_by_email:
                    local_by_email[email_key] = fname
        except Exception:
            local_by_email = {}

        errors = []
        added = []
        used_names = set()
        seen_ids = set()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for raw_id in ids:
                try:
                    acc_id = int(raw_id)
                except (TypeError, ValueError):
                    errors.append({"id": raw_id, "error": "ID 非法"})
                    continue
                if acc_id in seen_ids:
                    continue
                seen_ids.add(acc_id)

                acc = db.get_account(acc_id)
                if not acc:
                    errors.append({"id": acc_id, "error": "账号不存在"})
                    continue
                email = str(acc.get("email") or "").strip()
                if not email:
                    errors.append({"id": acc_id, "error": "账号缺少 email"})
                    continue

                local_filename = local_by_email.get(email.lower(), "")
                try:
                    meta = _match_cpa_file(email=email, local_filename=local_filename)
                    cpa_name_hint = str((meta or {}).get("name") or "").strip()
                    if not cpa_name_hint:
                        raise RuntimeError(f"[Codex][CPA] 未在 CPA auth-files 中找到匹配的 Codex 凭证: {email}")
                    cpa_text, cpa_name, meta = download_cpa_codex_auth_text(
                        cpa_name=cpa_name_hint,
                    )
                    arcname = cpa_name
                    if arcname in used_names:
                        stem, dot, ext = arcname.rpartition(".")
                        arcname = f"{stem or arcname}-{len(used_names)+1}{dot}{ext}" if dot else f"{arcname}-{len(used_names)+1}"
                    used_names.add(arcname)
                    zf.writestr(arcname, cpa_text)
                    added.append({
                        "id": acc_id,
                        "email": email,
                        "local_filename": local_filename,
                        "cpa_filename": cpa_name,
                        "cpa_meta": meta,
                    })
                    if local_filename:
                        try:
                            db.mark_codex_exported(local_filename)
                        except Exception:
                            pass
                except Exception as exc:
                    errors.append({"id": acc_id, "email": email, "error": f"{type(exc).__name__}: {exc}"})

            manifest = {
                "exported_at": _dt.now().isoformat(timespec="seconds"),
                "source": "accounts-cpa",
                "count": len(added),
                "files": added,
                "errors": errors,
            }
            zf.writestr("manifest.json", _json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

        if not added:
            return jsonify({"ok": False, "error": "没有成功从 CPA 下载任何凭证", "errors": errors}), 502
        now = _dt.now()
        dl_name = f"accounts-cpa-bulk-{now.strftime('%Y%m%d-%H%M%S')}.zip"
        buf.seek(0)
        zip_bytes = buf.getvalue()
        if isinstance(data, dict) and data.get("prepare"):
            download_id = _put_prepared_download(zip_bytes, dl_name, "application/zip")
            return jsonify({
                "ok": True,
                "prepared": True,
                "download_id": download_id,
                "download_url": f"/api/downloads/{download_id}",
                "filename": dl_name,
                "added_count": len(added),
                "error_count": len(errors),
            })
        return Response(
            zip_bytes,
            mimetype="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{dl_name}"',
                "Content-Length": str(len(zip_bytes)),
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
                "X-Download-Options": "noopen",
            },
        )

    # ----------------------------------------------------------
    # 邮箱池
    # ----------------------------------------------------------
    @app.get("/api/outlook")
    def api_outlook():
        status, status_error = _pool_status_arg(request.args.get("status"))
        if status_error:
            return jsonify({"ok": False, "error": status_error}), 400
        limit = request.args.get("limit", default=500, type=int)
        source = _pool_source_arg()
        q = str(request.args.get("q", default="") or "").strip()
        paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        fetch_limit = 1_000_000 if (paged or q) else limit
        if source == "all":
            rows = []
            rows += _with_pool_source(db.list_outlook_pool(status=status, limit=fetch_limit), "outlook")
            rows += _with_pool_source(db.list_generic_api_email_pool(status=status, limit=fetch_limit), "generic_api")
            rows += _with_pool_source(db.list_domain_email_pool(status=status, limit=fetch_limit), "cloudflare_domain")
            rows += _with_pool_source(db.list_icloud_email_pool(status=status, limit=fetch_limit), "icloud")
            rows += _with_pool_source(db.list_mailcom_email_pool(status=status, limit=fetch_limit), "mailcom")
            rows = sorted(rows, key=lambda x: str(x.get("created_at") or x.get("imported_at") or x.get("used_at") or ""), reverse=True)
        elif source == "generic_api":
            rows = _with_pool_source(db.list_generic_api_email_pool(status=status, limit=fetch_limit), "generic_api")
        elif source == "cloudflare_domain":
            rows = _with_pool_source(db.list_domain_email_pool(status=status, limit=fetch_limit), "cloudflare_domain")
        elif source == "icloud":
            rows = _with_pool_source(db.list_icloud_email_pool(status=status, limit=fetch_limit), "icloud")
        elif source == "mailcom":
            rows = _with_pool_source(db.list_mailcom_email_pool(status=status, limit=fetch_limit), "mailcom")
        else:
            rows = _with_pool_source(db.list_outlook_pool(status=status, limit=fetch_limit), "outlook")
        if q:
            rows = [r for r in rows if _matches_email_query(r, q)]
        if paged or page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            return jsonify(_paginate_items(rows, page=page, page_size=page_size))
        return jsonify(rows[:limit])

    @app.post("/api/outlook/import")
    def api_outlook_import():
        """
        粘贴文本导入邮箱素材。
        Outlook：email----password----clientId----refreshToken
        通用 API：email----code_url
        iCloud 已注册账号：email----accessToken
        分隔符兼容 ---- 与 ====。
        """
        data = request.get_json(silent=True) or {}
        source = (data.get("source") or data.get("type") or "").strip()
        if source not in ("outlook", "generic_api", "icloud", "mailcom"):
            return jsonify({"ok": False, "error": "导入时请选择具体类型：Outlook、通用 API、iCloud 或 mail.com"}), 400
        text = data.get("text") or ""
        as_registered = bool(data.get("as_registered", False))
        reactivate_existing = bool(data.get("reactivate_existing", False))
        records = []
        errors = []
        parsed = 0
        for line_no, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parsed += 1
            separator = "----" if "----" in line else "====" if "====" in line else None
            parts = (
                line.split(separator, 1)
                if separator and (source == "mailcom" or (source == "icloud" and as_registered))
                else line.split(separator)
                if separator
                else [line]
            )
            parts = [p.strip() for p in parts]
            if source == "mailcom":
                if as_registered:
                    errors.append({"line": line_no, "reason": "mail.com 账号仅用于收取验证码，不能作为已注册账号导入"})
                    continue
                if len(parts) != 2 or not parts[0] or not parts[1]:
                    errors.append({"line": line_no, "reason": "需填写邮箱----密码"})
                    continue
                records.append({"email": parts[0], "password": parts[1]})
                continue
            if source == "icloud":
                if not as_registered:
                    records.append({"email": line})
                    continue
                if len(parts) != 2 or not parts[0] or not parts[1] or separator in parts[1]:
                    errors.append({"line": line_no, "reason": "需填写邮箱----AT"})
                    continue
                normalized, reason = db.normalize_registered_icloud_import_record({
                    "email": parts[0],
                    "access_token": parts[1],
                })
                if normalized is None:
                    errors.append({"line": line_no, "reason": reason or "导入数据无效"})
                    continue
                records.append(normalized)
                continue
            if source == "generic_api":
                if len(parts) < 2:
                    continue
                records.append({
                    "email": parts[0],
                    "code_url": parts[1],
                    "access_token": parts[2] if len(parts) > 2 else "",
                    "totp_secret": parts[3] if len(parts) > 3 else "",
                })
                continue
            if len(parts) < 4:
                continue
            records.append({
                "email": parts[0],
                "password": parts[1],
                "client_id": parts[2],
                "refresh_token": parts[3],
                "access_token": parts[4] if len(parts) > 4 else "",
                "totp_secret": parts[5] if len(parts) > 5 else "",
            })
        if not records and not (source == "icloud" and as_registered and parsed):
            need = "2 段：邮箱----密码" if source == "mailcom" else "2 段：邮箱----AT" if source == "icloud" and as_registered else "每行一个 iCloud 邮箱地址" if source == "icloud" else ("2 段：邮箱----取码地址" if source == "generic_api" else "4 段：email----password----clientId----refreshToken")
            return jsonify({"ok": False, "error": f"未解析到有效邮箱行（需 {need}，---- 或 ==== 分隔）"}), 400
        inserted = db_skipped = 0
        db_errors = []
        if as_registered:
            if records:
                inserted, db_skipped = db.import_registered_email_accounts(records, source=source)
        elif source == "icloud":
            inserted, db_skipped = db.import_icloud_emails(records, reactivate_existing=reactivate_existing)
        elif source == "generic_api":
            inserted, db_skipped = db.import_generic_api_emails(records, reactivate_existing=reactivate_existing)
        elif source == "mailcom":
            inserted, db_skipped, db_errors = db.import_mailcom_emails(
                records,
                reactivate_existing=reactivate_existing,
            )
            from core.mailcom_alias_pool_service import enqueue_parent_snapshot_sync
            for record in records:
                enqueue_parent_snapshot_sync(str(record.get("email") or ""))
            for item in db_errors:
                errors.append(item)
        else:
            inserted, db_skipped = db.import_outlook_accounts(records, reactivate_existing=reactivate_existing)
        result = {
            "ok": True,
            "inserted": inserted,
            "skipped": len(errors) + db_skipped - (len(db_errors) if source == "mailcom" else 0),
            "parsed": parsed,
            "as_registered": as_registered,
            "reactivate_existing": reactivate_existing,
        }
        if errors:
            result["errors"] = errors
        return jsonify(result)

    @app.get("/api/mailcom")
    def api_mailcom_pool():
        """mail.com 母号一级列表；凭据不出此边界，母号邮箱地址可展示。"""
        limit = request.args.get("limit", default=500, type=int)
        from core.mailcom_alias_pool_service import queue_state
        return jsonify({"ok": True, "items": db.list_mailcom_parents(limit=limit), "summary": db.mailcom_pool_summary(), "queue": queue_state()})

    @app.get("/api/mailcom/domains")
    def api_mailcom_domains():
        rows = db.list_mailcom_alias_domains()
        summary = db.mailcom_alias_domain_summary()
        return jsonify({"ok": True, "items": rows, "summary": summary})

    @app.patch("/api/mailcom/domains/<path:domain>")
    def api_mailcom_domain_update(domain: str):
        data = request.get_json(silent=True) or {}
        enabled = data.get("enabled")
        if not isinstance(enabled, bool):
            return jsonify({"ok": False, "error": "enabled 必须是 JSON 布尔值"}), 400
        try:
            item = db.set_mailcom_alias_domain_enabled(domain, enabled)
        except KeyError:
            return jsonify({"ok": False, "error": "mail.com 别名域名不在固定目录中"}), 404
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "item": item, "summary": db.mailcom_alias_domain_summary()})

    @app.post("/api/mailcom/domains/bulk-status")
    def api_mailcom_domains_bulk_status():
        data = request.get_json(silent=True) or {}
        enabled = data.get("enabled")
        if not isinstance(enabled, bool):
            return jsonify({"ok": False, "error": "enabled 必须是 JSON 布尔值"}), 400
        return jsonify({"ok": True, "summary": db.set_all_mailcom_alias_domains_enabled(enabled)})

    @app.get("/api/mailcom/aliases")
    def api_mailcom_aliases():
        """返回别名生命周期摘要，不暴露母号凭据或邮件内容。"""
        from core.mailcom_alias_service import MAX_ACTIVE_ALIASES

        status, status_error = _pool_status_arg(request.args.get("status"))
        if status_error:
            return jsonify({"ok": False, "error": status_error}), 400
        parent_id = request.args.get("parent_id", type=int)
        parent = db.get_mailcom_parent_by_id(parent_id, include_secrets=True) if parent_id else None
        if parent_id and parent is None:
            return jsonify({"ok": False, "error": "mail.com 母号不存在"}), 404
        limit = max(1, min(request.args.get("limit", default=500, type=int) or 500, 2000))
        parent_email = str((parent or {}).get("email") or "") or None
        summary = db.mailcom_alias_summary(parent_email)
        # 本地记录仅用于展示和审计，创建时仍以 settings 地址列表的远端计数为准。
        summary["remote_active_alias_limit"] = MAX_ACTIVE_ALIASES
        items = db.list_mailcom_aliases(parent_email=parent_email, status=status, limit=limit)
        legacy_plan_categories = {
            "eligible_for_delete": "free_no_trial",
            "trial_eligible": "free_trial_eligible",
            "non_free": "paid",
        }
        for item in items:
            raw_category = str(item.get("plan_result_class") or "unknown").strip().lower()
            category = raw_category if raw_category in PLAN_CATEGORY_CODES else legacy_plan_categories.get(raw_category, "unknown")
            item["plan_category_code"] = category
            item["cleanup_capabilities"] = mailcom_cleanup_capabilities(
                item.get("cleanup_status"), plan_category=category,
            )
        return jsonify({
            "ok": True,
            "items": items,
            "summary": summary,
        })

    @app.post("/api/mailcom/import")
    def api_mailcom_import():
        """批量导入 mail.com ``email----password``，单行错误不影响其他行。"""
        data = request.get_json(silent=True) or {}
        reactivate_existing = bool(data.get("reactivate_existing", False))
        text = str(data.get("text") or "")
        records: list[dict] = []
        errors: list[dict] = []
        parsed = 0
        for line_no, raw_line in enumerate(text.splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parsed += 1
            separator = "----" if "----" in line else "====" if "====" in line else None
            if not separator:
                errors.append({"line": line_no, "reason": "需填写邮箱----密码"})
                continue
            email, password = (part.strip() for part in line.split(separator, 1))
            if not email or not password:
                errors.append({"line": line_no, "reason": "邮箱或密码为空"})
                continue
            records.append({"email": email, "password": password})
        if not records:
            return jsonify({"ok": False, "error": "未解析到有效 mail.com 邮箱（格式：email----password）", "errors": errors}), 400
        inserted, skipped, db_errors = db.import_mailcom_emails(
            records,
            reactivate_existing=reactivate_existing,
        )
        from core.mailcom_alias_pool_service import enqueue_parent_snapshot_sync
        sync = [enqueue_parent_snapshot_sync(record["email"]) for record in records]
        errors.extend(db_errors)
        return jsonify({"ok": True, "inserted": inserted, "skipped": skipped + len(errors) - len(db_errors), "parsed": parsed, "errors": errors, "sync": sync, "reactivate_existing": reactivate_existing})

    @app.post("/api/mailcom/config")
    def api_mailcom_config():
        """保存或更新单条 mail.com 凭据；更新密码会清除该条 AT。"""
        data = request.get_json(silent=True) or {}
        email = str(data.get("email") or "").strip()
        password = str(data.get("password") or "")
        if "@" not in email or not password:
            return jsonify({"ok": False, "error": "mail.com 邮箱或密码无效"}), 400
        inserted, skipped, errors = db.import_mailcom_emails(
            [{"email": email, "password": password}],
            update_existing=True,
            reactivate_existing=bool(data.get("reactivate_existing", False)),
        )
        if not inserted:
            return jsonify({"ok": False, "error": (errors or [{"reason": "保存失败"}])[0]["reason"]}), 400
        row = db.get_mailcom_email_by_email(email)
        from core.mailcom_alias_pool_service import enqueue_parent_snapshot_sync
        return jsonify({"ok": True, "saved": True, "item": row, "skipped": skipped, "sync": enqueue_parent_snapshot_sync(email)})

    @app.post("/api/mailcom/parents/<int:parent_id>/sync")
    def api_mailcom_parent_sync(parent_id: int):
        parent = db.get_mailcom_parent_by_id(parent_id, include_secrets=True)
        if not parent:
            return jsonify({"ok": False, "error": "mail.com 母号不存在"}), 404
        from core.mailcom_alias_pool_service import enqueue_parent_snapshot_sync
        result = enqueue_parent_snapshot_sync(str(parent.get("email") or ""))
        return jsonify({"ok": True, **result}), 202 if result.get("accepted") else 200

    @app.post("/api/mailcom/parents/<int:parent_id>/replenish")
    def api_mailcom_parent_replenish(parent_id: int):
        parent = db.get_mailcom_parent_by_id(parent_id, include_secrets=True)
        if not parent:
            return jsonify({"ok": False, "error": "mail.com 母号不存在", "action": "replenish"}), 404
        from core.mailcom_alias_pool_service import enqueue_parent_replenish
        result = enqueue_parent_replenish(str(parent.get("email") or ""))
        return jsonify({"ok": True, **result}), 202 if result.get("accepted") else 200

    @app.post("/api/mailcom/parents/<int:parent_id>/history-refresh")
    def api_mailcom_parent_history_refresh(parent_id: int):
        """只读异步刷新母号历史容量；后台不会创建或删除别名。"""
        parent = db.get_mailcom_parent_by_id(parent_id, include_secrets=True)
        if not parent:
            return jsonify({"ok": False, "error": "mail.com 母号不存在"}), 404
        from core.mailcom_alias_pool_service import enqueue_parent_history_refresh

        result = enqueue_parent_history_refresh(str(parent.get("email") or ""))
        # accepted/busy 都是已被队列正确处理的异步结果，统一使用 202。
        if result.get("accepted") or result.get("busy"):
            return jsonify({"ok": True, **result}), 202
        return jsonify({"ok": False, **result}), 400

    @app.post("/api/mailcom/parents/<int:parent_id>/disable")
    def api_mailcom_parent_disable(parent_id: int):
        data = request.get_json(silent=True) or {}
        parent = db.get_mailcom_parent_by_id(parent_id, include_secrets=True)
        if not parent:
            return jsonify({"ok": False, "error": "parent_missing"}), 404
        try:
            result = db.disable_mailcom_parent(
                str(parent.get("email") or ""),
                reason=data.get("reason") or data.get("note"),
                actor=data.get("actor") or "webui",
            )
        except db.EmailPoolLifecycleError as exc:
            return _lifecycle_error_response(exc)
        return jsonify({"ok": True, **result})

    @app.post("/api/mailcom/parents/<int:parent_id>/delete")
    def api_mailcom_parent_delete(parent_id: int):
        data = request.get_json(silent=True) or {}
        parent = db.get_mailcom_parent_by_id(parent_id, include_secrets=True)
        if not parent:
            return jsonify({"ok": False, "error": "parent_missing"}), 404
        try:
            result = db.delete_mailcom_parent(
                str(parent.get("email") or ""),
                reason=data.get("reason") or data.get("note"),
                actor=data.get("actor") or "webui",
            )
        except db.EmailPoolLifecycleError as exc:
            return _lifecycle_error_response(exc)
        return jsonify({"ok": True, **result})

    @app.post("/api/mailcom/aliases/delete")
    def api_mailcom_alias_delete():
        data = request.get_json(silent=True) or {}
        alias_email = str(data.get("alias_email") or data.get("email") or "").strip()
        if not alias_email:
            return jsonify({"ok": False, "error": "alias_email 为空"}), 400
        from core.mailcom_alias_pool_service import delete_alias_now
        result = delete_alias_now(
            alias_email,
            force=bool(data.get("force", False)),
            reason=data.get("reason") or data.get("note"),
        )
        status_code = int(result.pop("status", 200))
        return jsonify(result), status_code

    @app.post("/api/outlook/status")
    def api_outlook_status():
        """手动改邮箱状态；终态恢复必须提供明确原因。"""
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 或 status 非法"}), 400
        status = (data.get("status") or "").strip()
        try:
            status = validate_status(status)
        except EmailPoolStatusError:
            return jsonify({"ok": False, "error": "email 或 status 非法"}), 400
        source = (data.get("source") or _pool_source_arg()).strip()
        if source == "all":
            source = "outlook"
        if source not in {"outlook", "generic_api", "cloudflare_domain", "icloud", "mailcom"}:
            return jsonify({"ok": False, "error": "source 非法"}), 400
        try:
            if source == "mailcom" and db.get_mailcom_alias_internal(email) is None and status != "available":
                changed = db.release_mailcom_email(email, status=status, note=data.get("reason") or data.get("note"))
                if not changed:
                    raise db.EmailPoolLifecycleError("email_not_found", "邮箱不存在")
                result = {"email": email, "status": status, "previous_status": None}
            else:
                result = db.set_email_pool_status(
                    email,
                    status,
                    source=source,
                    reason=data.get("reason") or data.get("note"),
                    actor=data.get("actor") or "webui",
                )
        except db.EmailPoolLifecycleError as exc:
            return _lifecycle_error_response(exc)
        except (ValueError, EmailPoolStatusError) as exc:
            return jsonify({"ok": False, "error": "status_transition_invalid", "message": str(exc)}), 409
        return jsonify({"ok": True, **result})

    @app.post("/api/outlook/status-bulk")
    def api_outlook_status_bulk():
        """批量修改邮箱状态。Body {items:[{email,source}], status, note?}。"""
        data = request.get_json(silent=True) or {}
        items = data.get("items") or data.get("emails") or []
        status = (data.get("status") or "").strip()
        note = data.get("note")
        default_source = (data.get("source") or _pool_source_arg()).strip()
        try:
            status = validate_status(status)
        except EmailPoolStatusError:
            return jsonify({"ok": False, "error": "status 非法"}), 400
        if not isinstance(items, list) or not items:
            return jsonify({"ok": False, "error": "items/emails 必须是非空数组"}), 400
        if len(items) > 5000:
            return jsonify({"ok": False, "error": "单次最多操作 5000 个邮箱"}), 400

        updated = []
        skipped = []
        seen = set()
        for raw_item in items:
            if isinstance(raw_item, dict):
                email = (str(raw_item.get("email") or "")).strip()
                item_source = (raw_item.get("source") or default_source or "outlook").strip()
            else:
                email = (str(raw_item or "")).strip()
                item_source = default_source
            if item_source == "all":
                item_source = "outlook"
            if item_source not in {"outlook", "generic_api", "cloudflare_domain", "icloud", "mailcom"}:
                skipped.append({"email": email, "source": item_source, "reason": "source 非法"})
                continue
            key = f"{item_source}:{email.lower()}"
            if not email:
                skipped.append({"email": raw_item, "reason": "邮箱为空"})
                continue
            if key in seen:
                continue
            seen.add(key)
            try:
                result = db.set_email_pool_status(
                    email,
                    status,
                    source=item_source,
                    reason=note,
                    actor=data.get("actor") or "webui",
                )
                updated.append({"email": email, "source": item_source, **result})
            except db.EmailPoolLifecycleError as exc:
                skipped.append({"email": email, "source": item_source, "reason": exc.code, "message": str(exc), **exc.details})
            except Exception as exc:
                skipped.append({"email": email, "source": item_source, "reason": f"{type(exc).__name__}: {exc}"})
        return jsonify({
            "ok": True,
            "updated": updated,
            "updated_count": len(updated),
            "skipped": skipped,
        })

    @app.post("/api/outlook/delete")
    def api_outlook_delete():
        """从邮箱池物理删除非 mail.com 条目；mail.com 别名必须从母号管理删除。"""
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        source = (data.get("source") or _pool_source_arg()).strip()
        if source == "all":
            source = "outlook"
        if source == "mailcom":
            return jsonify({
                "ok": False,
                "error": "mailcom_alias_management_required",
                "message": "mail.com 别名只能在母号管理中删除，只能在邮箱池中停用",
            }), 409
        if source not in {"outlook", "generic_api", "cloudflare_domain", "icloud"}:
            return jsonify({"ok": False, "error": "source_invalid"}), 400
        try:
            result = db.delete_email_pool_entry(
                email,
                source=source,
                force=bool(data.get("force", False)),
                reason=data.get("reason") or data.get("note"),
                actor=data.get("actor") or "webui",
            )
        except db.EmailPoolLifecycleError as exc:
            return _lifecycle_error_response(exc)
        return jsonify({"ok": True, **result})

    @app.post("/api/outlook/delete-bulk")
    def api_outlook_delete_bulk():
        """从邮箱池批量彻底删除邮箱：body {emails: [...]}。"""
        data = request.get_json(silent=True) or {}
        source = _pool_source_arg()
        force = bool(data.get("force", False))
        reason = data.get("reason") or data.get("note")
        emails = data.get("items") or data.get("emails") or []
        if not isinstance(emails, list) or not emails:
            return jsonify({"ok": False, "error": "emails/items 必须是非空数组"}), 400
        if len(emails) > 5000:
            return jsonify({"ok": False, "error": "单次最多删除 5000 个邮箱"}), 400

        deleted: list[str] = []
        skipped: list[dict] = []
        seen: set[str] = set()
        for raw_item in emails:
            if isinstance(raw_item, dict):
                email = (str(raw_item.get("email") or "")).strip()
                item_source = (raw_item.get("source") or source or "outlook").strip()
            else:
                email = (str(raw_item or "")).strip()
                item_source = source
            if item_source == "all":
                item_source = "outlook"
            key = f"{item_source}:{email.lower()}"
            if not email:
                skipped.append({"email": raw_item, "reason": "邮箱为空"})
                continue
            if key in seen:
                continue
            seen.add(key)
            if item_source == "mailcom":
                skipped.append({"email": email, "source": item_source, "reason": "mailcom_alias_management_required", "message": "mail.com 别名只能在母号管理中删除"})
                continue
            else:
                try:
                    result = db.delete_email_pool_entry(
                        email,
                        source=item_source,
                        force=bool(raw_item.get("force", force)) if isinstance(raw_item, dict) else force,
                        reason=(raw_item.get("reason") or reason) if isinstance(raw_item, dict) else reason,
                        actor=data.get("actor") or "webui",
                    )
                    deleted.append({"email": email, "source": item_source, **result})
                except db.EmailPoolLifecycleError as exc:
                    skipped.append({"email": email, "source": item_source, "reason": exc.code, "message": str(exc), **exc.details})

        return jsonify({
            "ok": True,
            "deleted": deleted,
            "deleted_count": len(deleted),
            "skipped": skipped,
        })

    # ----------------------------------------------------------
    # 域名邮箱池（Cloudflare 域名邮箱模式）
    # ----------------------------------------------------------
    @app.get("/api/domain-pool")
    def api_domain_pool():
        status, status_error = _pool_status_arg(request.args.get("status"))
        if status_error:
            return jsonify({"ok": False, "error": status_error}), 400
        limit = request.args.get("limit", default=500, type=int)
        return jsonify(db.list_domain_email_pool(status=status, limit=limit))

    @app.post("/api/domain-pool/status")
    def api_domain_pool_status():
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 或 status 非法"}), 400
        status = (data.get("status") or "").strip()
        try:
            status = validate_status(status)
        except EmailPoolStatusError:
            return jsonify({"ok": False, "error": "email 或 status 非法"}), 400
        try:
            result = db.set_email_pool_status(
                email,
                status,
                source="cloudflare_domain",
                reason=data.get("reason") or data.get("note"),
                actor=data.get("actor") or "webui",
            )
        except db.EmailPoolLifecycleError as exc:
            return _lifecycle_error_response(exc)
        except (ValueError, EmailPoolStatusError) as exc:
            return jsonify({"ok": False, "error": "status_transition_invalid", "message": str(exc)}), 409
        return jsonify({"ok": True, **result})

    @app.post("/api/domain-pool/delete")
    def api_domain_pool_delete():
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        try:
            result = db.delete_email_pool_entry(
                email,
                source="cloudflare_domain",
                force=bool(data.get("force", False)),
                reason=data.get("reason") or data.get("note"),
                actor=data.get("actor") or "webui",
            )
        except db.EmailPoolLifecycleError as exc:
            return _lifecycle_error_response(exc)
        return jsonify({"ok": True, **result})

    # ----------------------------------------------------------
    # Codex 授权账号（CPA 兼容凭证）
    # ----------------------------------------------------------
    @app.get("/api/codex")
    def api_codex_list():
        rows = db.list_codex_accounts(
            archived=str(request.args.get("archived", default="0") or "0").lower(),
            date_from=str(request.args.get("date_from", default="") or "").strip() or None,
            date_to=str(request.args.get("date_to", default="") or "").strip() or None,
        )
        q = str(request.args.get("q", default="") or "").strip()
        if q:
            rows = [r for r in rows if _matches_query(r, q)]
        export_status = str(request.args.get("export_status", default="all") or "all").strip().lower()
        if export_status not in {"all", "pending", "exported"}:
            return jsonify({"ok": False, "error": "export_status 仅支持 all、pending、exported"}), 400
        if export_status == "pending":
            rows = [r for r in rows if int(r.get("exported_count") or 0) == 0]
        elif export_status == "exported":
            rows = [r for r in rows if int(r.get("exported_count") or 0) > 0]
        limit = request.args.get("limit", default=500, type=int)
        paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        if paged or page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            result = _paginate_items(rows, page=page, page_size=page_size)
            result["accounts"] = result.pop("items")
            result["summary"] = db.codex_accounts_summary()
            return jsonify(result)
        return jsonify({
            "summary": db.codex_accounts_summary(),
            "accounts": rows[:limit],
        })

    @app.post("/api/codex/archive")
    def api_codex_archive():
        """归档/取消归档一条 Codex 授权凭证。Body {filename, archived}。"""
        data = request.get_json(silent=True) or {}
        filename = str(data.get("filename") or "").strip()
        archived = bool(data.get("archived", True))
        if not filename:
            return jsonify({"ok": False, "error": "filename 必填"}), 400
        try:
            rec = db.archive_codex(filename=filename, archived=archived)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if rec is None:
            return jsonify({"ok": False, "error": f"凭证不存在: {filename}"}), 404
        return jsonify({"ok": True, "filename": filename, "archived": archived, "record": rec})

    @app.post("/api/codex/archive-bulk")
    def api_codex_archive_bulk():
        """批量归档/取消归档 Codex 授权凭证。Body {filenames:[...], archived}。"""
        data = request.get_json(silent=True) or {}
        filenames = data.get("filenames") or []
        archived = bool(data.get("archived", True))
        if not isinstance(filenames, list) or not filenames:
            return jsonify({"ok": False, "error": "filenames 必须是非空数组"}), 400
        if len(filenames) > 1000:
            return jsonify({"ok": False, "error": "单次最多 1000 个"}), 400
        updated = []
        skipped = []
        seen = set()
        for fname in filenames:
            if not isinstance(fname, str) or not fname:
                skipped.append({"filename": str(fname), "reason": "非法文件名"})
                continue
            if fname in seen:
                continue
            seen.add(fname)
            try:
                rec = db.archive_codex(filename=fname, archived=archived)
            except ValueError as exc:
                skipped.append({"filename": fname, "reason": str(exc)})
                continue
            if rec is None:
                skipped.append({"filename": fname, "reason": "凭证不存在"})
            else:
                updated.append({"filename": fname, "archived": archived})
        return jsonify({"ok": True, "updated": updated, "updated_count": len(updated), "archived": archived, "skipped": skipped})

    @app.get("/api/codex/download/<path:filename>")
    def api_codex_download(filename: str):
        """
        下载一个 CPA 兼容的 codex-*.json 文件，下载即标记为已导出（计数+1）。
        前端通过浏览器原生下载触发（a 标签 / window.location）。
        """
        try:
            content, fname = db.read_codex_credential(filename)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        db.mark_codex_exported(fname)
        return Response(
            content,
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    @app.get("/api/codex/download-from-cpa/<path:filename>")
    def api_codex_download_from_cpa(filename: str):
        """按本地 codex 文件/回执匹配 CPA auth-files，并从 CPA 下载实际 Codex JSON。"""
        try:
            content, fname = db.read_codex_credential(filename)
            import json as _json
            try:
                local = _json.loads(content)
            except Exception:
                local = {}
            email = str(local.get("email") or "").strip()
            from core.codex_oauth import download_cpa_codex_auth_text
            cpa_text, cpa_name, _meta = download_cpa_codex_auth_text(email=email, local_filename=fname)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 502
        db.mark_codex_exported(fname)
        return Response(
            cpa_text,
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{cpa_name}"'},
        )

    @app.post("/api/codex/download-bulk-from-cpa")
    def api_codex_download_bulk_from_cpa():
        """
        批量从 CPA 下载选中的 Codex 凭证，打包成 zip；zip 内每个文件都是 CPA 原始 JSON。
        Body: {"filenames": ["codex-xxx-cpa-callback.json", ...]}
        """
        import io
        import json as _json
        import zipfile
        from datetime import datetime as _dt
        from core.codex_oauth import download_cpa_codex_auth_text

        data = request.get_json(silent=True) or {}
        filenames = data.get("filenames") or []
        if not isinstance(filenames, list) or not filenames:
            return jsonify({"ok": False, "error": "filenames 必须是非空数组"}), 400
        if len(filenames) > 1000:
            return jsonify({"ok": False, "error": "单次最多 1000 个"}), 400

        errors = []
        added = []
        used_names = set()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for fname in filenames:
                if not isinstance(fname, str):
                    errors.append({"filename": str(fname), "error": "非字符串"})
                    continue
                try:
                    content, real_fname = db.read_codex_credential(fname)
                    try:
                        local = _json.loads(content)
                    except Exception:
                        local = {}
                    email = str(local.get("email") or "").strip()
                    cpa_text, cpa_name, _meta = download_cpa_codex_auth_text(email=email, local_filename=real_fname)
                    arcname = cpa_name
                    if arcname in used_names:
                        stem, dot, ext = arcname.rpartition(".")
                        arcname = f"{stem or arcname}-{len(used_names)+1}{dot}{ext}" if dot else f"{arcname}-{len(used_names)+1}"
                    used_names.add(arcname)
                    zf.writestr(arcname, cpa_text)
                    added.append({"local_filename": real_fname, "cpa_filename": cpa_name})
                    db.mark_codex_exported(real_fname)
                except Exception as exc:
                    errors.append({"filename": fname, "error": f"{type(exc).__name__}: {exc}"})
            manifest = {
                "exported_at": _dt.now().isoformat(timespec="seconds"),
                "source": "cpa",
                "count": len(added),
                "files": added,
                "errors": errors,
            }
            zf.writestr("manifest.json", _json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

        if not added:
            return jsonify({"ok": False, "error": "没有成功从 CPA 下载任何凭证", "errors": errors}), 502
        now = _dt.now()
        dl_name = f"codex-cpa-bulk-{now.strftime('%Y%m%d-%H%M%S')}.zip"
        buf.seek(0)
        return Response(
            buf.getvalue(),
            mimetype="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{dl_name}"'},
        )

    @app.post("/api/codex/download-bulk")
    def api_codex_download_bulk():
        """
        批量下载选中的 codex 凭证，打包到一个 JSON 文件里。

        Body: {"filenames": ["codex-xxx.json", ...]}
        响应：聚合 JSON（attachment 触发浏览器下载），结构：
            {
              "exported_at": "...",
              "count": N,
              "credentials": [{"filename": "...", "data": {...原始凭证内容...}}, ...],
              "errors": [...]   // 仅当部分失败时出现
            }
        注意：聚合格式**不能直接被 CPA 读**，CPA 是按单文件加载 auths/ 目录的。
              本接口主要用途是备份 / 跨机迁移 / 二次处理。
        每个成功的凭证会自动标记 mark_exported（计数+1）。
        """
        import json as _json
        from datetime import datetime as _dt

        data = request.get_json(silent=True) or {}
        filenames = data.get("filenames") or []
        if not isinstance(filenames, list) or not filenames:
            return jsonify({"ok": False, "error": "filenames 必须是非空数组"}), 400
        if len(filenames) > 1000:
            return jsonify({"ok": False, "error": "单次最多 1000 个"}), 400

        bundle = []
        errors = []
        for fname in filenames:
            if not isinstance(fname, str):
                errors.append({"filename": str(fname), "error": "非字符串"})
                continue
            try:
                content, real_fname = db.read_codex_credential(fname)
                parsed = _json.loads(content)
                bundle.append({"filename": real_fname, "data": parsed})
                db.mark_codex_exported(real_fname)
            except Exception as exc:
                errors.append({"filename": fname, "error": f"{type(exc).__name__}: {exc}"})

        now = _dt.now()
        result = {
            "exported_at": now.isoformat(timespec="seconds"),
            "count": len(bundle),
            "credentials": bundle,
        }
        if errors:
            result["errors"] = errors

        dl_name = f"codex-bulk-{now.strftime('%Y%m%d-%H%M%S')}.json"
        return Response(
            _json.dumps(result, ensure_ascii=False, indent=2),
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{dl_name}"'},
        )

    @app.post("/api/codex/reset-export")
    def api_codex_reset_export():
        """清掉某个 codex 凭证的导出状态（重新标为未导出）。body {filename}。"""
        data = request.get_json(silent=True) or {}
        fname = (data.get("filename") or "").strip()
        if not fname:
            return jsonify({"ok": False, "error": "filename 为空"}), 400
        try:
            db.reset_codex_exported(fname)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True})

    @app.post("/api/codex/delete")
    def api_codex_delete():
        """删除一个 codex 凭证文件。body {filename}。"""
        data = request.get_json(silent=True) or {}
        fname = (data.get("filename") or "").strip()
        if not fname:
            return jsonify({"ok": False, "error": "filename 为空"}), 400
        try:
            deleted = db.delete_codex_credential(fname)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if not deleted:
            return jsonify({"ok": False, "error": "凭证文件不存在"}), 404
        return jsonify({"ok": True, "deleted": fname})

    @app.post("/api/codex/delete-bulk")
    def api_codex_delete_bulk():
        """批量删除 codex 凭证文件。body {filenames:[...]}。"""
        data = request.get_json(silent=True) or {}
        filenames = data.get("filenames") or []
        if not isinstance(filenames, list) or not filenames:
            return jsonify({"ok": False, "error": "filenames 必须是非空数组"}), 400
        if len(filenames) > 1000:
            return jsonify({"ok": False, "error": "单次最多删除 1000 个"}), 400
        deleted = []
        skipped = []
        seen = set()
        for fname in filenames:
            fname = str(fname or "").strip()
            if not fname or fname in seen:
                continue
            seen.add(fname)
            try:
                ok = db.delete_codex_credential(fname)
                if ok:
                    deleted.append(fname)
                else:
                    skipped.append({"filename": fname, "reason": "文件不存在"})
            except Exception as exc:
                skipped.append({"filename": fname, "reason": f"{type(exc).__name__}: {exc}"})
        return jsonify({"ok": True, "deleted": deleted, "deleted_count": len(deleted), "skipped": skipped})

    def _reserve_codex_retry(email: str) -> bool:
        """进程内防重复占位；成功返回 True。"""
        return codex_retry_service.reserve(email)

    def _release_codex_retry(email: str) -> None:
        codex_retry_service.release(email)

    def _run_codex_retry_worker(email: str, *, batch_label: str | None = None, clear_log: bool = True) -> None:
        """执行一个账号的 Codex 补跑。调用前必须已经 reserve。"""
        codex_retry_service.run_worker(email, batch_label=batch_label, clear_log=clear_log)


    @app.post("/api/codex/stop")
    def api_codex_stop():
        """停止单个 Codex 补跑。Body {email}。"""
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        acc = db.get_account_by_email(email)
        if acc is None:
            return jsonify({"ok": False, "error": f"账号不存在: {email}"}), 404
        codex_caps = build_account_status_contract(acc)["codex_capabilities"]
        if not codex_caps["can_stop"] and not codex_retry_service.is_retrying(email):
            return jsonify({"ok": False, "error": "该账号当前没有可停止的 Codex 补跑"}), 409
        result = codex_retry_service.request_stop(email)
        status = int(result.pop("status", 200) or 200)
        return jsonify(result), status

    @app.post("/api/codex/stop-bulk")
    def api_codex_stop_bulk():
        """批量停止 Codex 补跑。Body {emails:[...]} 或 {account_ids:[...]}。"""
        data = request.get_json(silent=True) or {}
        emails = data.get("emails") or []
        ids = data.get("account_ids") or data.get("ids") or []
        targets = []
        if isinstance(emails, list) and emails:
            targets = [str(x or "").strip() for x in emails]
        elif isinstance(ids, list) and ids:
            for raw in ids:
                try:
                    acc = db.get_account(int(raw))
                except Exception:
                    acc = None
                if acc and acc.get("email"):
                    targets.append(str(acc.get("email") or "").strip())
        else:
            return jsonify({"ok": False, "error": "emails 或 account_ids 必须是非空数组"}), 400
        if len(targets) > 500:
            return jsonify({"ok": False, "error": "单次最多停止 500 个"}), 400
        stopped = []
        skipped = []
        seen = set()
        for email in targets:
            key = email.lower()
            if not email or key in seen:
                continue
            seen.add(key)
            acc = db.get_account_by_email(email)
            if acc is None:
                skipped.append({"email": email, "reason": "账号不存在"})
                continue
            codex_caps = build_account_status_contract(acc)["codex_capabilities"]
            if not codex_caps["can_stop"] and not codex_retry_service.is_retrying(email):
                skipped.append({"email": email, "reason": "未处于补跑中"})
                continue
            r = codex_retry_service.request_stop(email)
            if r.get("ok"):
                stopped.append({"email": email, "injected": r.get("injected"), "running": r.get("running")})
            else:
                skipped.append({"email": email, "reason": r.get("error") or "停止失败"})
        return jsonify({"ok": True, "stopped": stopped, "stopped_count": len(stopped), "skipped": skipped})

    @app.post("/api/codex/reset-retrying")
    def api_codex_reset_retrying():
        """手动重置某账号的 Codex 补跑中状态。Body {email, status?}。"""
        from datetime import datetime as _dt

        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        raw_status = (data.get("status") or "failed").strip().lower()
        if raw_status in ("", "none", "null", "clear"):
            raw_status = "empty"
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        if raw_status not in ("failed", "skipped", "empty"):
            return jsonify({"ok": False, "error": "status 仅支持 failed/skipped/empty"}), 400

        acc = db.get_account_by_email(email)
        if acc is None:
            return jsonify({"ok": False, "error": f"账号不存在: {email}"}), 404

        new_status = "" if raw_status == "empty" else raw_status
        err = None if raw_status == "empty" else "用户手动重置补跑中状态"
        ok = db.update_account_codex_status(email, new_status, err)
        if not ok:
            return jsonify({"ok": False, "error": f"账号不存在: {email}"}), 404

        _release_codex_retry(email)

        try:
            log_path = codex_retry_service.log_path(email)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as f:
                ts = _dt.now().strftime("%H:%M:%S")
                shown = new_status or "空"
                f.write(f"{ts} [WARNING] [Codex 补跑] 用户手动重置补跑中状态，当前状态={shown}\n")
        except Exception:
            logger.exception("写入 Codex 补跑重置日志失败")

        return jsonify({"ok": True, "message": "已重置补跑中状态", "status": new_status})

    @app.post("/api/codex/retry")
    def api_codex_retry():
        """手动补跑某账号的 Codex 授权。Body {email}。"""
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        acc = db.get_account_by_email(email)
        if acc is None:
            return jsonify({"ok": False, "error": f"账号不存在: {email}"}), 404
        codex_caps = build_account_status_contract(acc)["codex_capabilities"]
        if not codex_caps["can_retry"]:
            reason = (
                "账号已废号，不能补跑 Codex"
                if str(acc.get("live_check_status") or "").lower() == "deactivated"
                else "当前 Codex 状态不允许补跑"
            )
            return jsonify({"ok": False, "error": reason}), 409
        if not _reserve_codex_retry(email):
            return jsonify({"ok": False, "error": "该账号已有消费邮箱验证码的任务，请稍候"}), 409

        db.update_account_codex_status(email, "retrying", None)
        threading.Thread(
            target=_run_codex_retry_worker,
            kwargs={"email": email, "clear_log": True},
            name=f"codex-retry-{email}",
            daemon=True,
        ).start()
        return jsonify({"ok": True, "message": "已在后台开始补跑，~1-2 分钟后刷新查看"})

    @app.post("/api/codex/retry-bulk")
    def api_codex_retry_bulk():
        """批量补跑 Codex。Body {account_ids:[...], workers: 1-16}。"""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from datetime import datetime as _dt

        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        workers = data.get("workers", 1)
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        try:
            workers = max(1, min(16, int(workers)))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 必须是数字"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多选择 500 个账号"}), 400

        selected = []
        skipped = []
        seen_ids = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen_ids:
                continue
            seen_ids.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = (acc.get("email") or "").strip()
            if not email:
                skipped.append({"id": acc_id, "reason": "邮箱为空"})
                continue
            codex_caps = build_account_status_contract(acc)["codex_capabilities"]
            if not codex_caps["can_retry"]:
                reason = (
                    "账号已废号"
                    if str(acc.get("live_check_status") or "").lower() == "deactivated"
                    else "当前 Codex 状态不允许补跑"
                )
                skipped.append({"id": acc_id, "email": email, "reason": reason})
                continue
            if not _reserve_codex_retry(email):
                skipped.append({"id": acc_id, "email": email, "reason": "已有消费邮箱验证码的任务"})
                continue
            selected.append({"id": acc_id, "email": email})

        if not selected:
            return jsonify({"ok": False, "error": "没有可补跑的账号", "skipped": skipped}), 409

        batch_id = _dt.now().strftime("%Y%m%d-%H%M%S")
        for item in selected:
            email = item["email"]
            db.update_account_codex_status(email, "retrying", None)
            log_path = codex_retry_service.log_path(email)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(
                f"{_dt.now().strftime('%H:%M:%S')} [INFO] [Codex 批量补跑] 已加入批量任务 batch={batch_id} workers={workers}，等待线程执行\n",
                encoding="utf-8",
            )

        def _bulk_runner(items: list[dict], max_workers: int, batch: str):
            logger.info(f"[Codex 批量补跑] 启动 batch={batch} count={len(items)} workers={max_workers}")
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=f"codex-bulk-{batch}") as ex:
                futures = [ex.submit(_run_codex_retry_worker, it["email"], batch_label=f"{batch} #{idx}/{len(items)}", clear_log=False) for idx, it in enumerate(items, 1)]
                for fut in as_completed(futures):
                    try:
                        fut.result()
                    except Exception:
                        logger.exception(f"[Codex 批量补跑] 子任务异常 batch={batch}")
            logger.info(f"[Codex 批量补跑] 完成 batch={batch}")

        threading.Thread(
            target=_bulk_runner,
            args=(selected, workers, batch_id),
            name=f"codex-bulk-dispatch-{batch_id}",
            daemon=True,
        ).start()
        return jsonify({
            "ok": True,
            "message": f"已开始批量补跑 {len(selected)} 个账号，并发 {workers}",
            "started": selected,
            "started_count": len(selected),
            "skipped": skipped,
            "batch_id": batch_id,
        })

    @app.get("/api/codex/retry-log")
    def api_codex_retry_log():
        """读取某邮箱最近一次补跑的日志。?email=xxx"""
        email = (request.args.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        p = codex_retry_service.log_path(email)
        if not p.exists():
            return jsonify({"ok": True, "log": "", "running": False})
        max_bytes = 50_000
        size = p.stat().st_size
        with p.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            content = f.read().decode("utf-8", errors="replace")
        return jsonify({
            "ok": True,
            "log": content,
            "running": codex_retry_service.is_retrying(email),
        })

    @app.get("/api/accounts/live-check-log")
    def api_account_live_check_log():
        """读取某邮箱最近一次查活日志。?email=xxx"""
        from core import account_liveness
        email = (request.args.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        p = account_liveness.log_path(email)
        if not p.exists():
            return jsonify({"ok": True, "log": "", "running": live_check_service.is_checking(email)})
        max_bytes = 80_000
        size = p.stat().st_size
        with p.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            content = f.read().decode("utf-8", errors="replace")
        return jsonify({
            "ok": True,
            "log": content,
            "running": live_check_service.is_checking(email),
        })

    # ----------------------------------------------------------
    # 注册任务
    # ----------------------------------------------------------
    @app.get("/api/jobs")
    def api_jobs():
        limit = request.args.get("limit", default=100, type=int)
        paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        fetch_limit = 1_000_000 if (paged or page_arg is not None or page_size_arg is not None) else limit
        from config import email as _email_cfg
        manual_otp_required = not bool(getattr(_email_cfg, "USE_EMAIL_SERVICE", True))
        rows = db.list_jobs(limit=fetch_limit)
        if paged or page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            result = _paginate_items(rows, page=page, page_size=page_size)
            _enrich_job_rows(result.get("items") or [], manual_otp_required)
            result["items"] = [_compact_job_for_list(r) for r in (result.get("items") or [])]
            result["status_counts"] = _job_status_counts(rows)
            result["compact"] = True
            return jsonify(result)
        _enrich_job_rows(rows, manual_otp_required)
        return jsonify(rows)

    @app.post("/api/jobs")
    def api_jobs_create():
        """启动批量注册：body {count, workers}。"""
        data = request.get_json(silent=True) or {}
        try:
            count = int(data.get("count", 1))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "count 非法"}), 400
        if count < 1 or count > 200:
            return jsonify({"ok": False, "error": "count 需在 1~200 之间"}), 400

        # workers 控制本次新提交任务使用的线程池；若和上次不同，服务层会为新任务切换到新池。
        try:
            workers = max(1, min(16, int(data.get("workers", 3))))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 非法"}), 400

        # 提交前先确认池里有足够可用邮箱，给前端一个温和提示（不阻断）
        from config import email as _email_cfg
        from config import register as _register_cfg
        from core.email_provider import parse_email_sources
        if not bool(getattr(_email_cfg, "USE_EMAIL_SERVICE", True)):
            reg_email = str(getattr(_register_cfg, "REGISTER_EMAIL", "") or "").strip()
            if not reg_email:
                return jsonify({
                    "ok": False,
                    "error": "手动模式未配置 REGISTER_EMAIL。请到配置页填写「手动注册邮箱」，或开启自动取邮箱+收码。",
                }), 400
            if count > 1:
                return jsonify({
                    "ok": False,
                    "error": "手动模式建议每次只跑 1 个任务（同一 REGISTER_EMAIL）。请把数量设为 1。",
                }), 400
            jobs = svc.submit_registration(count=count, workers=workers)
            return jsonify({
                "ok": True,
                "submitted": len(jobs),
                "jobs": jobs,
                "warning": f"手动 OTP 模式：将使用 {reg_email}；验证码请在任务页提交",
                "workers": workers,
            })
        sources = parse_email_sources(_email_cfg.EMAIL_SOURCE)
        if "mailcom" in sources:
            health = db.mailcom_pool_health()
            if not health.get("configured"):
                return jsonify({
                    "ok": False,
                    "error": "已选择 mailcom 邮箱来源，但尚未配置账号和密码。请在邮箱池导入 email----password。",
                }), 400
            if not health.get("has_available_credentials"):
                message = "mail.com 账号池没有可用账号"
                if health.get("auth_failed"):
                    message += "；存在认证失败记录，请检查账号密码、人工验证或启用可用账号"
                else:
                    message += "；请导入或回收可用账号"
                return jsonify({"ok": False, "error": message}), 400
            if health.get("has_available_aliases") is False:
                return jsonify({
                    "ok": False,
                    "error": "mail.com 母号已配置，但别名池没有可用 alias；请等待同步完成或手动触发补齐。",
                }), 400
        if "gptmail" in sources:
            api_key = str(getattr(_email_cfg, "GPTMAIL_API_KEY", "") or "").strip()
            if not api_key:
                return jsonify({
                    "ok": False,
                    "error": "已选择 gptmail 邮箱来源，请填写 GPTMail API Key（配置 → 邮箱 / OTP）。",
                }), 400
        if "cloudflare" in sources:
            api_base = str(getattr(_email_cfg, "CLOUDFLARE_API_BASE", "") or "").strip()
            if not api_base:
                return jsonify({
                    "ok": False,
                    "error": "已选择 cloudflare 邮箱来源，请填写 Cloudflare API 地址（配置 → 邮箱 / OTP）。",
                }), 400
            auth_mode = str(getattr(_email_cfg, "CLOUDFLARE_AUTH_MODE", "none") or "none").strip().lower()
            accounts_path = str(getattr(_email_cfg, "CLOUDFLARE_PATH_ACCOUNTS", "/api/new_address") or "").strip().lower()
            api_key = str(getattr(_email_cfg, "CLOUDFLARE_API_KEY", "") or "").strip()
            needs_key = auth_mode in ("x-admin-auth", "bearer", "x-api-key", "query-key") or accounts_path.rstrip("/").endswith("/admin/new_address")
            if needs_key and not api_key:
                return jsonify({
                    "ok": False,
                    "error": "Cloudflare admin/鉴权模式需要填写 Cloudflare API Key（配置 → 邮箱 / OTP）。",
                }), 400
            try:
                from core.cf_temp_mail_client import CFTempMailError, validate_random_subdomain_config
                validate_random_subdomain_config()
            except CFTempMailError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400
        if "mailnest" in sources:
            api_key = str(getattr(_email_cfg, "MAIL_NEST_API_KEY", "") or "").strip()
            project_code = str(getattr(_email_cfg, "MAIL_NEST_PROJECT_CODE", "") or "").strip()
            if not api_key:
                return jsonify({
                    "ok": False,
                    "error": "已选择 mailnest 邮箱来源，请填写 MailNest API Key（配置 → 邮箱 / OTP）。",
                }), 400
            if not project_code:
                return jsonify({
                    "ok": False,
                    "error": "已选择 mailnest 邮箱来源，请填写 MailNest 项目代码（配置 → 邮箱 / OTP）。",
                }), 400
        if "cloudmail" in sources:
            api_base = str(getattr(_email_cfg, "CLOUDMAIL_API_BASE", "") or "").strip()
            token = str(getattr(_email_cfg, "CLOUDMAIL_AUTH_TOKEN", "") or "").strip()
            if not api_base:
                return jsonify({
                    "ok": False,
                    "error": "已选择 cloudmail 邮箱来源，请填写 CloudMail API 地址（配置 → 邮箱 / OTP）。",
                }), 400
            if not token:
                return jsonify({
                    "ok": False,
                    "error": "已选择 cloudmail 邮箱来源，请填写 CloudMail Token（配置 → 邮箱 / OTP）。",
                }), 400
        if "gptmail" in sources or "mailnest" in sources or "cloudmail" in sources or "cloudflare" in sources:
            # 临时邮箱在任务开始时动态生成，不需要本地邮箱池容量提示。
            warning = ""
        elif "cloudflare_domain" in sources:
            pool = db.domain_email_pool_summary()
            warning = ""
            if sources == ["cloudflare_domain"] and pool.get("available", 0) < count:
                warning = f"域名邮箱池仅 {pool.get('available', 0)} 个可用，少于任务数 {count}，不足的会自动生成"
        elif sources == ["icloud"]:
            pool = db.icloud_email_pool_summary()
            warning = ""
            if pool.get("available", 0) < count:
                warning = f"iCloud 隐私邮箱池仅 {pool.get('available', 0)} 个可用，少于任务数 {count}，不足的会失败"
        elif sources == ["generic_api"]:
            pool = db.generic_api_email_pool_summary()
            warning = ""
            if pool.get("available", 0) < count:
                warning = f"通用 API 邮箱池仅 {pool.get('available', 0)} 个可用，少于任务数 {count}，不足的会失败"
        elif sources == ["mailcom"]:
            pool = db.mailcom_pool_summary()
            warning = ""
            if pool.get("available", 0) < count:
                warning = f"mail.com 邮箱池仅 {pool.get('available', 0)} 个可用，少于任务数 {count}，不足的会失败"
        elif len(sources) > 1:
            available = 0
            if "outlook" in sources:
                available += db.outlook_pool_summary().get("available", 0)
            if "generic_api" in sources:
                available += db.generic_api_email_pool_summary().get("available", 0)
            if "icloud" in sources:
                available += db.icloud_email_pool_summary().get("available", 0)
            if "mailcom" in sources:
                available += db.mailcom_pool_summary().get("available", 0)
            warning = ""
            if available < count:
                warning = f"多个邮箱池合计仅 {available} 个可用，少于任务数 {count}，不足的会失败"
        else:
            pool = db.outlook_pool_summary()
            warning = ""
            if pool.get("available", 0) < count:
                warning = f"可用邮箱仅 {pool.get('available', 0)} 个，少于任务数 {count}，不足的会失败"
        jobs = svc.submit_registration(count=count, workers=workers)
        return jsonify({"ok": True, "submitted": len(jobs), "jobs": jobs, "warning": warning, "workers": workers})

    @app.get("/api/manual-otp/waiting")
    def api_manual_otp_waiting():
        """列出当前正在等待手动验证码的邮箱。"""
        from core.manual_otp import list_waiting
        return jsonify({"ok": True, "waiting": list_waiting()})

    @app.post("/api/manual-otp")
    def api_manual_otp_submit():
        """提交手动邮箱验证码。Body: {email, code} 或 {job_id, code}。"""
        from core.manual_otp import submit_manual_otp
        data = request.get_json(silent=True) or {}
        code = (data.get("code") or data.get("otp") or "").strip()
        email = (data.get("email") or "").strip()
        job_id = data.get("job_id")
        if not email and job_id is not None:
            job = db.get_job(int(job_id))
            email = (job or {}).get("email") or ""
        if not email:
            return jsonify({"ok": False, "error": "email/job_id 缺失"}), 400
        try:
            result = submit_manual_otp(email, code)
            return jsonify(result)
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    @app.post("/api/jobs/cancel-pending")
    def api_jobs_cancel_pending():
        """取消所有还在排队（status=pending）的任务。已在 running 的不动。"""
        cancelled = svc.cancel_pending_jobs()
        return jsonify({"ok": True, "cancelled": cancelled})

    @app.post("/api/jobs/<int:job_id>/stop")
    def api_job_stop(job_id: int):
        """手动停止单个注册任务。pending 取消；running 发送停止信号。"""
        result = svc.request_stop_job(job_id)
        if not result.get("ok"):
            return jsonify({"ok": False, "error": result.get("error") or "停止失败"}), int(result.get("status") or 400)
        return jsonify(result)

    @app.post("/api/jobs/<int:job_id>/retry")
    def api_job_retry(job_id: int):
        """重试失败/停止/取消任务；服务端自动判断完整注册或 Codex 补跑。"""
        data = request.get_json(silent=True) or {}
        try:
            workers = max(1, min(16, int(data.get("workers", svc.get_executor_workers()))))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 非法"}), 400
        result = svc.retry_job(job_id, workers=workers)
        if not result.get("ok"):
            return jsonify(result), int(result.get("status") or 400)
        return jsonify(result)

    @app.post("/api/jobs/retry-bulk")
    def api_jobs_retry_bulk():
        """批量重试任务；不支持项逐条跳过并返回原因。"""
        data = request.get_json(silent=True) or {}
        job_ids = data.get("job_ids") or data.get("ids") or []
        if not isinstance(job_ids, list) or not job_ids:
            return jsonify({"ok": False, "error": "job_ids 必须是非空数组"}), 400
        if len(job_ids) > 500:
            return jsonify({"ok": False, "error": "单次最多重试 500 个任务"}), 400
        try:
            workers = max(1, min(16, int(data.get("workers", svc.get_executor_workers()))))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 非法"}), 400

        started: list[dict] = []
        reused: list[dict] = []
        skipped: list[dict] = []
        seen: set[int] = set()
        for raw_id in job_ids:
            try:
                one_id = int(raw_id)
            except (TypeError, ValueError):
                skipped.append({"id": raw_id, "reason": "ID 非法"})
                continue
            if one_id in seen:
                continue
            seen.add(one_id)
            result = svc.retry_job(one_id, workers=workers)
            if not result.get("ok"):
                skipped.append({"id": one_id, "reason": result.get("error") or "不能重试"})
            elif result.get("reused"):
                reused.append(result)
            else:
                started.append(result)
        return jsonify({
            "ok": True,
            "started": started,
            "started_count": len(started),
            "reused": reused,
            "reused_count": len(reused),
            "skipped": skipped,
            "skipped_count": len(skipped),
            "workers": workers,
        })

    @app.post("/api/jobs/<int:job_id>/delete")
    def api_job_delete(job_id: int):
        """删除一个任务记录。运行中的任务不允许删除；排队任务删除后执行前会自动跳过。"""
        job = db.get_job(job_id)
        if not job:
            return jsonify({"ok": False, "error": "任务不存在"}), 404
        if job.get("status") in ("running", "stopping"):
            return jsonify({"ok": False, "error": "运行中的任务不能删除，请等待完成后再删"}), 409
        deleted = db.delete_job(job_id, delete_log=True, allow_running=False)
        if not deleted:
            return jsonify({"ok": False, "error": "任务不存在或已开始运行"}), 409
        return jsonify({"ok": True, "deleted": deleted})

    @app.post("/api/jobs/delete-bulk")
    def api_jobs_delete_bulk():
        """批量删除任务记录。running 任务跳过，其它任务删除记录和日志。"""
        data = request.get_json(silent=True) or {}
        job_ids = data.get("job_ids") or data.get("ids") or []
        if not isinstance(job_ids, list) or not job_ids:
            return jsonify({"ok": False, "error": "job_ids 必须是非空数组"}), 400
        if len(job_ids) > 1000:
            return jsonify({"ok": False, "error": "单次最多删除 1000 个任务"}), 400

        deleted: list[int] = []
        skipped: list[dict] = []
        seen: set[int] = set()
        for raw_id in job_ids:
            try:
                job_id = int(raw_id)
            except (TypeError, ValueError):
                skipped.append({"id": raw_id, "reason": "ID 非法"})
                continue
            if job_id in seen:
                continue
            seen.add(job_id)

            job = db.get_job(job_id)
            if not job:
                skipped.append({"id": job_id, "reason": "任务不存在"})
                continue
            if job.get("status") in ("running", "stopping"):
                skipped.append({"id": job_id, "reason": "运行中，不能删除"})
                continue
            if db.delete_job(job_id, delete_log=True, allow_running=False):
                deleted.append(job_id)
            else:
                skipped.append({"id": job_id, "reason": "任务不存在或已开始运行"})

        return jsonify({"ok": True, "deleted": deleted, "deleted_count": len(deleted), "skipped": skipped})

    @app.get("/api/jobs/<int:job_id>/log")
    def api_job_log(job_id: int):
        job = db.get_job(job_id)
        if not job:
            return jsonify({"ok": False, "error": "任务不存在"}), 404
        return jsonify({
            "ok": True,
            "job": job,
            "log": svc.read_job_log(job_id),
        })

    # ----------------------------------------------------------
    # RoxyBrowser 辅助接口
    # ----------------------------------------------------------
    @app.get("/api/roxy/workspaces")
    def api_roxy_workspaces():
        try:
            from core.roxybrowser_client import RoxyBrowserClient
            result = RoxyBrowserClient().list_workspaces()
            return jsonify(result)
        except Exception as exc:
            logger.exception("获取 Roxy 团队/工作区失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500

    # ----------------------------------------------------------
    # 配置读写
    # ----------------------------------------------------------
    @app.get("/api/config")
    def api_config_get():
        return jsonify(config_editor.get_config())

    @app.post("/api/smsbower/metadata")
    def api_smsbower_metadata():
        """读取 SMSBower 服务和国家元数据；不会取号或产生号码费用。"""
        data = request.get_json(silent=True) or {}
        try:
            from types import SimpleNamespace

            from config import codex as _codex_cfg
            from core.smsbower_provider import SmsBowerProvider

            api_base = str(data.get("api_base") or getattr(_codex_cfg, "SMSBOWER_API_BASE", "") or "").strip()
            api_key = str(data.get("api_key") or getattr(_codex_cfg, "SMSBOWER_API_KEY", "") or "").strip()
            provider = SmsBowerProvider(config=SimpleNamespace(
                SMSBOWER_API_BASE=api_base,
                SMSBOWER_API_KEY=api_key,
                SMS_REQUEST_TIMEOUT=getattr(_codex_cfg, "SMS_REQUEST_TIMEOUT", 30),
            ))
            metadata = provider.get_metadata()
            return jsonify({"ok": True, **metadata})
        except Exception as exc:
            logger.warning("获取 SMSBower 元数据失败: %s", type(exc).__name__)
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/cloudmail/gen-token")
    def api_cloudmail_gen_token():
        """手动生成 CloudMail Authorization Token，并把本次填写的 CloudMail 配置一并写入 .env。"""
        data = request.get_json(silent=True) or {}
        try:
            from core.cloudmail_client import gen_token
            from config.env_loader import write_env_values

            api_base = (data.get("api_base") or "").strip()
            admin_email = (data.get("email") or data.get("admin_email") or "").strip()
            password = (data.get("password") or "").strip()
            path = (data.get("path") or "/api/public/genToken").strip() or "/api/public/genToken"
            token = gen_token(
                email=admin_email,
                password=password,
                path=path,
                base_url=api_base,
            )
            updates = {"CLOUDMAIL_AUTH_TOKEN": token}
            # 生成 Token 时用户通常尚未点“保存配置”；这里同步保存本次填写的字段，
            # 避免 loadConfig() 后 API 地址/账号/密码被旧 .env 值覆盖。
            if api_base:
                updates["CLOUDMAIL_API_BASE"] = api_base
            if admin_email:
                updates["CLOUDMAIL_ADMIN_EMAIL"] = admin_email
            if password:
                updates["CLOUDMAIL_PASSWORD"] = password
            if path:
                updates["CLOUDMAIL_TOKEN_PATH"] = path
            written = write_env_values(updates)
            try:
                import config as _config_pkg
                _config_pkg.reload_all()
            except Exception:
                logger.exception("CloudMail Token 写入后热加载失败")
            return jsonify({
                "ok": True,
                "token": token,
                "written": written,
                "message": "CloudMail Token 已生成，且当前 CloudMail 配置已保存",
            })
        except Exception as exc:
            logger.exception("生成 CloudMail Token 失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    @app.post("/api/cloudmail/domains")
    def api_cloudmail_domains():
        """从 CloudMail 平台获取域名列表，并可写入 .env 作为本地缓存。"""
        data = request.get_json(silent=True) or {}
        try:
            from core.cloudmail_client import fetch_domains
            from config.env_loader import write_env_values

            updates = {}
            api_base = (data.get("api_base") or "").strip()
            admin_email = (data.get("email") or data.get("admin_email") or "").strip()
            password = (data.get("password") or "").strip()
            token = (data.get("token") or "").strip()
            if api_base:
                updates["CLOUDMAIL_API_BASE"] = api_base
            if admin_email:
                updates["CLOUDMAIL_ADMIN_EMAIL"] = admin_email
            if password:
                updates["CLOUDMAIL_PASSWORD"] = password
            if token:
                updates["CLOUDMAIL_AUTH_TOKEN"] = token
            if updates:
                write_env_values(updates)
                import config as _config_pkg
                _config_pkg.reload_all()

            domains = fetch_domains(force=True)
            written = write_env_values({"CLOUDMAIL_DOMAINS": "\n".join(domains)})
            try:
                import config as _config_pkg
                _config_pkg.reload_all()
            except Exception:
                logger.exception("CloudMail 域名写入后热加载失败")
            return jsonify({
                "ok": True,
                "domains": domains,
                "count": len(domains),
                "written": written,
                "message": f"已获取 {len(domains)} 个 CloudMail 可用域名并保存",
            })
        except Exception as exc:
            logger.exception("获取 CloudMail 域名失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    @app.post("/api/config")
    def api_config_set():
        data = request.get_json(silent=True) or {}
        updates = data.get("updates") if isinstance(data.get("updates"), dict) else data
        if not isinstance(updates, dict) or not updates:
            return jsonify({"ok": False, "error": "无更新内容"}), 400
        if "MAILCOM_DELETE_ALIAS_IF_NO_TRIAL" in updates and not isinstance(
            updates["MAILCOM_DELETE_ALIAS_IF_NO_TRIAL"], bool
        ):
            return jsonify({
                "ok": False,
                "error": "ValueError: MAILCOM_DELETE_ALIAS_IF_NO_TRIAL 必须是 JSON 布尔值 true 或 false",
            }), 400
        if "EMAIL_SOURCE" in updates:
            try:
                updates["EMAIL_SOURCE"] = config_editor.normalize_email_source_value(
                    updates.get("EMAIL_SOURCE")
                )
            except ValueError as exc:
                return jsonify({"ok": False, "error": f"ValueError: {exc}"}), 400
            if "mailcom" in updates["EMAIL_SOURCE"].split(","):
                health = db.mailcom_pool_health()
                if not health.get("configured"):
                    return jsonify({
                        "ok": False,
                        "error": "已选择 mailcom 邮箱来源，请先在邮箱池导入 mail.com 账号和密码（格式：email----password）。",
                    }), 400
                if not health.get("has_available_credentials"):
                    return jsonify({
                        "ok": False,
                        "error": "已选择 mailcom 邮箱来源，但没有可用母号凭据；请启用有效账密或处理已停用账号。",
                    }), 400
                if health.get("has_available_aliases") is False:
                    return jsonify({
                        "ok": False,
                        "error": "已选择 mailcom 邮箱来源，但没有可用 alias；请等待母号同步或手动补齐。",
                    }), 400
        if "PLAN_CHECK_PROXY_MODE" in updates:
            from core.chatgpt_plan import PLAN_CHECK_PROXY_MODES

            mode = str(updates.get("PLAN_CHECK_PROXY_MODE") or "").strip().lower()
            if mode not in PLAN_CHECK_PROXY_MODES:
                choices = " / ".join(("auto", "proxy", "pool", "direct"))
                return jsonify({
                    "ok": False,
                    "error": f"ValueError: PLAN_CHECK_PROXY_MODE={mode!r} 无效，可选 {choices}",
                }), 400
            updates["PLAN_CHECK_PROXY_MODE"] = mode
        if any(key in updates for key in ("EXTRACT_LINK_PROVIDER", "EXTRACT_LINK_TYPE", "EXTRACT_LINK_UPDATE_MODE")):
            from config import extract_link as _extract_cfg
            try:
                extract_link_service.validate_route_combination(
                    provider=str(updates.get("EXTRACT_LINK_PROVIDER", _extract_cfg.EXTRACT_LINK_PROVIDER)),
                    link_type=str(updates.get("EXTRACT_LINK_TYPE", _extract_cfg.EXTRACT_LINK_TYPE)),
                    update_mode=str(updates.get("EXTRACT_LINK_UPDATE_MODE", _extract_cfg.EXTRACT_LINK_UPDATE_MODE)),
                )
            except ValueError as exc:
                return jsonify({"ok": False, "error": f"ValueError: {exc}"}), 400
        if "EXTRACT_LINK_PROXY" in updates:
            try:
                extract_link_service.validate_extract_proxy(updates.get("EXTRACT_LINK_PROXY"))
            except ValueError as exc:
                return jsonify({"ok": False, "error": f"ValueError: {exc}"}), 400
        smsbower_keys = {
            "SMSBOWER_API_BASE", "SMSBOWER_API_KEY", "SMSBOWER_SERVICE", "SMSBOWER_COUNTRY",
            "SMSBOWER_MIN_PRICE", "SMSBOWER_MAX_PRICE",
        }
        if smsbower_keys.intersection(updates) or str(updates.get("SMS_PROVIDER") or "").strip().lower() == "smsbower":
            from config import codex as _codex_cfg
            from core.sms_provider import SmsProviderError
            from core.smsbower_provider import validate_config_values

            merged = {key: getattr(_codex_cfg, key, "") for key in smsbower_keys}
            for key in smsbower_keys.intersection(updates):
                value = updates.get(key)
                if key == "SMSBOWER_API_KEY" and not str(value or "").strip():
                    continue
                merged[key] = value
            provider = str(updates.get("SMS_PROVIDER", getattr(_codex_cfg, "SMS_PROVIDER", "")) or "").strip().lower()
            try:
                validate_config_values(merged, require_required=provider == "smsbower")
            except (ValueError, SmsProviderError) as exc:
                return jsonify({"ok": False, "error": f"ValueError: {exc}"}), 400
        checkout_keys = {
            "CHECKOUT_SESSION_AUTO_CHECK", "CHECKOUT_SESSION_PROXY_MODE", "CHECKOUT_SESSION_PROXY",
            "CHECKOUT_SESSION_BILLING_COUNTRY", "CHECKOUT_SESSION_BILLING_CURRENCY",
            "CHECKOUT_SESSION_TIMEOUT", "CHECKOUT_SESSION_MAX_ATTEMPTS", "CHECKOUT_SESSION_RETRY_DELAY",
            "CHECKOUT_SESSION_WORKERS", "CHECKOUT_SESSION_QUEUE_LIMIT",
            "CHECKOUT_SESSION_MIN_INTERVAL", "CHECKOUT_SESSION_JITTER",
        }
        if checkout_keys.intersection(updates):
            from core.chatgpt_checkout import checkout_settings_from_config, validate_checkout_config_values

            current_checkout = checkout_settings_from_config()
            merged_checkout = {
                "CHECKOUT_SESSION_PROXY_MODE": current_checkout.proxy_mode,
                "CHECKOUT_SESSION_PROXY": current_checkout.proxy,
                "CHECKOUT_SESSION_BILLING_COUNTRY": current_checkout.billing_country,
                "CHECKOUT_SESSION_BILLING_CURRENCY": current_checkout.billing_currency,
                "CHECKOUT_SESSION_TIMEOUT": current_checkout.timeout,
                "CHECKOUT_SESSION_MAX_ATTEMPTS": current_checkout.max_attempts,
                "CHECKOUT_SESSION_RETRY_DELAY": current_checkout.retry_delay,
                "CHECKOUT_SESSION_WORKERS": current_checkout.workers,
                "CHECKOUT_SESSION_QUEUE_LIMIT": current_checkout.queue_limit,
                "CHECKOUT_SESSION_MIN_INTERVAL": current_checkout.min_interval,
                "CHECKOUT_SESSION_JITTER": current_checkout.jitter,
            }
            merged_checkout.update({key: value for key, value in updates.items() if key in checkout_keys})
            # secret 字段的空提交由 config_editor 保留旧值，校验时也沿用旧值。
            if "CHECKOUT_SESSION_PROXY" in updates and not str(updates.get("CHECKOUT_SESSION_PROXY") or "").strip():
                merged_checkout["CHECKOUT_SESSION_PROXY"] = current_checkout.proxy
            checkout_errors = validate_checkout_config_values(merged_checkout, require_request_values=False)
            if checkout_errors:
                return jsonify({"ok": False, "error": "ValueError: " + "；".join(checkout_errors)}), 400
        try:
            result = config_editor.update_config(updates)
        except ValueError as exc:
            return jsonify({"ok": False, "error": f"ValueError: {exc}"}), 400
        except Exception as exc:
            logger.exception("配置写入失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500

        # 写盘成功后立即热加载所有 config 子模块，让运行时代码看到新值。
        reload_ok = True
        reload_err = ""
        try:
            import config as _config_pkg
            _config_pkg.reload_all()
        except Exception as exc:
            reload_ok = False
            reload_err = f"{type(exc).__name__}: {exc}"
            logger.exception("配置热加载失败")

        return jsonify({
            "ok": True,
            "updated": result["updated"],
            "ignored": result["ignored"],
            "reloaded": reload_ok,
            "note": (
                "✅ 已保存并热加载，新值立即生效"
                if reload_ok
                else f"⚠️ 已写入文件但热加载失败（{reload_err}），需重启 Web 服务才能生效"
            ),
        })

    return app
