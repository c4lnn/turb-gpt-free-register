# -*- coding: utf-8 -*-
"""mail.com mailbox 协议的无状态辅助函数。

这里故意只保存协议形状和错误分类，不保存任何真实 Cookie、sid、邮件正文
或用户 token。运行时客户端负责把敏感值留在内存中。
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


SENSITIVE_HEADERS = frozenset({
    "authorization",
    "cookie",
    "set-cookie",
    "proxy-authorization",
    "x-mailbox-token",
})
SENSITIVE_QUERY_KEYS = frozenset({"sid", "ott", "access_token", "token", "password"})
SENSITIVE_FIELD_NAMES = frozenset({
    "password",
    "mail_access_token",
    "access_token",
    "refresh_token",
    "cookie",
    "set-cookie",
    "sid",
    "mail_body",
    "body",
    "html",
})


def _split_header_parts(value: str) -> list[str]:
    """按逗号拆分 header，同时保留引号内的逗号。"""
    parts: list[str] = []
    start = 0
    quoted = False
    escaped = False
    for index, char in enumerate(str(value or "")):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quoted:
            escaped = True
            continue
        if char == '"':
            quoted = not quoted
        elif char == "," and not quoted:
            part = value[start:index].strip()
            if part:
                parts.append(part)
            start = index + 1
    tail = value[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1]
    return re.sub(r"\\(.)", r"\1", value)


def parse_www_authenticate(value: str | None) -> list[dict[str, str]]:
    """解析一个或多个 WWW-Authenticate challenge。

    返回 ``[{"scheme": "Bearer", "error": "invalid_token", ...}]``。
    不依赖响应正文，也不会把未识别的 scheme 当成 Bearer。
    """
    raw = str(value or "").strip()
    if not raw:
        return []
    challenges: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    known_schemes = {"bearer", "basic", "digest", "dpop", "negotiate", "ntlm"}

    def add_parameter(target: dict[str, str], fragment: str) -> None:
        if "=" not in fragment:
            token = fragment.strip()
            if token and "token" not in target:
                target["token"] = _unquote(token)
            return
        key, val = fragment.split("=", 1)
        key = key.strip().lower()
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", key):
            target[key] = _unquote(val)

    # 参数形式中的逗号与多个 challenge 的分隔符相同；已知 scheme 开头
    # 的片段开启新 challenge，其余片段均属于当前 challenge 的参数。
    for part in _split_header_parts(raw):
        match = re.match(r"^([A-Za-z][A-Za-z0-9_-]*)(?:\s+(.*))?$", part)
        if current is None:
            if not match:
                continue
            scheme, remainder = match.group(1), (match.group(2) or "").strip()
            current = {"scheme": scheme}
            challenges.append(current)
            if remainder:
                add_parameter(current, remainder)
            continue

        # ``Bearer ...``/``Basic ...`` 可能紧接另一个 challenge；普通的
        # ``error=...`` 参数不会匹配这里的 scheme + 空格形式。
        if match and match.group(1).casefold() in known_schemes and (match.group(2) or "").strip() and "=" not in (match.group(2) or "").split("=", 1)[0]:
            current = {"scheme": match.group(1)}
            challenges.append(current)
            add_parameter(current, (match.group(2) or "").strip())
            continue
        add_parameter(current, part)
    return challenges


def parse_bearer_challenge(value: str | None) -> dict[str, str] | None:
    """返回第一个 Bearer challenge；没有则返回 ``None``。"""
    for challenge in parse_www_authenticate(value):
        if challenge.get("scheme", "").casefold() == "bearer":
            return challenge
    return None


def is_invalid_token_challenge(value: str | None) -> bool:
    challenge = parse_bearer_challenge(value)
    return bool(challenge and challenge.get("error", "").casefold() == "invalid_token")


def is_invalid_token_response(response: Any) -> bool:
    """严格判断 mail.com 已确认的 AT 失效响应。"""
    try:
        status = int(getattr(response, "status_code", 0))
    except (TypeError, ValueError):
        status = 0
    if status != 401:
        return False
    headers = getattr(response, "headers", {}) or {}
    challenge = None
    for key, val in (headers.items() if hasattr(headers, "items") else []):
        if str(key).casefold() == "www-authenticate":
            challenge = val
            break
    return is_invalid_token_challenge(challenge)


def redact_value(value: Any, *, keep_tail: int = 0) -> str:
    """将敏感值转换为可安全写入日志/API 的摘要。"""
    text = str(value or "")
    if not text:
        return ""
    if keep_tail > 0:
        return "*" * max(4, len(text) - keep_tail) + text[-keep_tail:]
    return "[redacted]"


def redact_headers(headers: Mapping[str, Any] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in (headers or {}).items():
        name = str(key)
        if name.casefold() in SENSITIVE_HEADERS:
            out[name] = "[redacted]"
        elif name.casefold() == "www-authenticate":
            challenge = parse_bearer_challenge(str(value))
            out[name] = (
                f"Bearer error={challenge.get('error', '') or 'none'}"
                if challenge
                else "[redacted-challenge]"
            )
        elif name.casefold() in {"x-request-id", "request-id"}:
            out[name] = redact_value(value, keep_tail=6)
        else:
            out[name] = str(value)
    return out


def redact_mapping(value: Any, *, _key: str = "") -> Any:
    """递归脱敏 JSON 诊断对象；邮件正文和 token 永不返回。"""
    key = _key.casefold()
    if key in SENSITIVE_FIELD_NAMES or any(part in key for part in ("password", "token", "cookie", "sid")):
        return "[redacted]" if value else ""
    if isinstance(value, Mapping):
        return {str(k): redact_mapping(v, _key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_mapping(item, _key=key) for item in value]
    return value


def safe_request_diagnostic(*, endpoint: str, status: int | None = None,
                            headers: Mapping[str, Any] | None = None,
                            error_type: str | None = None) -> dict[str, Any]:
    """生成不含 Authorization、Cookie、sid、正文的统一诊断摘要。"""
    return {
        "endpoint": str(endpoint),
        "status": int(status) if status is not None else None,
        "headers": redact_headers(headers),
        "error_type": str(error_type or ""),
    }


__all__ = [
    "SENSITIVE_HEADERS",
    "SENSITIVE_FIELD_NAMES",
    "parse_www_authenticate",
    "parse_bearer_challenge",
    "is_invalid_token_challenge",
    "is_invalid_token_response",
    "redact_value",
    "redact_headers",
    "redact_mapping",
    "safe_request_diagnostic",
]
