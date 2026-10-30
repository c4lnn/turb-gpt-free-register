# -*- coding: utf-8 -*-
"""
配置读写层（供 WebUI /api/config 使用）。

设计原则：
    1. 白名单：只暴露"运行时安全"的开关/数值/默认值，协议级常量
       （client_id / scope / sentinel 版本等）一律不开放，避免一改就废号。
    2. 所有 WebUI 可编辑项统一写入项目根 `.env`，不再修改 `config/*.py`。
    3. `config/*.py` 只保留默认值；运行时通过 config.env_loader 用 `.env` 覆盖。
    4. 读取时优先 `.env`，缺失时回退解析 `config/*.py` 默认值。
"""
import ast
import os
import re
from pathlib import Path

from core.email_provider import VALID_EMAIL_SOURCES

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_DIR = _PROJECT_ROOT / "config"
EXPLICIT_EMPTY_LIST_KEYS = {
    "PROXY_POOL",
}

_EMAIL_SOURCE_LABELS = {
    "outlook": "Outlook 邮箱池",
    "generic_api": "通用 API 邮箱",
    "cloudflare_domain": "Cloudflare 域名邮箱",
    "icloud": "iCloud 隐私邮箱",
    "cloudflare": "Cloudflare 临时邮箱",
    "remail": "Remail 动态邮箱",
}
EMAIL_SOURCE_OPTIONS = tuple(
    {"value": source, "label": _EMAIL_SOURCE_LABELS[source]}
    for source in VALID_EMAIL_SOURCES
)


# ============================================================
# 白名单：每个可编辑项声明它在哪个文件、键名、类型、分组、说明
# type 决定前端控件 + 写回时的字面量格式：
#   bool   -> True/False
#   int    -> 整数
#   str    -> 带引号字符串
#   list_str_multiline -> 多行字符串列表（PROXY_POOL 专用，整块替换）
# ============================================================

