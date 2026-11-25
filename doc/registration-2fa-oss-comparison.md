# 当前注册与 2FA 流程对比 OSS

## 1. 范围与结论

本文对比当前仓库与只读快照 `../turb-gpt-free-register-oss-20260928` 中的协议注册、已有账号登录、OAuth 回调和 TOTP 2FA 绑定实现。OSS 结论限于该固定快照，不代表上游当前版本；它只提供源码构造证据，不能代替 HAR / SAZ wire 证据或服务器接受证明。

对比对象：

- 当前仓库：`main.py`、`core/openai/register/registration_flow.py`、`core/openai/auth/openai_auth.py`、`core/openai/auth/chatgpt_auth.py`、`core/openai/account/account_export.py`、`core/openai/transport/session.py`、`core/openai/account/twofa_service.py`。
- OSS：`协议/auth_flow.py`、`协议/webui/two_factor.py`、`协议/webui/registrar.py`、`协议/_turb_bridge.py`，均相对于下述快照源码根目录。

路径约定：本地源码以及 `har/`、`saz/` 捕获路径均相对于当前仓库根目录，不相对于本文所在的 `doc/`。OSS 的 `协议/...` 是 `../turb-gpt-free-register-oss-20260928/vendor/gpt-register-tool/` 下的快照相对路径。其他对照报告位于 `doc/`： [HAR 注册分支对照](registration-2fa-har-comparison.md)（`doc/registration-2fa-har-comparison.md`）与 [SAZ 密码 / TOTP 对照](login-password-totp-403-analysis.md)（`doc/login-password-totp-403-analysis.md`）；原始捕获仍保留在 `har/`、`saz/`，不随报告迁移。

结论：两边主链路相似，但不是逐请求等价。主要差异如下：

1. 当前代码先按服务端落地状态分类，再执行密码分支与后续认证状态循环；OSS 新号路径在验证码后会显式读取 `client_auth_session_dump`，随后直接进入 `create_account()`。
2. OSS 非 refresh-only 且存在 callback 或 continue URL 时，在 2FA hook 和可选 OAuth/Codex 交换之后还会刷新 session，即使未执行交换也会刷新；当前注册成功路径在 OAuth callback 后通常只取一次 `/api/auth/session`。慢速 2FA 重认证只有到达 callback/session 才会另取，注册回退的设备上下文限制见第 7.1 节。
3. 当前代码对 Sentinel 主凭据按 flow 和设备严格绑定，但 `probe_registration_state()` 对 `authorize_continue` 特意不消费 SO；OSS 保留实例级 Sentinel，并有可选的跨 flow 复用补丁。
4. 两边普通注册的 `document-navigation-id` 轮换点大致对应；当前慢速 2FA 与查活重认证的 authorize 响应轮换时点与 OSS 一致，服务端接受情况仍需实时验证。
5. 注册后 2FA 的常见未启用路径都涉及同一组四个 backend 请求，但头构造、触发位置和失败收尾不同；分支可能提前结束。

本文是源码静态对照，不是新的真实网络抓包，也未重新执行历史离线测试。实际请求数量会受服务端分支、重试、workspace 跳转和配置开关影响；引用历史报告时仅沿用其独立捕获窗口内的证据，不拼接账号或 session，不补写未捕获的请求或动态凭据。

## 2. 注册主流程

### 2.0 比较边界与辅助输入来源

本地与 OSS 的注册 / 已有账号登录主链均从各自的**正式 `get_csrf_token()` → signin** 开始：正式 CSRF 是响应中的 `csrfToken` 被后续 signin 表单消费的那次 `GET /api/auth/csrf`，不是按请求次数定义的“第二次 CSRF”，设备 warmup 也不是正式 CSRF。本地赋值与调用见 `core/openai/auth/chatgpt_auth.py:88-118,171-175`、`core/openai/register/registration_flow.py:305-314`；OSS 见 `协议/auth_flow.py:2173-2209,2324-2334,3828-3831`。

新号和已有账号分支由 authorize 落点 / 状态分类开始分流，分别保留密码、OTP、TOTP、profile 等条件节点；截止 callback/session、可选 2FA 及现有收尾。慢速 2FA 的重新登录也从自身正式 CSRF → signin 起，不与先前注册会话拼接。下述图表表示源码的条件性控制流，不表示某份捕获覆盖了全部节点。

正式 CSRF 之前的 trace、设备 warmup、providers、入口页面及国家探测统一排除，不逐项审计顺序、请求头或响应；省略不代表这些请求未发生或两边前置流程一致。仅保留影响正式 CSRF / signin 和后续请求的辅助来源：

- **设备 Cookie / 设备 ID【辅助来源】**：本地注册以 `BrowserSession(server_device_id=True)` 创建空设备 ID，设备 warmup【辅助请求】之后读取服务端 `oai-did` Cookie 并绑定到 `session.device_id`，不消费这次 warmup 的 CSRF 响应 token；该设备值参与正式 CSRF / signin 的登录上下文和后续 authorize。依据：`core/openai/transport/session.py:100,125,148-158`、`core/openai/register/registration_preflight.py:64-99`、`core/openai/auth/chatgpt_auth.py:21-53`。OSS warmup【辅助请求】以 CookieJar 中存在 `oai-did` 为判据，signin 经 `_resolve_device_id()` 读取设备值；依据：OSS `协议/auth_flow.py:1981-2007,2291-2316`。这是各自源码的数据依赖，不证明捕获中的初始 Cookie 也由相同请求生成。
- **UA / Client Hints、语言与时区画像【辅助来源】**：两边从各自会话画像构造 HTTP 头；本地注册预检提供出口 / 画像冻结的输入，OSS 采用其初始化画像。只保留这些输入对主链字段的影响，头构造见第 4.4 节及 `core/openai/register/registration_preflight.py:27-62`。HTTP 头画像、Sentinel SDK 的 JS 环境和传输后端指纹是不同层，不能由共用配置推断等同真实浏览器。
- **邮箱与登录上下文【辅助来源】**：本地使用注册入口 / 邮箱服务提供的邮箱和会话 `auth_session_logging_id` 构造完整 `login_with` Referer，再显式共享给正式 CSRF 和 signin；生成 Referer 不等于已请求入口页面。logging ID 可复用传入值、seed 派生或生成 UUID。OSS 使用邮箱服务创建的邮箱，在 signin 缺少 logging ID 时生成并复用实例值。依据：`core/openai/transport/session.py:160-166`、`core/openai/auth/chatgpt_auth.py:21-29,156-164`、`core/openai/register/registration_flow.py:305-314`；OSS `协议/auth_flow.py:2310-2334,3824-3830`。跨请求传递关系不能证明两边随机源或浏览器内部生成算法一致。

