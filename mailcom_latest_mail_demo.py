# -*- coding: utf-8 -*-
"""mail.com 独立取信 demo。

输入文件 ``mail_test.txt`` 的格式固定为三行：

1. mail.com 账号
2. mail.com 密码
3. 要匹配的发件人邮箱

脚本只读取 INBOX，并返回匹配发件人的最新一封邮件正文。登录会话、
OAuth token 和邮件内容只保留在内存中，不写入文件。
"""

from __future__ import annotations

import argparse
import base64
import html
import re
import secrets
import sys
from dataclasses import dataclass
from datetime import datetime
from email.utils import getaddresses
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit
from uuid import uuid4

from curl_cffi import requests


LOGIN_PAGE_URL = "https://www.mail.com/premiumlogin"
LOGIN_URL = "https://login.mail.com/login"
OAUTH_URL = "https://oauthbridge.navigator-lxa.mail.com/navigator/oauth2/token"
NAVIGATOR_HOST = "navigator-lxa.mail.com"
WEBMAIL_ORIGIN = "https://webmailer.mail.com"
MAILBOX_API = "https://webmail-cats-live.mail.com"
MAILLIST_API = "https://maillist.mail.com"
MAILBODY_API = "https://mailcom.mailbody-ui.de"
REQUEST_TIMEOUT = 30
PAGE_SIZE = 50
OAUTH_CLIENT_ID = "mailcom_webmailermaillist_passport_live"
# 该 public secret 来自 mail.com 公开下发的 Webmailer mail-list 配置，
# 是 OAuthBridge 的客户端识别字段，不是用户邮箱密码或会话凭据。
OAUTH_PUBLIC_SECRET = "*******"


class MailComDemoError(RuntimeError):
    """demo 可预期的失败。"""


@dataclass(frozen=True)
class MailCredentials:
    account: str
    password: str
    sender: str


@dataclass(frozen=True)
class MailSummary:
    mail_id: str
    sender: str
    subject: str
    date: str
    body: str