EDITABLE_FIELDS = [
    # ---- WebUI 授权 ----
    {
        "key": "WEBUI_AUTH_CODE", "file": "codex.py", "type": "str", "group": "WebUI 授权",
        "label": "WebUI 授权码", "help": "仅保存在 .env（WEBUI_AUTH_CODE），避免出现在进程命令行中；保存后重启 WebUI 生效",
        "storage": "env", "secret": True,
    },
    {
        "key": "WEBUI_SESSION_SECRET", "file": "codex.py", "type": "str", "group": "WebUI 授权",
        "label": "Session 签名密钥", "help": "可选，保存在 .env（WEBUI_SESSION_SECRET）；不填则从固定授权码派生，修改授权码会使已有登录失效",
        "storage": "env", "secret": True,
    },
    # ---- 功能开关 ----
    {
        "key": "ENABLE_CODEX_AUTO", "file": "codex.py", "type": "bool", "group": "功能开关",
        "label": "启用 Codex OAuth", "help": "注册成功后自动跑 Codex 授权（全新session+接码），落盘 codex-邮箱.json",
    },
    {
        "key": "AUTO_PLAN_CHECK_AFTER_REGISTER", "file": "register.py", "type": "bool", "group": "注册方式",
        "label": "注册后自动查套餐", "help": "注册成功后自动入队查询套餐/Plus 资格；关闭后仅保存账号，不自动查套餐",
    },

    {
        "key": "ENABLE_2FA", "file": "twofa.py", "type": "bool", "group": "功能开关",
        "label": "启用 2FA(TOTP)", "help": "注册完成后自动设置动态口令（会多收一封 OTP 邮件）；失败会保存账号并记录 2FA 状态",
    },
    {
        "key": "ENABLE_FLOW_TRIGGER", "file": "flow_trigger.py", "type": "bool", "group": "功能开关",
        "label": "启用 Flow 触发", "help": "注册成功后自动调用内部 Flow 接口（不影响注册结果）",
    },
    {
        "key": "ENABLE_HUMANIZE_DELAY", "file": "humanize.py", "type": "bool", "group": "人工节奏",
        "label": "启用随机停顿", "help": "在注册、OTP、授权等步骤之间加入随机等待，更接近人工操作节奏",
    },
    {
        "key": "HUMANIZE_DELAY_FACTOR", "file": "humanize.py", "type": "float", "group": "人工节奏",
        "label": "停顿倍率", "help": "随机停顿整体倍率；1.0=默认，0.5=减半，2.0=加倍",
    },
    # ---- 邮箱 / OTP ----
    {
        "key": "USE_EMAIL_SERVICE", "file": "email.py", "type": "bool", "group": "邮箱 / OTP",
        "label": "自动取邮箱+收码", "help": "True=从邮箱池自动领邮箱并自动收 OTP；False=手动模式：用 REGISTER_EMAIL，OTP 在任务页手填",
    },
    {
        "key": "REGISTER_EMAIL", "file": "register.py", "type": "str", "group": "邮箱 / OTP",
        "label": "手动注册邮箱", "help": "USE_EMAIL_SERVICE=False 时必填。例如你的 outlook.com 地址；OTP 去网页邮箱看，再回任务页提交",
    },
    {
        "key": "REGISTER_NAME", "file": "register.py", "type": "str", "group": "邮箱 / OTP",
        "label": "显示名称", "help": "留空则自动生成英文名",
    },
    {
        "key": "OTP_MAX_WAIT", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "OTP 最长等待(秒)", "help": "等待验证码邮件的最长秒数，超时判失败",
    },
    {
        "key": "OTP_POLL_INTERVAL", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "OTP 轮询间隔(秒)", "help": "每隔多少秒查一次新邮件",
    },
    {
        "key": "EMAIL_SOURCE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "邮箱来源", "help": "选择一个或多个来源；第一个为主来源，后续来源按顺序兜底。",
        "control": "ordered_multi_select", "options": EMAIL_SOURCE_OPTIONS,
    },
    {
        "key": "CLOUDFLARE_API_BASE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare API 地址", "help": "Worker 临时邮箱 API 根地址，如 https://mail.example.com；选择 cloudflare 时必填",
        "storage": "env",
    },
    {
        "key": "CLOUDFLARE_API_KEY", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare API Key", "help": "匿名可空；admin 模式填 ADMIN_PASSWORD；保存在 .env",
        "storage": "env", "secret": True,
    },
    {
        "key": "CLOUDFLARE_AUTH_MODE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare 鉴权模式", "help": "none / bearer / x-api-key / x-admin-auth / query-key",
    },
    {
        "key": "CLOUDFLARE_CUSTOM_AUTH", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare 全局密码", "help": "Worker PASSWORDS，注入 x-custom-auth；保存在 .env",
        "storage": "env", "secret": True,
    },
    {
        "key": "CLOUDFLARE_PATH_ACCOUNTS", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare 创建路径", "help": "默认 /api/new_address；admin 常用 /admin/new_address",
    },
    {
        "key": "CLOUDFLARE_PATH_MESSAGES", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare 邮件路径", "help": "默认 /api/mails",
    },
    {
        "key": "CLOUDFLARE_PATH_DOMAINS", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare 域名路径", "help": "默认 /api/domains（预留）",
    },
    {
        "key": "CLOUDFLARE_PATH_TOKEN", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare Token路径", "help": "默认 /api/token（fallback 预留）",
    },
    {
        "key": "CLOUDFLARE_DEFAULT_DOMAINS", "file": "email.py", "type": "list_str_multiline", "group": "邮箱 / OTP",
        "label": "Cloudflare 默认域名", "help": "收信域名，每行一个或逗号分隔；创建时轮询使用，可留空",
    },
    {
        "key": "CLOUDFLARE_REQUEST_TIMEOUT", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "Cloudflare 请求超时(秒)", "help": "HTTP 请求超时，默认 20",
    },
    {
        "key": "CLOUDFLARE_NAME_LENGTH", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "Cloudflare 随机名前缀长度", "help": "admin 创建时 local-part 长度，默认 10",
    },
    {
        "key": "CLOUDFLARE_RANDOM_SUBDOMAIN_ENABLED", "file": "email.py", "type": "bool", "group": "邮箱 / OTP",
        "label": "启用 Cloudflare 随机子域名", "help": "开启后创建 <local>@<随机字母>-<固定后缀>.<默认域名>",
    },
    {
        "key": "CLOUDFLARE_RANDOM_SUBDOMAIN_LENGTH", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "Cloudflare 随机子域名长度", "help": "随机小写字母数量，默认 6，允许 1-32",
    },
    {
        "key": "CLOUDFLARE_RANDOM_SUBDOMAIN_SUFFIX", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare 子域名固定后缀", "help": "启用随机子域名后必填，例如 mail；只允许小写字母、数字和内部连字符",
    },
    {
        "key": "OUTLOOK_FETCH_MODE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Outlook取件模式", "help": "auto=远端优先，远端 402/DEPLOYMENT_DISABLED 自动切 Graph 直连；direct=只用 Microsoft Graph 直连；remote=只用远端服务",
    },
    {
        "key": "EMAIL_DOMAIN", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "转发域名(cloudflare_domain)", "help": "仅 cloudflare_domain 使用：Email Routing 的域名，如 mydomain.com；与 EMAIL_SOURCE=cloudflare 无关",
    },
    {
        "key": "QQ_EMAIL", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "QQ 邮箱地址", "help": "仅 cloudflare_domain：接收 Email Routing 转发的 QQ 邮箱，如 123456@qq.com",
    },
    {
        "key": "QQ_IMAP_PASSWORD", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "QQ 邮箱 IMAP 授权码", "help": "仅 cloudflare_domain：QQ IMAP 授权码，保存在 .env，不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "REMAIL_API_BASE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Remail API 地址", "help": "默认 https://remail.aishop6.com；也可填写文档地址 https://remail.aishop6.com/docs",
        "external_url": "https://remail.aishop6.com/register?aff=AFFLGYQMTYIXH",
        "external_label": "打开 Remail 官网",
    },
    {
        "key": "REMAIL_API_KEY", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Remail API Key", "help": "Remail 控制台生成的 rk- 开头 API Key；选择 remail 来源时必填，保存在 .env",
        "storage": "env", "secret": True,
    },
    {
        "key": "REMAIL_PROJECT_ID", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "Remail 项目 ID", "help": "Remail API 项目列表中的 projectId，用于匹配 ChatGPT/OpenAI 验证码项目",
    },
    {
        "key": "REMAIL_EMAIL_SUFFIX", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Remail 邮箱后缀", "help": "下单时使用的邮箱后缀，默认 outlook.com；不要填写完整邮箱",
    },
    {
        "key": "REMAIL_SERVICE_MODE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Remail 服务模式", "help": "code=短效接码；purchase=长效购买（可重复收件，默认）",
    },
    {
        "key": "REMAIL_SUPPLY_POLICY", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Remail 库存策略", "help": "private_first 优先自有库存；public_only 只使用公开库存（默认）",
    },
    {
        "key": "REMAIL_ORDER_WAIT_SECONDS", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "Remail 订单等待(秒)", "help": "下单后未立即返回 service token 时等待订单补齐凭证，默认 30 秒",
    },
    {
        "key": "REMAIL_REQUEST_TIMEOUT", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "Remail 请求超时(秒)", "help": "Remail API 单次 HTTP 请求超时，默认 20 秒",
    },
    # ---- 浏览器地区画像 ----
    {
        "key": "BROWSER_LOCALE_PROFILE", "file": "browser.py", "type": "str", "group": "浏览器画像",
        "label": "地区画像", "help": "应与代理出口地区一致；可选 jp/cn/us/sg。当前本地代理实测为日本东京，推荐 jp",
    },

    {
        "key": "AUTO_BROWSER_LOCALE_FROM_IP", "file": "browser.py", "type": "bool", "group": "浏览器画像",
        "label": "按出口IP自动画像", "help": "开启后每个 BrowserSession 会用当前代理出口 IP 自动选择语言/时区；失败时回退到地区画像",
    },
    {
        "key": "IP_GEO_TIMEOUT", "file": "browser.py", "type": "float", "group": "浏览器画像",
        "label": "IP定位超时(秒)", "help": "出口 IP 地理信息接口的单次请求超时；接口失败会自动回退，不影响注册",
    },

    # ---- 代理池 ----
    {
        "key": "PROXY_POOL", "file": "proxy.py", "type": "list_str_multiline", "group": "代理池",
        "label": "代理池(每行一个)", "help": "每行一个代理 URL，留空行会被忽略；为空则不使用代理",
    },
    {
        "key": "PLAN_CHECK_PROXY_MODE", "file": "proxy.py", "type": "str", "group": "代理池",
        "label": "套餐网络模式", "help": "auto=专用代理优先并允许回退；proxy=专用代理优先且强制代理；direct=强制直连",
    },
    {
        "key": "PLAN_CHECK_PROXY", "file": "proxy.py", "type": "str", "group": "代理池",
        "label": "套餐查询专用代理", "help": "用于查套餐；留空时 auto/proxy 从代理池选择。可能包含认证信息，仅保存到 .env",
        "storage": "env", "secret": True,
    },
    {
        "key": "PLAN_CHECK_TIMEOUT", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "套餐查询超时(秒)", "help": "查套餐的单次请求超时，建议 10-20 秒；独立于注册请求超时",
    },
    {
        "key": "PLAN_CHECK_MAX_ATTEMPTS", "file": "proxy.py", "type": "int", "group": "代理池",
        "label": "套餐查询最大尝试次数", "help": "查套餐遇到网络错误、429、5xx 等临时错误时的重试次数，建议 2 次",
    },
    {
        "key": "PLAN_CHECK_RETRY_DELAY", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "套餐查询重试间隔(秒)", "help": "查套餐的重试间隔，按尝试次数递增；服务端 Retry-After 优先",
    },
    {
        "key": "PLAN_CHECK_REGISTRATION_RECHECK_DELAY", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "新账号资格复查延迟(秒)", "help": "新注册 free 账号未发现试用资格或首次查询失败时复查一次；0 表示关闭",
    },
    {
        "key": "PLAN_CHECK_WORKERS", "file": "proxy.py", "type": "int", "group": "代理池",
        "label": "套餐查询并发数", "help": "自动、手动和批量查套餐共用；建议 2-4 个线程",
    },
    {
        "key": "PLAN_CHECK_QUEUE_LIMIT", "file": "proxy.py", "type": "int", "group": "代理池",
        "label": "套餐查询队列上限", "help": "防止异常批量操作无限堆积，建议 100-1000",
    },
    {
        "key": "PLAN_CHECK_MIN_INTERVAL", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "套餐查询请求最小间隔(秒)", "help": "限制查套餐的请求启动频率，降低 429 风险",
    },
    {
        "key": "PLAN_CHECK_JITTER", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "套餐查询请求随机抖动(秒)", "help": "在查套餐的最小间隔上增加随机延迟，避免请求过于规律",
    },
    # ---- Checkout Session ----
    {
        "key": "CHECKOUT_SESSION_AUTO_CHECK", "file": "checkout_session.py", "type": "bool", "group": "Checkout Session",
        "label": "自动查 Checkout 类型", "help": "开启后仅在 registration_auto 的最终套餐结果确认 free + plus-1-month-free + 100% 试用资格时自动入队；默认关闭",
    },
    {
        "key": "CHECKOUT_SESSION_PROXY_MODE", "file": "checkout_session.py", "type": "str", "group": "Checkout Session",
        "label": "Checkout网络模式", "help": "只允许 proxy 或 direct；proxy 必须填写下方专用代理，不会复用 PROXY_POOL，也不会自动回退直连",
    },
    {
        "key": "CHECKOUT_SESSION_PROXY", "file": "checkout_session.py", "type": "str", "group": "Checkout Session",
        "label": "Checkout专用代理", "help": "只用于初始 Checkout POST，可能包含认证信息，仅保存到 .env；proxy 模式为空时会拒绝发送",
        "storage": "env", "secret": True,
    },
    {
        "key": "CHECKOUT_SESSION_BILLING_COUNTRY", "file": "checkout_session.py", "type": "str", "group": "Checkout Session",
        "label": "Checkout账单国家", "help": "必须填写两位大写 ASCII 国家代码，例如 DE；需与代理出口地区策略一致",
    },
    {
        "key": "CHECKOUT_SESSION_BILLING_CURRENCY", "file": "checkout_session.py", "type": "str", "group": "Checkout Session",
        "label": "Checkout账单货币", "help": "必须填写三位大写 ASCII 货币代码，例如 EUR",
    },
    {
        "key": "CHECKOUT_SESSION_TIMEOUT", "file": "checkout_session.py", "type": "float", "group": "Checkout Session",
        "label": "Checkout单次超时(秒)", "help": "初始 Checkout POST 的单次超时，范围 1-60 秒",
    },
    {
        "key": "CHECKOUT_SESSION_MAX_ATTEMPTS", "file": "checkout_session.py", "type": "int", "group": "Checkout Session",
        "label": "Checkout最大尝试次数", "help": "仅网络错误、408/409/425/5xx 或 2xx 解析失败可重试，范围 1-4",
    },
    {
        "key": "CHECKOUT_SESSION_RETRY_DELAY", "file": "checkout_session.py", "type": "float", "group": "Checkout Session",
        "label": "Checkout重试间隔(秒)", "help": "Retry-After 合法时优先使用，否则按尝试次数递增；范围 0-30 秒",
    },
    {
        "key": "CHECKOUT_SESSION_WORKERS", "file": "checkout_session.py", "type": "int", "group": "Checkout Session",
        "label": "Checkout并发数", "help": "独立后台线程数，默认 1；修改后需重启 WebUI 才会重建线程池",
    },
    {
        "key": "CHECKOUT_SESSION_QUEUE_LIMIT", "file": "checkout_session.py", "type": "int", "group": "Checkout Session",
        "label": "Checkout队列上限", "help": "独立后台队列容量，默认 100；修改后需重启 WebUI 才会重建队列",
    },
    {
        "key": "CHECKOUT_SESSION_MIN_INTERVAL", "file": "checkout_session.py", "type": "float", "group": "Checkout Session",
        "label": "Checkout请求最小间隔(秒)", "help": "限制独立队列启动请求的频率，范围 0-30 秒",
    },
    {
        "key": "CHECKOUT_SESSION_JITTER", "file": "checkout_session.py", "type": "float", "group": "Checkout Session",
        "label": "Checkout请求随机抖动(秒)", "help": "在最小间隔上增加随机等待，范围 0-30 秒",
    },
    # ---- 提链 ----
    {
        "key": "EXTRACT_LINK_API_BASE", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "提链服务地址", "help": "填写提链服务 API 地址",
    },
    {
        "key": "EXTRACT_LINK_CDK", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "提链 CDK", "help": "创建提链任务和监听任务事件使用；成功提链扣 1 次",
        "storage": "env", "secret": True,
    },
    {
        "key": "EXTRACT_LINK_TYPE", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "提链类型", "help": "支持 pix / upi / kakao_pay / ideal",
    },
    {
        "key": "EXTRACT_LINK_PROVIDER", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "提链服务提供方", "help": "legacy=现有 SSE 服务；masi=Masi Kakao Job API",
    },
    {
        "key": "EXTRACT_LINK_UPDATE_MODE", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "结果更新方式", "help": "sse=监听服务端事件；poll=由后端主动轮询远端 Job",
    },
    {
        "key": "EXTRACT_LINK_PROXY", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "提链代理", "help": "legacy/Masi 共用；例如 http://127.0.0.1:7816，留空不显式指定代理",
        "storage": "env", "secret": True,
    },
    {
        "key": "MASI_KAKAO_API_BASE", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "Masi API 地址", "help": "Masi Kakao Job API Base URL，默认 https://masi.cc.cd",
    },
    {
        "key": "EXTRACT_LINK_WORKERS", "file": "extract_link.py", "type": "int", "group": "提链",
        "label": "提链并发数", "help": "批量提链后台线程数，建议 1-4；修改后需重启 WebUI 才会重建线程池",
    },
    {
        "key": "EXTRACT_LINK_POLL_INTERVAL", "file": "extract_link.py", "type": "float", "group": "提链",
        "label": "Job轮询间隔(秒)", "help": "poll 模式下后端查询远端 Job 的间隔，建议 2-5 秒",
    },
    {
        "key": "EXTRACT_LINK_POLL_MAX_ERRORS", "file": "extract_link.py", "type": "int", "group": "提链",
        "label": "Job连续错误上限", "help": "poll 模式下网络错误、429、5xx 的连续容忍次数",
    },
    {
        "key": "MASI_CDK_QUERY_MAX_ATTEMPTS", "file": "extract_link.py", "type": "int", "group": "提链",
        "label": "CDK查询尝试次数", "help": "Masi CDK 额度查询失败时的最大尝试次数",
    },
    {
        "key": "MASI_CDK_QUERY_RETRY_DELAY", "file": "extract_link.py", "type": "float", "group": "提链",
        "label": "CDK查询重试间隔(秒)", "help": "Masi CDK 额度查询失败后的重试间隔",
    },
    {
        "key": "MASI_CDK_SELECTION_TIMEOUT", "file": "extract_link.py", "type": "int", "group": "提链",
        "label": "CDK选择超时(秒)", "help": "全部 CDK 被占用时等待出现可用次数的最长时间",
    },
    {
        "key": "MASI_CDK_REFRESH_WORKERS", "file": "extract_link.py", "type": "int", "group": "提链",
        "label": "CDK刷新并发数", "help": "批量刷新 Masi CDK 额度时的并发数，建议 2-6",
    },
    # ---- Codex 配置 ----
    {
        "key": "SUB2API_API_BASE", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "sub2 OAuth API基址", "help": "Codex OAuth 使用的 sub2api 服务地址，例如 http://127.0.0.1:8080",
    },
    {
        "key": "SUB2API_API_KEY", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "sub2 API Key", "help": "sub2api 管理接口 API Key；请求头使用 x-api-key；为空则不带鉴权头", "storage": "env", "secret": True,
    },
    {
        "key": "SUB2API_API_TIMEOUT", "file": "sub2api.py", "type": "int", "group": "Codex",
        "label": "sub2 超时", "help": "sub2api 请求超时秒数",
    },
    # ---- 接码平台 ----
    # ---- Codex：基础 / CPA / sub2api 配置 ----
    {
        "key": "CODEX_AUTH_URL_SOURCE", "file": "codex.py", "type": "str", "group": "Codex",
        "label": "授权地址来源", "help": "cpa=CPA生成并上传CPA；sub2=sub2生成并上传sub2；local=本地PKCE",
    },
    {
        "key": "CPA_MANAGEMENT_URL", "file": "codex.py", "type": "str", "group": "Codex",
        "label": "CPA 管理地址", "help": "例如 http://localhost:8317/admin/oauth；程序会取 origin 调用 /v0/management/*",
    },
    {
        "key": "CPA_MANAGEMENT_KEY", "file": "codex.py", "type": "str", "group": "Codex",
        "label": "管理密钥", "help": "保存在 .env（CPA_MANAGEMENT_KEY），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "CPA_REQUEST_TIMEOUT", "file": "codex.py", "type": "int", "group": "Codex",
        "label": "CPA 超时(秒)", "help": "请求 CPA 管理接口的超时时间",
    },
    {
        "key": "CPA_SAVE_CALLBACK_RECEIPT", "file": "codex.py", "type": "bool", "group": "Codex",
        "label": "保存CPA回执", "help": "CPA 未返回完整授权文件时，本地仍保存一份回调提交记录",
    },

    {
        "key": "SMS_PROVIDER", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "接码通道", "help": "grizzly / l / h；l 使用 L_API.md，h 使用 H_API.md 定义的本地取号服务",
    },
    {
        "key": "SMS_COUNTRY", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "国家代码", "help": "传给接码平台的 country；GrizzlySMS 常用：美国=187；H 通道作为 H_API.md 的 country",
    },
    {
        "key": "SMS_SERVICE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "服务/项目代码", "help": "GrizzlySMS/L 作为 service；H 通道作为 H_API.md 的 projectId",
    },
    {
        "key": "SMS_MAX_RETRIES", "file": "codex.py", "type": "int", "group": "接码平台",
        "label": "换号重试次数", "help": "一个号收不到短信/被OpenAI拒时换下一个号，最多重试几次",
    },
    {
        "key": "SMS_CODE_WAIT", "file": "codex.py", "type": "int", "group": "接码平台",
        "label": "单号等短信(秒)", "help": "单个号等待短信到达的最长秒数，超时则换号",
    },
    {
        "key": "SMS_API_KEY", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "GrizzlySMS API密钥", "help": "GrizzlySMS 平台 API Key，保存在 .env（SMS_API_KEY），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "SMSBOWER_API_BASE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "SMSBower API地址", "help": "默认使用官方 V1 handler API；与 GrizzlySMS 配置完全独立",
    },
    {
        "key": "SMSBOWER_API_KEY", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "SMSBower API密钥", "help": "保存在 .env；配置读取和日志不会返回明文",
        "storage": "env", "secret": True,
    },
    {
        "key": "SMSBOWER_SERVICE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "SMSBower服务代码", "help": "必须通过 SMSBower getServicesList 确认，不能复用其他平台代码",
    },
    {
        "key": "SMSBOWER_COUNTRY", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "SMSBower国家ID", "help": "必须通过 SMSBower getCountries 确认",
    },
    {
        "key": "SMSBOWER_MIN_PRICE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "SMSBower最低价格", "help": "可留空；使用十进制字符串，不能大于最高价格",
    },
    {
        "key": "SMSBOWER_MAX_PRICE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "SMSBower最高价格", "help": "可留空；系统不会自动提高或移除价格限制",
    },
    {
        "key": "H_API_BASE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "H API 地址", "help": "H 取号服务基础地址，例如 http://localhost:8788",
    },
    {
        "key": "H_ADMIN_AUTH_CODE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "H 授权码", "help": "保存在 .env（H_ADMIN_AUTH_CODE），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "H_PHONE_PREFIX", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "H 号码前缀", "help": "H 返回号码不含国家码时填写，例如美国 10 位本地号填 1；留空则不补",
    },
    {
        "key": "H_PHONE_ACQUIRE_MODE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "H 取号方式", "help": "reusable=优先复用历史可用号码；new=每次都取一个新号码",
    },
    {
        "key": "L_API_BASE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "L API 地址", "help": "L 取号服务基础地址，例如 http://localhost:8788",
    },
    {
        "key": "L_ADMIN_AUTH_CODE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "L 授权码", "help": "保存在 .env（L_ADMIN_AUTH_CODE），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "L_PHONE_PREFIX", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "L 号码前缀", "help": "L 返回号码不含国家码时填写，例如美国 10 位本地号填 1；留空则不补",
    },
]