### 2.1 当前仓库

当前 `core.openai.register.registration_flow.run_registration()` 的典型顺序；`main.py` 只负责 CLI 参数和批量循环，并调用同一入口：

```text
[正式 CSRF]
  +-- GET chatgpt.com/api/auth/csrf     # get_csrf_token；响应 token 传给紧随其后的 signin
  +-- POST chatgpt.com/api/auth/signin/openai
  +-- GET signin 返回的 authorize URL，跟随重定向
  |
[分类]
  +-- 按 authorize 落地 URL 分类；未知时才探测
  |     +-- Sentinel/req(authorize_continue)
  |         -> POST /api/accounts/authorize/continue（只发主 token）
  |     +-- 探测后仍未知时按 new_password 继续
  |
[新账号（包括探测识别为 passwordless_signup）]
  +-- 必要时 GET /create-account/password
  +-- Sentinel/req(username_password_create)
  +-- POST /api/accounts/user/register
  +-- 成功后先持久化密码；返回 OTP 状态时再导航发码
  +-- passwordless_signup 被明确拒绝时，按响应支持情况请求登录 OTP
  |
[已有账号]
  +-- 按状态发码；密码步骤优先使用本地密码验证，缺失或明确拒绝时请求登录 OTP
  |
[后续状态循环：otp / totp / profile / callback（未知、冲突或重复则停止）]
  +-- otp：可选 Sentinel/req(email_otp_validate) -> POST email-otp/validate
  +-- totp：POST mfa/issue_challenge -> Sentinel/req(verify_mfa) -> POST mfa/verify
  +-- profile：仅新账号可 Sentinel/req(oauth_create_account) -> POST create_account
  +-- callback：GET continue_url 跟随重定向
  |     新号手动逐跳；已有账号自动跟随
  |     -> GET chatgpt.com/api/auth/session（失败时外层最多重试 5 次）
  |
[2FA] 可选：查询服务端状态并绑定 TOTP；已启用时只恢复本地 secret
[落库] 保存账号
[后置] 触发 flow（结果只记录，不影响账号保存）
```

关键实现位置：`core/openai/register/registration_flow.py:256-659`（编排、正式 CSRF → signin 的 token 传递、状态循环、2FA 收尾与落库）、`core/openai/auth/openai_auth.py:347-423,517-1200`（authorize、Sentinel、密码/OTP 原语、状态分类、MFA 与登录 OTP）、`core/openai/auth/chatgpt_auth.py:88-189`（正式 CSRF/signin）和 `core/openai/account/account_export.py:193-285`（OAuth 回调、session）。`main.py:85-172` 只保留 CLI 入口、批量循环与对同一入口的调用；会话与设备等辅助输入见第 2.0 节。

当前注册的特点：

- `authorize` 落地 URL 可直接判定时，不发送 `authorize_continue`；只有未知落地才探测。`signup` 意图下 `/email-verification` 被归为新号密码路径；这只是客户端分类，不保证服务端一定接受。
- 非严格探测接受 HTTP 200/204；JSON 解析失败或结构无效时兼容地按新号密码路径处理，未匹配密码页等已知分支的未知 `page.type` 则按已有账号 OTP 且需发码处理。探测请求或 Sentinel 本身失败时注册终止，不将其当作解析失败回退。
- 两类新号都先尝试密码注册。服务端确认密码生效后立即保存；只有后续状态为 OTP 才按 `continue_url` 导航，通常指向发码端点。`new_passwordless` 只有在密码被明确拒绝时才尝试由响应支持的登录 OTP 路径，网络结果未知不回退。
- 已有账号按状态选密码或 OTP；进入后续循环后按服务端返回依次处理 OTP、TOTP、profile、callback，而非假设 OTP 或 profile 必经。已有账号意外进入 profile 会停止，不创建资料。`create_account` 成功后的响应也仍需通过状态循环识别 callback。
- 登录 OTP 投递仅在继续地址明确指向发码路径时 GET 该地址；已知 password/OTP 状态否则使用 POST resend，未知或不支持的状态不盲目发码。已有账号的首次投递要求则由主流程单独调用 GET send。
- 注册 OTP 验证遇到明确的无效或过期验证码时，最多提交三次，前两次失败后通过 POST resend 并重新取码；开启配置时每次验证重新获取 `email_otp_validate` Sentinel。其他失败并不自动视为无效验证码。当前仓库没有实现 OSS 的 `passwordless/send-otp` 发码路径。

### 2.2 OSS 协议引擎

OSS `AuthFlow.run_register()` 的典型顺序：

```text
get_csrf_token()                       # 正式 CSRF；不是设备 warmup
  +-- GET chatgpt.com/api/auth/csrf     # 响应 token 传给 get_auth_url()
get_auth_url()
  +-- POST chatgpt.com/api/auth/signin/openai
auth_oauth_init()
  +-- GET auth.openai.com authorize 重定向链
signup()
  +-- 优先按 authorize 落地 URL 分类
  +-- 未知时：Sentinel/req(authorize_continue)
                  -> POST /api/accounts/authorize/continue

新账号路径：
  +-- GET /create-account/password
  +-- Sentinel/req(username_password_create)
  +-- POST /api/accounts/user/register
  +-- GET /api/accounts/email-otp/send
  +-- Sentinel/req(email_otp_validate)
  +-- POST /api/accounts/email-otp/validate
  +-- GET /api/accounts/client_auth_session_dump
  +-- Sentinel/req(oauth_create_account)
  +-- POST /api/accounts/create_account

已有账号路径：
  +-- 按 login_password / passwordless / OTP / MFA 状态分支
  +-- 必要时 resend 或 send OTP，并 POST /api/accounts/email-otp/validate
  +-- 必要时 about-you/create_account

共同收尾（常规非 refresh-only；可选开关也允许部分交换在 callback 前执行）：
  +-- 手动跟踪 redirect chain，捕获 callback
  +-- 有 callback 时消费它，设置 NextAuth session cookie
  +-- GET chatgpt.com/api/auth/session
  +-- 2FA hook（如果启用且有 access token）
  +-- 若存在 callback 或 continue_url：
        +-- GET client_auth_session_dump
        +-- 按开关可选 OAuth/Codex exchange
        +-- 非 refresh-only 时再次 GET chatgpt.com/api/auth/session（未交换时也会执行）
```

