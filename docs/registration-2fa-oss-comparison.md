# 当前注册与 2FA 流程对比 OSS

## 1. 范围与结论

本文对比当前仓库与只读快照 `../turb-gpt-free-register-oss-20260928` 中的协议注册、OAuth 回调和 TOTP 2FA 绑定实现。OSS 结论限于该快照，不代表上游当前版本。

对比对象：

- 当前仓库：`main.py`、`core/openai_auth.py`、`core/chatgpt_auth.py`、`core/account_export.py`、`core/session.py`、`core/twofa_service.py`。
- OSS：`vendor/gpt-register-tool/协议/auth_flow.py`、`协议/webui/two_factor.py`、`协议/webui/registrar.py`、`协议/_turb_bridge.py`。

结论：两边主链路相似，但不是逐请求等价。主要差异如下：

1. 当前代码先按服务端落地状态分类，再执行密码分支与后续认证状态循环；OSS 新号路径在验证码后会显式读取 `client_auth_session_dump`，随后直接进入 `create_account()`。
2. OSS 非 refresh-only 且存在 callback 或 continue URL 时，在 2FA hook 和可选 OAuth/Codex 交换之后还会刷新 session，即使未执行交换也会刷新；当前注册成功路径在 OAuth callback 后通常只取一次 `/api/auth/session`。慢速 2FA 重认证只有到达 callback/session 才会另取，注册回退的设备上下文限制见第 7.1 节。
3. 当前代码对 Sentinel 主凭据按 flow 和设备严格绑定，但 `probe_registration_state()` 对 `authorize_continue` 特意不消费 SO；OSS 保留实例级 Sentinel，并有可选的跨 flow 复用补丁。
4. 两边普通注册的 `document-navigation-id` 轮换点大致对应；当前慢速 2FA 与查活重认证的 authorize 响应轮换时点与 OSS 一致，服务端接受情况仍需实时验证。
5. 注册后 2FA 的常见未启用路径都涉及同一组四个 backend 请求，但头构造、触发位置和失败收尾不同；分支可能提前结束。

本文是源码静态对照，不是新的真实网络抓包。实际请求数量会受服务端分支、重试、workspace 跳转和配置开关影响；相关历史 HAR 只提供各自捕获窗口内的证据，不补写未捕获的请求或动态凭据。

## 2. 注册主流程

### 2.1 当前仓库

当前 `main.run_registration()` 的典型顺序：

```text
创建 BrowserSession(server_device_id=True)；如会话提供重置方法则重置 Auth 导航上下文
  +-- cloudflare.com/cdn-cgi/trace
  +-- chatgpt.com/api/auth/csrf       # 注册 warmup，绑定服务端 oai-did，可能重试
  +-- chatgpt.com/api/auth/csrf       # get_csrf_token
  +-- POST chatgpt.com/api/auth/signin/openai
  +-- GET signin 返回的 authorize URL，跟随重定向
  |
  +-- 按 authorize 落地 URL 分类；未知时才探测
  |     +-- Sentinel/req(authorize_continue)
  |         -> POST /api/accounts/authorize/continue（只发主 token）
  |
  +-- 新账号（包括探测识别为 passwordless_signup）
  |     +-- 必要时 GET /create-account/password
  |     +-- Sentinel/req(username_password_create)
  |     +-- POST /api/accounts/user/register
  |     +-- 成功后先持久化密码；返回 OTP 状态时再导航发码
  |     +-- passwordless_signup 被明确拒绝时，按响应支持情况请求登录 OTP
  |
  +-- 已有账号：按状态发码；密码步骤优先使用本地密码验证，缺失或明确拒绝时请求登录 OTP
  |
  +-- 后续状态循环：otp / totp / profile / callback（未知、冲突或重复则停止）
        +-- otp：可选 Sentinel/req(email_otp_validate) -> POST email-otp/validate
        +-- totp：POST mfa/issue_challenge -> Sentinel/req(verify_mfa) -> POST mfa/verify
        +-- profile：仅新账号可 Sentinel/req(oauth_create_account) -> POST create_account
        +-- callback：GET continue_url 跟随重定向 -> GET chatgpt.com/api/auth/session
  +-- 可选：查询并绑定 TOTP 2FA
  +-- 保存账号
```

