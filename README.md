# Turb GPT Free Register

ChatGPT / OpenAI 账号自动注册与 Codex OAuth 授权工具。注册使用 `wreq/chrome152` 与真实 Sentinel SDK 的纯协议链路；独立任务保留各自 HTTP 后端配置。

项目提供 **CLI** 和 **本地 WebUI** 两种使用方式。日常推荐使用 WebUI。

> 项目说明：本项目基于 [xiaoguzuiniu/gpt-free-register](https://github.com/xiaoguzuiniu/gpt-free-register) 进行改造与扩展。

- TG 交流群：[https://t.me/+gu_cvEKq_vcyZWRl](https://t.me/+gu_cvEKq_vcyZWRl)

> 开源版说明：仓库只保留源码、配置模板和文档；运行时账号、Token、邮箱池、Codex 凭证、日志等真实数据均已通过 `.gitignore` 排除。

---

## 功能概览

### 注册

- 批量注册 ChatGPT 账号。

### 邮箱来源

支持多种邮箱来源：

- Outlook 邮箱池：`email----password----clientId----refreshToken`
- Cloudflare 域名邮箱 + QQ 邮箱 IMAP 收信（`cloudflare_domain`）
- Cloudflare Worker 临时邮箱：自动创建 + JWT 取码（`cloudflare`，兼容 cloudflare_temp_email）
- 通用 API 邮箱：`email----取码地址`
- Remail 开放 API：按项目下单短效邮箱并自动收取验证码（`remail`）
- `EMAIL_SOURCE` 支持多个来源组合，例如：

```python
EMAIL_SOURCE = "outlook,generic_api"
```

### Codex OAuth

- 注册成功后可自动跑 Codex OAuth。
- 支持 CPA 管理接口生成授权 URL，并提交 OAuth callback。
- 支持接码平台：
  - GrizzlySMS
  - SMSBower V1 handler API
  - 本地 L 取号服务，见 `L_API.md`
- 手机验证支持自动取号、填号、收码、提交、失败换号重试。
- Codex 凭证落盘到 `codex_accounts/`。

### WebUI

- 批量启动注册任务。
- 实时查看任务日志。
- 动态调整注册线程数，提交后新任务立即使用最新值。
- 批量补跑 Codex，补跑线程数每次提交即时生效。
- 管理账号、邮箱池、Codex 凭证；账号页支持复制全部/选中整行，邮箱池列表展示导入时间、已用时间和状态。
- 配置页支持热加载，保存后无需重启。

### 提链路由与 Masi CDK 池

提链由三个独立维度决定：

- `EXTRACT_LINK_TYPE`：要提取的链类型，例如 `pix`、`upi`、`kakao_pay`、`ideal`。
- `EXTRACT_LINK_PROVIDER`：提链服务提供方。目前支持 `legacy` 和 `masi`。
- `EXTRACT_LINK_UPDATE_MODE`：结果更新方式，支持 `sse` 和 `poll`。

所有 provider 共用可选的 `EXTRACT_LINK_PROXY`。例如本机 Clash HTTP 代理：

```dotenv
EXTRACT_LINK_PROXY=http://127.0.0.1:7816
```

该配置同时作用于 legacy 的额度查询、任务创建和 SSE 连接，以及 Masi 的额度查询、Job 创建、Job 查询和取消请求。支持 `http://`、`https://`、`socks5://`、`socks5h://`；留空时应用不显式指定代理。Windows“系统代理”不一定会被底层 HTTP 库自动读取，需要稳定走代理时应填写此字段。

当前合法组合如下：

| Provider | Link type | Update mode | 凭据来源 |
| --- | --- | --- | --- |
| `legacy` | `pix` / `upi` / `kakao_pay` / `ideal` | `sse` | `.env` 中的单个 `EXTRACT_LINK_CDK` |
| `masi` | `kakao_pay` | `poll` | WebUI「账号 → CDK池」维护的 Masi CDK 池 |

未配置新字段时仍使用 `legacy + sse`，兼容原有部署。前端只轮询本地账号状态；`poll` 模式由 WebUI 后端查询远端 Job，浏览器不会直接请求 Masi。

Masi CDK 使用「可选池」和「已用完池」两个业务池。可在 WebUI 中一行一个批量导入，并查看 `total_uses`、`remaining_uses`、`pending_uses`、`available_uses`。导入面板的“导入后立即查询额度”默认不勾选，因此大批量导入只执行本地保存和去重；需要立即取得额度时可手动勾选，或导入后使用“刷新当前池/刷新全部”。

- 只有 `remaining_uses == 0` 才移入已用完池。
- `available_uses > 0` 才能创建新 Job。
- `available_uses == 0` 但仍有剩余次数时，CDK 放回可选池队尾等待复查。
- 网络、TLS、HTTP 或响应解析失败只按配置重试并记录 `last_error`，不改变业务池归属。
- 已用完池中的 CDK 再次查询到 `remaining_uses > 0` 时会移回可选池。
- Masi Job 明确返回 `failed` 时会重新提交新 Job，包含首次提交在内最多尝试 3 次；轮询断连只重试查询原 Job，不会创建重复 Job。

同一 CDK 从额度确认到 Job 创建响应期间由进程内租约保护，任务创建后会持久化 CDK ID 和脱敏指纹，后续查询与取消继续使用同一个 CDK。完整 CDK、账号 AT、带认证信息的代理 URL 和付款链接不会写入普通日志；付款链接只保存并展示，不会被后端主动访问或验证。`EXTRACT_LINK_PROXY` 作为 secret 保存在 `.env`，配置读取接口不会返回其完整值。

`EXTRACT_LINK_POLL_INTERVAL`、`EXTRACT_LINK_POLL_MAX_ERRORS`、`MASI_CDK_QUERY_MAX_ATTEMPTS`、`MASI_CDK_QUERY_RETRY_DELAY` 和 `MASI_CDK_SELECTION_TIMEOUT` 可在配置页调整。大多数配置会热加载，但 `EXTRACT_LINK_WORKERS` 在进程启动时创建线程池，修改后必须重启 WebUI。

---

## 环境要求

- Python 3.11+（`wreq==0.13.0` 要求；本轮实测 Python 3.12.15）
- Node.js 18+，需具备 `node:vm`、WebCrypto 和可用的 Intl/IANA 时区数据；本轮实测 v22.19.0
- 可用代理、系统代理/VPN
- 如启用 Codex 自动授权：需要接码平台配置

安装依赖：

```bash
pip install -r requirements.txt
node --version
```

注册入口固定使用已声明的 `wreq==0.13.0` 与 `chrome152`，HTTP UA／Client Hints 和 SDK navigator／userAgentData 同步为 152，平台仍为既有 macOS。缺依赖或不支持该档位时在认证前失败，不自动安装或退回 curl_cffi。共享 `PROTOCOL_HTTP_CLIENT`、`CURL_CFFI_IMPERSONATE`、`WREQ_EMULATION` 继续控制独立查活、Codex 和补设 2FA，不覆盖注册入口；注册同步 2FA 沿用成功注册会话及既有重认证流程。

### 注册地域与 SDK 运行要求

- 注册初始化经选定路由的当前 Cloudflare trace 确认有效 IP 与受支持国家，不读取历史地域缓存、不切换直连、不要求多个 GeoIP 服务 IP 或外部时区一致。trace 在首次 warmup 前执行一次，设备重试不重复；重建或轮换后无新 trace 时，原国家只代表初始／最近旧传输观测，不保证当前出口固定。初始探测失败、未知国家或无效 IP 均停止；手动画像也不能救活未知出口国家。
- 默认自动使用实际国家的完整语言／IANA 时区映射。关闭 `AUTO_BROWSER_LOCALE_FROM_IP` 后沿用 `BROWSER_LOCALE_PROFILE` 显式选择画像；原生调用也可通过显式 profile 的国家／locale 选择覆盖另一已知出口国家。实际国家与 `profile_country`、`locale_source` 分开记录；所选画像自身不完整或语言／时区不相容仍拒绝。AU／CA 使用本国语言与时区，不复用 GB／US 模板。
- 同国 IP 轮换、有效初始化后的额外预检不可达或另一已知国家仅诊断，保留任务内冻结画像；后续成功响应明确为未知国家仍停止。有效服务端 `oai-did` 是独立前提，CSRF 200 或 403 但没有有效设备 cookie 都不能继续认证。
- 设备 warmup 采用限定的 navigation 转 XHR 头形态（JSON、empty／cors／same-origin、priority u=0），不额外发送 Referer／Content-Type 或导航标志，不改变其它认证请求的头。失败尝试沿用有界重试、空 CookieJar 重建和池候选策略；成功会话直接继续注册，不新增独立探针或假邮箱 POST。设备仍须域／根路径适用、非空、未过期且无歧义；wreq 的 host-only cookie 来源从真实响应及重定向恢复，不放宽门禁。
- `proxy` 表示已有代理连接／认证失败证据；`geo_service`、`geo_parse`、`geo_incomplete`、`geo_mismatch` 分别表示服务不可用、解析失败、信息不足与所选画像自身冲突，不用于合法跨国覆盖或同国轮换。独立查活、Codex 与补设 2FA 保持原策略。
- Python 时区数据由依赖 `tzdata` 提供，Windows 同样需要安装。SDK Date/Intl 使用冻结的 IANA 时区并按实际日期计算夏令时，不用宿主时区或当天静态长名冒充其它日期。
- 保留 `sentinel/sdk.js` 与 `sentinel/sentinel-runner.js`。每个单 flow 使用一个短生命周期 Node 宿主完成同 VM 的 requirements/solve，HTTP challenge 仍由 Python 当前会话发起；可通过 `NODE_EXECUTABLE` 指定 Node 程序。OSS 的 QuickJS 文件名不表示本项目需要另装 QuickJS 引擎。
- SDK 消息通过内存中的 stdin/stdout 交互，原始输出、challenge、指纹和 token 不进入诊断日志或临时 JSON；失败不回退手工 requirements 或旧 runner。主 token 与当前上下文绑定，PoW/SO 要求读取本次 challenge。
- 测试 WebUI 使用 5100，生产保持 5000。本轮真实初始化、SDK challenge 和一条新号密码／OTP／资料／最终会话／同步 2FA 已通过；受控失败边界与历史出口故障另列，不承诺所有路由成功。实际通过范围见 `docs/protocol-registration-flow-diff.md` 4.5.5。


### 密钥配置（.env）

重要 API Key 请放在项目根目录 `.env`，不要写进 `config/*.py`。

```bash
cp .env.example .env
```

当前支持从 `.env` 读取的密钥：

- `WEBUI_AUTH_CODE`（WebUI 登录授权码）
- `WEBUI_SESSION_SECRET`（可选，Session Cookie 签名密钥）
- `QQ_IMAP_PASSWORD`
- `CLOUDFLARE_API_KEY` / `CLOUDFLARE_CUSTOM_AUTH`（`EMAIL_SOURCE=cloudflare` 时）
- `REMAIL_API_KEY`（`EMAIL_SOURCE=remail` 时）
- `CPA_MANAGEMENT_KEY`
- `SMS_API_KEY`
- `L_ADMIN_AUTH_CODE`
- `H_ADMIN_AUTH_CODE`

WebUI 配置页保存这些字段时会写入 `.env`（不是 config 源码）。

---

## 快速开始

### WebUI 授权码

WebUI 启动后，除 `/login` 外所有页面和 `/api/*` 接口都会校验授权码。推荐在 `.env` 中配置：

```dotenv
WEBUI_AUTH_CODE=你的授权码
```

也可以启动时直接传入：

```bash
python web.py --auth-code 你的授权码
```

优先级：`--auth-code` > `.env`/环境变量。若都未设置，启动时会在日志中生成并打印本次临时授权码。接口调用可使用登录后的 Cookie，或传 `X-Auth-Code: <授权码>` / `Authorization: Bearer <授权码>`。

`WEBUI_SESSION_SECRET` 可选；未设置时会从固定授权码派生稳定的 Session 签名密钥，修改授权码后已有登录会自动失效。

### 1. 配置邮箱源

#### Outlook 邮箱池

复制示例文件：

```bash
cp 用于注册的邮箱.txt.example 用于注册的邮箱.txt
```

每行格式：

```text
email----password----clientId----refreshToken
```

也可以在 WebUI 的「邮箱池」页面导入。

#### 通用 API 邮箱

每行格式：

```text
email----code_url
```

在 `config/email.py` 设置：

```python
EMAIL_SOURCE = "generic_api"
```

或使用组合来源：

```python
EMAIL_SOURCE = "outlook,generic_api,remail"
```

#### Cloudflare Worker 临时邮箱（`cloudflare`）

兼容 `cloudflare_temp_email` 类 Worker：注册时自动创建域名邮箱，并用 JWT 轮询收件箱提取 OpenAI 六位验证码。  
（与下方 `cloudflare_domain` / QQ IMAP 方案不同，请勿混用标识。）

```dotenv
EMAIL_SOURCE=cloudflare
CLOUDFLARE_API_BASE=https://你的-worker-api-域名
CLOUDFLARE_API_KEY=你的_ADMIN_PASSWORD
CLOUDFLARE_AUTH_MODE=x-admin-auth
# admin 创建时常用：
# CLOUDFLARE_PATH_ACCOUNTS=/admin/new_address
CLOUDFLARE_DEFAULT_DOMAINS=你的收信域名.com

# 可选：创建 <local>@<随机小写字母>-<固定后缀>.<默认域名>
CLOUDFLARE_RANDOM_SUBDOMAIN_ENABLED=false
CLOUDFLARE_RANDOM_SUBDOMAIN_LENGTH=6
CLOUDFLARE_RANDOM_SUBDOMAIN_SUFFIX=mail
```

匿名模式可将 `CLOUDFLARE_AUTH_MODE=none` 且 Key 留空，创建路径默认 `/api/new_address`；若被 Turnstile 拦截请改用 admin 模式。更多字段见 WebUI「配置 → 邮箱 / OTP」或 `.env.example`。

`CLOUDFLARE_DEFAULT_DOMAINS` 是本项目创建邮箱时使用的基础域名列表。多个域名按顺序轮询选择；本项目不会通过 `CLOUDFLARE_PATH_DOMAINS`、`/open_api/settings` 或其他远端接口自动发现 Worker 支持的域名。

随机子域名默认关闭。开启后，程序为每次创建独立生成 `1-32` 位纯小写字母，并与固定后缀组成一个 DNS 标签。例如基础域名为 `example.com`、长度为 `6`、后缀为 `mail`，可能创建：

```text
xxxxxxxxxx@kqmfax-mail.example.com
```

开启前必须同时完成以下 Worker 和收信配置：

- `cloudflare_temp_email` Worker 必须开启 `ENABLE_CREATE_ADDRESS_SUBDOMAIN_MATCH`，允许创建接口将 `kqmfax-mail.example.com` 匹配到基础域名 `example.com`。
- 基础域名必须配置能够覆盖任意随机子域名的通配 MX 或等效 Email Routing。父域的 Email Routing 不会自动覆盖随机子域名。

缺少 Worker 开关时，创建接口会拒绝子域名；缺少通配 MX/路由时，地址可能创建成功但无法收到验证码邮件。客户端不会在失败后静默降级为基础域名邮箱。

#### Cloudflare 域名邮箱（`cloudflare_domain`）

在 `config/email.py` 设置：

```python
EMAIL_SOURCE = "cloudflare_domain"
EMAIL_DOMAIN = "你的域名"
QQ_EMAIL = "你的QQ邮箱"
QQ_IMAP_PASSWORD = "QQ邮箱IMAP授权码"
```

Cloudflare Email Routing 需要把域名邮件转发到 QQ 邮箱。此模式不调用 Worker 创建接口，仅本地生成地址并通过 QQ IMAP 取件。

#### Remail 开放 API

Remail API 文档：[https://remail.aishop6.com/docs](https://remail.aishop6.com/docs)。该服务使用 API Key
按项目创建短效接码订单，订单返回的邮箱和 service token 会自动用于后续取码。

在 WebUI「配置 → 邮箱 / OTP」填写：

- `REMAIL_API_KEY`：Remail 控制台生成的 `rk-` 开头 API Key；
- `REMAIL_PROJECT_ID`：Remail「项目」列表中用于 ChatGPT/OpenAI 验证码的 `projectId`；
- `REMAIL_EMAIL_SUFFIX`：下单后缀，微软邮箱通常填 `outlook.com`。

然后设置：

```dotenv
USE_EMAIL_SERVICE=True
EMAIL_SOURCE=remail
REMAIL_API_BASE=https://remail.aishop6.com
REMAIL_API_KEY=你的_Remail_API_Key
REMAIL_PROJECT_ID=项目ID
REMAIL_EMAIL_SUFFIX=outlook.com
REMAIL_SERVICE_MODE=purchase
REMAIL_SUPPLY_POLICY=public_only
```

`REMAIL_SERVICE_MODE` 默认为 `purchase`（长效购买，可重复收件），也可改为 `code`（短效接码）。
`REMAIL_SUPPLY_POLICY` 默认为 `public_only`，也可改为 `private_first`。每个注册任务会创建一个
对应模式的订单，验证码通过 `/v1/pickup` 获取；Remail 订单余额和对应项目库存需可用。

---

### 2. 配置代理

编辑 `config/proxy.py`：

```python
PROXY_POOL = [
    "http://user:pass@host:port",
]
```

---

### 3. 配置 Codex OAuth

如不需要 Codex，关闭：

```python
ENABLE_CODEX_AUTO = False
```

如需要自动授权：

```python
ENABLE_CODEX_AUTO = True
```

接码配置在 `config/codex.py`：

```python
SMS_PROVIDER = "l"        # 可选 grizzly / smsbower / l / h
SMS_API_KEY = "你的 GrizzlySMS key"  # 仅 GrizzlySMS 需要
SMS_SERVICE = "openai"
SMS_COUNTRY = "国家代码"
SMS_MAX_RETRIES = 10
SMS_CODE_WAIT = 120
SMS_POLL_INTERVAL = 5

# 若 SMS_PROVIDER="h"，H 固定复用：
#   SMS_SERVICE -> H projectId
#   SMS_COUNTRY -> H country
H_API_BASE = "http://localhost:8788"
H_ADMIN_AUTH_CODE = "你的H后台授权码"
```

SMSBower 使用独立配置，不复用 GrizzlySMS 的 API Key、服务代码、国家 ID 或价格：

```env
SMS_PROVIDER="smsbower"
SMSBOWER_API_BASE="https://smsbower.page/stubs/handler_api.php"
SMSBOWER_API_KEY="你的 SMSBower API Key"
SMSBOWER_SERVICE="通过 getServicesList 确认"
SMSBOWER_COUNTRY="通过 getCountries 确认"
SMSBOWER_MIN_PRICE=""
SMSBOWER_MAX_PRICE="0.15"
```

WebUI 的“SMSBower”配置页会通过非计费的 `getServicesList` / `getCountries` 接口加载服务代码和国家 ID 下拉选项；首次配置时填写 API Key 后点击“刷新选项”即可，无需先保存。元数据查询不会取号，失败时也不会覆盖当前值。

取号可能计费。系统会在取号前校验价格，且不会自动提高最高价格或移除价格限制。第一版仅使用 V1 `getNumber/getStatus/setStatus` 轮询流程，不包含 `getNumberV2`、Webhook 或价格库存预查询。供应商接口细节见 `docs/vendor/smsbower_client_api.md`。回滚时把 `SMS_PROVIDER` 改回原 provider 即可，SMSBower 配置不会被其他 provider 读取。

CPA 授权地址来源：

```python
CODEX_AUTH_URL_SOURCE = "cpa"
CPA_MANAGEMENT_URL = "你的CPA管理地址"
CPA_MANAGEMENT_KEY = "你的CPA管理密钥"
```

---

## 使用方式

## WebUI 推荐方式

推荐使用项目根目录单脚本后台管理：

```bash
./webui.sh start      # 启动
./webui.sh stop       # 关闭
./webui.sh restart    # 重启
./webui.sh status     # 状态
./webui.sh logs       # 查看实时日志
```

脚本默认启动 `http://127.0.0.1:5000`，日志写入 `logs/webui.log`，PID 写入 `run/webui.pid`。

可通过环境变量调整：

```bash
PORT=8000 OPEN_BROWSER=1 ./webui.sh start
HOST=0.0.0.0 PORT=5000 ./webui.sh restart
AUTH_CODE=你的授权码 ./webui.sh start
```

也可以直接前台启动：

启动：

```bash
python web.py --open-browser
```

默认地址：

```text
http://127.0.0.1:5000
```

可指定端口：

```bash
python web.py --port 8000 --open-browser
```

允许局域网访问：

```bash
python web.py --host 0.0.0.0 --port 5000
```

WebUI 页面说明：

| 页面 | 功能 |
|---|---|
| 注册 | 设置注册数量、线程数，启动批量注册，查看任务和日志 |
| 账号 | 查看账号、复制 token、补跑 Codex、批量删除账号 |
| Codex 授权 | 查看/下载/删除 `codex_accounts/` 凭证 |
| 邮箱池 | 导入邮箱、筛选来源、标记可用/失败、删除邮箱 |
| 配置 | 修改运行配置并热加载，含 Codex、邮箱、代理、人工节奏等 |

### 线程数说明

- 注册线程数在每次点击「开始注册」时读取。
- 如果线程数和上次不同，新提交任务会使用新线程池。
- 旧线程池里已经排队/运行的任务会继续跑完，不会被强制取消。
- Codex 批量补跑每次都会按本次提交的补跑线程数创建独立线程池。

---

## CLI 使用方式

注册 1 个：

```bash
python main.py
```

批量注册 10 个，3 线程：

```bash
python main.py -n 10 --workers 3 --continue-on-fail
```

详细日志：

```bash
python main.py -n 1 --verbose
```

参数：

| 参数 | 说明 | 默认 |
|---|---|---|
| `-n, --count` | 注册数量 | 1 |
| `--workers` | 并发线程数 | 1 |
| `--delay` | 每次注册结束后的间隔秒数 | 0 |
| `--continue-on-fail` | 单个失败后继续 | False |
| `--verbose` | DEBUG 日志 | False |

---

## Codex 补跑

WebUI 账号页可单个或批量补跑 Codex。

CLI 单独补跑：

```bash
python tools/test_codex_oauth.py --email <已注册邮箱> --verbose
```

补跑会消耗：

- 1 次邮箱 OTP
- 1 个接码号码

补跑日志在：

```text
注册日志/codex-retry-邮箱.log
```

---

## 注册密码说明

注册如果遇到新版流程：

```text
/create-account/password
```

会自动设置密码。

密码来源：

1. 优先使用 `config/register.py`：

```python
REGISTER_PASSWORD = "你的固定密码"
```

2. 如果为空，自动生成 14 位强密码，包含大写、小写、数字、符号。

保存位置：

- 账号 `extra_json.registration_password`
- 批次归档 `accounts/YYYYMMDD-.../注册成功账号.json` 的 `extra.registration_password`

注意：账号表里的 `password` 字段仍用于 Outlook 邮箱素材密码，不会被 OpenAI 注册密码覆盖。

---

## 重要配置文件

| 文件 | 说明 |
|---|---|
| `config/codex.py` | Codex OAuth、CPA 管理接口、接码平台 |
| `config/email.py` | 邮箱来源、OTP 轮询、QQ IMAP、域名邮箱、Cloudflare Worker 临时邮箱 |
| `config/proxy.py` | 代理池 |
| `config/register.py` | 默认邮箱、密码、显示名 |
| `config/twofa.py` | 2FA 开关 |
| `config/humanize.py` | 随机停顿/人工节奏 |
| `config/flow_trigger.py` | 注册成功后触发 Flow |
| `config/browser.py` | 协议模式浏览器指纹 |
| `config/openai_protocol.py` | OpenAI OAuth/Sentinel 参数 |

WebUI 配置页保存后会调用热加载；Codex、邮箱、代理、人工节奏等常用项可立即生效。

---

## 数据与产物

| 路径 | 内容 |
|---|---|
| `runtime.db` | SQLite 运行时权威数据：账号、任务和邮箱池状态 |
| `runtime_backups/` | 经完整性校验的滚动 SQLite 备份 |
| `用于注册的邮箱.txt/json` | 从 SQLite 生成的 Outlook 邮箱池兼容快照 |
| `用于注册的API邮箱.txt/json` | 从 SQLite 生成的通用 API 邮箱池兼容快照 |
| `注册成功的邮箱.txt/json` | 从 SQLite 生成的注册账号兼容快照 |
| `注册成功的token.txt` | 从 SQLite 生成的 ChatGPT access token 导出 |
| `accounts/` | 每次运行的批次归档 |
| `codex_accounts/` | Codex OAuth 凭证 JSON |
| `注册任务.json` | 从 SQLite 生成的 WebUI 注册任务兼容快照 |
| `注册日志/` | 注册任务日志、Codex 补跑日志 |
| `accounts_viewer.html` | 本地账号查看页 |

不要直接编辑上述 JSON/TXT 快照；运行时状态以 `runtime.db` 为准。服务启动时会执行 schema 和 `PRAGMA integrity_check`，数据库损坏时将拒绝以空数据启动。

管理命令必须使用项目虚拟环境运行，并在恢复数据库前停止 WebUI：

```powershell
.\.venv\Scripts\python.exe tools\runtime_storage_admin.py check
.\.venv\Scripts\python.exe tools\runtime_storage_admin.py backup --keep 7
.\.venv\Scripts\python.exe tools\runtime_storage_admin.py export
.\.venv\Scripts\python.exe tools\runtime_storage_admin.py restore runtime_backups\runtime-YYYYMMDD-HHMMSS.db
```

首次从旧文件迁移时，先停止 WebUI，再执行：

```powershell
.\.venv\Scripts\python.exe tools\migrate_runtime_storage.py `
  --database runtime.db `
  --accounts 注册成功的邮箱.json `
  --jobs 注册任务.json `
  --outlook-emails 用于注册的邮箱.json `
  --icloud-emails 用于注册的iCloud邮箱.json `
  --domain-emails 用于注册的域名邮箱.json
```

批次目录示例：

```text
accounts/20260709-10个-3线程/
├── 注册成功的邮箱.txt
├── 注册成功的token.txt
├── 注册成功整行.txt
└── 注册成功账号.json
```

---

## 常见问题

### 配置保存后没生效？

WebUI 配置页保存后会热加载。Codex 补跑线程启动前也会重新热加载一次配置。

如果你直接手改 `config/*.py`，CLI 进程需要重启；WebUI 建议在配置页修改。

### 出口 IP 不是日本时点到 Google 登录？

注册邮箱入口已改为只按 DOM 技术属性定位，并排除三方登录按钮。不会再靠按钮文字匹配“Continue”。

### Codex 显示 `Check your phone` 被误判失败？

已兼容：`Check your phone / Enter the verification code...` 会识别为手机验证码页，进入等待短信验证码流程。

### 手机 OTP 提交后日志曾显示失败，但后面成功？

已修复：提交手机 OTP 后会等待页面离开手机号流程或 callback，不再 3 秒后用旧页面文案误判失败。

### Codex 失败但注册成功怎么办？

账号会保存，Codex 状态会标记失败。可以在 WebUI 账号页点击补跑，或使用：

```bash
python tools/test_codex_oauth.py --email <邮箱> --verbose
```

### Cloudflare Worker 邮箱怎么配？

将 `EMAIL_SOURCE` 设为 `cloudflare`，并配置 `CLOUDFLARE_API_BASE` 等（见上文「Cloudflare Worker 临时邮箱」）。  
注意与 `cloudflare_domain`（QQ IMAP 转发）不是同一来源。

### 没有接码平台能注册吗？

可以。关闭：

```python
ENABLE_CODEX_AUTO = False
```

注册主流程不依赖接码，Codex 自动授权才需要。

---

## 项目结构

```text
.
├── main.py                         # CLI 入口
├── web.py                          # WebUI 入口
├── config/                         # 配置
│   ├── codex.py                    # Codex OAuth / CPA / 接码
│   ├── email.py                    # 邮箱来源/OTP
│   ├── proxy.py                    # 代理池
│   ├── register.py                 # 默认注册信息
│   └── ...
├── core/
│   ├── registration_service.py     # WebUI 注册线程池
│   ├── codex_oauth.py              # Codex 协议授权
│   ├── email_provider.py           # 邮箱来源调度
│   ├── cf_temp_mail_client.py      # Cloudflare Worker 临时邮箱
│   ├── sms_provider.py             # 接码平台
│   ├── account_export.py           # 保存账号/批次归档
│   ├── sqlite_store.py             # SQLite 事务、迁移和备份基础设施
│   └── db.py                       # 运行时数据访问与兼容导出
├── webui/
│   ├── app.py                      # Flask API
│   ├── config_editor.py            # 配置读写/热加载
│   └── templates/index.html        # 单页控制台
├── sentinel/
│   ├── sdk.js
│   └── sentinel-runner.js
├── tools/
│   └── test_codex_oauth.py         # Codex 单独补跑
└── L_API.md                        # 本地 L 接码接口说明
```

---

## 使用建议

- 日常批量使用 WebUI，不建议直接同时开多个 CLI 进程。
- 注册线程数建议不超过可用代理数。

---

## 🙏 致谢

- [LINUX DO](https://linux.do) — 社区交流与用户反馈
- [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) — Codex OAuth 凭证格式参考
- [curl_cffi](https://github.com/yifeikong/curl_cffi) — 底层 HTTP 库，提供 TLS 指纹 impersonate 能力

---

## License

MIT
