# -*- coding: utf-8 -*-
"""导入型 iCloud 隐私邮箱客户端：地址来自本地池，邮件转发到 QQ IMAP。"""
from core import db

class ICloudMailError(RuntimeError):
    pass


def _normalize_poll_address(email: str) -> str:
    """Return the primary iCloud address used for forwarded-mail matching."""
    local, separator, domain = (email or "").partition("@")
    if not separator or domain.lower() != "icloud.com" or "+" not in local:
        return email
    primary_local = local.split("+", 1)[0]
    return f"{primary_local}@{domain}" if primary_local else email


def pick_account() -> dict:
    row = db.claim_next_icloud_email()
    if row is None:
        raise ICloudMailError("iCloud 隐私邮箱池没有可用地址，请先导入邮箱")
    return row

def fetch_latest_otp(email: str, after_ts: float | None = None, **kwargs) -> str:
    from core.qqmail_client import fetch_latest_otp as fetch_qq_otp
    return fetch_qq_otp(_normalize_poll_address(email), after_ts=after_ts, **kwargs)

def get_account_context(email: str):
    return db.get_icloud_email_by_email(email)

def release_account(email: str, status: str = "available", note: str | None = None) -> None:
    db.release_icloud_email(email, status=status, note=note)
