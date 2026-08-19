# -*- coding: utf-8 -*-
"""mail.com 邮箱读取 client。

该模块只负责协议访问和邮件解析；邮箱池、AT 持久化以及失效恢复由
``core.email_provider``/``core.db`` 管理。Cookie 和 sid 只在当前 client
生命周期内存在，绝不写入邮箱池。
"""
from __future__ import annotations

import base64
import html
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import getaddresses
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit
from uuid import uuid4

try:  # curl_cffi 能更接近 Webmail 的 TLS/指纹协商
    from curl_cffi import requests as _http
except Exception:  # pragma: no cover - 依赖缺失时允许使用 requests
    import requests as _http

from config import email as _email_cfg
from core.mailcom_protocol import (
    is_invalid_token_response,
    redact_headers,
    safe_request_diagnostic,
)
from core.otp_utils import extract_otp, looks_like_openai_email

logger = logging.getLogger(__name__)

LOGIN_PAGE_URL = "https://www.mail.com/premiumlogin"
LOGIN_URL = "https://login.mail.com/login"
OAUTH_URL = "https://oauthbridge.navigator-lxa.mail.com/navigator/oauth2/token"
NAVIGATOR_HOST = "navigator-lxa.mail.com"
WEBMAIL_ORIGIN = "https://webmailer.mail.com"
SETTINGS_NAVIGATION_URL = f"https://{NAVIGATOR_HOST}/mail_settings"
SETTINGS_ROOT_ORIGIN = "https://mailset-root.mail.com"
SETTINGS_OAUTH_CLIENT_ID = "mailcom_mailset_root_live"
SETTINGS_OAUTH_SCOPE = "mail_mailbox_w webmailer_setting_r webmailer_setting_w mail_confix_w"
MAILBOX_API = "https://webmail-cats-live.mail.com"
MAILLIST_API = "https://maillist.mail.com"
MAILBODY_API = "https://mailcom.mailbody-ui.de"
OAUTH_CLIENT_ID = "mailcom_webmailermaillist_passport_live"
# 公开客户端标识对应的 Webmailer 字段，不是用户密码。
OAUTH_PUBLIC_SECRET = "*******"
DEFAULT_TIMEOUT = 30
DEFAULT_PAGE_SIZE = 50
DEFAULT_MAX_PAGES = 200


class MailComError(RuntimeError):
    """mail.com 请求、协议或业务错误。"""

    def __init__(self, message: str, *, error_type: str = "protocol_error",
                 diagnostic: dict[str, Any] | None = None):
        super().__init__(message)
        self.error_type = error_type
        self.diagnostic = diagnostic or {}


class MailComAuthError(MailComError):
    def __init__(self, message: str, **kwargs: Any):
        super().__init__(message, error_type="auth_error", **kwargs)


class MailComInvalidTokenError(MailComAuthError):
    def __init__(self, message: str = "mailbox access token 已失效", **kwargs: Any):
        super().__init__(message, **kwargs)
        self.error_type = "invalid_token"


class MailComCredentialError(MailComAuthError):
    def __init__(self, message: str, **kwargs: Any):
        super().__init__(message, **kwargs)
        self.error_type = "invalid_credentials"


class MailComRateLimitError(MailComError):
    def __init__(self, message: str, **kwargs: Any):
        super().__init__(message, error_type="rate_limited", **kwargs)


class MailComTransientError(MailComError):
    def __init__(self, message: str, **kwargs: Any):
        super().__init__(message, error_type="network_error", **kwargs)


class MailComProtocolError(MailComError):
    def __init__(self, message: str, **kwargs: Any):
        super().__init__(message, error_type="protocol_error", **kwargs)


@dataclass
class MailComAccount:
    email: str
    password: str = ""
    status: str = "available"
    mail_access_token: str = ""
    mail_access_token_expires_at: float | None = None
    mail_access_token_updated_at: str | None = None
    mail_auth_error: str | None = None
    used_at: str | None = None
    id: int | None = None


@dataclass(frozen=True)
class MailComToken:
    access_token: str
    expires_in: int
    expires_at: float
    scope: str = ""
    token_type: str = "Bearer"


@dataclass
class MailComMessage:
    mail_id: str
    sender: str = ""
    subject: str = ""
    date: str = ""
    internal_date: float | None = None
    body: str = ""
    raw_header: dict[str, Any] = field(default_factory=dict)