关键实现位置：`main.py:209-229,267-302,308-596`、`core/openai_auth.py:342-522,525-612,641-831,904-1037,1065-1224` 和 `core/account_export.py:192-254`。

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
check_proxy()
  +-- cloudflare.com/cdn-cgi/trace
warmup()
  +-- GET chatgpt.com/api/auth/csrf
get_csrf_token()
  +-- GET chatgpt.com/api/auth/csrf
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

### 2.3 端点顺序差异表

| 阶段 | 当前仓库 | OSS | 差异影响 |
|---|---|---|---|
| 预检 | trace + CSRF warmup + CSRF | `check_proxy()` trace + `warmup()` CSRF + CSRF | 成功路径相近；当前注册要求拿到服务端设备 cookie，OSS 初始化缺少 fingerprint/country 时可能先做 country probe。 |
| OAuth signin | POST `/api/auth/signin/openai` 后跟随 authorize | 相同 | signin 的 Origin、Referer、query 和设备上下文构造不同，见第 3、4 节。 |
| 账号分类 | 优先 landing URL，未知才 `/authorize/continue` | 同样优先 landing URL，未知才探测 | 可分类的新号落点可省掉探测 POST；两边探测失败后的策略不同，当前不会把请求异常当作新号。 |
| 密码页 | landing 已是密码页时跳过 GET | `register_password()` 总会尝试 GET | 可能导致 document ID 和页面状态不同。 |
| 设置密码 | POST `/api/accounts/user/register`，成功后先持久化 | 相同；通过回调尽早保存 | 请求头、Sentinel fallback 和持久化失败处理不同。 |
| 密码后发码 | 成功后只有返回 OTP 状态才导航服务端继续地址 | 新号路径主动 GET `/email-otp/send` | 当前依赖状态解析；OSS 新号路径更固定。 |
| OTP 验证后 | 按响应状态继续，可能进入 TOTP、profile 或 callback | 新号先 GET `client_auth_session_dump`，再 `create_account()` | OSS 新号多一个尽力而为的 dump 请求，且建号顺序固定。 |
| callback | `follow_oauth_callback()` 自动跟随重定向 | `follow_redirect_chain()` 手动逐跳并捕获 callback | OSS 可以保留 callback code，后续再显式消费。 |
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
| 注册预检 CSRF warmup | 无 Referer；这是初始化设备 cookie 的 CORS 请求，不是登录页导航。 |
| `get_csrf_token()` | `https://chatgpt.com/auth/login`。 |
| 注册 signin | `https://chatgpt.com/auth/login_with`，query 动态包含 `callback_path`、`screen_hint`、`login_hint`、`auth_session_logging_id`、`ext-oai-did`；另有 `Origin: https://chatgpt.com`。 |
| authorize | `https://chatgpt.com/`。 |
| `/create-account/password` | 当前落点若以 `https://auth.openai.com/` 开头则使用该 URL，否则使用 `https://auth.openai.com/`。 |
| `/api/accounts/authorize/continue` 状态探测 | `https://auth.openai.com/create-account`。 |
| `/api/accounts/user/register` | `https://auth.openai.com/create-account/password`。 |
| `/api/accounts/email-otp/send` | 注册密码后导航及已有账号首次发码为 `https://auth.openai.com/create-account/password`；登录 follow-send 用 `/log-in/password`，重发导航默认用 `/email-verification`。 |
| `/api/accounts/email-otp/resend` | `https://auth.openai.com/email-verification`。 |
| `/api/accounts/email-otp/validate` | `https://auth.openai.com/email-verification`。 |
| `/api/accounts/create_account` | `https://auth.openai.com/about-you`。 |
| 注册 callback | 初始使用分类落点；经历 OTP、TOTP、profile 后分别更新为 email-verification、mfa、about-you。`follow_oauth_callback()` 的默认值是 about-you。 |
| `/api/auth/session` | `https://chatgpt.com/`。 |

依据：`core/chatgpt_auth.py:77-97,114-171`、`core/session.py:785-871`、`core/openai_auth.py:342-418,455-467,641-808,1004-1018,1136-1160`、`main.py:422-525`、`core/account_export.py:192-240,403-427`。慢速 2FA 的 reauth signin 使用根路径 Referer 和 ChatGPT Origin，不套用注册 signin 的 `/auth/login_with` Referer。

