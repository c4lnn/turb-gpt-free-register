# -*- coding: utf-8 -*-
"""sub2api 对接配置。"""
from config.env_loader import apply_env_overrides

# sub2api API 基址；Codex OAuth 使用这个地址。
SUB2API_API_BASE: str = ""

# sub2api 管理接口 API Key；为空则不带鉴权头。
SUB2API_API_KEY: str = ""

# 兼容旧配置名：SUB2API_API_TOKEN。
SUB2API_API_TOKEN: str = ""

# sub2api 管理接口鉴权头：x-api-key: <your-admin-api-key>。
SUB2API_API_AUTH_HEADER: str = "x-api-key"

# x-api-key 不需要 Bearer 前缀。
SUB2API_API_AUTH_PREFIX: str = ""

# 上传超时秒数。
SUB2API_API_TIMEOUT: int = 20

# ============================================================
# Codex OAuth 授权对接 sub2
# 当 config.codex.CODEX_AUTH_URL_SOURCE="sub2" 时使用：
#   1) 从 sub2 获取 Codex 授权链接
#   2) 浏览器/协议流程拿到 localhost callback 后回传给 sub2
# ============================================================

# 兼容旧配置：sub2 Codex 管理 API 基址；为空时使用 SUB2API_API_BASE。
SUB2_CODEX_API_BASE: str = ""

# 获取 Codex 授权链接接口路径。
# sub2api 当前接口：POST /api/v1/admin/openai/generate-auth-url
SUB2_CODEX_AUTH_URL_PATH: str = "/api/v1/admin/openai/generate-auth-url"

# 上传/提交 OAuth callback 并创建账号接口路径。
# sub2api 当前创建账号接口：POST /api/v1/admin/openai/create-from-oauth
SUB2_CODEX_CALLBACK_PATH: str = "/api/v1/admin/openai/create-from-oauth"

# 兼容旧配置：sub2 Codex API 鉴权 Token；为空时复用 SUB2API_API_KEY / SUB2API_API_TOKEN。
SUB2_CODEX_API_TOKEN: str = ""

# 鉴权头名称/前缀；为空时复用 SUB2API_API_AUTH_HEADER / SUB2API_API_AUTH_PREFIX。
SUB2_CODEX_AUTH_HEADER: str = ""
SUB2_CODEX_AUTH_PREFIX: str = ""

# callback 上传 payload：
# create_from_oauth => sub2api 原生创建账号：{"session_id","code","state","redirect_uri","name","concurrency","priority"}
# exchange_code     => 只换 token，不创建账号（兼容旧逻辑）
SUB2_CODEX_CALLBACK_PAYLOAD_MODE: str = "create_from_oauth"

apply_env_overrides(globals(), {
    'SUB2API_API_BASE': 'str',
    'SUB2API_API_KEY': 'str',
    'SUB2API_API_TOKEN': 'str',
    'SUB2API_API_AUTH_HEADER': 'str',
    'SUB2API_API_AUTH_PREFIX': 'str',
    'SUB2API_API_TIMEOUT': 'int',
    'SUB2_CODEX_API_BASE': 'str',
    'SUB2_CODEX_AUTH_URL_PATH': 'str',
    'SUB2_CODEX_CALLBACK_PATH': 'str',
    'SUB2_CODEX_API_TOKEN': 'str',
    'SUB2_CODEX_AUTH_HEADER': 'str',
    'SUB2_CODEX_AUTH_PREFIX': 'str',
    'SUB2_CODEX_CALLBACK_PAYLOAD_MODE': 'str',
})
