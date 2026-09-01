# -*- coding: utf-8 -*-
"""ChatGPT 账号套餐/试用资格查询。"""
from __future__ import annotations

import base64
import ipaddress
import json
import logging
from math import isfinite
import socket
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote, urlparse

from core.session import BrowserSession

logger = logging.getLogger(__name__)

ACCOUNTS_CHECK_PATH = "/backend-api/accounts/check/v4-2023-04-27"
PLAN_CHECK_PROXY_MODES = frozenset({"auto", "proxy", "pool", "direct"})
_PUBLIC_ROUTE_META_KEYS = (
    "proxy_mode",
    "network_route",
    "proxy_used",
    "proxy_fallback_reason",
)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_token(token: str) -> str:
    token = (token or "").strip().strip('"').strip("'")
    if token.lower().startswith("authorization:"):
        token = token.split(":", 1)[1].strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def discount_percentage_state(value: Any) -> str:
    """分类服务端折扣比例，避免布尔值被当作 100%。"""
    if value is None or isinstance(value, bool):
        return "invalid"
    try:
        percentage = float(value)
    except (TypeError, ValueError):
        return "invalid"
    if not isfinite(percentage):
        return "invalid"
    return "full" if percentage == 100.0 else "non_full"


def is_full_discount_percentage(value: Any) -> bool:
    return discount_percentage_state(value) == "full"


def _is_timeout_exception(exc: BaseException) -> bool:
    """识别不同 HTTP 客户端可能使用的超时异常类型。"""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, TimeoutError):
            return True
        name = type(current).__name__.lower()
        if "timeout" in name or "timedout" in name:
            return True
        current = current.__cause__ or current.__context__
    return False


def classify_plan_check_error(
    *,
    http_status: int | None = None,
    exc: BaseException | None = None,
    response_format: bool = False,
    local_token_expired: bool = False,
) -> str | None:
    """将套餐查询最终失败归一为供 WebUI 使用的非敏感分类。"""
    try:
        status = int(http_status) if http_status is not None else None
    except (TypeError, ValueError):
        status = None
    if status is not None:
        if 400 <= status < 500:
            return "http_4xx"
        if 500 <= status < 600:
            return "http_5xx"
    if response_format:
        return "response_format"
    if local_token_expired:
        return "local_token_expired"
    if exc is not None:
        return "network_timeout" if _is_timeout_exception(exc) else "network_connection"
    return None


def _mask_proxy(proxy: str) -> str:
    """返回可用于日志/API 结果的代理摘要，不泄露用户名和密码。"""
    value = str(proxy or "").strip()
    if not value:
        return ""
    try:
        parsed = urlparse(value if "://" in value else f"//{value}")
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        scheme = f"{parsed.scheme}://" if parsed.scheme else ""
        auth = "***:***@" if parsed.username or parsed.password else ""
        return f"{scheme}{auth}{host}{port}" or "***"
    except Exception:
        return "***"


def _local_proxy_status(proxy: str) -> tuple[bool, bool, str | None]:
    """检查回环代理端口；非本地代理不做预探测，避免额外网络请求。"""
    value = str(proxy or "").strip()
    if not value:
        return False, False, None
    try:
        parsed = urlparse(value if "://" in value else f"//{value}")
        host = parsed.hostname or ""
        is_loopback = host.lower() == "localhost"
        if not is_loopback:
            try:
                is_loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                is_loopback = False
        if not is_loopback:
            return False, True, None
        if not parsed.port:
            return True, False, "本地代理未配置端口"
        try:
            with socket.create_connection((host, parsed.port), timeout=0.5):
                return True, True, None
        except OSError as exc:
            return True, False, f"本地代理 {host}:{parsed.port} 未监听（{type(exc).__name__}）"
    except Exception as exc:
        return False, False, f"代理地址解析失败（{type(exc).__name__}）"


def plan_check_route_metadata(route: dict) -> dict:
    """返回可落库或公开的套餐网络诊断字段。"""
    return {key: route.get(key) for key in _PUBLIC_ROUTE_META_KEYS}


