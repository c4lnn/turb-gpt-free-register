# -*- coding: utf-8 -*-
"""套餐查询完成后的 mail.com 别名一次性清理。"""
from __future__ import annotations

import logging
from typing import Any, Callable

from config import email as email_cfg
from core import db
from core.mailcom_alias_service import MailComAliasError


logger = logging.getLogger(__name__)


def _mask_alias(email: str) -> str:
    local, separator, domain = str(email or "").partition("@")
    return f"{local[:1]}***@{domain}" if separator else "[redacted-email]"


def _result_error_type(exc: BaseException) -> str:
    return str(getattr(exc, "error_type", "cleanup_error") or "cleanup_error")[:80]


def process_plan_result(
    *,
    account_id: int,
    result: dict[str, Any] | None,
    delete_alias_fn: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    """将套餐结果映射为别名清理状态，并在唯一安全条件下执行一次删除。

    该函数故意不抛出套餐或 mail.com 协议错误。套餐查询已经完成，删除失败
    只应保留别名并留下脱敏的 ``cleanup_pending`` 状态。
    """
    result = result or {}
    alias = db.get_mailcom_alias_by_account(int(account_id))
    if alias is None:
        return {"handled": False, "reason": "not_mailcom_alias"}

    alias_email = str(alias.get("alias_email") or "")
    account = db.get_account(int(account_id))
    if account is None:
        db.update_mailcom_alias(
            alias_email,
            plan_check_status="incomplete",
            cleanup_status="not_requested",
            plan_result_class="unknown",
            last_error="关联账号不存在，未执行别名清理",
        )
        return {"handled": True, "deleted": False, "reason": "account_missing"}

    if bool(account.get("archived")):
        db.update_mailcom_alias(
            alias_email,
            plan_check_status="success" if result.get("ok") else "failed",
            cleanup_status="not_requested",
            plan_result_class="unknown",
            last_error="关联账号已归档，未执行别名清理",
        )
        return {"handled": True, "deleted": False, "reason": "account_archived"}

    if not bool(result.get("ok")):
        db.update_mailcom_alias(
            alias_email,
            plan_check_status="failed",
            cleanup_status="not_requested",
            plan_result_class="unknown",
            last_error="套餐查询失败，未执行别名清理",
        )
        return {"handled": True, "deleted": False, "reason": "plan_query_failed"}

    if result.get("trial_eligibility_known") is not True:
        db.update_mailcom_alias(
            alias_email,
            plan_check_status="incomplete",
            cleanup_status="not_requested",
            plan_result_class="unknown",
            last_error="套餐试用资格字段不完整，未执行别名清理",
        )
        return {"handled": True, "deleted": False, "reason": "trial_eligibility_unknown"}

    plan_type = str(account.get("current_plan_type") or account.get("plan_type") or "").strip().casefold()
    if plan_type != "free":
        db.update_mailcom_alias(
            alias_email,
            plan_check_status="success",
            cleanup_status="not_eligible",
            plan_result_class="non_free" if plan_type else "unknown",
            last_error="" if plan_type else "套餐类型不明确，未执行别名清理",
        )
        return {"handled": True, "deleted": False, "reason": "non_free" if plan_type else "plan_unknown"}

    if result.get("plus_trial_eligible") is True:
        db.update_mailcom_alias(
            alias_email,
            plan_check_status="success",
            cleanup_status="not_eligible",
            plan_result_class="trial_eligible",
            last_error="",
        )
        return {"handled": True, "deleted": False, "reason": "trial_eligible"}

    # 只接受 JSON 布尔 false；0、空字符串、None 等不具备“明确无资格”的含义。
    if result.get("plus_trial_eligible") is not False:
        db.update_mailcom_alias(
            alias_email,
            plan_check_status="incomplete",
            cleanup_status="not_requested",
            plan_result_class="unknown",
            last_error="套餐试用资格不是明确布尔值，未执行别名清理",
        )
        return {"handled": True, "deleted": False, "reason": "trial_eligibility_unknown"}

    if not bool(getattr(email_cfg, "MAILCOM_DELETE_ALIAS_IF_NO_TRIAL", False)):
        db.update_mailcom_alias(
            alias_email,
            plan_check_status="success",
            cleanup_status="not_requested",
            plan_result_class="eligible_for_delete",
            last_error="",
        )
        return {"handled": True, "deleted": False, "reason": "cleanup_disabled"}

    claimed = db.claim_mailcom_alias_cleanup(int(account_id))
    if claimed is None:
        # deleted、cleanup_pending 或其他任务正在删除都不可再次发起写请求。
        return {"handled": True, "deleted": False, "reason": "cleanup_already_handled"}

    delete = delete_alias_fn
    if delete is None:
        from core.mailcom_alias_service import delete_alias

        delete = delete_alias
    try:
        confirmed = bool(delete(claimed))
    except MailComAliasError as exc:
        db.mark_mailcom_alias_cleanup_pending(alias_email, _result_error_type(exc))
        logger.warning("[MailComAlias] 自动清理失败: alias=%s type=%s", _mask_alias(alias_email), _result_error_type(exc))
        return {"handled": True, "deleted": False, "reason": "delete_failed", "error_type": _result_error_type(exc)}
    except Exception as exc:  # pragma: no cover - 防止后台线程因插件异常退出
        db.mark_mailcom_alias_cleanup_pending(alias_email, type(exc).__name__)
        logger.warning("[MailComAlias] 自动清理异常: alias=%s type=%s", _mask_alias(alias_email), type(exc).__name__)
        return {"handled": True, "deleted": False, "reason": "delete_failed", "error_type": type(exc).__name__}

    if not confirmed:
        db.mark_mailcom_alias_cleanup_pending(alias_email, "删除接口未确认")
        return {"handled": True, "deleted": False, "reason": "delete_unconfirmed"}

    # 默认 service 已在列表回读成功后标记 deleted；显式补一次是幂等的，
    # 也支持注入的测试 delete 函数。
    db.mark_mailcom_alias_deleted(alias_email)
    try:
        from core.mailcom_alias_pool_service import enqueue_parent_sync

        enqueue_parent_sync(str(claimed.get("parent_email") or ""))
    except Exception as exc:  # pragma: no cover - 删除结果优先于补齐入队
        logger.warning("[MailComAlias] 删除后补齐入队失败: alias=%s type=%s", _mask_alias(alias_email), type(exc).__name__)
    logger.info("[MailComAlias] 套餐明确无试用资格，已删除别名: %s", _mask_alias(alias_email))
    return {"handled": True, "deleted": True, "reason": "deleted"}


__all__ = ["process_plan_result"]