注册 callback 根据 `continue_url` 的前缀选择头：以 `https://chatgpt.com` 开头时使用 ChatGPT 导航头，否则使用 Auth 导航头；后续跳转由 HTTP client 自动跟随。见 `core/account_export.py:209-218`。

### 3.2 OSS

OSS 普通注册的后续来源部分对应当前实现，但 signin 不同，手动 redirect chain 会在每一跳更新 Referer：

- signin：`https://chatgpt.com/auth/login`，不显式添加 Origin；query 中含 `ext-oai-did`。
- OAuth 初始化：`Referer: https://chatgpt.com/`，跨站 `sec-fetch-site=cross-site`。
- 密码页：`https://auth.openai.com/create-account`。
- OTP 验证：`https://auth.openai.com/email-verification`。
- 建号：`https://auth.openai.com/about-you`。
- redirect chain：每次跳转将上一跳 URL 作为下一跳 Referer。
- callback 消费：使用 `https://auth.openai.com/` 作为简化 Referer。

依据：OSS `协议/auth_flow.py:2291-2346,2377-2389,2701-2723,3034-3068,3138-3162,3346-3433,3570-3624`。

重要差异：OSS `send_otp()` 代码虽然注释记录真实文档导航不应携带 Sentinel，但实现仍从实例字段添加 `_last_sentinel_token` 和 `_last_sentinel_so_token`；当前 `navigate_email_otp_send()` 使用纯导航头，不携带 Sentinel。见 OSS `协议/auth_flow.py:2792-2821` 与当前 `core/openai_auth.py:698-712`。

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

见 `core/session.py:723-735, 816-832`。

OSS 将 Auth JSON 头拆成 `_auth_json_headers()`，每次调用生成新的 `x-access-flow-invocation-id`，并挂当前 `x-openai-document-navigation-id`；`_common_headers()` 本身不带这两个一次性头。见 OSS `协议/auth_flow.py:1704-1840`。

### 4.2 Origin

- 当前 `get_auth_headers()` 对 Auth JSON 请求固定添加同源 `Origin: https://auth.openai.com`。注册 `signin_openai()` 使用 NextAuth 头并显式设置 `Origin: https://chatgpt.com`；慢速 2FA 的 reauth signin 也显式设置同一 Origin。
- OSS `_common_headers()` 只有提供的 `target_url` 与 Referer 跨源时才添加 Origin，未提供目标时视为同源；其 signin 未传 `target_url`，不显式添加 Origin。

这是源码层的明确差异，不能仅由 Referer 推断为等价，也不能将当前注册 signin 的行为推广到其他 NextAuth 请求。依据：`core/chatgpt_auth.py:138-160`、`core/account_export.py:415-436`、`core/session.py:785-797,816-832`；OSS `协议/auth_flow.py:1704-1762,2291-2326`。

### 4.3 `oai-device-id`

- 当前 `get_auth_headers()` 本身不添加 `oai-device-id`，但 backend API 的 `get_chatgpt_headers()` 会添加；`get_mfa_enabled()` 还显式设置该头。
- 当前对 `chatgpt.com/backend-api/*`、`backend-anon/*` 和 `ces/*` 自动添加 target path/route 诊断头。
- OSS `_common_headers()` 仅在传入的 `target_url` 为 ChatGPT backend 端点时添加 `oai-device-id`；未提供目标 URL 时不添加。其 2FA 协议 helper 未传目标 URL，不能仅因目标实际是 backend API 就推断该头存在。

依据：当前 `core/session.py:748-783,909-931`、`core/openai_auth.py:1227-1237`；OSS `协议/auth_flow.py:1704-1808`、`协议/webui/two_factor.py:74-93`。

### 4.4 Client hints 与 trace

两边都会从各自的浏览器画像构造 UA、Accept-Language 及 Datadog/W3C trace；Chromium 画像按各自规则添加 client hints，非 Chromium 画像不应一概而论：

- 当前公共头按画像和配置决定 client hints；Auth JSON 每次刷新 Datadog trace ID/parent ID，并以其生成 W3C trace。
- OSS 在 `_common_headers()` 生成指纹与 Datadog/W3C trace；导航头由 `_navigation_headers()` 单独构造。