关键实现位置：

- 主流程：OSS `协议/auth_flow.py:3810-4199`
- OAuth 初始化与分类：OSS `协议/auth_flow.py:2291-2647`
- 密码注册、发码：OSS `协议/auth_flow.py:2695-2913`
- OTP、建号：OSS `协议/auth_flow.py:3034-3182`
- redirect/session：OSS `协议/auth_flow.py:3346-3624`

### 2.3 正式 CSRF 之后的端点顺序差异表

| 阶段 | 当前仓库 | OSS | 差异影响 |
|---|---|---|---|
| 正式 CSRF | `get_csrf_token()` 的响应 token 传给 `signin_openai()` | `get_csrf_token()` 的响应 token 传给 `get_auth_url()` | 两边均以被 signin 消费的响应 token 定义起点；设备 / 画像等辅助输入见第 2.0 节，不按端点出现次数判断。 |
| OAuth signin | POST `/api/auth/signin/openai` 后跟随 authorize | 相同 | signin 的 Origin、Referer、query 和设备上下文构造不同，见第 3、4 节。 |
| 账号分类 | 优先 landing URL，未知才 `/authorize/continue` | 同样优先 landing URL，未知才探测 | 可分类的新号落点可省掉探测 POST；两边探测失败后的策略不同，当前不会把请求异常当作新号。 |
| 密码页 | landing 已是密码页时跳过 GET | `register_password()` 总会尝试 GET | 可能导致 document ID 和页面状态不同。 |
| 设置密码 | POST `/api/accounts/user/register`，成功后先持久化 | 相同；通过回调尽早保存 | 请求头、Sentinel fallback 和持久化失败处理不同。 |
| 密码后发码 | 成功后只有返回 OTP 状态才导航服务端继续地址 | 新号路径主动 GET `/email-otp/send` | 当前依赖状态解析；OSS 新号路径更固定。 |
| OTP 验证后 | 按响应状态继续，可能进入 TOTP、profile 或 callback | 新号先 GET `client_auth_session_dump`，再 `create_account()` | OSS 新号多一个尽力而为的 dump 请求，且建号顺序固定。 |
| callback | 新号 `follow_oauth_callback(user_initiated=False)` 手动逐跳；已有账号自动跟随 | `follow_redirect_chain()` 手动逐跳并捕获 callback | 当前新号虽也手动逐跳，但不捕获 callback code；OSS 可以保留 callback code，后续再显式消费。 |
| session | callback 后 GET `/api/auth/session`；若慢速 2FA 重认证到达 callback/session 才再取，注册回退受第 7.1 节的设备限制 | 常规非 refresh-only 下有 callback 才消费，再 GET session；有 callback 或 continue URL 时收尾再次 GET | OSS 收尾可多一次，即使未发生可选 exchange。 |

## 3. 跳转来源与 Referer

### 3.1 当前仓库

当前导航请求使用不同的头构造器：

- ChatGPT 页面导航：`get_chatgpt_navigate_headers()`。
- Auth 页面导航：`get_auth_navigate_headers()`。
- Auth JSON/XHR：`get_auth_headers()`。
- NextAuth `/api/auth/*`：`get_nextauth_headers()`。

主要来源：

| 请求 | 当前 Referer/来源 |
|---|---|
| 注册正式 `get_csrf_token()` | 使用与 signin 共享的完整 `https://chatgpt.com/auth/login_with?...` Referer；显式注册参数下移除 GET Content-Type，并添加 `cache-control/pragma: no-cache`。无密码的标准邮箱 OTP 登录保留 `/auth/login` 默认上下文；有密码的查活 full-web 分支另选 `PASSWORD_TOTP_LOGIN`，构造并传递完整 `login_with` Referer。 |
| 注册 signin | 查询参数为 `prompt=login`、`auth_session_logging_id`、`screen_hint=login_or_signup`、`login_hint`；Referer 与正式 CSRF 共享，由 `callback_path`、`screen_hint`、`login_hint`、`auth_session_logging_id`、`ext-oai-did` 构造；另有 `Origin: https://chatgpt.com` 和两个 no-cache 头；form 的 `callbackUrl=https://chatgpt.com/`，`csrfToken` 来自正式 CSRF。查活有密码分支也显式共享同一 Referer；无密码标准分支仍使用原默认策略。 |
| authorize | `https://chatgpt.com/`。 |
| `/create-account/password` | 当前落点若以 `https://auth.openai.com/` 开头则使用该 URL，否则使用 `https://auth.openai.com/`。 |
| `/api/accounts/authorize/continue` 状态探测 | 注册为 `https://auth.openai.com/`，body 额外携带 `screen_hint=signup`；2FA 重认证保留 `/create-account` Referer，不发送 screen hint。 |
| `/api/accounts/user/register` | `https://auth.openai.com/`。 |
| `/api/accounts/email-otp/send` | 注册密码后导航用 `https://auth.openai.com/`；已有账号首次发码用 `https://auth.openai.com/create-account/password`；查活/默认重发用 `/email-verification`；登录 follow-send 用 `/log-in/password`。 |
| `/api/accounts/email-otp/resend` | `https://auth.openai.com/email-verification`。 |
| `/api/accounts/email-otp/validate` | 注册调用为 `https://auth.openai.com/`；查活调用保留 `https://auth.openai.com/email-verification`。 |
| `/api/accounts/create_account` | `https://auth.openai.com/`。 |
| 注册 callback | 新号固定使用 `https://auth.openai.com/`；已有账号初始使用分类落点，并在 OTP、TOTP 后分别更新为 email-verification、mfa。`follow_oauth_callback()` 的默认值是 about-you，但注册流程始终显式传入 referer。 |
| `/api/auth/session` | `https://chatgpt.com/`。 |

依据：`core/openai/auth/chatgpt_auth.py:21-53,88-189`、`core/openai/transport/session.py:573-581,664-861`、`core/openai/auth/openai_auth.py:361-365,483-514,628-861,1000-1041,1095-1154`、`core/openai/register/registration_flow.py:326-501`、`core/openai/account/account_export.py:193-236,434-486`。慢速 2FA 的 reauth signin 使用根路径 Referer 和 ChatGPT Origin，不套用注册 signin 的 `/auth/login_with` Referer。