_FIELD_BY_KEY = {f["key"]: f for f in EDITABLE_FIELDS}


def email_source_options() -> list[dict[str, str]]:
    """返回供 API 输出使用的邮箱来源选项，避免调用方修改字段常量。"""
    return [dict(item) for item in EMAIL_SOURCE_OPTIONS]


def normalize_email_source_value(value) -> str:
    """严格校验并规范化 WebUI 提交的 EMAIL_SOURCE。"""
    if isinstance(value, str):
        raw_items = re.split(r"[,;|]", value)
    elif isinstance(value, (list, tuple)):
        raw_items = list(value)
    else:
        raise ValueError("EMAIL_SOURCE 必须是邮箱来源字符串或列表")

    sources: list[str] = []
    empty_items = False
    for item in raw_items:
        source = str(item or "").strip().strip("\"'")
        if not source:
            empty_items = True
            continue
        sources.append(source)

    if not sources:
        raise ValueError("EMAIL_SOURCE 至少需要选择一个邮箱来源")
    if empty_items:
        raise ValueError("EMAIL_SOURCE 包含空邮箱来源")

    unknown = [source for source in sources if source not in VALID_EMAIL_SOURCES]
    if unknown:
        raise ValueError(
            "EMAIL_SOURCE 包含未知邮箱来源: " + ", ".join(unknown)
            + "；可选 " + ", ".join(VALID_EMAIL_SOURCES)
        )

    seen: set[str] = set()
    duplicates: list[str] = []
    for source in sources:
        if source in seen and source not in duplicates:
            duplicates.append(source)
        seen.add(source)
    if duplicates:
        raise ValueError("EMAIL_SOURCE 包含重复邮箱来源: " + ", ".join(duplicates))

    return ",".join(sources)


