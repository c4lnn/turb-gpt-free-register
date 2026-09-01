"""账号状态/分类契约。

本模块只负责无副作用的规范化、分类和能力计算。原始字段由调用方保留，
这里返回的 code 才是 API、筛选、统计和动作判断使用的稳定值。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.chatgpt_checkout import classify_checkout_session_id


CODEX_AUTH_STATUSES = (
    "not_started", "success", "failed", "skipped", "unknown",
)
CODEX_OPERATION_STATUSES = (
    "idle", "queued", "running", "success", "failed", "canceled", "unknown",
)
PLAN_CATEGORY_CODES = (
    "free_trial_eligible", "free_no_trial", "paid", "unknown",
)
PLAN_QUERY_STATUSES = (
    "pending", "queued", "running", "success", "failed", "unknown",
)
EXTRACT_LINK_STATUSES = (
    "pending", "queued", "running", "success", "failed", "canceled", "unknown",
)
CHECKOUT_SESSION_TYPES = ("oaics", "cs_live", "other_cs", "unknown")
CHECKOUT_QUERY_STATUSES = ("pending", "queued", "running", "success", "failed", "unknown")
LIVE_CHECK_STATUSES = (
    "pending", "queued", "running", "live", "deactivated", "failed", "unknown",
)

_CODEX_AUTH_SET = frozenset(CODEX_AUTH_STATUSES)
_CODEX_OPERATION_SET = frozenset(CODEX_OPERATION_STATUSES)
_PLAN_QUERY_SET = frozenset(PLAN_QUERY_STATUSES)
_EXTRACT_SET = frozenset(EXTRACT_LINK_STATUSES)
_LIVE_SET = frozenset(LIVE_CHECK_STATUSES)
_CHECKOUT_QUERY_SET = frozenset(CHECKOUT_QUERY_STATUSES)

# 这是语义集合，不是对上游套餐开放集合的完整枚举。其它值保留为 raw，
# 只有明确识别的付费语义才归入 paid。
KNOWN_PAID_PLAN_TYPES = frozenset({
    "plus", "pro", "team", "business", "enterprise", "go",
    "chatgpt_plus", "chatgpt_pro", "chatgpt_team", "chatgpt_business",
})
FREE_PLAN_TYPES = frozenset({"free", "chatgptfreeplan", "chatgpt_free"})


def _text(value: Any) -> str:
    return str(value or "").strip().lower()


def normalize_codex_auth_status(raw: Any) -> str:
    value = _text(raw)
    if value in _CODEX_AUTH_SET:
        return value
    # retrying/stopped/deactivated 属于历史混合字段，不能伪造为授权结果。
    if value in {"retrying", "stopped", "deactivated", "canceled", "cancelled"}:
        return "unknown"
    if not value:
        return "not_started"
    return "unknown"


def normalize_codex_operation_status(raw: Any) -> str:
    value = _text(raw)
    aliases = {
        "retrying": "running",
        "stopped": "canceled",
        "cancelled": "canceled",
        "": "idle",
    }
    return aliases.get(value, value if value in _CODEX_OPERATION_SET else "unknown")


def codex_auth_status(row: Mapping[str, Any] | None) -> str:
    row = row or {}
    explicit = row.get("codex_auth_status")
    if explicit is not None and _text(explicit):
        return normalize_codex_auth_status(explicit)
    return normalize_codex_auth_status(row.get("codex_status"))


def codex_operation_status(row: Mapping[str, Any] | None) -> str:
    row = row or {}
    explicit = row.get("codex_operation_status")
    if explicit is not None and _text(explicit):
        return normalize_codex_operation_status(explicit)
    return normalize_codex_operation_status(row.get("codex_status")) if _text(row.get("codex_status")) in {
        "retrying", "stopped", "cancelled", "canceled",
    } else "idle"


def codex_capabilities(
    auth_status: str,
    operation_status: str,
    *,
    live_status: str | None = None,
) -> dict[str, bool]:
    auth = normalize_codex_auth_status(auth_status)
    operation = normalize_codex_operation_status(operation_status)
    live = _text(live_status)
    blocked = live == "deactivated"
    running = operation in {"queued", "running"}
    return {
        "is_running": running,
        "is_terminal": operation in {"success", "failed", "canceled"},
        "can_retry": not blocked and not running and auth in {"not_started", "failed", "skipped"},
        "can_stop": running,
        "can_start": not blocked and not running and auth != "unknown",
    }


def classify_plan_category(row: Mapping[str, Any] | None) -> str:
    row = row or {}
    query_status = normalize_plan_query_status(row.get("plan_check_status"))
    if query_status == "failed" and not row.get("plan_last_success_at"):
        return "unknown"
    if query_status in {"queued", "running"} and not row.get("plan_last_success_at"):
        return "unknown"
    plan = _text(row.get("current_plan_type")) or _text(row.get("plan_type"))
    if not plan:
        return "unknown"
    if plan in FREE_PLAN_TYPES:
        if row.get("trial_eligibility_known") is not True:
            return "unknown"
        if row.get("plus_trial_eligible") is True:
            return "free_trial_eligible"
        if row.get("plus_trial_eligible") is False:
            return "free_no_trial"
        return "unknown"
    if plan in KNOWN_PAID_PLAN_TYPES:
        return "paid"
    return "unknown"


def normalize_plan_query_status(raw: Any) -> str:
    value = _text(raw)
    return value if value in _PLAN_QUERY_SET else ("pending" if not value else "unknown")


def plan_capabilities(
    category: str,
    query_status: str,
    *,
    has_access_token: bool = True,
) -> dict[str, bool]:
    category = category if category in PLAN_CATEGORY_CODES else "unknown"
    query = normalize_plan_query_status(query_status)
    checking = query in {"pending", "queued", "running"}
    return {
        "is_checking": checking,
        "is_terminal": query in {"success", "failed"},
        "is_eligible": category == "free_trial_eligible" and query != "failed",
        "can_start": bool(has_access_token) and not checking and query != "unknown",
        "has_access_token": bool(has_access_token),
    }


def normalize_checkout_query_status(raw: Any) -> str:
    value = _text(raw)
    return value if value in _CHECKOUT_QUERY_SET else ("pending" if not value else "unknown")


def checkout_capabilities(status: Any, *, has_access_token: bool = True) -> dict[str, bool]:
    value = normalize_checkout_query_status(status)
    running = value in {"queued", "running"}
    actionable = value in {"pending", "success", "failed"} and bool(has_access_token)
    return {
        "is_checking": running,
        "is_terminal": value in {"success", "failed"},
        "can_retry": actionable and not running,
        "can_start": actionable and not running,
        "can_stop": False,
        "has_access_token": bool(has_access_token),
    }


def mailcom_cleanup_capabilities(status: Any, *, plan_category: str = "unknown") -> dict[str, bool]:
    value = _text(status) or "pending"
    running = value == "cleanup_running"
    return {
        "is_running": running,
        "is_terminal": value in {"not_eligible", "not_requested", "deleted", "cleanup_pending"},
        "can_cleanup": plan_category == "free_no_trial" and value in {"pending", "not_requested"},
        "can_retry": value == "cleanup_pending",
    }


def normalize_extract_link_status(raw: Any) -> str:
    value = _text(raw)
    aliases = {"cancelled": "canceled", "stopped": "canceled", "": "pending"}
    return aliases.get(value, value if value in _EXTRACT_SET else "unknown")


def extract_link_capabilities(
    status: Any,
    *,
    resumable: bool = False,
    has_access_token: bool = True,
) -> dict[str, bool]:
    value = normalize_extract_link_status(status)
    running = value in {"queued", "running"}
    return {
        "is_running": running,
        "is_terminal": value in {"success", "failed", "canceled"},
        "can_retry": value in {"failed", "canceled"} and (resumable or value == "canceled"),
        "can_start": bool(has_access_token) and value in {"pending", "failed", "canceled"} and not running,
        "can_stop": running,
        "resumable": bool(resumable),
        "has_access_token": bool(has_access_token),
    }


def normalize_live_check_status(raw: Any) -> str:
    value = _text(raw)
    aliases = {"live": "live", "success": "live", "": "pending"}
    return aliases.get(value, value if value in _LIVE_SET else "unknown")


def live_check_capabilities(status: Any) -> dict[str, bool]:
    value = normalize_live_check_status(status)
    running = value in {"queued", "running"}
    return {
        "is_running": running,
        "is_terminal": value in {"live", "deactivated", "failed"},
        "can_retry": value in {"pending", "live", "deactivated", "failed"} and not running,
        "can_start": value in {"pending", "live", "deactivated", "failed"} and not running,
        "account_available": value == "live",
    }


def classify_checkout_session_type(session_id: Any) -> str:
    return classify_checkout_session_id(str(session_id or "").strip())


def build_account_status_contract(row: Mapping[str, Any] | None) -> dict[str, Any]:
    """生成可直接合并进账号 DTO 的非敏感规范状态摘要。"""
    row = row or {}
    auth = codex_auth_status(row)
    operation = codex_operation_status(row)
    plan = classify_plan_category(row)
    plan_query = normalize_plan_query_status(row.get("plan_check_status"))
    extract = normalize_extract_link_status(row.get("extract_link_status"))
    live = normalize_live_check_status(row.get("live_check_status"))
    return {
        "codex_auth_status": auth,
        "codex_operation_status": operation,
        "codex_capabilities": codex_capabilities(auth, operation, live_status=live),
        "plan_category_code": plan,
        "plan_query_status": plan_query,
        "plan_capabilities": plan_capabilities(
            plan,
            plan_query,
            has_access_token=bool(str(row.get("access_token") or "").strip()),
        ),
        "checkout_query_status": normalize_checkout_query_status(row.get("checkout_check_status")),
        "checkout_capabilities": checkout_capabilities(
            row.get("checkout_check_status"),
            has_access_token=bool(str(row.get("access_token") or "").strip()),
        ),
        "extract_link_status": extract,
        "extract_link_capabilities": extract_link_capabilities(
            extract,
            resumable=bool(row.get("extract_link_resumable")),
            has_access_token=bool(str(row.get("access_token") or "").strip()),
        ),
        "live_check_status": live,
        "live_check_capabilities": live_check_capabilities(live),
        "checkout_session_type": classify_checkout_session_type(row.get("checkout_session_id"))
        if row.get("checkout_session_id")
        else (_text(row.get("checkout_session_type"))
              if _text(row.get("checkout_session_type")) in CHECKOUT_SESSION_TYPES
              else "unknown"),
    }


__all__ = [
    "CHECKOUT_QUERY_STATUSES", "CHECKOUT_SESSION_TYPES", "CODEX_AUTH_STATUSES", "CODEX_OPERATION_STATUSES",
    "EXTRACT_LINK_STATUSES", "LIVE_CHECK_STATUSES", "PLAN_CATEGORY_CODES",
    "PLAN_QUERY_STATUSES", "build_account_status_contract", "classify_checkout_session_type",
    "classify_plan_category", "codex_auth_status", "codex_capabilities",
    "codex_operation_status", "extract_link_capabilities", "normalize_codex_auth_status",
    "normalize_codex_operation_status", "normalize_extract_link_status",
    "normalize_live_check_status", "normalize_plan_query_status", "plan_capabilities",
    "live_check_capabilities", "checkout_capabilities", "normalize_checkout_query_status",
    "mailcom_cleanup_capabilities",
]