注册 callback 每跳按目标 URL 前缀选择头：以 `https://chatgpt.com` 开头时使用 ChatGPT 导航头，否则使用 Auth 导航头。新号把 `user_initiated` 设为 `False`，由 `follow_oauth_callback()` 手动逐跳（最多 31 跳）；已有账号为 `True`，交给 HTTP client 自动跟随。见 `core/openai/account/account_export.py:214-227` 与 `core/openai/register/registration_flow.py:496-501`。

### 3.2 OSS

OSS 普通注册的正式 CSRF / signin 使用固定登录 Referer，后续来源部分对应当前实现，手动 redirect chain 会在每一跳更新 Referer：

- 正式 CSRF 与 signin：`https://chatgpt.com/auth/login`；signin 不显式添加 Origin，query 中含 `ext-oai-did`；form 的 `csrfToken` 来自正式 CSRF 响应，`callbackUrl=/`。
- OAuth 初始化：`Referer: https://chatgpt.com/`，跨站 `sec-fetch-site=cross-site`。
- 密码页：`https://auth.openai.com/create-account`。
- OTP 验证：`https://auth.openai.com/email-verification`。
- 建号：`https://auth.openai.com/about-you`。
- redirect chain：每次跳转将上一跳 URL 作为下一跳 Referer。
- callback 消费：使用 `https://auth.openai.com/` 作为简化 Referer。

依据：OSS `协议/auth_flow.py:2173-2209,2291-2346,2377-2389,2701-2723,3034-3068,3138-3162,3346-3433,3570-3624`。

重要差异：OSS `send_otp()` 代码虽然注释记录真实文档导航不应携带 Sentinel，但实现仍从实例字段添加 `_last_sentinel_token` 和 `_last_sentinel_so_token`；当前 `navigate_email_otp_send()` 使用纯导航头，不携带 Sentinel。见 OSS `协议/auth_flow.py:2792-2821` 与当前 `core/openai/auth/openai_auth.py:602-622`。

## 4. 请求头差异

### 4.1 Auth JSON 请求

当前 `BrowserSession.get_auth_headers()` 会统一添加：

- `Accept: application/json`
- `Content-Type: application/json`
- `Sec-Fetch-Site/Mode/Dest`
- Referer、priority、`Origin: https://auth.openai.com`
- `traceparent`、`tracestate`
- `x-access-flow-invocation-id`
- `x-openai-document-navigation-id`
- `x-datadog-*`

见 `core/openai/transport/session.py:583-599,664-724,812-825`。

OSS 将 Auth JSON 头拆成 `_auth_json_headers()`，每次调用生成新的 `x-access-flow-invocation-id`，并挂当前 `x-openai-document-navigation-id`；`_common_headers()` 本身不带这两个一次性头。见 OSS `协议/auth_flow.py:1704-1840`。

OSS 的 `_auth_json_headers()` 只用于注册 / 登录链的 Auth JSON POST：`authorize/continue`、`user/register`、`passwordless/send-otp`、`email-otp/resend`、`email-otp/validate`、`create_account`（`协议/auth_flow.py:2510,2755,2827,2851,3065,3142`）。`email-otp/send`（`send_otp()`，`2793-2822`）改用 `_common_headers()`；已有账号的 `password/verify`（`login_password_verify()`，`2978-3001`）与 `mfa/verify`（`submit_mfa_totp()`，`3002-3033`）也改用 `_common_headers()` 并手动补 JSON Content-Type，因此不带上述两个一次性头。

本地没有这层二分，而是按用途拆 builder：

| 请求类型 | OSS | 本地 |
|---|---|---|
| 注册 / 登录链 Auth JSON POST | `_auth_json_headers()` | `get_auth_headers()`（本地未实现 `passwordless/send-otp` 端点） |
| `password/verify`、`mfa/verify` | `_common_headers()` + 手动 JSON Content-Type，无一次性头 | `get_auth_headers()`，含一次性头 |
| `email-otp/send`（GET） | `_common_headers()`，XHR 形态 | `get_auth_navigate_headers()`，文档导航形态 |
| NextAuth csrf / signin | `_common_headers()` | `get_nextauth_headers()` |
| ChatGPT backend / Sentinel | `_common_headers()` / SDK 专用头 | `get_chatgpt_headers()` / `get_sentinel_headers()` |
| 文档导航 | `_navigation_headers()` | `get_auth_navigate_headers()` / `get_chatgpt_navigate_headers()` |

本地 `get_auth_headers()` 的调用点覆盖所有 Auth JSON POST，包括 `password/verify`、`mfa/issue_challenge`、`mfa/verify`、2FA 重认证 OTP 与 Codex 登录提交（`core/openai/auth/openai_auth.py:1003,1051,1025,1082`、`core/openai/account/account_export.py:565`、`core/openai/codex/codex_oauth.py:729`）。这些请求比 OSS 多带一次性头；`mfa/issue_challenge` 在 OSS 中没有同构 JSON POST，其状态由 `mfa-challenge` 页面 GET 表达。

以上是本地 / OSS 源码构造差异，不在本文重新裁定与浏览器 wire 是否一致。注册 HAR 的请求序号、捕获窗口和字段证据见 [HAR 分支对照](registration-2fa-har-comparison.md)：其中 HAR B（`har/11.1-密码注册-快路径设置2fa.har`）从 `GET /create-account` 开始，未覆盖正式 CSRF / signin；这段缺口及各捕获未覆盖的已有账号密码 / MFA 段不能由 OSS 或另一份捕获补齐。HAR 未记录某头也不等于源码没有构造或最终报文一定不发送。Origin 与 device-id 的源码取舍见第 4.2 / 4.3 节。

### 4.2 Origin

- 当前 `get_auth_headers()` 对 Auth JSON 请求固定添加同源 `Origin: https://auth.openai.com`。注册 `signin_openai()` 使用 NextAuth 头并显式设置 `Origin: https://chatgpt.com`；慢速 2FA 的 reauth signin 也显式设置同一 Origin。
- OSS `_common_headers()` 只有提供的 `target_url` 与 Referer 跨源时才添加 Origin，未提供目标时视为同源；其 signin 未传 `target_url`，不显式添加 Origin。

这是源码层的明确差异，不能仅由 Referer 推断为等价，也不能将当前注册 signin 的行为推广到其他 NextAuth 请求。依据：`core/openai/auth/chatgpt_auth.py:156-175`、`core/openai/account/account_export.py:434-470`、`core/openai/transport/session.py:685-687,713-724,812-825`；OSS `协议/auth_flow.py:1704-1762,2291-2326`。