# ============================================================
# 读：解析源码取当前值（不 import，避免缓存/副作用）
# ============================================================

def _config_path(filename: str) -> Path:
    path = (_CONFIG_DIR / filename).resolve()
    # 防目录穿越：必须落在 config/ 下
    if _CONFIG_DIR not in path.parents:
        raise ValueError(f"非法配置路径: {filename}")
    return path


def _literal_default_from_expr(node):
    """尽量从赋值表达式中取“源码默认值”，不执行模块代码。

    兼容：
      KEY = "literal"
      KEY: str = env_str("KEY", "default")
      KEY = env_bool("KEY", True)
      KEY = env_value("KEY", 123, "int")
    """
    try:
        return ast.literal_eval(node)
    except Exception:
        pass

    if isinstance(node, ast.Call):
        func_name = ""
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            func_name = node.func.attr

        # env_str/env_bool/env_int/env_float/env_list 的第二个位置参数是默认值。
        if func_name in {"env_str", "env_bool", "env_int", "env_float", "env_list"}:
            if len(node.args) >= 2:
                try:
                    return ast.literal_eval(node.args[1])
                except Exception:
                    return None
            return None

        # env_value(key, default, vtype)
        if func_name == "env_value" and len(node.args) >= 2:
            try:
                return ast.literal_eval(node.args[1])
            except Exception:
                return None

    return None