def resolve_plan_check_route(explicit_proxy: Optional[str] = None) -> dict:
    """解析套餐查询的实际网络路径。

    explicit_proxy 不是 None 时表示 API 调用方明确覆盖配置；空字符串代表直连。
    """
    if explicit_proxy is not None:
        selected = str(explicit_proxy or "").strip()
        return {
            "proxy": selected,
            "proxy_mode": "request",
            "network_route": "proxy" if selected else "direct",
            "proxy_used": _mask_proxy(selected) or None,
            "proxy_fallback_reason": None,
            "allow_direct_fallback": bool(selected),
        }

    from config import proxy as proxy_cfg

    mode = str(getattr(proxy_cfg, "PLAN_CHECK_PROXY_MODE", "auto") or "auto").strip().lower()
    if mode not in PLAN_CHECK_PROXY_MODES:
        choices = " / ".join(("auto", "proxy", "pool", "direct"))
        raise ValueError(f"PLAN_CHECK_PROXY_MODE={mode!r} 无效，可选 {choices}")
    if mode == "direct":
        return {
            "proxy": "",
            "proxy_mode": mode,
            "network_route": "direct",
            "proxy_used": None,
            "proxy_fallback_reason": None,
            "allow_direct_fallback": False,
        }

    if mode == "pool":
        selected = str(proxy_cfg.pick_proxy() or "").strip()
        if not selected:
            raise ValueError("套餐查询网络模式为 pool，但 PROXY_POOL 为空")
    else:
        selected = str(getattr(proxy_cfg, "PLAN_CHECK_PROXY", "") or "").strip()
        if not selected:
            selected = str(proxy_cfg.pick_proxy() or "").strip()
    if not selected:
        if mode == "proxy":
            raise ValueError("套餐查询网络模式为 proxy，但未配置 PLAN_CHECK_PROXY 或 PROXY_POOL")
        return {
            "proxy": "",
            "proxy_mode": mode,
            "network_route": "direct",
            "proxy_used": None,
            "proxy_fallback_reason": "未配置套餐查询代理或代理池",
            "allow_direct_fallback": False,
        }

    is_local, available, reason = _local_proxy_status(selected)
    if mode == "auto" and is_local and not available:
        return {
            "proxy": "",
            "proxy_mode": mode,
            "network_route": "direct_fallback",
            "proxy_used": _mask_proxy(selected),
            "proxy_fallback_reason": reason,
            "allow_direct_fallback": False,
        }
    return {
        "proxy": selected,
        "proxy_mode": mode,
        "network_route": "proxy",
        "proxy_used": _mask_proxy(selected),
        "proxy_fallback_reason": None,
        "allow_direct_fallback": mode != "pool",
    }


def decode_jwt_payload_unverified(token: str) -> dict:
    """仅本地解析 JWT payload，不校验签名。"""
    token = normalize_token(token)
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return {}


def token_claims(token: str) -> dict:
    payload = decode_jwt_payload_unverified(token)
    auth = payload.get("https://api.openai.com/auth") or {}
    profile = payload.get("https://api.openai.com/profile") or {}
    exp = payload.get("exp")
    exp_iso = None
    expired = None
    if isinstance(exp, (int, float)) and not isinstance(exp, bool):
        try:
            exp_value = float(exp)
            if isfinite(exp_value):
                exp_iso = datetime.fromtimestamp(exp_value, tz=timezone.utc).isoformat().replace("+00:00", "Z")
                expired = datetime.now(tz=timezone.utc).timestamp() >= exp_value
        except (OverflowError, OSError, TypeError, ValueError):
            pass
    return {
        "payload": payload,
        "email": profile.get("email"),
        "user_name": profile.get("name"),
        "user_id": auth.get("chatgpt_user_id") or auth.get("user_id"),
        "account_id": auth.get("chatgpt_account_id"),
        "claim_plan_type": auth.get("chatgpt_plan_type"),
        "exp": exp,
        "token_expires_at": exp_iso,
        "token_expired": expired,
    }


def _common_headers(env: BrowserSession, token: str) -> dict[str, str]:
    headers = env._get_common_headers()
    headers.update({
        "accept": "*/*",
        "authorization": f"Bearer {normalize_token(token)}",
        "oai-device-id": env.device_id,
        "oai-language": env.navigator_language(),
        "referer": "https://chatgpt.com/",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "x-openai-target-path": ACCOUNTS_CHECK_PATH,
        "x-openai-target-route": ACCOUNTS_CHECK_PATH,
    })
    return headers