### 4.3 `oai-device-id`

- 当前 `get_auth_headers()` 本身不添加 `oai-device-id`，但 backend API 的 `get_chatgpt_headers()` 会添加；`get_mfa_enabled()` 还显式设置该头。
- 当前对 `chatgpt.com/backend-api/*`、`backend-anon/*` 和 `ces/*` 自动添加 target path/route 诊断头。
- OSS `_common_headers()` 仅在传入的 `target_url` 为 ChatGPT backend 端点时添加 `oai-device-id`；未提供目标 URL 时不添加。其 2FA 协议 helper 未传目标 URL，不能仅因目标实际是 backend API 就推断该头存在。

依据：当前 `core/openai/transport/session.py:626-633,657-661,805-825,866-900,1013-1025`、`core/openai/auth/openai_auth.py:1176-1200`；OSS `协议/auth_flow.py:1704-1808`、`协议/webui/two_factor.py:74-93`。

### 4.4 Client Hints、画像与 trace

第 2.0 节合并说明了正式主链使用的画像辅助来源；这里只保留头构造层的比较：

- 当前公共头从 `browser_profile` 取 UA、Accept-Language，并按画像和配置决定 Client Hints；Auth JSON 每次刷新 Datadog trace ID / parent ID，并以其生成 W3C trace。见 `core/openai/transport/session.py:542-566,583-610,664-724,812-825`。
- OSS 在 `_common_headers()` 生成指纹头与 Datadog/W3C trace，导航头由 `_navigation_headers()` 单独构造。见 OSS `协议/auth_flow.py:1704-1840`。Chromium 画像的 Client Hints 规则不能一概推广到非 Chromium 画像。

这里只能确认各自源码输入与构造规则；未对两边真实站点 wire 做逐请求指纹头集合核验，不能声称字段集合或值完全一致。HTTP 头画像、SDK 的 JS 环境与传输后端指纹不能互相替代；共用画像也不证明与浏览器内部采集、随机源或运行时行为一致。

## 5. Sentinel 刷新与复用

### 当前仓库

当前注册、已有账号认证和 2FA 慢路径在到达相应操作时获取对应 Sentinel；下表不表示各分支必定执行，注册回退受第 7.1 节的设备限制：

| 操作 | flow | 业务请求与 SO 策略 |
|---|---|---|
| 状态探测 | `authorize_continue` | POST `/api/accounts/authorize/continue`；注册携带 `screen_hint=signup`，2FA 重认证不发送该字段；只发送主 token，不消费、不校验或发送 SO，即使 challenge 声明 SO required。 |
| 密码验证 | `password_verify` | POST `/api/accounts/password/verify`；每次新取主 token，仅在当前 challenge 要求时发送 SO。 |
| MFA challenge | 无 | POST `/api/accounts/mfa/issue_challenge`；不携带主/SO token。 |
| MFA verify | `verify_mfa` | POST `/api/accounts/mfa/verify`；每次新取匹配 flow 的主 token，仅在当前 challenge 要求时发送 SO。 |
| 设置密码 | `username_password_create` | POST `/api/accounts/user/register`；有当前 challenge 的有效 SO 时发送。 |
| OTP 验证 | `email_otp_validate` | POST `/api/accounts/email-otp/validate`；有当前 challenge 的有效 SO 时发送。 |
| 建账号 | `oauth_create_account` | POST `/api/accounts/create_account`；有当前 challenge 的有效 SO 时发送。 |

`request_sentinel_token()` 在同一 SDK 上下文中完成 requirements、challenge 和 solve；`build_sentinel_header()` 始终校验主凭据的 flow、device ID、challenge token 及所需 proof，不接受不同 flow 的主头混用。默认 `consume_so=True` 时，会校验已有 SO 与上下文的匹配，并在服务端要求而 SO 缺失时失败；密码和 MFA verify 进一步只在本次 challenge 声明 required 时发送 SO。唯独 `probe_registration_state()` 显式使用 `consume_so=False`，注册分类和慢速 2FA 的严格探测共享此例外，不因 SO 缺失或无效而在构造业务头时失败，也不发送 SO；这不等于其他操作可忽略 SO 或 SDK/主凭据失败可继续。

注册 OTP 的 `SEND_SENTINEL_ON_EMAIL_OTP_VALIDATE` 默认是 `True`；关闭时 `registration_flow.py` 的 OTP 验证（`_validate_registration_otp`）可不带 Sentinel。2FA 慢路径若到达邮箱 OTP 验证，则独立获取 `email_otp_validate` 凭据，不受该注册开关控制；其有限重试会复用当前凭据和验证码。注册回退当前未必能到达此步骤，独立 2FA 不直接套用该设备限制。独立 Codex 路径使用 `authorize_continue` flow 时仍按默认策略消费并校验 SO，不受状态探测的例外影响。

密码验证、MFA challenge 和 MFA verify 均使用 Auth JSON 的根路径 Referer、`Origin: https://auth.openai.com`、JSON `Content-Type`、同源 CORS `Sec-Fetch-*` 和独立 `x-access-flow-invocation-id`，并复用当前认证文档的 `x-openai-document-navigation-id`。challenge 的 `Accept` 为 `*/*`，密码验证和 MFA verify 为 `application/json`。Auth Sentinel runner 仅允许受控的 Auth flow；verify MFA 不回退到 `password_verify` 或 `authorize_continue`。

依据：`core/openai/auth/openai_auth.py:517-540,568-599,644-705,708-757,930-972,1000-1074,1095-1154`、`core/openai/register/registration_flow.py:154-180`、`core/openai/account/account_export.py:528-581,786-862`、`core/openai/codex/codex_oauth.py:821-848`、`core/openai/auth/sentinel_runner.py:71-79,190-227`、`config/openai_protocol.py:44-45`。

### OSS

OSS 正常实现也会在密码注册、OTP 验证和 `create_account` 前刷新 token，但存在两个降级层：

1. Sentinel 获取失败时，部分路径会继续使用实例上一次的 token。
2. OTP 验证缺 SO 时记录 warning 后仍继续提交，而不是立即终止。