def _find_assignment_value_node(source: str, key: str):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        for t in targets:
            if isinstance(t, ast.Name) and t.id == key:
                return node.value
    return None


def _parse_value_from_source(source: str, key: str, vtype: str):
    """从源码里解析 KEY 的当前值。失败返回 None。"""
    if vtype == "list_str_multiline":
        # 用 AST 解析整个模块，取这个赋值的 list 字面量
        value_node = _find_assignment_value_node(source, key)
        if value_node is None:
            return None
        try:
            val = ast.literal_eval(value_node)
            if isinstance(val, (list, tuple)):
                return [str(x) for x in val]
        except (ValueError, SyntaxError):
            return None
        return None

    # 标量：优先 AST 取默认值，避免 env_str("KEY", "") 被当成普通字符串。
    value_node = _find_assignment_value_node(source, key)
    if value_node is not None:
        value = _literal_default_from_expr(value_node)
        if value is not None:
            return value

    # AST 失败时再回退到旧的正则解析。
    m = re.search(
        rf"^{re.escape(key)}\s*(?::[^=\n]+)?=\s*(.+?)\s*(?:#.*)?$",
        source, re.MULTILINE,
    )
    if not m:
        return None
    raw = m.group(1).strip()
    try:
        return ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return raw


def _parse_env_typed_value(raw: str, fallback, vtype: str):
    """把 .env 字符串按字段类型转换；失败时回退 fallback。"""
    from config.env_loader import env_value
    return env_value("__NO_SUCH_ENV_KEY__", fallback, vtype) if raw is None else _coerce_raw_value(raw, fallback, vtype)