def parse_accounts_check(data: dict, *, token: str = "") -> dict:
    """从 accounts/check 响应提取套餐和 Plus 试用资格。"""
    claims = token_claims(token) if token else {}
    claim_account_id = claims.get("account_id")
    accounts = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(accounts, dict):
        raise ValueError("响应缺少 accounts 对象")

    item = None
    account_key = None
    if claim_account_id and isinstance(accounts.get(claim_account_id), dict):
        item = accounts.get(claim_account_id)
        account_key = claim_account_id
    elif isinstance(accounts.get("default"), dict):
        item = accounts.get("default")
        account = item.get("account") or {}
        account_key = account.get("account_id") or "default"
    else:
        for k, v in accounts.items():
            if k != "default" and isinstance(v, dict):
                item = v
                account_key = k
                break
    if not isinstance(item, dict):
        raise ValueError("未找到可解析的账号条目")

    account = item.get("account") or {}
    entitlement = item.get("entitlement") or {}
    last_sub = item.get("last_active_subscription") or {}
    # 空对象代表服务端已明确给出“当前没有可用促销”；字段缺失或非对象则
    # 不能把普通 False 当成“明确无试用资格”。
    eligible_promo_campaigns_raw = item.get("eligible_promo_campaigns")
    eligible_promo_campaigns = (
        eligible_promo_campaigns_raw
        if isinstance(eligible_promo_campaigns_raw, dict)
        else {}
    )
    plus_campaign = eligible_promo_campaigns.get("plus")
    plus_campaign_id = plus_campaign.get("id") if isinstance(plus_campaign, dict) else None
    plus_meta = plus_campaign.get("metadata") if isinstance(plus_campaign, dict) else {}
    plus_meta = plus_meta if isinstance(plus_meta, dict) else {}
    discount = plus_meta.get("discount") or {}
    discount = discount if isinstance(discount, dict) else {}
    discount_percentage = discount.get("percentage") if "percentage" in discount else None
    discount_state = discount_percentage_state(discount_percentage)
    # 空的促销对象是服务端明确的“没有优惠”；一旦 plus 存在，必须同时
    # 提供活动 ID、折扣对象和有效比例，才能把“不符合 0 元资格”视为已知。
    plus_campaign_shape_valid = (
        "plus" not in eligible_promo_campaigns
        or (
            isinstance(plus_campaign, dict)
            and bool(str(plus_campaign_id or "").strip())
            and isinstance(plus_campaign.get("metadata"), dict)
            and isinstance(plus_meta.get("discount"), dict)
            and "percentage" in discount
            and discount_state != "invalid"
        )
    )
    duration = plus_meta.get("duration") or {}
    duration = duration if isinstance(duration, dict) else {}

    plan_type = account.get("plan_type") or claims.get("claim_plan_type") or ""
    subscription_plan = entitlement.get("subscription_plan") or ""
    has_active_subscription = bool(entitlement.get("has_active_subscription"))
    is_free = str(plan_type).lower() == "free" or str(subscription_plan).lower() == "chatgptfreeplan"
    plan_evidence_present = bool(
        str(account.get("plan_type") or "").strip()
        or str(entitlement.get("subscription_plan") or "").strip()
    )
    trial_eligibility_known = bool(
        plan_evidence_present
        and isinstance(eligible_promo_campaigns_raw, dict)
        and plus_campaign_shape_valid
    )
    # 未拿到完整的促销字段时，必须保留未知态，不能把它压成普通 False。
    # 后续 mail.com 别名清理只接受明确的 JSON false。
    plus_trial_eligible = (
        bool(
            is_free
            and str(plus_campaign_id or "").strip() == "plus-1-month-free"
            and discount_state == "full"
        )
        if trial_eligibility_known
        else None
    )

    offers = ((item.get("eligible_offers") or {}).get("offers") or [])
    eligible_offer_ids = [o.get("id") for o in offers if isinstance(o, dict) and o.get("id")]

    result = {
        "ok": True,
        "checked_at": now_iso(),
        "account_id": account.get("account_id") or account_key or claim_account_id,
        "account_user_role": account.get("account_user_role"),
        "current_plan_type": plan_type,
        "subscription_plan": subscription_plan,
        "has_active_subscription": has_active_subscription,
        "is_active_subscription_gratis": bool(entitlement.get("is_active_subscription_gratis")),
        "expires_at": entitlement.get("expires_at"),
        "renews_at": entitlement.get("renews_at"),
        "cancels_at": entitlement.get("cancels_at"),
        "billing_period": entitlement.get("billing_period"),
        "billing_currency": entitlement.get("billing_currency"),
        "is_delinquent": bool(entitlement.get("is_delinquent")),
        "discount_type": (entitlement.get("discount") or {}).get("discount_type"),
        "discount_amount": (entitlement.get("discount") or {}).get("amount"),
        "discount_duration_num_periods": (entitlement.get("discount") or {}).get("duration_num_periods"),
        "discount_expires_at": (entitlement.get("discount") or {}).get("discount_expires_at"),
        "discount_cancellation_policy": (entitlement.get("discount") or {}).get("cancellation_policy"),
        "discount_promo_campaign_id": (entitlement.get("discount") or {}).get("promo_campaign_id"),
        "last_purchase_origin_platform": last_sub.get("purchase_origin_platform"),
        "last_will_renew": bool(last_sub.get("will_renew")),
        "plus_trial_eligible": plus_trial_eligible,
        "trial_eligibility_known": trial_eligibility_known,
        "plus_trial_campaign_id": plus_campaign_id,
        "plus_trial_title": plus_meta.get("title"),
        "plus_trial_summary": plus_meta.get("summary"),
        "plus_trial_discount_percentage": discount_percentage,
        "plus_trial_duration_num_periods": duration.get("num_periods"),
        "plus_trial_duration_period": duration.get("period"),
        "plus_trial_promotion_type_label": plus_meta.get("promotion_type_label"),
        "eligible_offer_ids": eligible_offer_ids,
        "features_count": len(item.get("features") or []),
        "can_access_with_session": bool(item.get("can_access_with_session")),
        "raw_account_plan_type": account.get("plan_type"),
    }
    result.update({k: v for k, v in claims.items() if k != "payload" and v is not None})
    return result