已有账号重认证另有差异：`login_password_verify()` 和 `submit_mfa_totp()` 直接读取实例缓存的主/SO token，不为 `password_verify` 或 `verify_mfa` 单独刷新 flow；TOTP 路径直接 POST `/api/accounts/mfa/verify`，没有 `mfa/issue_challenge` 请求。

此外，`协议/_turb_bridge.py` 提供 `TURB_SENTINEL_REUSE` 实验开关。默认关闭；开启后，`email_otp_validate` 和 `oauth_create_account` 可以直接复用上一个 token。该行为可能因服务端按 flow 校验而返回 400/409。

依据：OSS `协议/auth_flow.py:2725-2767, 2978-3023, 3034-3109, 3121-3149`；OSS `协议/_turb_bridge.py:210-250`。

## 6. `document-navigation-id` 刷新时机

### 普通注册

普通注册与 OSS 在 authorize HTTP 响应后的轮换边界一致；当前在检查响应状态之前轮换，OSS 在记录落点之前轮换。收到响应前的传输异常不轮换。

当前主要轮换点：

1. `BrowserSession` 初始化时生成初始 ID。
2. 每次 authorize GET 返回 HTTP 响应后，立即轮换，再进行状态检查、错误处理或重试。传输异常在收到响应前发生时不轮换；`follow_authorize()` 也供账号查活 full-web fallback 使用。
3. 仅当当前落点不是 `/create-account/password` 时才 GET 密码页；成功状态检查后轮换。已落在密码页时跳过 GET 和轮换；密码页 GET 返回错误状态时，状态检查先抛出，因此不轮换。
4. 注册 OTP 继续地址的导航、`send_email_otp()` 或登录 OTP follow-send 前轮换；因此导航请求失败前也可能已轮换。POST resend 不轮换。
5. Auth JSON 请求使用 session 当前 ID；同一文档内不因 JSON 请求或 SPA 路由切换而轮换。

OSS 主要轮换点：

1. `AuthFlow` 初始化时生成初始 ID。
2. `auth_oauth_init()` 收到 authorize HTTP 响应后轮换，再保存落地 URL；请求在响应前发生传输异常时不会执行该轮换。
3. `register_password()` 总会尝试 GET `/create-account/password`，并在尝试结束后轮换；即使 GET 抛出异常也会轮换。
4. `send_otp()` 在 GET `/api/accounts/email-otp/send` 前轮换。
5. OTP validate 与 `create_account` 共用之后的 ID。

authorize 响应边界一致不消除密码页分支差异：当前落点已是密码页时会跳过密码页 GET，而 OSS 仍会尝试该 GET，因此这类分支的总轮换次数及 `user/register` 使用的 ID 仍可能不同。

依据：当前 `core/openai/transport/session.py:169,608,612-614`、`core/openai/auth/openai_auth.py:27-29,378,560,616,636,1104`、`core/openai/account/account_export.py:483`；OSS `协议/auth_flow.py:210,1810-1840,2384-2395,2695-2723,2792-2817`。取值来源（捕获为响应头下发、本地与 OSS 自生成）、OSS 轮换规则及暂不调整的决定见[密码注册链路总结](registration-password-flow-summary.md)第 4.4 节。

### 慢速 2FA 重认证

当前注册后慢速 2FA、独立 2FA 与账号查活的 reauth 分支共用 `_follow_reauth_with_retry()`，轮换边界如下：

1. 实际 authorize GET 每当返回 HTTP 响应，立即轮换 `document-navigation-id`，再进行状态检查或重试决策；因此 HTTP 错误响应也会推进 ID。
2. authorize GET 在收到响应前发生传输异常时不轮换；重试收到响应后才按同一边界轮换。
3. 慢速 2FA 随后调用 `authorize_continue`；在没有新的文档级导航时，密码验证、邮箱 OTP 验证等 Auth JSON 请求复用最终成功导航的 ID。若服务端指示 GET `/api/accounts/email-otp/send`，该后续文档导航自身仍按既有规则轮换 ID。查活 reauth 使用相同导航 helper，但其后直接验证邮箱 OTP，不经过该状态探测。

这与 OSS `auth_oauth_init()` 在 authorize 响应后的轮换时点一致；源码及历史离线测试记录只能说明客户端预期，本文未重新运行测试，不能证明服务端只接受该序列，也未用同一账号、同一出口的实时 HTTP trace 验证线上接受结果。

依据：当前 `core/openai/account/account_export.py:367-404,434-486`、`core/openai/auth/openai_auth.py:27-30,1000-1041`、`core/openai/account/account_liveness.py:344-427`；OSS `协议/auth_flow.py:2351-2437`。

## 7. 2FA 绑定流程

### 7.1 当前仓库注册后的 2FA

当 `ENABLE_2FA=False` 时跳过整个注册后 2FA；默认值见 `config/twofa.py:15`。

开启后，`core/openai/register/registration_flow.py:506-548` 的主要分支如下；慢路径的后续步骤是 `setup_2fa()` 的条件性控制流，当前注册回退到探测时的限制见图后说明：

```text
已有 access_token
  +-- GET /backend-api/accounts/mfa_info
  |     +-- 已启用且本地有正式或 pending secret：恢复/提升 secret，不重复 enroll
  |     +-- 已启用但本地无 secret：不重复绑定，标记 2FA 失败
  |     +-- 未确认或未启用：setup_2fa_inline()
  |           +-- POST mfa/enroll -> 持久化 pending secret
  |           +-- POST mfa/user/activate_enrollment -> 成功后 GET mfa_info 诊断
  |
  +-- 普通快路径异常且存在已保存密码：同代理新建 BrowserSession，调用 setup_2fa()
        +-- GET /api/auth/csrf -> POST signin/openai?reauth=password&max_age=0
        +-- GET authorize
        +-- 严格 authorize_continue 探测；当前注册回退前置成功时在此因设备 ID 为空终止
        +-- 以下为具备设备上下文的 setup_2fa() 调用可能执行的后续步骤：
              必要时 password/verify、email OTP
              GET callback + GET /api/auth/session，取得新 access_token
              GET mfa_info；已启用则尝试恢复 pending secret
              否则 POST mfa/enroll -> 保存 pending secret -> activate -> GET mfa_info 诊断
```

注册回退调用没有传 `access_token`，因此不执行 `setup_2fa()` 的可选 `authenticated_bootstrap`；缺少已保存密码或 pending secret 持久化报 `CredentialPersistenceError` 时也不会回落慢路径。慢路径状态循环只处理密码、邮箱 OTP 和 callback，不支持 TOTP 等其他状态。activation 返回 HTTP 200 后的 `mfa_info` 仅作诊断；快路径 activation 异常本身不会先复核，普通异常可进入慢路径，只有重认证成功并到达复核时才再查状态、尝试恢复 pending secret。