def _coerce_raw_value(raw: str, fallback, vtype: str):
    try:
        if raw is None or str(raw).strip() == "":
            return fallback
        if vtype == "bool":
            return str(raw).strip().lower() in ("true", "1", "yes", "on", "y")
        if vtype == "int":
            return int(str(raw).strip())
        if vtype == "float":
            return float(str(raw).strip())
        if vtype == "list_str_multiline":
            text = str(raw)
            try:
                val = ast.literal_eval(text)
                if isinstance(val, (list, tuple)):
                    return [str(x).strip() for x in val if str(x).strip()]
            except Exception:
                pass
            return [line.strip() for line in text.splitlines() if line.strip()]
        return str(raw)
    except Exception:
        return fallback


def get_config() -> list[dict]:
    """返回所有可编辑项的当前值 + 元信息，供前端渲染表单。

    优先读取 `.env` / 环境变量；没有配置时回退到 `config/*.py` 默认值。
    """
    from config.env_loader import load_env, read_env_file
    load_env(override=True)
    env_file_values = read_env_file()

    out = []
    for field in EDITABLE_FIELDS:
        key = field["key"]
        path = _config_path(field["file"])
        source = path.read_text(encoding="utf-8") if path.exists() else ""
        fallback = _parse_value_from_source(source, key, field["type"])

        if key in env_file_values:
            raw_env_value = env_file_values[key]
            if field["type"] == "list_str_multiline" and key in EXPLICIT_EMPTY_LIST_KEYS and str(raw_env_value).strip() == "":
                value = []
            else:
                value = _coerce_raw_value(raw_env_value, fallback, field["type"])
        elif os.getenv(key) is not None:
            value = _coerce_raw_value(os.getenv(key, ""), fallback, field["type"])
        else:
            value = fallback

        if field["type"] in ("str", "list_str_multiline"):
            value = _normalize_config_value(value, field["type"])
        item = dict(field)
        item["storage"] = "env"
        if key == "EMAIL_SOURCE":
            item["options"] = email_source_options()
        if field.get("secret") or key == "EXTRACT_LINK_PROXY":
            item["configured"] = bool(value)
            item["value"] = ""
        else:
            item["value"] = value
        out.append(item)
    return out