class _LoginFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[tuple[str, dict[str, str]]] = []
        self._action = ""
        self._fields: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {str(k).lower(): str(v or "") for k, v in attrs}
        if tag.lower() == "form":
            self._action = values.get("action", "")
            self._fields = {}
        elif tag.lower() == "input" and self._action:
            name = values.get("name", "")
            if name:
                self._fields[name] = values.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "form" and self._action:
            self.forms.append((self._action, dict(self._fields)))
            self._action = ""
            self._fields = {}


class _VisibleTextParser(HTMLParser):
    _BLOCK_TAGS = {
        "address", "article", "aside", "blockquote", "br", "dd", "div", "dl",
        "dt", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "li",
        "ol", "p", "pre", "section", "table", "td", "th", "tr", "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"}:
            self._ignored_depth += 1
        if self._ignored_depth == 0 and tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._ignored_depth and tag in {"script", "style", "noscript"}:
            self._ignored_depth -= 1
        if self._ignored_depth == 0 and tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth == 0:
            self.parts.append(data)

    def text(self) -> str:
        value = html.unescape("".join(self.parts))
        lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in value.splitlines()]
        result: list[str] = []
        for line in lines:
            if line or (result and result[-1]):
                result.append(line)
        return "\n".join(result).strip()


def clean_html_body(value: str) -> str:
    parser = _VisibleTextParser()
    parser.feed(str(value or ""))
    return parser.text()


def _is_email(value: str) -> bool:
    parsed = getaddresses([str(value or "")])
    return len(parsed) == 1 and bool(parsed[0][1]) and parsed[0][1].count("@") == 1


def _sender_addresses(value: Any) -> set[str]:
    values = [str(item) for item in value] if isinstance(value, (list, tuple)) else [str(value or "")]
    return {address.casefold() for _, address in getaddresses(values) if address}


def _recipient_addresses(value: Any) -> set[str]:
    """解析 mailHeader.to 的字符串、地址列表或前端对象形状。"""
    if value is None:
        return set()
    if isinstance(value, Mapping):
        candidates = []
        for key in ("address", "email", "value", "mailAddress"):
            if value.get(key):
                candidates.append(value.get(key))
        return _recipient_addresses(candidates)
    if isinstance(value, (list, tuple, set)):
        addresses: set[str] = set()
        for item in value:
            addresses.update(_recipient_addresses(item))
        return addresses
    return {address.casefold() for _, address in getaddresses([str(value)]) if address and address.count("@") == 1}


def _header_recipient_matches(header: Mapping[str, Any], recipient: str) -> bool:
    target = str(recipient or "").strip().casefold()
    return bool(target and target in _recipient_addresses(header.get("to")))


