# -*- coding: utf-8 -*-
"""Plus 试用提链服务配置。"""
from config.env_loader import apply_env_overrides

# 提链服务地址
EXTRACT_LINK_API_BASE: str = ""

# 提链 CDK；创建任务和监听事件都需要。
EXTRACT_LINK_CDK: str = ""

# 提链类型：pix / upi / kakao_pay / ideal
EXTRACT_LINK_TYPE: str = "pix"

# 提链路由：默认保持现有 legacy + SSE 行为。
EXTRACT_LINK_PROVIDER: str = "legacy"
EXTRACT_LINK_UPDATE_MODE: str = "sse"

# 所有提链 provider 共用的可选显式代理。
EXTRACT_LINK_PROXY: str = ""

# Masi Kakao Job API。CDK 由 WebUI 的 Masi CDK 池管理，不保存在 .env。
MASI_KAKAO_API_BASE: str = "https://masi.cc.cd"

# 后台提链并发与超时
EXTRACT_LINK_WORKERS: int = 3
EXTRACT_LINK_QUEUE_LIMIT: int = 500
EXTRACT_LINK_REQUEST_TIMEOUT: int = 30
EXTRACT_LINK_EVENT_TIMEOUT: int = 180
EXTRACT_LINK_POLL_INTERVAL: float = 2.5
EXTRACT_LINK_POLL_MAX_ERRORS: int = 3

# Masi CDK 额度查询与选择。
MASI_CDK_QUERY_MAX_ATTEMPTS: int = 3
MASI_CDK_QUERY_RETRY_DELAY: float = 2.0
MASI_CDK_SELECTION_TIMEOUT: int = 180
MASI_CDK_REFRESH_WORKERS: int = 4

apply_env_overrides(globals(), {
    'EXTRACT_LINK_API_BASE': 'str',
    'EXTRACT_LINK_CDK': 'str',
    'EXTRACT_LINK_TYPE': 'str',
    'EXTRACT_LINK_PROVIDER': 'str',
    'EXTRACT_LINK_UPDATE_MODE': 'str',
    'EXTRACT_LINK_PROXY': 'str',
    'MASI_KAKAO_API_BASE': 'str',
    'EXTRACT_LINK_WORKERS': 'int',
    'EXTRACT_LINK_QUEUE_LIMIT': 'int',
    'EXTRACT_LINK_REQUEST_TIMEOUT': 'int',
    'EXTRACT_LINK_EVENT_TIMEOUT': 'int',
    'EXTRACT_LINK_POLL_INTERVAL': 'float',
    'EXTRACT_LINK_POLL_MAX_ERRORS': 'int',
    'MASI_CDK_QUERY_MAX_ATTEMPTS': 'int',
    'MASI_CDK_QUERY_RETRY_DELAY': 'float',
    'MASI_CDK_SELECTION_TIMEOUT': 'int',
    'MASI_CDK_REFRESH_WORKERS': 'int',
})