def _plan_check_settings(
    timeout: float | None,
    max_attempts: int | None,
    retry_delay: float | None,
) -> tuple[float, int, float]:
    from config import proxy as proxy_cfg

    timeout_value = timeout if timeout is not None else getattr(proxy_cfg, "PLAN_CHECK_TIMEOUT", 15.0)
    attempts_value = max_attempts if max_attempts is not None else getattr(proxy_cfg, "PLAN_CHECK_MAX_ATTEMPTS", 2)
    delay_value = retry_delay if retry_delay is not None else getattr(proxy_cfg, "PLAN_CHECK_RETRY_DELAY", 1.5)
    return (
        max(1.0, min(60.0, float(timeout_value or 15.0))),
        max(1, min(4, int(attempts_value or 1))),
        max(0.0, min(30.0, float(delay_value or 0.0))),
    )


def _retryable_plan_error(http_status: int | None) -> bool:
    if http_status is None:
        return True
    return http_status in {408, 409, 425, 429} or http_status >= 500


def _retry_wait_seconds(resp: Any, base_delay: float, attempt: int) -> float:
    try:
        retry_after = (getattr(resp, "headers", {}) or {}).get("retry-after")
        if retry_after is not None:
            return max(0.0, min(30.0, float(retry_after)))
    except (TypeError, ValueError):
        pass
    return max(0.0, min(30.0, base_delay * attempt))


