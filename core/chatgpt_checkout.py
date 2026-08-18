# -*- coding: utf-8 -*-
"""只创建初始 Checkout Session 并识别其 ID 类型。

本模块刻意停在 ``POST /backend-api/payments/checkout`` 的响应边界。
它不调用 Checkout update、Stripe、confirm、approve 或任何支付后续接口。
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from core.chatgpt_plan import normalize_token
from core.session import BrowserSession

logger = logging.getLogger(__name__)

CHECKOUT_SESSION_PATH = "/backend-api/payments/checkout"
CHECKOUT_SESSION_URL = f"https://chatgpt.com{CHECKOUT_SESSION_PATH}"
# 与现有协议模块的命名风格保持兼容。
CHECKOUT_PATH = CHECKOUT_SESSION_PATH
CHECKOUT_URL = CHECKOUT_SESSION_URL
CHECKOUT_SESSION_ID_KEYS = ("checkout_session_id", "session_id", "id")
# 400 按需求视为临时错误；认证、支付资格、权限和限流错误立即终止。
CHECKOUT_TERMINAL_STATUSES = frozenset({401, 402, 403, 429})
CHECKOUT_RETRY_STATUSES = frozenset({400, 408, 409, 425})

_MESSAGE_LIMIT = 240
_PREFIX_RE = re.compile(r"(?i)(?:oaics_|cs_)[A-Za-z0-9._~-]+")
_URL_CREDENTIAL_RE = re.compile(r"(?i)((?:[a-z][a-z0-9+.-]*://)?)([^/@\s:]+):([^/@\s]+)@")
_SECRET_FIELD_RE = re.compile(
    r"(?ix)(?P<prefix>[\"']?(?:client_secret|customer_session[^\s\"'=:\\]*|publishable_key|authorization)[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^,;\s}]+)"
)
_SECRET_TOKEN_RE = re.compile(r"(?i)(?:client_secret|customer_session(?:_secret)?|publishable_key)[A-Za-z0-9._~-]*")


class CheckoutConfigError(ValueError):
    """Checkout 配置或输入不满足发送条件。"""


@dataclass(frozen=True)
class CheckoutSettings:
    """入队时固化的 Checkout 任务配置。"""

    auto_check: bool = False
    proxy_mode: str = "proxy"
    proxy: str = ""
    billing_country: str = ""
    billing_currency: str = ""
    timeout: float = 35.0
    max_attempts: int = 2
    retry_delay: float = 1.5
    workers: int = 1
    queue_limit: int = 100
    min_interval: float = 0.4
    jitter: float = 0.3

    @property
    def network_route(self) -> str:
        return "direct" if self.proxy_mode == "direct" else "proxy"

    def public_dict(self) -> dict[str, Any]:
        """返回可用于日志/API 的配置摘要，不含完整代理。"""
        effective_proxy = self.proxy if self.proxy_mode == "proxy" else ""
        return {
            "proxy_mode": self.proxy_mode,
            "network_route": self.network_route,
            "proxy_used": mask_proxy(effective_proxy) or None,
            "billing_country": self.billing_country,
            "billing_currency": self.billing_currency,
            "timeout": self.timeout,
            "max_attempts": self.max_attempts,
            "retry_delay": self.retry_delay,
            "workers": self.workers,
            "queue_limit": self.queue_limit,
            "min_interval": self.min_interval,
            "jitter": self.jitter,
        }


def _number(value: Any, default: float, lower: float, upper: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(lower, min(upper, parsed))


def _integer(value: Any, default: int, lower: int, upper: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(lower, min(upper, parsed))


def checkout_settings_from_config() -> CheckoutSettings:
    """读取当前配置并生成任务快照。

    配置模块通过 ``config.reload_all`` 原地热加载，因此这里每次入队时
    都从子模块属性读取，而不是在模块导入时绑定一份旧值。
    """
    from config import checkout_session as cfg

    return CheckoutSettings(
        auto_check=bool(getattr(cfg, "CHECKOUT_SESSION_AUTO_CHECK", False)),
        proxy_mode=str(getattr(cfg, "CHECKOUT_SESSION_PROXY_MODE", "proxy") or "proxy").strip().lower(),
        proxy=str(getattr(cfg, "CHECKOUT_SESSION_PROXY", "") or "").strip(),
        billing_country=str(getattr(cfg, "CHECKOUT_SESSION_BILLING_COUNTRY", "") or "").strip(),
        billing_currency=str(getattr(cfg, "CHECKOUT_SESSION_BILLING_CURRENCY", "") or "").strip(),
        timeout=_number(getattr(cfg, "CHECKOUT_SESSION_TIMEOUT", 35.0), 35.0, 1.0, 60.0),
        max_attempts=_integer(getattr(cfg, "CHECKOUT_SESSION_MAX_ATTEMPTS", 2), 2, 1, 4),
        retry_delay=_number(getattr(cfg, "CHECKOUT_SESSION_RETRY_DELAY", 1.5), 1.5, 0.0, 30.0),
        workers=_integer(getattr(cfg, "CHECKOUT_SESSION_WORKERS", 1), 1, 1, 16),
        queue_limit=_integer(getattr(cfg, "CHECKOUT_SESSION_QUEUE_LIMIT", 100), 100, 1, 5000),
        min_interval=_number(getattr(cfg, "CHECKOUT_SESSION_MIN_INTERVAL", 0.4), 0.4, 0.0, 30.0),
        jitter=_number(getattr(cfg, "CHECKOUT_SESSION_JITTER", 0.3), 0.3, 0.0, 30.0),
    )


def validate_checkout_config_values(
    values: Mapping[str, Any] | CheckoutSettings,
    *,
    require_request_values: bool = False,
) -> list[str]:
    """返回配置错误；WebUI 保存和实际发送分别选择是否要求国家/货币。"""
    errors: list[str] = []
    if isinstance(values, CheckoutSettings):
        settings = values
    else:
        current = checkout_settings_from_config()
        settings = replace(
            current,
            proxy_mode=str(values.get("CHECKOUT_SESSION_PROXY_MODE", current.proxy_mode) or "").strip().lower(),
            proxy=str(values.get("CHECKOUT_SESSION_PROXY", current.proxy) or "").strip(),
            billing_country=str(values.get("CHECKOUT_SESSION_BILLING_COUNTRY", current.billing_country) or "").strip(),
            billing_currency=str(values.get("CHECKOUT_SESSION_BILLING_CURRENCY", current.billing_currency) or "").strip(),
        )

        numeric_ranges = (
            ("CHECKOUT_SESSION_TIMEOUT", values.get("CHECKOUT_SESSION_TIMEOUT", current.timeout), 1, 60),
            ("CHECKOUT_SESSION_MAX_ATTEMPTS", values.get("CHECKOUT_SESSION_MAX_ATTEMPTS", current.max_attempts), 1, 4),
            ("CHECKOUT_SESSION_RETRY_DELAY", values.get("CHECKOUT_SESSION_RETRY_DELAY", current.retry_delay), 0, 30),
            ("CHECKOUT_SESSION_WORKERS", values.get("CHECKOUT_SESSION_WORKERS", current.workers), 1, 16),
            ("CHECKOUT_SESSION_QUEUE_LIMIT", values.get("CHECKOUT_SESSION_QUEUE_LIMIT", current.queue_limit), 1, 5000),
            ("CHECKOUT_SESSION_MIN_INTERVAL", values.get("CHECKOUT_SESSION_MIN_INTERVAL", current.min_interval), 0, 30),
            ("CHECKOUT_SESSION_JITTER", values.get("CHECKOUT_SESSION_JITTER", current.jitter), 0, 30),
        )
        for key, raw, lower, upper in numeric_ranges:
            try:
                number = float(raw)
            except (TypeError, ValueError):
                errors.append(f"{key} 必须是数字")
                continue
            if number < lower or number > upper:
                errors.append(f"{key} 必须在 {lower}-{upper} 范围内")
    if isinstance(values, CheckoutSettings):
        numeric_ranges = (
            ("CHECKOUT_SESSION_TIMEOUT", values.timeout, 1, 60),
            ("CHECKOUT_SESSION_MAX_ATTEMPTS", values.max_attempts, 1, 4),
            ("CHECKOUT_SESSION_RETRY_DELAY", values.retry_delay, 0, 30),
            ("CHECKOUT_SESSION_WORKERS", values.workers, 1, 16),
            ("CHECKOUT_SESSION_QUEUE_LIMIT", values.queue_limit, 1, 5000),
            ("CHECKOUT_SESSION_MIN_INTERVAL", values.min_interval, 0, 30),
            ("CHECKOUT_SESSION_JITTER", values.jitter, 0, 30),
        )
        for key, raw, lower, upper in numeric_ranges:
            try:
                number = float(raw)
            except (TypeError, ValueError):
                errors.append(f"{key} 必须是数字")
                continue
            if number < lower or number > upper:
                errors.append(f"{key} 必须在 {lower}-{upper} 范围内")
    if settings.proxy_mode not in {"proxy", "direct"}:
        errors.append("CHECKOUT_SESSION_PROXY_MODE 只允许 proxy 或 direct")
    if settings.billing_country and not re.fullmatch(r"[A-Z]{2}", settings.billing_country):
        errors.append("CHECKOUT_SESSION_BILLING_COUNTRY 必须是两位大写 ASCII 国家代码")
    if settings.billing_currency and not re.fullmatch(r"[A-Z]{3}", settings.billing_currency):
        errors.append("CHECKOUT_SESSION_BILLING_CURRENCY 必须是三位大写 ASCII 货币代码")
    if require_request_values:
        if not settings.billing_country:
            errors.append("未配置 CHECKOUT_SESSION_BILLING_COUNTRY")
        if not settings.billing_currency:
            errors.append("未配置 CHECKOUT_SESSION_BILLING_CURRENCY")
        if settings.proxy_mode == "proxy" and not settings.proxy:
            errors.append("proxy 模式必须配置 CHECKOUT_SESSION_PROXY")
    return errors


def validate_checkout_settings(settings: CheckoutSettings, *, require_request_values: bool = True) -> None:
    errors = validate_checkout_config_values(settings, require_request_values=require_request_values)
    if errors:
        raise CheckoutConfigError("；".join(errors))


def mask_proxy(proxy: str | None) -> str:
    """生成不含代理账号密码的摘要。"""
    value = str(proxy or "").strip()
    if not value:
        return ""
    try:
        parsed = urlparse(value if "://" in value else f"//{value}")
        scheme = f"{parsed.scheme}://" if parsed.scheme else ""
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        auth = "***:***@" if parsed.username or parsed.password else ""
        return f"{scheme}{auth}{host}{port}" or "***"
    except Exception:
        return "***"


def _sanitize_text(value: Any, *, secrets: tuple[str, ...] = ()) -> str:
    text = str(value or "").replace("\x00", " ").strip()
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, "[redacted]")
    text = _URL_CREDENTIAL_RE.sub(r"\1[redacted]@", text)
    text = _SECRET_FIELD_RE.sub(lambda m: f"{m.group('prefix')}[redacted]", text)
    text = _SECRET_TOKEN_RE.sub("[redacted-secret]", text)
    text = _PREFIX_RE.sub("[redacted-session]", text)
    text = re.sub(r"\s+", " ", text)
    return text[:_MESSAGE_LIMIT]


def _response_headers(response: Any) -> Mapping[str, Any]:
    headers = getattr(response, "headers", None)
    return headers if isinstance(headers, Mapping) else {}


def _header_value(response: Any, name: str) -> str:
    headers = _response_headers(response)
    value = headers.get(name)
    if value is None:
        lowered = name.lower()
        for key, candidate in headers.items():
            if str(key).lower() == lowered:
                value = candidate
                break
    return str(value or "").strip()


def _response_text(response: Any) -> str:
    text = getattr(response, "text", "")
    if isinstance(text, str):
        return text
    content = getattr(response, "content", b"")
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    return str(content or "")


def _response_bytes(response: Any, text: str) -> int:
    content = getattr(response, "content", None)
    if isinstance(content, (bytes, bytearray)):
        return len(content)
    return len(text.encode("utf-8", errors="replace"))


def _parse_json(response: Any, text: str) -> tuple[bool, Any]:
    try:
        data = response.json()
        return True, data
    except Exception:
        pass
    if not text.strip():
        return False, None
    try:
        return True, json.loads(text)
    except Exception:
        return False, None


def extract_checkout_session_id(value: Any) -> str | None:
    """按字段优先级和 JSON 出现顺序深度优先提取第一个字符串 ID。"""
    if isinstance(value, Mapping):
        for key in CHECKOUT_SESSION_ID_KEYS:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        for child in value.values():
            found = extract_checkout_session_id(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = extract_checkout_session_id(child)
            if found:
                return found
    return None


def classify_checkout_session_id(session_id: str) -> str:
    value = str(session_id or "").strip()
    if value.startswith("oaics_"):
        return "oaics"
    if value.startswith("cs_live_"):
        return "cs_live"
    if value.startswith("cs_"):
        return "other_cs"
    return "unknown"


def _iter_mappings(value: Any):
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _iter_mappings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_mappings(child)


def extract_checkout_error(value: Any) -> tuple[str | None, str | None]:
    """提取结构化错误 code/message，不保留原始响应。"""
    code: str | None = None
    message: str | None = None
    for mapping in _iter_mappings(value):
        if code is None:
            for key in ("code", "error_code", "errorCode"):
                candidate = mapping.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    code = _sanitize_text(candidate)
                    break
        if message is None:
            for key in ("message", "error_message", "errorMessage", "detail", "error_description"):
                candidate = mapping.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    message = _sanitize_text(candidate)
                    break
        if code and message:
            break
    return code, message


def has_token_revoked(value: Any) -> bool:
    for mapping in _iter_mappings(value):
        for key in ("code", "error_code", "errorCode"):
            candidate = mapping.get(key)
            if isinstance(candidate, str) and candidate.strip().lower() == "token_revoked":
                return True
    return False


def _retryable_status(status: int | None) -> bool:
    return status is None or status in CHECKOUT_RETRY_STATUSES or status >= 500


def _retry_after_seconds(response: Any, base_delay: float, attempt: int) -> float:
    raw = _header_value(response, "retry-after") if response is not None else ""
    if raw:
        try:
            return max(0.0, min(30.0, float(raw)))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(raw)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                seconds = retry_at.timestamp() - datetime.now(tz=timezone.utc).timestamp()
                return max(0.0, min(30.0, seconds))
            except (TypeError, ValueError, OverflowError):
                pass
    return max(0.0, min(30.0, float(base_delay) * attempt))


def build_checkout_body(billing_country: str, billing_currency: str) -> dict[str, Any]:
    return {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {
            "country": billing_country,
            "currency": billing_currency,
        },
        "promo_campaign": {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        },
        "checkout_ui_mode": "custom",
    }


def build_checkout_headers(session: BrowserSession, access_token: str) -> dict[str, str]:
    """在 BrowserSession 的浏览器头基础上补齐 Checkout 目标头。"""
    headers = dict(session.get_chatgpt_headers(referer="https://chatgpt.com/"))
    headers.update({
        "authorization": f"Bearer {normalize_token(access_token)}",
        "origin": "https://chatgpt.com",
        "referer": "https://chatgpt.com/",
        "content-type": "application/json",
        "accept": "*/*",
        "oai-device-id": str(session.device_id),
        "oai-session-id": str(session.oai_session_id),
        "x-openai-target-path": CHECKOUT_SESSION_PATH,
        "x-openai-target-route": CHECKOUT_SESSION_PATH,
    })
    return headers


def _base_result(settings: CheckoutSettings, *, token: str, attempt: int, max_attempts: int) -> dict[str, Any]:
    effective_proxy = settings.proxy if settings.proxy_mode == "proxy" else ""
    return {
        "ok": False,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "http_status": None,
        "attempt_count": attempt,
        "max_attempts": max_attempts,
        "request_timeout": settings.timeout,
        "proxy_mode": settings.proxy_mode,
        "network_route": settings.network_route,
        "proxy_used": mask_proxy(effective_proxy) or None,
        "retryable": False,
    }


def public_checkout_result(result: Mapping[str, Any] | None) -> dict[str, Any]:
    """移除完整 Session ID，供 API、普通日志和测试快照使用。"""
    out = dict(result or {})
    out.pop("checkout_session_id", None)
    out.pop("access_token", None)
    out.pop("proxy", None)
    out.pop("response", None)
    out.pop("payload", None)
    return out


def check_checkout_session(
    access_token: str,
    *,
    settings: CheckoutSettings | None = None,
    proxy: str | None = None,
    billing_country: str | None = None,
    billing_currency: str | None = None,
    timeout: float | None = None,
    max_attempts: int | None = None,
    retry_delay: float | None = None,
    session_factory: Callable[..., BrowserSession] = BrowserSession,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """执行初始 Checkout POST，并按独立策略处理响应/重试。"""
    token = normalize_token(access_token)
    if settings is None:
        settings = checkout_settings_from_config()
    overrides: dict[str, Any] = {}
    if proxy is not None:
        overrides["proxy"] = str(proxy or "").strip()
        overrides["proxy_mode"] = "direct" if not overrides["proxy"] else "proxy"
    if billing_country is not None:
        overrides["billing_country"] = str(billing_country or "").strip()
    if billing_currency is not None:
        overrides["billing_currency"] = str(billing_currency or "").strip()
    if timeout is not None:
        overrides["timeout"] = _number(timeout, settings.timeout, 1.0, 60.0)
    if max_attempts is not None:
        overrides["max_attempts"] = _integer(max_attempts, settings.max_attempts, 1, 4)
    if retry_delay is not None:
        overrides["retry_delay"] = _number(retry_delay, settings.retry_delay, 0.0, 30.0)
    if overrides:
        settings = replace(settings, **overrides)

    if not token:
        result = _base_result(settings, token=token, attempt=0, max_attempts=settings.max_attempts)
        result.update({"error_code": "invalid_token", "error_message": "access_token 为空", "error": "access_token 为空"})
        return result
    try:
        validate_checkout_settings(settings, require_request_values=True)
    except Exception as exc:
        result = _base_result(settings, token=token, attempt=0, max_attempts=settings.max_attempts)
        message = _sanitize_text(exc, secrets=(token, settings.proxy))
        result.update({"error_code": "configuration_error", "error_message": message, "error": message})
        return result

    body = build_checkout_body(settings.billing_country, settings.billing_currency)
    env: BrowserSession | None = None
    last_result: dict[str, Any] | None = None
    response: Any = None
    try:
        try:
            env = session_factory(proxy=settings.proxy if settings.proxy_mode == "proxy" else "", detect_exit_geo=False)
        except TypeError:
            # 便于协议测试注入只接受 proxy 的轻量 fake；生产 BrowserSession 使用上面的完整签名。
            env = session_factory(proxy=settings.proxy if settings.proxy_mode == "proxy" else "")
        headers = build_checkout_headers(env, token)
        for attempt in range(1, settings.max_attempts + 1):
            response = None
            try:
                response = env.session.post(
                    CHECKOUT_SESSION_URL,
                    headers=headers,
                    json=body,
                    allow_redirects=False,
                    timeout=settings.timeout,
                )
                status = int(getattr(response, "status_code", 0) or 0)
                text = _response_text(response)
                body_bytes = _response_bytes(response, text)
                content_type = _header_value(response, "content-type")
                parsed, payload = _parse_json(response, text)
                code, message = extract_checkout_error(payload) if parsed else (None, None)
                common = _base_result(settings, token=token, attempt=attempt, max_attempts=settings.max_attempts)
                common.update({
                    "http_status": status,
                    "content_type": content_type,
                    "response_bytes": body_bytes,
                    "error_code": code,
                    "error_message": message,
                })

                if 200 <= status < 300:
                    if parsed and has_token_revoked(payload):
                        message = message or "上游拒绝当前 access token"
                        common.update({
                            "error_code": "token_revoked",
                            "error_message": _sanitize_text(message, secrets=(token, settings.proxy)),
                            "error": _sanitize_text(message, secrets=(token, settings.proxy)),
                            "retryable": False,
                        })
                    elif not parsed:
                        message = "2xx 响应不是 JSON"
                        common.update({
                            "error_code": "invalid_json",
                            "error_message": message,
                            "error": message,
                            "retryable": True,
                        })
                    else:
                        session_id = extract_checkout_session_id(payload)
                        if session_id:
                            common.update({
                                "ok": True,
                                "status": "success",
                                "checkout_session_id": session_id,
                                "checkout_session_type": classify_checkout_session_id(session_id),
                                "retryable": False,
                                "error_code": None,
                                "error_message": None,
                                "error": None,
                            })
                            return common
                        message = "2xx 响应未找到 Checkout Session ID"
                        common.update({
                            "error_code": "missing",
                            "error_message": message,
                            "error": message,
                            "retryable": True,
                        })
                    last_result = common
                else:
                    retryable = _retryable_status(status)
                    if status in CHECKOUT_TERMINAL_STATUSES or (parsed and has_token_revoked(payload)):
                        retryable = False
                    if not message:
                        message = f"HTTP {status} 非 JSON 响应" if not parsed else f"HTTP {status}"
                    message = _sanitize_text(message, secrets=(token, settings.proxy))
                    common.update({
                        "error_message": message,
                        "error": message,
                        "retryable": retryable,
                    })
                    if parsed and has_token_revoked(payload):
                        common["error_code"] = "token_revoked"
                    last_result = common
            except Exception as exc:
                message = _sanitize_text(exc, secrets=(token, settings.proxy))
                last_result = _base_result(settings, token=token, attempt=attempt, max_attempts=settings.max_attempts)
                last_result.update({
                    "error_code": "transport_error",
                    "transport_error_type": type(exc).__name__,
                    "error_message": message,
                    "error": message,
                    "retryable": True,
                })

            if not last_result.get("retryable") or attempt >= settings.max_attempts:
                return last_result
            wait_seconds = _retry_after_seconds(response, settings.retry_delay, attempt)
            last_result["retry_after_seconds"] = wait_seconds
            logger.warning(
                "[Checkout] 初始请求临时失败，第 %s/%s 次，%.1fs 后重试: status=%s code=%s",
                attempt,
                settings.max_attempts,
                wait_seconds,
                last_result.get("http_status"),
                last_result.get("error_code") or "-",
            )
            if wait_seconds > 0:
                sleep(wait_seconds)
        return last_result or {
            **_base_result(settings, token=token, attempt=0, max_attempts=settings.max_attempts),
            "error_code": "not_executed",
            "error_message": "Checkout 检测未执行",
            "error": "Checkout 检测未执行",
        }
    finally:
        if env is not None:
            try:
                env.session.close()
            except Exception:
                pass


# 便于调用方按“检测”语义命名；两者都只执行初始 Checkout POST。
detect_checkout_session = check_checkout_session
detect_checkout_session_id = check_checkout_session
