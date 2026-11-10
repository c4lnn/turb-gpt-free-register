# -*- coding: utf-8 -*-
"""邮箱来源的稳定契约。

配置层、持久化层与 WebUI 都按这份清单做校验和展示，新增来源时
应同步补充 WebUI 显示名称。
"""

VALID_EMAIL_SOURCES = (
    "outlook",
    "generic_api",
    "cloudflare_domain",
    "icloud",
    "cloudflare",
    "remail",
)