注册回退还有当前源码的设备上下文限制：新建会话使用 `server_device_id=True`，其 `device_id` 初始为空；该回退没有执行注册预检绑定设备，重认证 helper 也没有赋值。若先前请求未失败，随后 `probe_registration_state()` 请求 Sentinel 时会因设备 ID 为空而失败，因此上图后续步骤不能视为当前注册回退一定能够到达。独立 2FA 后台使用默认设备初始化，不能将此限制直接推广到该入口。历史离线验证记录使用真实新会话并模拟前置重认证成功，不是实际账号或网络接受证据，本文未重新执行。

慢路径如果取得新 token，会在 enroll 前调用 `on_access_token`：注册回退只更新当前 `run_registration()` 中的变量，最终保存账号时写入；独立后台任务的回调则立即更新数据库。普通绑定失败后注册路径仍尝试保存已取得的登录态；要求启用 2FA 而未满足时任务不报告成功。

依据：`core/openai/register/registration_flow.py:218-253,506-548,626-653`、`core/openai/transport/session.py:148-169,878-900`、`core/openai/auth/openai_auth.py:347-423,517-540,1000-1041`、`core/openai/account/account_export.py:407-486,624-689,692-881`、`core/openai/account/twofa_service.py:118-214`。

### 7.2 OSS 2FA

OSS 由 `want_2fa` 控制。协议引擎正常情况下在拿到 access token、尚未进行 Codex 授权时执行 hook；未启用时的典型绑定顺序如下，已启用或失败时可能提前结束：

```text
GET  /backend-api/accounts/mfa_info
POST /backend-api/accounts/mfa/enroll
POST /backend-api/accounts/mfa/user/activate_enrollment
GET  /backend-api/accounts/mfa_info
```

OSS 可能在 hook 未成功后再次尝试快路径；仍未成功且有密码时，fallback 新建独立 `AuthFlow`，重走 login 正式链。慢路径成功消费 callback、取得新的 access token 后再调用同一组 MFA helper，已启用或失败时同样可能提前结束。见 OSS `协议/webui/registrar.py:339-375,529-588`、`协议/webui/two_factor.py:113-199,232-408`。

OSS 的浏览器引擎另有 `_bind_2fa_via_api()`，直接使用 Playwright API context 的 cookie 和 access token；这条路径不应与协议引擎的 `_common_headers()` 结果混为一谈。见 OSS `协议/browser_flow.py:1039-1121`。

### 7.3 快路径 API 对照

| 请求 | 当前仓库 | OSS |
|---|---|---|
| 查询状态 | GET `/backend-api/accounts/mfa_info` | 相同 |
| 申请 TOTP | POST `/backend-api/accounts/mfa/enroll`，JSON `{"factor_type":"totp"}` | 相同 |
| 激活 | POST `/backend-api/accounts/mfa/user/activate_enrollment` | 相同 |
| 复核 | GET `/backend-api/accounts/mfa_info` | 相同 |
| Authorization | Bearer access token | Bearer access token |
| device/language | 显式带 `oai-device-id`、`oai-language` | 协议 helper 未显式设置这两个字段，也未向 `_common_headers()` 传 `target_url`；浏览器引擎使用简单头 |
| target headers | backend API 自动补 target path/route | 协议 helper 未显式构造这两个 target 字段 |
| Referer | `https://chatgpt.com/` | 协议 helper 使用具体 endpoint URL |
| activate 成功后 | `_verify_mfa_after_activation()` 查询状态，只作诊断 | 等待后查询状态，复核结果不改变已成功的绑定返回 |
| activate 非 200 | 快路径抛错，不先做同一会话复核；普通异常可回落慢路径，只有重认证到达复核才再查询，注册回退另受设备限制 | 等待后复核 `mfa_info`，factor ID 匹配时可接受已生效的绑定 |

当前实现：`core/openai/register/registration_flow.py:506-548`、`core/openai/account/account_export.py:600-664,858-881`、`core/openai/auth/openai_auth.py:1176-1200`、`core/openai/transport/session.py:805-810,878-900`。

OSS 实现：`协议/webui/two_factor.py:74-199`。

### 7.4 已有账号登录时的 TOTP challenge

已有 [SAZ 密码 / TOTP 对照](login-password-totp-403-analysis.md)记录 `saz/login-password-totp-403.saz` 中的 #58 `password/verify` → #84 `mfa/issue_challenge` → #93 `mfa/verify`，#93 返回结构化 `account_deactivated`；这是独立 SAZ 窗口的转引，不是 OSS 运行结果，也不是本文新采集的 HAR。当前实现对应为：密码请求每次取 `password_verify` Sentinel；issue 请求不带 Sentinel；verify 请求每次取 `verify_mfa` Sentinel，且只在本次 challenge 要求时附带 SO。issue 和 verify 复用同一 factor ID；账号废弃错误作为不可重试结果结束查活或已有账号认证，不触发 OTP/signup fallback，也不释放已识别的 existing 邮箱。

OSS `login_password_verify()` 与 `submit_mfa_totp()` 使用实例缓存的 Sentinel/SO；TOTP 路径直接请求 `/api/accounts/mfa/verify`，没有对应的 `issue_challenge` 请求。

依据：当前 `core/openai/auth/openai_auth.py:1018-1074,1157-1200`、`core/openai/account/account_liveness.py:473-491,761-764`、`core/openai/register/registration_flow.py:626-653` 已有账号认证异常收尾；OSS `协议/auth_flow.py:2978-3023`。捕获证据及比较边界以所引 SAZ 报告为准，本文不重新审计原始捕获，不把客户端静态构造或离线测试等同于线上服务接受证明。

## 8. Session 与 token 刷新时机

### 当前仓库

1. 注册 callback 重定向完成后 GET `/api/auth/session`；callback 或 session 失败时收尾可能重试，所以请求并非始终恰好一次。
2. 若慢速 2FA 重认证到达 callback/session，才会再次 GET session 取得新 token；注册回退的设备 ID 限制见第 7.1 节。
3. 注册回退的 `on_access_token` 更新内存中的 token，账号保存时才写入；独立 2FA 后台入口的回调在 enroll 前立即更新数据库。
4. 注册账号最终保存当前 `access_token`。