# ============================================================
# 写：统一写 .env，不修改 config/*.py
# ============================================================


_PLACEHOLDER_EMPTY = {
    "", "-", "—", "无", "空", "none", "null", "n/a", "na", "未设置", "未配置",
}


def _normalize_config_value(value, vtype: str):
    """把前端/历史占位空值规范化，避免 '-' 被当成真实配置。"""
    if vtype == "str":
        s = "" if value is None else str(value).strip()
        if s.lower() in {x.lower() for x in _PLACEHOLDER_EMPTY}:
            return ""
        return s
    if vtype == "list_str_multiline":
        if value is None:
            return []
        if isinstance(value, str):
            lines = value.splitlines()
        elif isinstance(value, (list, tuple)):
            lines = list(value)
        else:
            lines = [str(value)]
        out = []
        for item in lines:
            s = str(item or "").strip()
            if not s or s.lower() in {x.lower() for x in _PLACEHOLDER_EMPTY}:
                continue
            out.append(s)
        return out
    return value


def _format_literal(value, vtype: str) -> str:
    """把前端传来的值格式化成 Python 字面量字符串。"""
    if vtype == "bool":
        if isinstance(value, str):
            value = value.strip().lower() in ("true", "1", "yes", "on")
        return "True" if value else "False"
    if vtype == "int":
        return str(int(value))
    if vtype == "float":
        return repr(float(value))
    if vtype == "str":
        s = str(value)
        # 用 repr 保证转义安全，但统一成双引号风格
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    raise ValueError(f"_format_literal 不支持的类型: {vtype}")