不能仅根据 Python 函数名判断最终字节完全一致；最终值还受画像、HTTP client 和请求库影响。

## 5. Sentinel 刷新与复用

### 当前仓库

当前注册、已有账号认证和 2FA 慢路径在到达相应操作时获取对应 Sentinel；下表不表示各分支必定执行，注册回退受第 7.1 节的设备限制：

| 操作 | flow | 业务请求与 SO 策略 |
|---|---|---|
| 状态探测 | `authorize_continue` | POST `/api/accounts/authorize/continue`；只发送主 token，不消费、不校验或发送 SO，即使 challenge 声明 SO required。 |
| 密码验证 | `password_verify` | POST `/api/accounts/password/verify`；每次新取主 token，仅在当前 challenge 要求时发送 SO。 |
| MFA challenge | 无 | POST `/api/accounts/mfa/issue_challenge`；不携带主/SO token。 |
| MFA verify | `verify_mfa` | POST `/api/accounts/mfa/verify`；每次新取匹配 flow 的主 token，仅在当前 challenge 要求时发送 SO。 |
| 设置密码 | `username_password_create` | POST `/api/accounts/user/register`；有当前 challenge 的有效 SO 时发送。 |
| OTP 验证 | `email_otp_validate` | POST `/api/accounts/email-otp/validate`；有当前 challenge 的有效 SO 时发送。 |
| 建账号 | `oauth_create_account` | POST `/api/accounts/create_account`；有当前 challenge 的有效 SO 时发送。 |

`request_sentinel_token()` 在同一 SDK 上下文中完成 requirements、challenge 和 solve；`build_sentinel_header()` 始终校验主凭据的 flow、device ID、challenge token 及所需 proof，不接受不同 flow 的主头混用。默认 `consume_so=True` 时，会校验已有 SO 与上下文的匹配，并在服务端要求而 SO 缺失时失败；密码和 MFA verify 进一步只在本次 challenge 声明 required 时发送 SO。唯独 `probe_registration_state()` 显式使用 `consume_so=False`，注册分类和慢速 2FA 的严格探测共享此例外，不因 SO 缺失或无效而在构造业务头时失败，也不发送 SO；这不等于其他操作可忽略 SO 或 SDK/主凭据失败可继续。

注册 OTP 的 `SEND_SENTINEL_ON_EMAIL_OTP_VALIDATE` 默认是 `True`；关闭时 `main.py` 的 OTP 验证可不带 Sentinel。2FA 慢路径若到达邮箱 OTP 验证，则独立获取 `email_otp_validate` 凭据，不受该注册开关控制；其有限重试会复用当前凭据和验证码。注册回退当前未必能到达此步骤，独立 2FA 不直接套用该设备限制。独立 Codex 路径使用 `authorize_continue` flow 时仍按默认策略消费并校验 SO，不受状态探测的例外影响。

密码验证、MFA challenge 和 MFA verify 均使用 Auth JSON 的根路径 Referer、`Origin: https://auth.openai.com`、JSON `Content-Type`、同源 CORS `Sec-Fetch-*` 和独立 `x-access-flow-invocation-id`，并复用当前认证文档的 `x-openai-document-navigation-id`。challenge 的 `Accept` 为 `*/*`，密码验证和 MFA verify 为 `application/json`。Auth Sentinel runner 仅允许受控的 Auth flow；verify MFA 不回退到 `password_verify` 或 `authorize_continue`。

依据：`core/openai_auth.py:525-612,666-678,733-757,791-811,1004-1018,1065-1119`、`main.py:209-222`、`core/account_export.py:499-524,801-823`、`core/codex_oauth.py:823-849`、`core/sentinel_runner.py:70-78,189-225`、`config/openai_protocol.py:44-45`。

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

依据：当前 `core/session.py:167-169,734-736`、`core/openai_auth.py:467-476,641-658,698-726,1136-1160`；OSS `协议/auth_flow.py:2384-2395,2695-2723,2792-2817`。

### 慢速 2FA 重认证

当前注册后慢速 2FA、独立 2FA 与账号查活的 reauth 分支共用 `_follow_reauth_with_retry()`，轮换边界如下：

