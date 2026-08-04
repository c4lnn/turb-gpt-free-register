# -*- coding: utf-8 -*-
"""SMSBower V1 handler API adapter."""
from __future__ import annotations

import json
import logging
import time
from decimal import Decimal, InvalidOperation

from curl_cffi.requests import Session as CurlSession

from config import IMPERSONATE
from config import codex as _cfg
from core.sms_provider import SmsCodeTimeout, SmsNoBalanceError, SmsNoNumbersError, SmsProviderError

logger = logging.getLogger(__name__)

_CANCEL_RETRIES = 2
_CANCEL_RETRY_DELAY = 1
_ALLOWED_STATUSES = {1, 3, 6, 8}
_STATUS_SUCCESS = {
    1: "ACCESS_READY",
    3: "ACCESS_RETRY_GET",
    6: "ACCESS_ACTIVATION",
    8: "ACCESS_CANCEL",
}


class SmsBowerEarlyCancelError(SmsProviderError):
    """SMSBower 拒绝购买后过早取消。"""


def _normalize_decimal(value, name: str) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = Decimal(raw)
    except InvalidOperation as exc:
        raise SmsProviderError(f"{name} 必须是合法十进制价格") from exc
    if not parsed.is_finite() or parsed < 0:
        raise SmsProviderError(f"{name} 必须是非负有限十进制价格")
    normalized = format(parsed, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def validate_price_range(min_price, max_price) -> tuple[str | None, str | None]:
    """校验并规范化 SMSBower 价格范围。"""
    normalized_min = _normalize_decimal(min_price, "SMSBOWER_MIN_PRICE")
    normalized_max = _normalize_decimal(max_price, "SMSBOWER_MAX_PRICE")
    if normalized_min is not None and normalized_max is not None:
        if Decimal(normalized_min) > Decimal(normalized_max):
            raise SmsProviderError("SMSBOWER_MIN_PRICE 不能大于 SMSBOWER_MAX_PRICE")
    return normalized_min, normalized_max


def validate_config_values(values, *, require_required: bool = True) -> dict[str, str | None]:
    """校验 SMSBower 配置；不发出网络请求。"""
    get = values.get if isinstance(values, dict) else lambda key, default=None: getattr(values, key, default)
    result = {
        "api_base": str(get("SMSBOWER_API_BASE", "") or "").strip(),
        "api_key": str(get("SMSBOWER_API_KEY", "") or "").strip(),
        "service": str(get("SMSBOWER_SERVICE", "") or "").strip(),
        "country": str(get("SMSBOWER_COUNTRY", "") or "").strip(),
    }
    min_price, max_price = validate_price_range(
        get("SMSBOWER_MIN_PRICE", ""), get("SMSBOWER_MAX_PRICE", ""),
    )
    result["min_price"] = min_price
    result["max_price"] = max_price
    if require_required:
        missing = [
            key for key, value in (
                ("SMSBOWER_API_BASE", result["api_base"]),
                ("SMSBOWER_API_KEY", result["api_key"]),
                ("SMSBOWER_SERVICE", result["service"]),
                ("SMSBOWER_COUNTRY", result["country"]),
            ) if not value
        ]
        if missing:
            raise SmsProviderError(f"SMSBower 缺少必要配置：{', '.join(missing)}")
    return result


def _mask(value: str, *, tail: int = 3) -> str:
    text = str(value or "").strip()
    if not text:
        return "-"
    if len(text) <= tail:
        return "***"
    return f"***{text[-tail:]}"


class SmsBowerProvider:
    """SMSBower provider；不读取或委托 Grizzly provider 配置。"""

    def __init__(
        self,
        *,
        config=_cfg,
        http_factory=None,
        sleep=time.sleep,
        time_fn=time.time,
        acquired_at: dict[str, float] | None = None,
    ):
        self.config = config
        self._http_factory = http_factory or self._new_http
        self._sleep = sleep
        self._time = time_fn
        self.acquired_at = acquired_at if acquired_at is not None else {}

    def _new_http(self):
        session = CurlSession(impersonate=IMPERSONATE)
        session.timeout = int(getattr(self.config, "SMS_REQUEST_TIMEOUT", 30) or 30)
        return session

    def _settings(self, *, require_required: bool = True) -> dict[str, str | None]:
        return validate_config_values(self.config, require_required=require_required)

    def _raise_response_error(self, action: str, text: str) -> None:
        code = str(text or "").strip().split(":", 1)[0]
        if code == "BAD_KEY":
            raise SmsProviderError("SMSBower API Key 无效（BAD_KEY）")
        if code == "NO_BALANCE":
            raise SmsNoBalanceError("SMSBower 余额不足（NO_BALANCE）")
        if code in {"NO_NUMBERS", "NO_NUMBER"}:
            raise SmsNoNumbersError("SMSBower 暂无满足配置和价格范围的号码（NO_NUMBERS）")
        if code == "EARLY_CANCEL_DENIED":
            raise SmsBowerEarlyCancelError("SMSBower 暂不允许取消（EARLY_CANCEL_DENIED）")
        if code in {"BAD_ACTION", "BAD_SERVICE", "BAD_COUNTRY", "BAD_STATUS", "NO_ACTIVATION"}:
            raise SmsProviderError(f"SMSBower {action} 请求失败：{code}")
        raise SmsProviderError(f"SMSBower {action} 返回未识别响应")

    def _request(self, action: str, *, http=None, params: dict | None = None) -> str:
        api_base = str(getattr(self.config, "SMSBOWER_API_BASE", "") or "").strip()
        api_key = str(getattr(self.config, "SMSBOWER_API_KEY", "") or "").strip()
        if not api_base:
            raise SmsProviderError("SMSBOWER_API_BASE 不能为空")
        if not api_key:
            raise SmsProviderError("SMSBOWER_API_KEY 不能为空")
        own_http = http is None
        client = http or self._http_factory()
        request_params = {"api_key": api_key, "action": action}
        request_params.update(params or {})
        try:
            try:
                response = client.get(api_base, params=request_params)
            except Exception as exc:
                raise SmsProviderError(f"SMSBower {action} 网络错误：{type(exc).__name__}") from exc
            if int(getattr(response, "status_code", 0) or 0) != 200:
                raise SmsProviderError(f"SMSBower {action} HTTP {getattr(response, 'status_code', 0)}")
            text = str(getattr(response, "text", "") or "").strip()
            if not text:
                raise SmsProviderError(f"SMSBower {action} 返回空响应")
            return text
        finally:
            if own_http:
                client.close()

    def _request_json(self, action: str, *, http=None) -> object:
        text = self._request(action, http=http)
        try:
            return json.loads(text)
        except (TypeError, json.JSONDecodeError) as exc:
            if text and not text.startswith(("{", "[")):
                self._raise_response_error(action, text)
            raise SmsProviderError(f"SMSBower {action} 返回无效 JSON") from exc

    def list_services(self, *, http=None) -> list[dict[str, str]]:
        """查询服务代码，返回适合 WebUI 下拉框使用的稳定结构。"""
        payload = self._request_json("getServicesList", http=http)
        raw_items = payload.get("services", []) if isinstance(payload, dict) else payload
        entries = raw_items.items() if isinstance(raw_items, dict) else enumerate(raw_items or [])
        services: dict[str, dict[str, str]] = {}
        for fallback_code, raw in entries:
            if isinstance(raw, dict):
                code = str(raw.get("code") or raw.get("id") or fallback_code or "").strip()
                name = str(raw.get("name") or raw.get("eng") or raw.get("rus") or code).strip()
            else:
                code = str(fallback_code if isinstance(raw_items, dict) else raw).strip()
                name = str(raw or code).strip()
            if not code:
                continue
            services[code] = {"value": code, "label": f"{name} ({code})" if name != code else code}
        if not services:
            raise SmsProviderError("SMSBower getServicesList 未返回可用服务")
        return sorted(services.values(), key=lambda item: (item["label"].casefold(), item["value"]))

    def list_countries(self, *, http=None) -> list[dict[str, str]]:
        """查询国家 ID，兼容官方文档中数组或对象两种可能结构。"""
        payload = self._request_json("getCountries", http=http)
        raw_items = payload.get("countries", []) if isinstance(payload, dict) and "countries" in payload else payload
        entries = raw_items.items() if isinstance(raw_items, dict) else enumerate(raw_items or [])
        countries: dict[str, dict[str, str]] = {}
        for fallback_id, raw in entries:
            if not isinstance(raw, dict):
                continue
            country_id = str(raw.get("id") or fallback_id or "").strip()
            if not country_id:
                continue
            names = []
            for key in ("chn", "eng", "name", "rus"):
                name = str(raw.get(key) or "").strip()
                if name and name not in names:
                    names.append(name)
            display = " / ".join(names[:2]) or country_id
            countries[country_id] = {
                "value": country_id,
                "label": f"{display} ({country_id})" if display != country_id else country_id,
            }
        if not countries:
            raise SmsProviderError("SMSBower getCountries 未返回可用国家")
        return sorted(countries.values(), key=lambda item: (item["label"].casefold(), item["value"]))

    def get_metadata(self) -> dict[str, list[dict[str, str]]]:
        """复用单个 HTTP session 获取服务和国家元数据。"""
        http = self._http_factory()
        try:
            return {
                "services": self.list_services(http=http),
                "countries": self.list_countries(http=http),
            }
        finally:
            http.close()

    def acquire_number(self, *, http=None, service: str | None = None, country: str | None = None) -> tuple[str, str]:
        settings = self._settings(require_required=True)
        params = {
            "service": str(service or settings["service"]),
            "country": str(country or settings["country"]),
        }
        if settings["min_price"] is not None:
            params["minPrice"] = str(settings["min_price"])
        if settings["max_price"] is not None:
            params["maxPrice"] = str(settings["max_price"])
        logger.info(
            "[SMS:SMSBower] 取号请求 service=%s country=%s(国家ID/区号) minPrice=%s maxPrice=%s",
            params["service"],
            params["country"],
            params.get("minPrice", "不限"),
            params.get("maxPrice", "不限"),
        )
        text = self._request("getNumber", http=http, params=params)
        if not text.startswith("ACCESS_NUMBER:"):
            self._raise_response_error("getNumber", text)
        parts = text.split(":", 2)
        activation_id = parts[1].strip() if len(parts) > 1 else ""
        phone = "".join(ch for ch in (parts[2] if len(parts) > 2 else "") if ch.isdigit())
        if not activation_id or not phone:
            raise SmsProviderError("SMSBower getNumber 返回的激活 ID 或手机号为空")
        self.acquired_at[activation_id] = self._time()
        logger.info("[SMS:SMSBower] 取号成功 activation=%s phone=%s", _mask(activation_id), _mask(phone, tail=4))
        return activation_id, phone

    def wait_for_sms_code(
        self,
        activation_id: str,
        *,
        http=None,
        max_wait: int | None = None,
        poll_interval: int | None = None,
    ) -> str:
        total_wait = int(getattr(self.config, "SMS_CODE_WAIT", 120) if max_wait is None else max_wait)
        interval = float(getattr(self.config, "SMS_POLL_INTERVAL", 5) if poll_interval is None else poll_interval)
        deadline = self._time() + total_wait
        while self._time() < deadline:
            try:
                from core.registration_service import check_stop_requested
                check_stop_requested()
            except ImportError:
                pass
            text = self._request("getStatus", http=http, params={"id": activation_id})
            if text == "STATUS_WAIT_CODE" or text.startswith("STATUS_WAIT_RETRY:"):
                self._sleep(interval)
                continue
            if text.startswith("STATUS_OK:"):
                code = text.split(":", 1)[1].strip()
                if not code:
                    raise SmsProviderError("SMSBower getStatus 返回空验证码")
                logger.info("[SMS:SMSBower] 已收到验证码 activation=%s", _mask(activation_id))
                return code
            if text == "STATUS_CANCEL":
                raise SmsProviderError("SMSBower 激活已取消（STATUS_CANCEL）")
            self._raise_response_error("getStatus", text)
        raise SmsCodeTimeout(f"SMSBower 等待短信超时（>{total_wait}s），activation={_mask(activation_id)}")

    def set_status(self, activation_id: str, status: int, *, http=None) -> str:
        try:
            normalized = int(status)
        except (TypeError, ValueError) as exc:
            raise SmsProviderError("SMSBower status 必须是 1/3/6/8") from exc
        if normalized not in _ALLOWED_STATUSES:
            raise SmsProviderError("SMSBower status 仅支持 1/3/6/8")
        text = self._request(
            "setStatus", http=http, params={"status": str(normalized), "id": activation_id},
        )
        if text != _STATUS_SUCCESS[normalized]:
            self._raise_response_error("setStatus", text)
        return text

    def complete(self, activation_id: str, *, http=None) -> None:
        try:
            self.set_status(activation_id, 6, http=http)
            logger.info("[SMS:SMSBower] 激活完成 activation=%s", _mask(activation_id))
        except Exception as exc:
            logger.warning(
                "[SMS:SMSBower] 完成状态提交失败（不影响已通过的手机验证） activation=%s error=%s",
                _mask(activation_id), type(exc).__name__,
            )
        finally:
            self.acquired_at.pop(activation_id, None)

    def _cancel_sync(self, activation_id: str, *, http=None) -> None:
        own_http = http is None
        client = http or self._http_factory()
        try:
            for attempt in range(1, _CANCEL_RETRIES + 1):
                try:
                    self.set_status(activation_id, 8, http=client)
                    self.acquired_at.pop(activation_id, None)
                    logger.info("[SMS:SMSBower] 激活已取消 activation=%s", _mask(activation_id))
                    return
                except Exception as exc:
                    if attempt >= _CANCEL_RETRIES:
                        logger.warning(
                            "[SMS:SMSBower] 取消最终失败 activation=%s error=%s",
                            _mask(activation_id), type(exc).__name__,
                        )
                        return
                    self._sleep(_CANCEL_RETRY_DELAY)
        finally:
            if own_http:
                client.close()

    def cancel(self, activation_id: str, *, http=None, background: bool = True) -> None:
        """立即同步取消；保留 background 参数仅用于兼容统一 facade。"""
        del background
        self._cancel_sync(activation_id, http=http)


_DEFAULT_PROVIDER = SmsBowerProvider(acquired_at={})


def get_provider() -> SmsBowerProvider:
    return _DEFAULT_PROVIDER