依据：`core/openai/register/registration_flow.py:67-118,496-501,534-536,556-574`、`core/openai/account/account_export.py:193-285,583-597,858-862`、`core/openai/account/twofa_service.py:158-170`。

### OSS

1. 在可选的 callback 前交换之外，`follow_redirect_chain()` 手动跟踪并尝试捕获 callback，不主动消费 callback code。
2. 非 refresh-only 且有 callback 时，`_consume_callback_for_session()` 主动 GET callback，使 NextAuth 设置 session cookie。
3. 非 refresh-only 时 `get_auth_session()` 获取 access token；启用 2FA hook 且已有 token 时，在 hook 中使用当前 token。
4. 若存在 callback 或 continue_url，读取 `client_auth_session_dump`，并按开关可选执行 OAuth/Codex token exchange。
5. 在同一条件下，只要不是 refresh-only 就再次 GET `/api/auth/session`，不要求 exchange 实际发生，以覆盖可能更新的 cookie/access token。

依据：OSS `协议/auth_flow.py:3346-3433,3522-3624,4116-4190`。

因此，存在 callback 或 continue URL 且非 refresh-only 时，OSS 会额外刷新一次 session；当前主要在 callback 后取得 access token，重试或可达的独立重认证另有请求。上述条件和差异不证明任何一次线上响应成功。

## 9. 已确认差异与未确认项

### 已确认的源码差异

- OSS 新号 OTP 验证后有 `client_auth_session_dump`；当前注册主链没有对应调用。
- OSS 常规非 refresh-only 收尾在 callback 或 continue_url 存在时再次拉取 session，未发生 exchange 也会刷新；当前注册主流程没有同等的固定收尾刷新。
- 当前注册正式 CSRF 与 signin 显式共享完整动态 `/auth/login_with` Referer 和 no-cache 头，signin 另加 ChatGPT Origin；OSS 两者使用 `/auth/login` Referer，signin 不显式添加 Origin；本地 signin form 的 callbackUrl 为 ChatGPT 绝对根 URL，OSS 为 `/`。当前 Auth JSON 固定添加同源 Origin；OSS 按提供的目标与 Referer 判断。依据见第 2.0、3.1 / 3.2、4.2 节。
- 当前发码导航 GET 不带 Sentinel；OSS `send_otp()` 会从实例字段带缓存 Sentinel。
- 当前 `authorize_continue` 状态探测仅消费主 Sentinel，不消费 SO；其他调用点仍各自执行其 SO 策略。OSS 支持默认关闭的 Sentinel 跨 flow 复用实验；当前没有同名机制。
- 当前已有账号 TOTP 验证包含 `mfa/issue_challenge`；OSS 直接调用 `mfa/verify`。
- 当前 2FA 默认关闭；OSS 由 `want_2fa` 运行选项控制。注册快路径失败后的当前设备 ID 静态限制见第 7.1 节。

### 仍需实时抓包才能确认的内容

- 同一分支下两边最终发出的完整 header 字节是否一致；尤其是 HTTP client 自动补充的大小写、Cookie、Accept-Encoding 和 TLS 层行为。
- 慢速 2FA 重认证中服务端是否接受当前客户端按 authorize HTTP 响应边界生成的 document-navigation-id 序列，以及是否存在 HTTP client 未观察到的隐藏导航。
- 服务端在当前代码的状态循环中是否始终返回与 OSS 固定顺序一致的 `continue_url`。
- 启用 Sentinel 复用后，服务端是否按 flow 拒绝 OSS 的复用 token。
- callback 重定向链的实际 hop 数及是否进入 workspace/select。

## 10. 源码索引

### 当前仓库

- `main.py:85-172`：CLI 入口、批量循环与 `run_registration()` 调用。
- `core/openai/register/registration_flow.py:256-659`：注册编排、状态循环、OTP/TOTP、2FA 收尾与落库。
- `core/openai/register/registration_preflight.py:27-101`：设备 Cookie / 画像的辅助输入来源，不是正式 CSRF 主链起点。
- `core/openai/auth/chatgpt_auth.py:21-53,88-189`：login referer、CSRF、signin 和 authorize 上下文。
- `core/openai/auth/openai_auth.py:347-514`：authorize 导航与 Sentinel。
- `core/openai/auth/openai_auth.py:517-1200`：密码注册、OTP、create_account、状态分类、MFA 与登录 OTP。
- `core/openai/auth/sentinel_runner.py:71-79,190-227`：受控 flow 与同一 challenge 主凭据校验。
- `core/openai/transport/session.py:148-169,601-614`：设备、auth session logging 与 document-navigation ID。
- `core/openai/transport/session.py:583-861`：通用头、NextAuth、ChatGPT/Auth 导航头与 target headers。
- `core/openai/transport/session.py:866-900,1013-1025`：backend target headers 和 GET/POST 封装。
- `core/openai/account/account_export.py:193-285,407-486`：OAuth callback、session 和重认证导航。
- `core/openai/account/account_export.py:528-881`：重认证 OTP、2FA 快/慢路径。
- `core/openai/account/twofa_service.py:118-214`：独立 2FA 后台任务。
- `core/openai/codex/codex_oauth.py:821-848`：Codex 登录邮箱/OTP 提交。
- `config/twofa.py:15-42`：2FA 默认配置和重认证参数。
- `config/openai_protocol.py:44-62`：OTP Sentinel、bootstrap 和网络重试配置。

### OSS

- `协议/auth_flow.py:2173-2209,2291-2647`：正式 CSRF、signin、authorize 初始化、账号分类；设备 Cookie 辅助来源见 `1981-2007`。
- `协议/auth_flow.py:2695-2913`：密码注册和 OTP 投递。
- `协议/auth_flow.py:3034-3182`：OTP 验证和 create_account。
- `协议/auth_flow.py:3346-3624`：redirect chain、callback 消费和 session。
- `协议/auth_flow.py:3810-4199`：完整注册主流程与刷新时机。
- `协议/webui/two_factor.py:71-199`：MFA API 快路径。
- `协议/webui/two_factor.py:232-408`：独立登录重认证慢路径。
- `协议/webui/registrar.py:339-375, 529-588`：2FA hook 与 fallback。
- `协议/browser_flow.py:1039-1121`：浏览器引擎的独立 2FA API 路径。
- `协议/_turb_bridge.py:210-250`：Sentinel 复用实验开关。