def check_account_plan(
    token: str,
    *,
    proxy: Optional[str] = None,
    timezone_offset_min: str = "-",
    timeout: float | None = None,
    max_attempts: int | None = None,
    retry_delay: float | None = None,
) -> dict:
    token = normalize_token(token)
    if not token:
        return {"ok": False, "checked_at": now_iso(), "error": "token 为空"}
    claims = token_claims(token)
    if claims.get("token_expired") is True:
        return {
            "ok": False,
            "checked_at": now_iso(),
            "http_status": None,
            "plan_check_error_kind": classify_plan_check_error(local_token_expired=True),
            "error": "本地AT已失效，请手动查活刷新",
            "needs_live_check": True,
            **{k: v for k, v in claims.items() if k != "payload"},
        }

    try:
        route = resolve_plan_check_route(proxy)
    except Exception as exc:
        return {
            "ok": False,
            "checked_at": now_iso(),
            "http_status": None,
            "error": f"套餐查询网络配置错误: {exc}",
            **{k: v for k, v in claims.items() if k != "payload"},
        }
    route_meta = plan_check_route_metadata(route)
    url = f"https://chatgpt.com{ACCOUNTS_CHECK_PATH}?timezone_offset_min={quote(str(timezone_offset_min))}"
    try:
        timeout_seconds, attempts, base_delay = _plan_check_settings(timeout, max_attempts, retry_delay)
    except Exception as exc:
        return {
            "ok": False,
            "checked_at": now_iso(),
            "http_status": None,
            "error": f"套餐查询重试配置错误: {exc}",
            "retryable": False,
            **route_meta,
            **{k: v for k, v in claims.items() if k != "payload"},
        }

    last_result: dict | None = None
    for attempt in range(1, attempts + 1):
        env = None
        resp = None
        try:
            # 套餐查询只需要稳定的请求头，不需要额外访问 IP 地理信息接口。
            env = BrowserSession(proxy=route["proxy"], detect_exit_geo=False)
            resp = env.session.get(
                url,
                headers=_common_headers(env, token),
                allow_redirects=False,
                timeout=timeout_seconds,
            )
            response_text = resp.text or ""
            http_status = int(resp.status_code)
            if not (200 <= http_status < 300):
                is_auth_expired = http_status == 401
                last_result = {
                    "ok": False,
                    "checked_at": now_iso(),
                    "http_status": http_status,
                    "plan_check_error_kind": classify_plan_check_error(http_status=http_status),
                    "error": "AT已失效，请手动查活刷新" if is_auth_expired else f"HTTP {http_status}",
                    "response_preview": response_text[:500],
                    "retryable": _retryable_plan_error(http_status),
                    "token_expired": True if is_auth_expired else claims.get("token_expired"),
                    "needs_live_check": True if is_auth_expired else False,
                }
            else:
                try:
                    data: Any = resp.json()
                except Exception:
                    data = json.loads(response_text) if response_text.strip().startswith(("{", "[")) else None
                if not isinstance(data, dict):
                    last_result = {
                        "ok": False,
                        "checked_at": now_iso(),
                        "http_status": http_status,
                        "plan_check_error_kind": classify_plan_check_error(
                            http_status=http_status,
                            response_format=True,
                        ),
                        "error": "响应不是 JSON 对象",
                        "response_preview": response_text[:500],
                        "retryable": True,
                    }
                else:
                    try:
                        parsed = parse_accounts_check(data, token=token)
                    except Exception as exc:
                        last_result = {
                            "ok": False,
                            "checked_at": now_iso(),
                            "http_status": http_status,
                            "plan_check_error_kind": classify_plan_check_error(
                                http_status=http_status,
                                response_format=True,
                            ),
                            "error": f"{type(exc).__name__}: {exc}",
                            "response_preview": response_text[:500],
                            "retryable": True,
                        }
                    else:
                        parsed["http_status"] = http_status
                        parsed["attempt_count"] = attempt
                        parsed["max_attempts"] = attempts
                        parsed["request_timeout"] = timeout_seconds
                        parsed["retryable"] = False
                        parsed.update(route_meta)
                        return parsed
        except Exception as exc:
            logger.debug("套餐查询失败: %s: %s", type(exc).__name__, exc, exc_info=True)
            http_status = (
                int(resp.status_code)
                if resp is not None and getattr(resp, "status_code", None)
                else None
            )
            last_result = {
                "ok": False,
                "checked_at": now_iso(),
                "http_status": http_status,
                "plan_check_error_kind": classify_plan_check_error(
                    http_status=http_status,
                    exc=exc,
                ),
                "error": f"{type(exc).__name__}: {exc}",
                "retryable": True,
            }
        finally:
            if env is not None:
                try:
                    env.session.close()
                except Exception:
                    pass

        last_result = last_result or {"ok": False, "checked_at": now_iso(), "error": "未知错误", "retryable": True}
        last_result.update({
            "attempt_count": attempt,
            "max_attempts": attempts,
            "request_timeout": timeout_seconds,
            **route_meta,
            **{k: v for k, v in claims.items() if k != "payload"},
        })
        if not last_result.get("retryable") or attempt >= attempts:
            return last_result

        wait_seconds = _retry_wait_seconds(resp, base_delay, attempt)
        logger.warning(
            "套餐查询临时失败，第 %s/%s 次，%.1fs 后重试: %s",
            attempt,
            attempts,
            wait_seconds,
            last_result.get("error"),
        )
        if wait_seconds > 0:
            time.sleep(wait_seconds)

    return last_result or {
        "ok": False,
        "checked_at": now_iso(),
        "http_status": None,
        "error": "套餐查询未执行",
        "retryable": False,
        **route_meta,
        **{k: v for k, v in claims.items() if k != "payload"},
    }