class _LoginFormParser(HTMLParser):
    """从登录页提取 login.mail.com/login 表单的字段。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[tuple[str, dict[str, str]]] = []
        self._action = ""
        self._fields: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "form":
            self._action = attrs_dict.get("action", "")
            self._fields = {}
        elif tag.lower() == "input" and self._action:
            name = attrs_dict.get("name", "")
            if name:
                self._fields[name] = attrs_dict.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "form" and self._action:
            self.forms.append((self._action, dict(self._fields)))
            self._action = ""
            self._fields = {}


class _VisibleTextParser(HTMLParser):
    """把邮件 HTML 转成适合终端输出的纯文本。"""

    _BLOCK_TAGS = {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "div",
        "dl",
        "dt",
        "dd",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
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


def _new_cache_buster() -> str:
    return "a-" + secrets.token_urlsafe(16).rstrip("=")


def _local_timezone_hours() -> int:
    offset = datetime.now().astimezone().utcoffset()
    return int((offset.total_seconds() if offset else 0) / 3600)


def _safe_response_json(response: Any, endpoint: str) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise MailComDemoError(
            f"mail.com 接口返回不是 JSON：{endpoint}（HTTP {response.status_code}）"
        ) from exc


def _check_status(response: Any, endpoint: str, expected: set[int] | None = None) -> None:
    allowed = expected or set(range(200, 400))
    if response.status_code not in allowed:
        raise MailComDemoError(f"mail.com 请求失败：{endpoint}（HTTP {response.status_code}）")


def _parse_login_form(page_html: str) -> tuple[str, dict[str, str]]:
    parser = _LoginFormParser()
    parser.feed(page_html)
    for action, fields in parser.forms:
        absolute_action = urljoin(LOGIN_PAGE_URL, action)
        if urlsplit(absolute_action).netloc == "login.mail.com" and urlsplit(absolute_action).path == "/login":
            return absolute_action, fields
    raise MailComDemoError("未找到 mail.com 登录表单")


def _read_credentials(path: Path) -> MailCredentials:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise MailComDemoError(f"无法读取输入文件：{path}") from exc

    if len(lines) != 3:
        raise MailComDemoError("mail_test.txt 必须严格包含三行：账号、密码、发件人邮箱")
    account, password, sender = (line.strip() for line in lines)
    if not account or not password or not sender:
        raise MailComDemoError("mail_test.txt 的三行都不能为空")
    if not _is_email(account) or not _is_email(sender):
        raise MailComDemoError("账号和发件人邮箱必须是有效邮箱地址")
    return MailCredentials(account=account, password=password, sender=sender.casefold())


def _is_email(value: str) -> bool:
    parsed = getaddresses([value])
    return len(parsed) == 1 and bool(parsed[0][1]) and parsed[0][1].count("@") == 1


def _sender_addresses(value: Any) -> set[str]:
    if isinstance(value, (list, tuple)):
        values = [str(item) for item in value]
    else:
        values = [str(value or "")]
    return {address.casefold() for _, address in getaddresses(values) if address}


def _mail_id(item: dict[str, Any]) -> str:
    raw = item.get("rawData") if isinstance(item.get("rawData"), dict) else item
    attribute = raw.get("attribute") if isinstance(raw.get("attribute"), dict) else {}
    value = attribute.get("mailIdentifier") or raw.get("mailIdentifier")
    mail_id = str(value or "").strip()
    if not mail_id or not re.fullmatch(r"[A-Za-z0-9._:-]+", mail_id):
        raise MailComDemoError("邮件列表返回了无效的 mailIdentifier")
    return mail_id


def _raw_mail(item: dict[str, Any]) -> dict[str, Any]:
    raw = item.get("rawData") if isinstance(item.get("rawData"), dict) else item
    return raw if isinstance(raw, dict) else {}


class MailComClient:
    """mail.com 登录、列表和正文读取客户端。"""

    def __init__(self, session: Any | None = None, timeout: int = REQUEST_TIMEOUT) -> None:
        self.session = session or requests.Session(impersonate="chrome")
        self.timeout = timeout
        self.access_token = ""
        self.sid = ""
        self.session.headers.update(
            {
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
            }
        )

    def _get(self, url: str, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", self.timeout)
        return self.session.get(url, **kwargs)

    def _post(self, url: str, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", self.timeout)
        return self.session.post(url, **kwargs)

    def login(self, account: str, password: str) -> None:
        page = self._get(LOGIN_PAGE_URL, headers={"Accept": "text/html,application/xhtml+xml"})
        _check_status(page, "登录页", {200})
        action, form = _parse_login_form(page.text)
        form["username"] = account
        form["password"] = password

        login_response = self._post(
            action,
            data=form,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Origin": "https://www.mail.com",
                "Referer": LOGIN_PAGE_URL,
            },
            allow_redirects=False,
        )
        _check_status(login_response, "登录提交", {302, 303})
        location = login_response.headers.get("Location")
        if not location:
            raise MailComDemoError("登录提交没有返回跳转地址")
        login_redirect = urljoin(action, location)
        split = urlsplit(login_redirect)
        if split.netloc != NAVIGATOR_HOST or split.path != "/login":
            raise MailComDemoError("登录跳转地址不是预期的 mail.com 导航入口")

        # 浏览器会先访问 /login，再把同一组一次性参数转交给 /halogin。
        login_page = self._get(login_redirect, headers={"Referer": LOGIN_PAGE_URL}, allow_redirects=False)
        _check_status(login_page, "登录中转", {200, 302, 303})
        params = dict(parse_qsl(split.query, keep_blank_values=True))
        params["auth_time"] = "1"
        params["tz"] = str(_local_timezone_hours())
        halogin = urlunsplit((split.scheme, split.netloc, "/halogin", urlencode(params), ""))
        final = self._get(halogin, headers={"Referer": login_redirect}, allow_redirects=True)
        _check_status(final, "登录完成", {200})

        final_urls = [str(getattr(final, "url", "") or "")]
        for history in getattr(final, "history", []) or []:
            final_urls.append(str(getattr(history, "url", "") or ""))
            final_urls.append(str(getattr(history, "headers", {}).get("Location", "") or ""))
        for candidate in final_urls:
            query = dict(parse_qsl(urlsplit(candidate).query, keep_blank_values=True))
            if urlsplit(candidate).netloc == NAVIGATOR_HOST and query.get("sid"):
                self.sid = query["sid"]
                break
        if not self.sid:
            raise MailComDemoError("登录完成后没有获得导航会话 sid；账号可能需要验证码或二次验证")

    def _token(self) -> str:
        basic_value = base64.b64encode(
            f"{OAUTH_CLIENT_ID}:{OAUTH_PUBLIC_SECRET}".encode("utf-8")
        ).decode("ascii")
        response = self._post(
            OAUTH_URL,
            params={"sid": self.sid},
            data={
                "grant_type": "urn:mam:oauth:grant-type:spa",
                "scope": "mail_mailbox_r",
            },
            headers={
                "Accept": "*/*",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": WEBMAIL_ORIGIN,
                "Referer": WEBMAIL_ORIGIN + "/",
                "Authorization": f"Basic {basic_value}",
                "X-UI-App": "mailcom.webmailer.mail-list/6.6.3",
            },
        )
        _check_status(response, "OAuth token", {200})
        payload = _safe_response_json(response, "OAuth token")
        token = str(payload.get("access_token") or "") if isinstance(payload, dict) else ""
        if not token:
            raise MailComDemoError("OAuth token 响应缺少 access_token")
        self.access_token = token
        return token

    def _api_headers(self, app: str, accept: str) -> dict[str, str]:
        return {
            "Accept": accept,
            "Authorization": f"Bearer {self.access_token}",
            "Origin": WEBMAIL_ORIGIN,
            "Referer": WEBMAIL_ORIGIN + "/",
            "X-Request-ID": str(uuid4()),
            "X-UI-App": app,
        }

    def _find_latest_item(self, sender: str) -> dict[str, Any]:
        target = sender.casefold()
        offset = 0
        while offset < 10000:
            response = self._post(
                f"{MAILLIST_API}/Mailbox/Mail",
                params={
                    "folderTypeOrId": "INBOX",
                    "offset": offset,
                    "amount": PAGE_SIZE,
                    "orderBy": "INTERNALDATE DESC",
                    "no_cache": _new_cache_buster(),
                },
                json={
                    "aditionContext": {
                        "brand": "mailcom",
                        "category": "mail",
                        "section": "3c/folder",
                        "tagid": "inline",
                        "layoutclass": "b",
                    },
                    "deviceContext": {"app": {"name": "browser"}, "deviceclass": "b"},
                    "adBlocker": False,
                    "mailboxContext": {"currentPage": offset // PAGE_SIZE + 1, "visibleMessages": 8},
                },
                headers={
                    **self._api_headers(
                        "mailcom.webmailer.mail-list/6.6.3",
                        "application/vnd.1and1.mms.unified-maillist-v1+json; charset=utf-8",
                    ),
                    "Content-Type": "application/vnd.1and1.mms.inboxadrequest-v1+json; charset=utf-8",
                },
            )
            _check_status(response, "邮件列表", {200})
            payload = _safe_response_json(response, "邮件列表")
            if not isinstance(payload, dict):
                raise MailComDemoError("邮件列表响应格式错误")
            elements = payload.get("mailListElements")
            if not isinstance(elements, list):
                raise MailComDemoError("邮件列表响应缺少 mailListElements")
            # 请求显式按 INTERNALDATE DESC 排序，因此第一页中第一个匹配项就是最新邮件。
            for item in elements:
                if not isinstance(item, dict) or item.get("type") != "mail":
                    continue
                raw = _raw_mail(item)
                header = raw.get("mailHeader") if isinstance(raw.get("mailHeader"), dict) else {}
                if target in _sender_addresses(header.get("from")):
                    return item
            total = int(payload.get("totalCount") or 0)
            if offset + PAGE_SIZE >= total or not elements:
                break
            offset += PAGE_SIZE
        raise MailComDemoError(f"收件箱中没有找到发件人 {sender} 的邮件")

    def _read_body(self, mail_id: str) -> str:
        response = self._post(
            f"{MAILBODY_API}/Mail/{quote(mail_id, safe='')}/Body/html",
            params={"target_origin": WEBMAIL_ORIGIN, "no_cache": _new_cache_buster()},
            data={"access_token": self.access_token},
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": WEBMAIL_ORIGIN,
                "Referer": WEBMAIL_ORIGIN + "/",
            },
        )
        _check_status(response, "邮件正文", {200})
        parser = _VisibleTextParser()
        parser.feed(response.text)
        return parser.text()

    def fetch_latest(self, sender: str) -> MailSummary:
        if not self.sid:
            raise MailComDemoError("尚未登录")
        self._token()
        item = self._find_latest_item(sender)
        raw = _raw_mail(item)
        header = raw.get("mailHeader") if isinstance(raw.get("mailHeader"), dict) else {}
        mail_id = _mail_id(item)

        detail_response = self._get(
            f"{MAILBOX_API}/mailbox/primary/mailheader/{quote(mail_id, safe='')}",
            params={"absoluteURI": "false", "no_cache": _new_cache_buster()},
            headers=self._api_headers(
                "mailcom.webmailer.mail-detail/7.40.1",
                "application/vnd.ui.trinity.message+json; charset=utf-8; client-meta=mail-drop;",
            ),
        )
        _check_status(detail_response, "邮件头", {200})
        detail = _safe_response_json(detail_response, "邮件头")
        if isinstance(detail, dict):
            detail_header = detail.get("mailHeader")
            if isinstance(detail_header, dict):
                header = detail_header

        sender_value = str(header.get("from") or "")
        subject = str(header.get("subject") or "")
        date_value = str(header.get("date") or "")
        body = self._read_body(mail_id)
        return MailSummary(mail_id=mail_id, sender=sender_value, subject=subject, date=date_value, body=body)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="读取 mail.com 中指定发件人的最新一封邮件")
    parser.add_argument(
        "--input",
        type=Path,
        default=Path(__file__).with_name("mail_test.txt"),
        help="三行输入文件，默认是项目根目录的 mail_test.txt",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        credentials = _read_credentials(args.input)
        client = MailComClient()
        try:
            client.login(credentials.account, credentials.password)
            result = client.fetch_latest(credentials.sender)
        finally:
            close = getattr(client.session, "close", None)
            if callable(close):
                close()
        print(f"发件人: {result.sender}")
        print(f"主题: {result.subject}")
        print(f"日期: {result.date}")
        print("正文:")
        print(result.body or "<空正文>")
        return 0
    except MailComDemoError as exc:
        print(f"失败: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"失败: 网络或协议异常（{type(exc).__name__}）", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