1. `/log-in` 顶层文档预热只建立/复用 Auth 文档 Cookie，不轮换后续 authorize 文档 ID。
2. 实际 authorize GET 每当返回 HTTP 响应，立即轮换 `document-navigation-id`，再进行状态检查或重试决策；因此 HTTP 错误响应也会推进 ID。
3. authorize GET 在收到响应前发生传输异常时不轮换；重试收到响应后才按同一边界轮换。
4. 慢速 2FA 随后调用 `authorize_continue`；在没有新的文档级导航时，密码验证、邮箱 OTP 验证等 Auth JSON 请求复用最终成功导航的 ID。若服务端指示 GET `/api/accounts/email-otp/send`，该后续文档导航自身仍按既有规则轮换 ID。查活 reauth 使用相同导航 helper，但其后直接验证邮箱 OTP，不经过该状态探测。

这与 OSS `auth_oauth_init()` 在 authorize 响应后的轮换时点一致；源码和对应离线测试的断言只能说明客户端预期，不能证明服务端只接受该序列，尚未用同一账号、同一出口的实时 HTTP trace 验证线上接受结果。

依据：当前 `core/account_export.py:336-400,444-456,751-758`、`core/openai_auth.py:24-27,1004-1018`、`core/account_liveness.py:333-416`；OSS `协议/auth_flow.py:2351-2437`。

## 7. 2FA 绑定流程

### 7.1 当前仓库注册后的 2FA

当 `ENABLE_2FA=False` 时跳过整个注册后 2FA；默认值见 `config/twofa.py:15`。

开启后，`main.py` 的主要分支如下；慢路径的后续步骤是 `setup_2fa()` 的条件性控制流，当前注册回退到探测时的限制见图后说明：

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
        +-- GET auth.openai.com/log-in 预热 -> GET authorize
        +-- 严格 authorize_continue 探测；当前注册回退前置成功时在此因设备 ID 为空终止
        +-- 以下为具备设备上下文的 setup_2fa() 调用可能执行的后续步骤：
              必要时 password/verify、email OTP
              GET callback + GET /api/auth/session，取得新 access_token
              GET mfa_info；已启用则尝试恢复 pending secret
              否则 POST mfa/enroll -> 保存 pending secret -> activate -> GET mfa_info 诊断