def _replace_scalar(source: str, key: str, literal: str) -> str:
    """替换 `KEY[: 类型] = 旧值` 行的右值，保留行内注释和类型标注。"""
    pattern = re.compile(
        rf"^(?P<head>{re.escape(key)}\s*(?::[^=\n]+)?=\s*)"
        rf"(?P<val>.+?)"
        rf"(?P<tail>\s*(?:#.*)?)$",
        re.MULTILINE,
    )
    if not pattern.search(source):
        raise ValueError(f"未在源码中找到可替换的赋值: {key}")
    return pattern.sub(lambda m: f"{m.group('head')}{literal}{m.group('tail')}", source, count=1)


def _replace_proxy_pool(source: str, lines: list[str]) -> str:
    """整块替换 PROXY_POOL = [ ... ] 列表字面量（保留前面的赋值头）。"""
    items = [ln.strip() for ln in lines if ln.strip()]
    if items:
        body = "\n".join(
            '    "' + it.replace("\\", "\\\\").replace('"', '\\"') + '",'
            for it in items
        )
        literal = "[\n" + body + "\n]"
    else:
        literal = "[]"

    # 匹配 PROXY_POOL = [ ... ]（含跨行），用 AST 定位起止偏移最稳
    tree = ast.parse(source)
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        for t in targets:
            if isinstance(t, ast.Name) and t.id == "PROXY_POOL":
                src_lines = source.splitlines(keepends=True)
                start = node.value.lineno          # 值（[）所在行，1-based
                end = node.value.end_lineno        # 值（]）所在行，1-based
                col = node.value.col_offset         # [ 在起始行的列偏移
                # 保留起始行 [ 之前的内容（即 "PROXY_POOL = " 或 "PROXY_POOL: list = "）
                prefix = src_lines[start - 1][:col]
                # 保留结束行 ] 之后的内容（行内注释 / 换行）
                end_line = src_lines[end - 1]
                suffix = end_line[node.value.end_col_offset:]
                new_lines = (
                    src_lines[: start - 1]
                    + [prefix + literal + suffix]
                    + src_lines[end:]
                )
                return "".join(new_lines)
    raise ValueError("未找到 PROXY_POOL 赋值")


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _format_env_value(value, vtype: str) -> str:
    """把前端值格式化成适合写入 .env 的字符串。"""
    if vtype == "bool":
        if isinstance(value, str):
            value = value.strip().lower() in ("true", "1", "yes", "on", "y")
        return "True" if value else "False"
    if vtype == "int":
        return str(int(value))
    if vtype == "float":
        return repr(float(value))
    if vtype == "list_str_multiline":
        lines = _normalize_config_value(value, vtype)
        return "\n".join(lines) if lines else "[]"
    if vtype == "str":
        return _normalize_config_value(value, vtype)
    return "" if value is None else str(value)


def update_config(updates: dict) -> dict:
    """批量更新配置。所有 WebUI 可编辑项只写项目根 `.env`。"""
    from config.env_loader import write_env_values, load_env, read_env_file

    updated, ignored, preserved = [], [], []
    env_updates: dict[str, str] = {}
    existing_env = read_env_file()

    for key, value in updates.items():
        field = _FIELD_BY_KEY.get(key)
        if field is None:
            ignored.append(key)
            continue
        if key == "EMAIL_SOURCE":
            formatted = normalize_email_source_value(value)
        else:
            formatted = _format_env_value(value, field["type"])
        if field.get("secret") and not formatted and str(existing_env.get(key) or "").strip():
            preserved.append(key)
            continue
        env_updates[key] = formatted
        updated.append(key)


    env_updated = write_env_values(env_updates) if env_updates else []
    if env_updated:
        load_env(override=True)

    return {"updated": updated, "ignored": ignored, "preserved": preserved, "env_updated": env_updated}