def _raw_mail(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    raw = item.get("rawData") if isinstance(item.get("rawData"), dict) else item
    return raw if isinstance(raw, dict) else {}


def _mail_id(item: Any) -> str:
    raw = _raw_mail(item)
    attribute = raw.get("attribute") if isinstance(raw.get("attribute"), dict) else {}
    value = attribute.get("mailIdentifier") or raw.get("mailIdentifier")
    result = str(value or "").strip()
    if not result or not re.fullmatch(r"[A-Za-z0-9._:-]+", result):
        raise MailComProtocolError("邮件列表响应缺少有效 mailIdentifier")
    return result


def _timestamp(value: Any) -> float | None:
    if isinstance(value, dict):
        raw = _raw_mail(value)
        attr = raw.get("attribute") if isinstance(raw.get("attribute"), dict) else {}
        for key in ("internalDate", "date", "timestamp", "created_at"):
            result = _timestamp(attr.get(key) if key in attr else raw.get(key))
            if result is not None:
                return result
        return None
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000 if number > 10_000_000_000 else number
    text = str(value).strip()
    try:
        number = float(text)
        return number / 1000 if number > 10_000_000_000 else number
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _cache_buster() -> str:
    return "a-" + secrets.token_urlsafe(12).rstrip("=")


def _timezone_hours() -> int:
    offset = datetime.now().astimezone().utcoffset()
    return int((offset.total_seconds() if offset else 0) / 3600)


def _parse_login_form(page_html: str) -> tuple[str, dict[str, str]]:
    parser = _LoginFormParser()
    parser.feed(str(page_html or ""))
    for action, fields in parser.forms:
        absolute = urljoin(LOGIN_PAGE_URL, action)
        split = urlsplit(absolute)
        if split.netloc.casefold() == "login.mail.com" and split.path == "/login":
            return absolute, fields
    raise MailComProtocolError("登录页没有找到 login.mail.com/login 表单")


def _parse_settings_root_url(page_html: str, sid: str) -> str:
    """从 navigator settings 页面提取一次性的 mailset-root 启动地址。"""
    content = html.unescape(str(page_html or "")).replace(r"\/", "/").replace(r"\u0026", "&")
    for candidate in re.findall(r"https://mailset-root\.mail\.com[^\s\"'<>\\]*", content, re.IGNORECASE):
        parsed = urlsplit(candidate.rstrip(".,;)"))
        if parsed.scheme != "https" or parsed.netloc.casefold() != "mailset-root.mail.com" or parsed.path not in {"", "/"}:
            continue
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if params.get("navsid") != sid:
            continue
        if not all(params.get(key) for key in ("iac_appname", "iac_token")):
            continue
        return parsed.geturl()
    raise MailComProtocolError("mail.com settings 启动页缺少有效 mailset-root 会话地址")


def _response_json(response: Any, endpoint: str) -> Any:
    try:
        return response.json()
    except (ValueError, TypeError) as exc:
        raise MailComProtocolError(
            f"mail.com {endpoint} 响应不是 JSON（HTTP {getattr(response, 'status_code', 0)}）",
            diagnostic=safe_request_diagnostic(
                endpoint=endpoint,
                status=getattr(response, "status_code", None),
                headers=getattr(response, "headers", {}),
                error_type="invalid_json",
            ),
        ) from exc


class MailComClient:
    """可注入 session 的 mail.com 协议客户端。"""

    def __init__(self, session: Any | None = None, *, timeout: int | None = None,
                 access_token: str = "", now: Callable[[], float] | None = None) -> None:
        self.session = session or _http.Session(impersonate="chrome")
        self.timeout = int(timeout if timeout is not None else getattr(_email_cfg, "MAILCOM_REQUEST_TIMEOUT", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT)
        self.access_token = str(access_token or "")
        self.sid = ""  # 仅当前登录链路使用，不写入持久化记录
        self._now = now or time.time
        headers = getattr(self.session, "headers", None)
        if headers is not None:
            headers.update({
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
            })

    @property
    def has_persisted_token(self) -> bool:
        return bool(self.access_token)

    def _request(self, method: str, url: str, *, endpoint: str, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", self.timeout)
        try:
            request = getattr(self.session, method.lower())
            response = request(url, **kwargs)
        except Exception as exc:
            raise MailComTransientError(
                f"mail.com {endpoint} 网络请求失败（{type(exc).__name__}）",
                diagnostic=safe_request_diagnostic(endpoint=endpoint, error_type="network_error"),
            ) from exc
        if is_invalid_token_response(response):
            raise MailComInvalidTokenError(
                f"mail.com {endpoint} 返回确认的 invalid_token",
                diagnostic=safe_request_diagnostic(
                    endpoint=endpoint,
                    status=getattr(response, "status_code", None),
                    headers=getattr(response, "headers", {}),
                    error_type="invalid_token",
                ),
            )
        status = int(getattr(response, "status_code", 0) or 0)
        if status == 401:
            error_type = "unauthorized"
        elif status == 403:
            error_type = "forbidden_or_risk"
        elif status == 429:
            error_type = "rate_limited"
        elif status >= 500:
            error_type = "upstream_error"
        else:
            error_type = "http_error"
        if status >= 400:
            cls = MailComRateLimitError if status == 429 else MailComError
            raise cls(
                f"mail.com {endpoint} 请求失败（HTTP {status}，{error_type}）",
                diagnostic=safe_request_diagnostic(
                    endpoint=endpoint,
                    status=status,
                    headers=getattr(response, "headers", {}),
                    error_type=error_type,
                ),
            )
        return response

    def set_access_token(self, token: str) -> None:
        token = str(token or "").strip()
        if not token:
            raise MailComAuthError("mailbox access token 为空")
        self.access_token = token

    def login(self, account: str, password: str) -> None:
        account = str(account or "").strip()
        password = str(password or "")
        if not _is_email(account) or not password:
            raise MailComCredentialError("mail.com 账号或密码为空/格式无效")
        page = self._request(
            "GET", LOGIN_PAGE_URL, endpoint="login_page",
            headers={"Accept": "text/html,application/xhtml+xml"},
        )
        if int(getattr(page, "status_code", 0)) != 200:
            raise MailComAuthError("mail.com 登录页请求失败")
        action, form = _parse_login_form(getattr(page, "text", ""))
        form.update({"username": account, "password": password})
        try:
            response = self._request(
                "POST", action, endpoint="login_submit", data=form,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Origin": "https://www.mail.com",
                    "Referer": LOGIN_PAGE_URL,
                }, allow_redirects=False,
            )
        except MailComError as exc:
            # 401 from the credential endpoint is a confirmed credential failure;
            # 403 remains a separate risk/forbidden category for the pool.
            if exc.diagnostic.get("status") == 401:
                raise MailComCredentialError("mail.com 账密登录被拒绝") from exc
            raise
        status = int(getattr(response, "status_code", 0))
        if status not in {302, 303}:
            raise MailComCredentialError("mail.com 账密登录未返回预期跳转（可能需要验证码/二次验证）")
        location = (getattr(response, "headers", {}) or {}).get("Location")
        if not location:
            raise MailComProtocolError("mail.com 登录提交缺少 Location")
        redirect = urljoin(action, str(location))
        split = urlsplit(redirect)
        if split.netloc.casefold() != NAVIGATOR_HOST or split.path != "/login":
            raise MailComProtocolError("mail.com 登录跳转不是预期 navigator 入口")
        self._request(
            "GET", redirect, endpoint="login_redirect",
            headers={"Referer": LOGIN_PAGE_URL}, allow_redirects=False,
        )
        params = dict(parse_qsl(split.query, keep_blank_values=True))
        params.update({"auth_time": "1", "tz": str(_timezone_hours())})
        halogin = urlunsplit((split.scheme, split.netloc, "/halogin", urlencode(params), ""))
        final = self._request(
            "GET", halogin, endpoint="login_complete",
            headers={"Referer": redirect}, allow_redirects=True,
        )
        candidates = [str(getattr(final, "url", "") or "")]
        for history in getattr(final, "history", []) or []:
            candidates.extend([
                str(getattr(history, "url", "") or ""),
                str((getattr(history, "headers", {}) or {}).get("Location", "") or ""),
            ])
        for candidate in candidates:
            parsed = urlsplit(candidate)
            query = dict(parse_qsl(parsed.query, keep_blank_values=True))
            if parsed.netloc.casefold() == NAVIGATOR_HOST and query.get("sid"):
                self.sid = query["sid"]
                break
        if not self.sid:
            raise MailComCredentialError("mail.com 登录完成后没有 sid（可能需要验证码或二次验证）")

    def _exchange_token(
        self,
        *,
        endpoint: str,
        client_id: str,
        scope: str,
        origin: str,
        referer: str,
        ui_app: str | None = None,
    ) -> MailComToken:
        if not self.sid:
            raise MailComAuthError("没有当前登录链路 sid，不能交换 mailbox token")
        basic = base64.b64encode(f"{client_id}:{OAUTH_PUBLIC_SECRET}".encode()).decode("ascii")
        headers = {
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": origin,
            "Referer": referer,
            "Authorization": f"Basic {basic}",
        }
        if ui_app:
            headers["X-UI-App"] = ui_app
        response = self._request(
            "POST", OAUTH_URL, endpoint=endpoint, params={"sid": self.sid},
            data={"grant_type": "urn:mam:oauth:grant-type:spa", "scope": scope},
            headers=headers,
        )
        if int(getattr(response, "status_code", 0)) != 200:
            raise MailComAuthError(f"mail.com {endpoint} 请求失败")
        payload = _response_json(response, endpoint)
        if not isinstance(payload, dict):
            raise MailComProtocolError(f"mail.com {endpoint} 响应不是对象")
        token = str(payload.get("access_token") or "").strip()
        try:
            expires_in = int(payload.get("expires_in"))
        except (TypeError, ValueError):
            expires_in = 0
        if not token or expires_in <= 0:
            raise MailComProtocolError(f"mail.com {endpoint} 缺少 access_token 或有效 expires_in")
        return MailComToken(
            access_token=token,
            expires_in=expires_in,
            expires_at=float(self._now()) + expires_in,
            scope=str(payload.get("scope") or ""),
            token_type=str(payload.get("token_type") or "Bearer"),
        )

    def exchange_token(self) -> MailComToken:
        result = self._exchange_token(
            endpoint="oauth_token",
            client_id=OAUTH_CLIENT_ID,
            scope="mail_mailbox_r",
            origin=WEBMAIL_ORIGIN,
            referer=WEBMAIL_ORIGIN + "/",
            ui_app="mailcom.webmailer.mail-list/6.6.3",
        )
        self.access_token = result.access_token
        return result

    def bootstrap_settings_session(self) -> MailComToken:
        """建立仅当前调用使用的 settings 启动上下文并换取 settings token。"""
        if not self.sid:
            raise MailComAuthError("没有当前登录链路 sid，不能启动 settings 会话")
        logger.info("[MailComSettings] stage=settings_navigation action=request")
        try:
            page = self._request(
                "GET",
                SETTINGS_NAVIGATION_URL,
                endpoint="settings_navigation",
                params={"sid": self.sid},
                headers={"Accept": "text/html,application/xhtml+xml", "Referer": f"https://{NAVIGATOR_HOST}/"},
            )
        except MailComError as exc:
            logger.warning(
                "[MailComSettings] stage=settings_navigation action=error error_type=%s",
                exc.error_type,
            )
            raise
        page_status = int(getattr(page, "status_code", 0) or 0)
        if page_status != 200:
            logger.warning(
                "[MailComSettings] stage=settings_navigation action=error status=%s error_type=protocol_error",
                page_status,
            )
            raise MailComProtocolError("mail.com settings 启动页返回非 200 响应")
        logger.info("[MailComSettings] stage=settings_navigation action=success status=%s", page_status)
        logger.info("[MailComSettings] stage=settings_root action=request")
        try:
            root_url = _parse_settings_root_url(getattr(page, "text", ""), self.sid)
            root = self._request(
                "GET",
                root_url,
                endpoint="settings_root",
                headers={"Accept": "text/html,application/xhtml+xml", "Referer": f"https://{NAVIGATOR_HOST}/"},
            )
        except MailComError as exc:
            logger.warning(
                "[MailComSettings] stage=settings_root action=error error_type=%s",
                exc.error_type,
            )
            raise
        root_status = int(getattr(root, "status_code", 0) or 0)
        if root_status != 200:
            logger.warning(
                "[MailComSettings] stage=settings_root action=error status=%s error_type=protocol_error",
                root_status,
            )
            raise MailComProtocolError("mail.com settings 根页面返回非 200 响应")
        logger.info("[MailComSettings] stage=settings_root action=success status=%s", root_status)
        logger.info("[MailComSettings] stage=settings_oauth_token action=request")
        try:
            token = self._exchange_token(
                endpoint="settings_oauth_token",
                client_id=SETTINGS_OAUTH_CLIENT_ID,
                scope=SETTINGS_OAUTH_SCOPE,
                origin=SETTINGS_ROOT_ORIGIN,
                referer=SETTINGS_ROOT_ORIGIN + "/",
            )
        except MailComError as exc:
            logger.warning(
                "[MailComSettings] stage=settings_oauth_token action=error error_type=%s",
                exc.error_type,
            )
            raise
        logger.info("[MailComSettings] stage=settings_oauth_token action=success")
        return token

    def authenticate(self, account: str, password: str) -> MailComToken:
        self.login(account, password)
        return self.exchange_token()

    def _api_headers(self, app: str, accept: str) -> dict[str, str]:
        if not self.access_token:
            raise MailComAuthError("mailbox access token 未设置")
        return {
            "Accept": accept,
            "Authorization": f"Bearer {self.access_token}",
            "Origin": WEBMAIL_ORIGIN,
            "Referer": WEBMAIL_ORIGIN + "/",
            "X-Request-ID": str(uuid4()),
            "X-UI-App": app,
        }

    def list_page(self, *, offset: int = 0, amount: int = DEFAULT_PAGE_SIZE) -> tuple[list[dict[str, Any]], int]:
        response = self._request(
            "POST", f"{MAILLIST_API}/Mailbox/Mail", endpoint="mail_list",
            params={
                "folderTypeOrId": "INBOX", "offset": int(offset), "amount": int(amount),
                "orderBy": "INTERNALDATE DESC", "no_cache": _cache_buster(),
            },
            json={
                "aditionContext": {"brand": "mailcom", "category": "mail", "section": "3c/folder", "tagid": "inline", "layoutclass": "b"},
                "deviceContext": {"app": {"name": "browser"}, "deviceclass": "b"},
                "adBlocker": False,
                "mailboxContext": {"currentPage": int(offset) // max(1, int(amount)) + 1, "visibleMessages": 8},
            },
            headers={**self._api_headers("mailcom.webmailer.mail-list/6.6.3", "application/vnd.1and1.mms.unified-maillist-v1+json; charset=utf-8"),
                     "Content-Type": "application/vnd.1and1.mms.inboxadrequest-v1+json; charset=utf-8"},
        )
        payload = _response_json(response, "mail_list")
        if not isinstance(payload, dict) or not isinstance(payload.get("mailListElements"), list):
            raise MailComProtocolError("mail.com 邮件列表缺少 mailListElements")
        try:
            total = max(0, int(payload.get("totalCount") or 0))
        except (TypeError, ValueError):
            total = 0
        return [item for item in payload["mailListElements"] if isinstance(item, dict)], total

    def find_latest(self, sender: str, *, after_ts: float | None = None,
                    page_size: int = DEFAULT_PAGE_SIZE, max_pages: int = DEFAULT_MAX_PAGES) -> dict[str, Any]:
        sender = str(sender or "").strip().casefold()
        if not _is_email(sender):
            raise MailComProtocolError("目标发件人邮箱格式无效")
        offset = 0
        best_item: dict[str, Any] | None = None
        best_stamp = float("-inf")
        for _ in range(max(1, int(max_pages))):
            elements, total = self.list_page(offset=offset, amount=page_size)
            for item in elements:
                if item.get("type") != "mail":
                    continue
                raw = _raw_mail(item)
                header = raw.get("mailHeader") if isinstance(raw.get("mailHeader"), dict) else {}
                stamp = _timestamp(item)
                if after_ts is not None and stamp is not None and stamp < float(after_ts) - 30:
                    continue
                if sender in _sender_addresses(header.get("from")):
                    _mail_id(item)  # 提前校验，避免把不可信值放入路径
                    sort_stamp = stamp if stamp is not None else float("-inf")
                    if best_item is None or sort_stamp > best_stamp:
                        best_item, best_stamp = item, sort_stamp
            if not elements or offset + int(page_size) >= total:
                break
            offset += int(page_size)
        if best_item is not None:
            return best_item
        raise MailComError(f"收件箱中没有找到发件人 {sender} 的新邮件", error_type="not_found")

    def find_openai_candidates(self, *, after_ts: float | None = None,
                               page_size: int = DEFAULT_PAGE_SIZE,
                               max_pages: int = DEFAULT_MAX_PAGES,
                               strict_after: bool = False) -> list[dict[str, Any]]:
        """扫描 OpenAI 候选邮件；别名模式使用严格时间下界。"""
        candidates: list[tuple[float, dict[str, Any]]] = []
        offset = 0
        for _ in range(max(1, int(max_pages))):
            elements, total = self.list_page(offset=offset, amount=page_size)
            for item in elements:
                if item.get("type") != "mail":
                    continue
                raw = _raw_mail(item)
                header = raw.get("mailHeader") if isinstance(raw.get("mailHeader"), dict) else {}
                stamp = _timestamp(item)
                if after_ts is not None and (
                    stamp is None or stamp < float(after_ts) if strict_after
                    else stamp is not None and stamp < float(after_ts) - 30
                ):
                    continue
                if looks_like_openai_email({"from": header.get("from"), "subject": header.get("subject")}):
                    _mail_id(item)
                    sort_stamp = stamp if stamp is not None else float("-inf")
                    candidates.append((sort_stamp, item))
            if not elements or offset + int(page_size) >= total:
                break
            offset += int(page_size)
        candidates.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _, item in candidates]

    def find_latest_openai(self, *, after_ts: float | None = None,
                           page_size: int = DEFAULT_PAGE_SIZE,
                           max_pages: int = DEFAULT_MAX_PAGES) -> dict[str, Any]:
        """查找按时间倒序排列的最新 OpenAI/ChatGPT 候选邮件。"""
        candidates = self.find_openai_candidates(
            after_ts=after_ts, page_size=page_size, max_pages=max_pages
        )
        if candidates:
            return candidates[0]
        raise MailComError("收件箱中没有找到新的 OpenAI 验证邮件", error_type="not_found")

    def read_header(self, mail_id: str) -> dict[str, Any]:
        mail_id = str(mail_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._:-]+", mail_id):
            raise MailComProtocolError("邮件 ID 格式无效")
        response = self._request(
            "GET", f"{MAILBOX_API}/mailbox/primary/mailheader/{quote(mail_id, safe='')}", endpoint="mail_header",
            params={"absoluteURI": "false", "no_cache": _cache_buster()},
            headers=self._api_headers("mailcom.webmailer.mail-detail/7.40.1", "application/vnd.ui.trinity.message+json; charset=utf-8; client-meta=mail-drop;"),
        )
        payload = _response_json(response, "mail_header")
        if not isinstance(payload, dict):
            raise MailComProtocolError("邮件头响应不是对象")
        return payload

    def read_body(self, mail_id: str) -> str:
        mail_id = str(mail_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._:-]+", mail_id):
            raise MailComProtocolError("邮件 ID 格式无效")
        # 正文服务是独立的跨域 HTML endpoint。它通过表单 access_token
        # 认证，不接受邮件列表/邮件头接口的 Bearer、X-Request-ID 或 X-UI-App 头。
        response = self._request(
            "POST", f"{MAILBODY_API}/Mail/{quote(mail_id, safe='')}/Body/html", endpoint="mail_body",
            params={"target_origin": WEBMAIL_ORIGIN, "no_cache": _cache_buster()},
            data={"access_token": self.access_token},
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": WEBMAIL_ORIGIN,
                "Referer": WEBMAIL_ORIGIN + "/",
            },
        )
        return clean_html_body(getattr(response, "text", ""))

    def read_message(self, item: dict[str, Any], *, header: dict[str, Any] | None = None) -> MailComMessage:
        mail_id = _mail_id(item)
        raw = _raw_mail(item)
        base_header = raw.get("mailHeader") if isinstance(raw.get("mailHeader"), dict) else {}
        if header is None:
            detail = self.read_header(mail_id)
            header = detail.get("mailHeader") if isinstance(detail.get("mailHeader"), dict) else {}
        if header:
            header = {**base_header, **header}
        else:
            header = base_header
        return MailComMessage(
            mail_id=mail_id,
            sender=str(header.get("from") or ""),
            subject=str(header.get("subject") or ""),
            date=str(header.get("date") or ""),
            internal_date=_timestamp(item),
            body=self.read_body(mail_id),
            raw_header=header,
        )

    def fetch_latest_otp(self, sender: str | None = None, *, after_ts: float | None = None,
                         max_wait: int | None = None, poll_interval: int | None = None,
                         settle_seconds: int | None = None, page_size: int | None = None,
                         recipient: str | None = None) -> str:
        wait = max(0, int(max_wait if max_wait is not None else getattr(_email_cfg, "OTP_MAX_WAIT", 90) or 90))
        interval = max(1, int(poll_interval if poll_interval is not None else getattr(_email_cfg, "OTP_POLL_INTERVAL", 3) or 3))
        settle = max(0, int(settle_seconds if settle_seconds is not None else getattr(_email_cfg, "OTP_SETTLE_SECONDS", 5) or 5))
        configured_page_size = max(1, int(getattr(_email_cfg, "MAILCOM_PAGE_SIZE", DEFAULT_PAGE_SIZE) or DEFAULT_PAGE_SIZE))
        configured_max_pages = max(1, int(getattr(_email_cfg, "MAILCOM_MAX_PAGES", DEFAULT_MAX_PAGES) or DEFAULT_MAX_PAGES))
        effective_page_size = page_size or configured_page_size
        deadline = time.monotonic() + wait
        best: tuple[float, str] | None = None
        settle_until: float | None = None
        last_error = "收件箱为空或没有新的 OpenAI 验证码"
        poll_count = 0
        # 即使 max_wait=0 也做一次即时读取，便于已到达邮件和 mock smoke test。
        while True:
            poll_count += 1
            poll_started = time.monotonic()
            try:
                if recipient:
                    # 别名模式先扫描候选并读取完整头，只有精确命中 to 才允许读取正文。
                    candidates = self.find_openai_candidates(
                        after_ts=after_ts,
                        page_size=effective_page_size,
                        max_pages=configured_max_pages,
                        strict_after=True,
                    )
                    item = None
                    full_header: dict[str, Any] | None = None
                    saw_recipient_mismatch = False
                    for candidate_item in candidates:
                        candidate_header = _raw_mail(candidate_item).get("mailHeader")
                        candidate_header = candidate_header if isinstance(candidate_header, dict) else {}
                        detail = self.read_header(_mail_id(candidate_item))
                        detail_header = detail.get("mailHeader") if isinstance(detail.get("mailHeader"), dict) else {}
                        merged_header = {**candidate_header, **detail_header}
                        if not _header_recipient_matches(merged_header, recipient):
                            saw_recipient_mismatch = True
                            continue
                        item = candidate_item
                        full_header = merged_header
                        break
                    if item is None:
                        raise MailComError(
                            "没有找到收件人为当前 mail.com 别名的 OpenAI 验证邮件",
                            error_type="recipient_mismatch" if saw_recipient_mismatch else "not_found",
                        )
                else:
                    item = (
                        self.find_latest(sender, after_ts=after_ts, page_size=effective_page_size, max_pages=configured_max_pages)
                        if sender else
                        self.find_latest_openai(after_ts=after_ts, page_size=effective_page_size, max_pages=configured_max_pages)
                    )
                    full_header = None
                raw = _raw_mail(item)
                header = raw.get("mailHeader") if isinstance(raw.get("mailHeader"), dict) else {}
                message_stub = {"from": header.get("from"), "subject": header.get("subject")}
                if not looks_like_openai_email(message_stub):
                    raise MailComError("匹配邮件不是 OpenAI 验证邮件", error_type="sender_mismatch")
                message = self.read_message(item, header=full_header)
                candidate = {"from": message.sender, "subject": message.subject, "text": message.body, "html": message.body}
                if not looks_like_openai_email(candidate):
                    raise MailComError("邮件正文不是 OpenAI 验证邮件", error_type="sender_mismatch")
                otp = extract_otp(candidate)
                if not otp:
                    raise MailComError("邮件正文没有独立六位验证码", error_type="otp_not_found")
                stamp = message.internal_date if message.internal_date is not None else float("-inf")
                if best is None or stamp >= best[0]:
                    if best is None or stamp > best[0] or otp != best[1]:
                        best = (stamp, otp)
                        settle_until = time.monotonic() + settle
                if best and settle_until is not None and time.monotonic() >= settle_until:
                    logger.info("[MailCom] OTP 轮询第 %s 轮找到验证码并完成稳定等待", poll_count)
                    return best[1]
            except MailComInvalidTokenError:
                raise
            except MailComError as exc:
                if exc.error_type == "unauthorized":
                    # 401 可能没有规范的 WWW-Authenticate invalid_token challenge；
                    # 仍应立即交给 Provider 刷新 Mailbox AT，而不是等待到超时。
                    raise
                last_error = f"{exc.error_type}"
                logger.info(
                    "[MailCom] OTP 轮询第 %s 轮未找到验证码: error=%s elapsed=%.1fs",
                    poll_count,
                    last_error,
                    time.monotonic() - poll_started,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(interval, remaining))
        if best:
            return best[1]
        logger.info("[MailCom] OTP 轮询结束: rounds=%s last_error=%s", poll_count, last_error)
        raise MailComError(f"等待 mail.com 验证码超时（{last_error}）", error_type="timeout")


# 进程内上下文只保留账号密码用于本次恢复；mailbox AT 由 DB 记录管理。
_CONTEXT_CACHE: dict[str, MailComAccount] = {}


def _cache_key(email: str) -> str:
    return str(email or "").strip().casefold()


def get_account_context(email: str) -> MailComAccount | None:
    return _CONTEXT_CACHE.get(_cache_key(email))


def put_account_context(account: MailComAccount) -> None:
    _CONTEXT_CACHE[_cache_key(account.email)] = account


def clear_account_context(email: str) -> None:
    _CONTEXT_CACHE.pop(_cache_key(email), None)


def build_client(*, access_token: str = "", timeout: int | None = None, session: Any | None = None) -> MailComClient:
    """创建新 session；传入 AT 时不会读取或恢复 Cookie/sid。"""
    return MailComClient(session=session, timeout=timeout, access_token=access_token)


def cold_start_read(*, access_token: str, sender: str | None = None, session: Any | None = None, **kwargs: Any) -> str:
    """AT-only 冷启动探针：新 session 只注入 Bearer AT。"""
    client = build_client(access_token=access_token, session=session)
    return client.fetch_latest_otp(sender, **kwargs)


# 改密历史模块曾引用的名称保留为兼容别名，但 provider 本身不导入它。
MailComLightClient = MailComClient
_http_session = lambda proxy=None: _http.Session(impersonate="chrome")
_mailcom_proxy = lambda: ""


__all__ = [
    "MailComError", "MailComAuthError", "MailComInvalidTokenError", "MailComCredentialError",
    "MailComRateLimitError", "MailComTransientError", "MailComProtocolError", "MailComAccount",
    "MailComToken", "MailComMessage", "MailComClient", "MailComLightClient", "clean_html_body",
    "get_account_context", "put_account_context", "clear_account_context", "build_client",
    "cold_start_read", "_CONTEXT_CACHE", "_http_session", "_mailcom_proxy",
]