```

注册回退调用没有传 `access_token`，因此不执行 `setup_2fa()` 的可选 `authenticated_bootstrap`；缺少已保存密码或 pending secret 持久化报 `CredentialPersistenceError` 时也不会回落慢路径。慢路径状态循环只处理密码、邮箱 OTP 和 callback，不支持 TOTP 等其他状态。activation 返回 HTTP 200 后的 `mfa_info` 仅作诊断；快路径 activation 异常本身不会先复核，普通异常可进入慢路径，只有重认证成功并到达复核时才再查状态、尝试恢复 pending secret。

注册回退还有当前源码的设备上下文限制：新建会话使用 `server_device_id=True`，其 `device_id` 初始为空；该回退没有执行注册预检绑定设备，重认证 helper 也没有赋值。若先前请求未失败，随后 `probe_registration_state()` 请求 Sentinel 时会因设备 ID 为空而失败，因此上图后续步骤不能视为当前注册回退一定能够到达。独立 2FA 后台使用默认设备初始化，不能将此限制直接推广到该入口。离线验证使用真实新会话并模拟前置重认证成功，未经真实账号或网络验证。

慢路径如果取得新 token，会在 enroll 前调用 `on_access_token`：注册回退只更新当前 `run_registration()` 中的变量，最终保存账号时写入；独立后台任务的回调则立即更新数据库。普通绑定失败后注册路径仍尝试保存已取得的登录态；要求启用 2FA 而未满足时任务不报告成功。

依据：`main.py:267-302,527-596,633-640`、`core/session.py:147-157,1048-1060`、`core/openai_auth.py:342-420,525-530,1004-1018`、`core/account_export.py:403-456,629-666,669-856`、`core/twofa_service.py:144-174`。

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

当前实现：`main.py:267-302,527-566`、`core/account_export.py:573-666,832-855`、`core/openai_auth.py:1227-1251`、`core/session.py:748-783,909-931`。

OSS 实现：`协议/webui/two_factor.py:74-199`。

### 7.4 已有账号登录时的 TOTP challenge

2026-11-01 浏览器抓包观察到已有账号链路 `password/verify -> mfa/issue_challenge -> mfa/verify`，MFA verify 返回结构化 `account_deactivated`。当前实现对应为：密码请求每次取 `password_verify` Sentinel；issue 请求不带 Sentinel；verify 请求每次取 `verify_mfa` Sentinel，且只在本次 challenge 要求时附带 SO。issue 和 verify 复用同一 factor ID；账号废弃错误作为不可重试结果结束查活或已有账号认证，不触发 OTP/signup fallback，也不释放已识别的 existing 邮箱。

OSS `login_password_verify()` 与 `submit_mfa_totp()` 使用实例缓存的 Sentinel/SO；TOTP 路径直接请求 `/api/accounts/mfa/verify`，没有对应的 `issue_challenge` 请求。

依据：当前 `core/openai_auth.py:1063-1129, 1216-1224`、`core/account_liveness.py:475-480, 746-749`、`main.py` 已有账号认证异常收尾；OSS `协议/auth_flow.py:2978-3023`。本文记录客户端实现与已提供抓包对照，不把离线测试等同于线上服务接受证明。

## 8. Session 与 token 刷新时机

### 当前仓库

1. 注册 callback 重定向完成后 GET `/api/auth/session`；callback 或 session 失败时收尾可能重试，所以请求并非始终恰好一次。
2. 若慢速 2FA 重认证到达 callback/session，才会再次 GET session 取得新 token；注册回退的设备 ID 限制见第 7.1 节。
3. 注册回退的 `on_access_token` 更新内存中的 token，账号保存时才写入；独立 2FA 后台入口的回调在 enroll 前立即更新数据库。
4. 注册账号最终保存当前 `access_token`。

依据：`main.py:96-135,556-584`、`core/account_export.py:192-254,556-570,832-837`、`core/twofa_service.py:155-174`。

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
- 当前注册 signin 显式添加 ChatGPT Origin 并使用动态 `/auth/login_with` Referer；OSS signin 不显式添加 Origin，Referer 为 `/auth/login`。当前 Auth JSON 固定添加同源 Origin；OSS 按提供的目标与 Referer 判断。
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

- `main.py:96-135,209-229,267-302,308-645`：callback/session 重试、注册 OTP、状态循环和注册后 2FA。
- `core/chatgpt_auth.py:77-97,114-171`：CSRF、signin 和 authorize 上下文。
- `core/openai_auth.py:342-522`：注册预检、authorize 导航。
- `core/openai_auth.py:525-612,641-831`：Sentinel、密码注册、OTP、create_account。
- `core/openai_auth.py:904-1037,1065-1251`：认证状态分类、登录 OTP、MFA challenge、MFA 状态查询。
- `core/sentinel_runner.py:70-78,189-225`：受控 flow 与同一 challenge 主凭据校验。
- `core/session.py:147-168,663-871`：设备与 document ID、通用头、NextAuth、ChatGPT/Auth 导航头。
- `core/session.py:909-931,1048-1060`：target headers 和 GET/POST 封装。
- `core/account_export.py:192-254,336-456`：OAuth callback、session 和重认证导航。
- `core/account_export.py:499-570,573-856`：重认证 OTP、2FA 快/慢路径。
- `core/twofa_service.py:118-205`：独立 2FA 后台任务。
- `config/twofa.py:5-42`：2FA 默认配置和重认证参数。
- `config/openai_protocol.py:44-62`：OTP Sentinel、bootstrap 和网络重试配置。

### OSS

- `协议/auth_flow.py:2291-2647`：signin、authorize 初始化、账号分类。
- `协议/auth_flow.py:2695-2913`：密码注册和 OTP 投递。
- `协议/auth_flow.py:3034-3182`：OTP 验证和 create_account。
- `协议/auth_flow.py:3346-3624`：redirect chain、callback 消费和 session。
- `协议/auth_flow.py:3810-4199`：完整注册主流程与刷新时机。
- `协议/webui/two_factor.py:71-199`：MFA API 快路径。
- `协议/webui/two_factor.py:232-408`：独立登录重认证慢路径。
- `协议/webui/registrar.py:339-375, 529-588`：2FA hook 与 fallback。
- `协议/browser_flow.py:1039-1121`：浏览器引擎的独立 2FA API 路径。
- `协议/_turb_bridge.py:210-250`：Sentinel 复用实验开关。

