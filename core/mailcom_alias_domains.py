# -*- coding: utf-8 -*-
"""mail.com 别名候选域名和 local-part 的纯本地辅助函数。"""
from __future__ import annotations

import json
import re
import secrets
from pathlib import Path
from random import SystemRandom
from typing import Callable, Sequence


EXPECTED_DOMAIN_COUNT = 138
_DOMAIN_RE = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z"
)
_LOCAL_RE = re.compile(r"[^a-z0-9._-]+")
_TRIM_LOCAL_RE = re.compile(r"^[._-]+|[._-]+$")
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DOMAIN_PATH = _PROJECT_ROOT / "config" / "mailcom_alias_domains.json"


class MailComAliasDomainError(ValueError):
    """本地别名域名目录或 local-part 无法安全使用。"""


def load_alias_domains(path: str | Path | None = None) -> tuple[str, ...]:
    """加载并严格校验受版本控制的候选域名目录。"""
    source = Path(path) if path is not None else DEFAULT_DOMAIN_PATH
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MailComAliasDomainError("mail.com 别名域名目录不可读取或不是有效 JSON") from exc
    if not isinstance(data, list):
        raise MailComAliasDomainError("mail.com 别名域名目录必须是 JSON 数组")

    domains: list[str] = []
    seen: set[str] = set()
    for raw in data:
        domain = str(raw or "").strip().casefold()
        if not domain or not _DOMAIN_RE.fullmatch(domain):
            raise MailComAliasDomainError("mail.com 别名域名目录包含非法域名")
        if domain in seen:
            raise MailComAliasDomainError("mail.com 别名域名目录包含重复域名")
        seen.add(domain)
        domains.append(domain)
    if len(domains) != EXPECTED_DOMAIN_COUNT:
        raise MailComAliasDomainError(
            f"mail.com 别名域名目录数量错误，期望 {EXPECTED_DOMAIN_COUNT}，实际 {len(domains)}"
        )
    return tuple(domains)


def choose_alias_domain(
    *,
    domains: Sequence[str] | None = None,
    rng: SystemRandom | None = None,
) -> str:
    """从已校验的目录随机选择一个域名。"""
    candidates = tuple(domains) if domains is not None else load_alias_domains()
    if not candidates:
        raise MailComAliasDomainError("mail.com 别名域名目录为空")
    return (rng or SystemRandom()).choice(candidates)


def normalize_alias_local_part(value: object) -> str:
    """把现有英文随机姓名转成保守的 ASCII 邮箱 local-part。"""
    text = str(value or "").encode("ascii", "ignore").decode("ascii").casefold()
    local = _LOCAL_RE.sub("", text)
    local = _TRIM_LOCAL_RE.sub("", local)
    if not 3 <= len(local) <= 58:
        raise MailComAliasDomainError("随机名称无法生成合法的 mail.com 别名 local-part")
    return local


def generate_alias_local_part(
    *,
    name_factory: Callable[[], str] | None = None,
    suffix_factory: Callable[[], str] | None = None,
) -> str:
    """以项目的随机英文名称为前缀，增加短随机后缀降低地址冲突率。"""
    if name_factory is None:
        from core.name_samples import random_display_name

        name_factory = random_display_name
    base = normalize_alias_local_part(name_factory())
    suffix = normalize_alias_local_part((suffix_factory or (lambda: secrets.token_hex(3)))())
    local = (base[: 64 - len(suffix)] + suffix).strip("._-")
    if not 3 <= len(local) <= 64:
        raise MailComAliasDomainError("随机名称无法生成合法的 mail.com 别名 local-part")
    return local


__all__ = [
    "DEFAULT_DOMAIN_PATH",
    "EXPECTED_DOMAIN_COUNT",
    "MailComAliasDomainError",
    "choose_alias_domain",
    "generate_alias_local_part",
    "load_alias_domains",
    "normalize_alias_local_part",
]
