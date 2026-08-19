# -*- coding: utf-8 -*-
"""历史 mail.com 改密入口的安全兼容边界。

mail.com 收码 provider 只依赖 ``core.mailcom_client`` 和
``core.mailcom_provider``。改密协议没有在本 change 中实现，故本模块不导入
client、不保留会话，也不记录或输出密码。
"""
from __future__ import annotations

import secrets
import string
from dataclasses import dataclass


class MailComPasswordChangeUnavailable(RuntimeError):
    """当前版本不提供 mail.com 改密能力。"""


# 保留旧调用方可能捕获的异常名。
MailComError = MailComPasswordChangeUnavailable


@dataclass(frozen=True)
class Account:
    """历史批处理入口使用的最小账号形状。"""

    username: str
    password: str


PASSWORD_ALPHABET = string.ascii_letters + string.digits + "!@#$%^&*_+="
PASSWORD_SYMBOLS = "!@#$%^&*_+="


def generate_mailcom_password(length: int = 12) -> str:
    """保留本地密码生成工具；不会向网络发送或记录生成值。"""
    length = max(12, int(length or 12))
    chars = [
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.digits),
        secrets.choice(PASSWORD_SYMBOLS),
    ]
    chars.extend(secrets.choice(PASSWORD_ALPHABET) for _ in range(length - len(chars)))
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def _unavailable() -> None:
    raise MailComPasswordChangeUnavailable(
        "mail.com 改密流程未在当前版本实现；收码 provider 不依赖该模块。"
    )


def ensure_mailcom_account_session(*args, **kwargs) -> None:
    _unavailable()


def change_mailcom_password(*args, **kwargs) -> None:
    _unavailable()


def change_account_password(*args, **kwargs):
    _unavailable()


def change_mailcom_password_for_email(*args, **kwargs) -> str:
    _unavailable()


def maybe_change_mailcom_password_before_register(*args, **kwargs) -> None:
    """显式 no-op，避免历史配置开关影响收码/注册主链路。"""
    return None


if __name__ == "__main__":
    raise SystemExit("mail.com 改密流程当前未实现；请勿把账号密码传给此模块。")
