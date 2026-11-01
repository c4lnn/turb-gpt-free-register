# 当前注册与 2FA 流程对比 OSS

## 1. 范围与结论

本文对比当前仓库与 `D:/code/turb-gpt-free-register-oss-20260928` 中的协议注册、OAuth 回调和 TOTP 2FA 绑定实现。

对比对象：

- 当前仓库：`main.py`、`core/openai_auth.py`、`core/chatgpt_auth.py`、`core/account_export.py`、`core/session.py`、`core/twofa_service.py`。
- OSS：`vendor/gpt-register-tool/协议/auth_flow.py`、`协议/webui/two_factor.py`、`协议/webui/registrar.py`、`协议/_turb_bridge.py`。

结论：两边主链路相似，但不是逐请求等价。主要差异如下：

1. 当前代码按服务端认证状态推进；OSS 新号路径在验证码后会显式读取 `client_auth_session_dump`，随后直接进入 `create_account()`。
2. OSS 注册完成后会在 2FA hook、可选 OAuth/Codex 交换前后额外刷新 session；当前正常注册路径在 OAuth callback 后主要只取一次 `/api/auth/session`。
3. 当前代码对 Sentinel 凭据按 flow 严格绑定；OSS 保留实例级 Sentinel，并有可选的跨 flow 复用补丁。
4. 两边普通注册的 `document-navigation-id` 轮换点大致对应；当前慢速 2FA 与查活重认证的客户端轮换时点也已按 OSS 对齐，服务端接受情况仍需实时验证。
5. 2FA 快路径调用的四个 backend API 相同，但请求头构造、触发位置和失败收尾不同。

本文是源码静态对照，不是新的真实网络抓包。实际请求数量会受服务端分支、重试、workspace 跳转和配置开关影响。

本文只修改当前仓库的实现、测试和对比记录；`D:/code/turb-gpt-free-register-oss-20260928` 是只读对照来源，不在本变更编辑范围。

## 2. 注册主流程

### 2.1 当前仓库

当前 `main.run_registration()` 的典型顺序：

```text
创建 BrowserSession(server_device_id=True)
  |
  +-- cloudflare.com/cdn-cgi/trace
  +-- chatgpt.com/api/auth/csrf       # 注册 warmup，绑定服务端 oai-did
  +-- chatgpt.com/api/auth/csrf       # get_csrf_token
  +-- POST chatgpt.com/api/auth/signin/openai
  +-- GET auth.openai.com authorize 重定向链
  |
  +-- 按 authorize 落地 URL 分类
  |     |
  |     +-- 无法分类时：Sentinel/req(authorize_continue)
  |     |                 -> POST /api/accounts/authorize/continue
  |     |
  |     +-- 新账号：GET /create-account/password（必要时）
  |     |             -> Sentinel/req(username_password_create)
  |     |             -> POST /api/accounts/user/register
  |     |
  |     +-- 已有账号：按状态发送或重发 OTP
  |
  +-- 必要时 GET /api/accounts/email-otp/send 或 POST /api/accounts/email-otp/resend
  +-- Sentinel/req(email_otp_validate)
  +-- POST /api/accounts/email-otp/validate
  |
  +-- 服务端状态循环：password / otp / totp / profile / callback
  |     |
  |     +-- profile：Sentinel/req(oauth_create_account)
  |                  -> POST /api/accounts/create_account
  |
  +-- GET continue_url，跟随 OAuth callback 重定向
  +-- GET chatgpt.com/api/auth/session
  +-- 可选：查询并绑定 TOTP 2FA
  +-- 保存账号
```

关键实现位置：

- 主流程：`main.py:307-525`
- 预检和 authorize：`core/openai_auth.py:333-513`
- 注册密码、发码、验码、建号：`core/openai_auth.py:628-818`
- OAuth callback/session：`core/account_export.py:192-254`

当前注册的特点：

- `authorize` 落地 URL 可直接判定时，不发送 `authorize_continue`；只有落地未知才探测。
- `user/register` 返回后，根据 `continue_url` 和认证步骤决定是否 GET `/email-otp/send`。
- 验证 OTP 后不假设固定的 profile/callback 顺序，而是循环识别服务端返回的 step。
- `create_account` 成功后直接汇合到 callback/session 流程。

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
  +-- 必要时 resend 或 send OTP
  +-- POST /api/accounts/email-otp/validate
  +-- 必要时 about-you/create_account

共同收尾：
  +-- 手动跟踪 redirect chain，捕获 callback
  +-- 消费 callback，设置 NextAuth session cookie
  +-- GET chatgpt.com/api/auth/session
  +-- 2FA hook（如果启用）
  +-- GET client_auth_session_dump
  +-- 可选 OAuth/Codex exchange
  +-- 再 GET chatgpt.com/api/auth/session
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
| 预检 | trace + CSRF warmup + CSRF | `check_proxy()` trace + `warmup()` CSRF + CSRF | 基本等价；OSS 初始化缺少 fingerprint/country 时可能先做 country probe。 |
| OAuth signin | POST `/api/auth/signin/openai` 后跟随 authorize | 相同 | query 参数和设备上下文构造不同。 |
| 账号分类 | 优先 landing URL，未知才 `/authorize/continue` | 同样优先 landing URL，未知才探测 | 正常新号都可省掉探测 POST。 |
| 密码页 | landing 已是密码页时可能跳过 GET | `register_password()` 总会尝试 GET | 可能导致 document ID 和页面状态不同。 |
| 设置密码 | POST `/api/accounts/user/register` | 相同 | 请求头、Sentinel fallback 不同。 |
| 密码后发码 | 使用服务端返回的 continue 状态决定 GET `/email-otp/send` | 新号路径主动 GET `/email-otp/send` | 当前更依赖状态解析；OSS 新号路径更固定。 |
| OTP 验证后 | 直接按认证 step 进入 profile/callback | 先 GET `client_auth_session_dump`，再 `create_account()` | OSS 多一个 dump 请求，且顺序固定在建号前。 |
| callback | `follow_oauth_callback()` 自动跟随重定向 | `follow_redirect_chain()` 手动逐跳并捕获 callback | OSS 可以保留 callback code，后续再显式消费。 |
| session | callback 后 GET `/api/auth/session` | 先消费 callback，再 GET session；后续可能再 GET | OSS session 刷新次数更多。 |

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
| ChatGPT 登录/预检 | 地址栏式导航，通常无 Referer。 |
| authorize | `https://chatgpt.com/`。 |
| `/create-account/password` | 当前 landing URL；否则 `https://auth.openai.com/`。 |
| `/api/accounts/user/register` | `https://auth.openai.com/create-account/password`。 |
| `/api/accounts/email-otp/send` | `https://auth.openai.com/create-account/password`。 |
| `/api/accounts/email-otp/validate` | `https://auth.openai.com/email-verification`。 |
| `/api/accounts/create_account` | `https://auth.openai.com/about-you`。 |
| callback | 默认 `https://auth.openai.com/about-you`，认证状态改变时由调用方传入 email-verification、mfa 或 about-you。 |
| `/api/auth/session` | `https://chatgpt.com/`。 |

依据：`core/session.py:785-871`、`core/openai_auth.py:446-699, 738-818`、`core/account_export.py:192-240`。

当前 callback 会根据目标 host 选择头：如果 `continue_url` 已是 `chatgpt.com`，使用 ChatGPT 导航头；否则使用 Auth 导航头。见 `core/account_export.py:209-218`。

### 3.2 OSS

OSS 普通注册的来源大致对应当前实现，但手动 redirect chain 会在每一跳更新 Referer：

- OAuth 初始化：`Referer: https://chatgpt.com/`，跨站 `sec-fetch-site=cross-site`。
- 密码页：`https://auth.openai.com/create-account`。
- OTP 验证：`https://auth.openai.com/email-verification`。
- 建号：`https://auth.openai.com/about-you`。
- redirect chain：每次跳转将上一跳 URL 作为下一跳 Referer。
- callback 消费：使用 `https://auth.openai.com/` 作为简化 Referer。

依据：OSS `协议/auth_flow.py:2377-2389, 2701-2723, 3034-3068, 3138-3162, 3346-3433, 3570-3624`。

重要差异：OSS `send_otp()` 代码虽然注释记录真实文档导航不应携带 Sentinel，但实现仍从实例字段添加 `_last_sentinel_token` 和 `_last_sentinel_so_token`；当前 `navigate_email_otp_send()` 使用纯导航头，不携带 Sentinel。见 OSS `协议/auth_flow.py:2792-2821` 与当前 `core/openai_auth.py:685-699`。

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

- 当前 `get_auth_headers()` 对 Auth JSON 请求固定添加同源 `Origin: https://auth.openai.com`；signin 单独移除 Origin。
- OSS `_common_headers()` 按目标和 Referer 判断，认为同源请求不添加 Origin；只有跨源时才添加。

这是源码层的明确差异，不能仅由 Referer 推断为等价。

### 4.3 `oai-device-id`

- 当前 `get_auth_headers()` 本身不添加 `oai-device-id`，但 backend API 的 `get_chatgpt_headers()` 会添加；`get_mfa_enabled()` 还显式设置该头。
- 当前对 `chatgpt.com/backend-api/*` 和 `backend-anon/*` 自动添加 target path/route 诊断头。
- OSS `_common_headers()` 根据 `target_url` 决定是否添加 `oai-device-id`；Auth API 调用通过目标 URL 判定为不添加，ChatGPT backend 端点才添加。

依据：当前 `core/session.py:748-783, 909-931`、`core/openai_auth.py:1208-1218`；OSS `协议/auth_flow.py:1779-1808`。

### 4.4 Client hints 与 trace

两边都会从各自的浏览器画像构造 UA、Accept-Language、Chromium client hints 和 Datadog/W3C trace 头，但实现边界不同：

- 当前由 `BrowserSession` 统一注入；Auth JSON 每次刷新 Datadog trace ID/parent ID。
- OSS 在 `_common_headers()` 生成指纹头，并在每次调用中生成 Datadog trace；导航头由 `_navigation_headers()` 单独构造。

不能仅根据 Python 函数名判断最终字节完全一致；最终值还受画像、HTTP client 和请求库影响。

## 5. Sentinel 刷新与复用

### 当前仓库

当前按敏感操作单独获取 Sentinel：

| 状态探测 | `authorize_continue` | POST `/api/accounts/authorize/continue`。 |
| 密码验证 | `password_verify` | POST `/api/accounts/password/verify`，每次新取主 token，可带当前 challenge 要求的 SO。 |
| MFA challenge | 无 Sentinel | POST `/api/accounts/mfa/issue_challenge`，不携带主/SO token。 |
| MFA verify | `verify_mfa` | POST `/api/accounts/mfa/verify`，每次新取匹配 flow 的主 token；SO 仅在当前 challenge 要求时发送。 |
| 设置密码 | `username_password_create` | POST `/api/accounts/user/register`。 |
| OTP 验证 | `email_otp_validate` | POST `/api/accounts/email-otp/validate`，可带当前 challenge 的 SO。 |
| 建账号 | `oauth_create_account` | POST `/api/accounts/create_account`。 |

密码验证、MFA challenge 和 MFA verify 均使用 Auth JSON 的根路径 `Referer: https://auth.openai.com/`、`Origin: https://auth.openai.com`、JSON `Content-Type`、同源 CORS `Sec-Fetch-*`、独立 `x-access-flow-invocation-id`，并复用当前认证文档的 `x-openai-document-navigation-id`。challenge 的 `Accept` 为 `*/*`，密码验证和 MFA verify 的 `Accept` 为 `application/json`。

`build_sentinel_header()` 会校验 flow、device ID、主 token 内容和服务端声明的 SO 要求；不接受不同 flow 的头混用。Auth Sentinel runner 只允许仓库内受控的 Auth flow，verify MFA 不回退到 `password_verify` 或 `authorize_continue`。见 `core/sentinel_runner.py`、`core/openai_auth.py`。

当前 `SEND_SENTINEL_ON_EMAIL_OTP_VALIDATE=True`，见 `config/openai_protocol.py:44-45`。

### OSS

OSS 正常实现也会在密码注册、OTP 验证和 `create_account` 前刷新 token，但存在两个降级层：

1. Sentinel 获取失败时，部分路径会继续使用实例上一次的 token。
2. OTP 验证缺 SO 时记录 warning 后仍继续提交，而不是立即终止。

已有账号重认证另有差异：`login_password_verify()` 和 `submit_mfa_totp()` 直接读取实例缓存的主/SO token，不为 `password_verify` 或 `verify_mfa` 单独刷新 flow；TOTP 路径直接 POST `/api/accounts/mfa/verify`，没有 `mfa/issue_challenge` 请求。

此外，`协议/_turb_bridge.py` 提供 `TURB_SENTINEL_REUSE` 实验开关。默认关闭；开启后，`email_otp_validate` 和 `oauth_create_account` 可以直接复用上一个 token。该行为可能因服务端按 flow 校验而返回 400/409。

依据：OSS `协议/auth_flow.py:2725-2767, 2978-3023, 3034-3109, 3121-3149`；OSS `协议/_turb_bridge.py:210-250`。

## 6. `document-navigation-id` 刷新时机

### 普通注册

普通注册与 OSS 在 authorize 文档响应后的轮换边界现已一致；导航响应返回后轮换，随后才处理响应状态或落点。收到响应前的传输异常不轮换。

当前主要轮换点：

1. `BrowserSession` 初始化时生成初始 ID。
2. 每次 authorize GET 返回 HTTP 响应后，立即轮换，再进行状态检查、错误处理或重试。传输异常在收到响应前发生时不轮换；`follow_authorize()` 也供账号查活 full-web fallback 使用。
3. 仅当当前落点不是 `/create-account/password` 时才 GET 密码页；成功状态检查后轮换。已落在密码页时跳过 GET 和轮换；密码页 GET 返回错误状态时，状态检查先抛出，因此不轮换。
4. GET `/api/accounts/email-otp/send` 或登录 OTP follow-send 前轮换；因此导航请求失败前也可能已轮换。
5. Auth JSON 请求使用 session 当前 ID；同一文档内不因 JSON 请求或 SPA 路由切换而轮换。

OSS 主要轮换点：

1. `AuthFlow` 初始化时生成初始 ID。
2. `auth_oauth_init()` 收到 authorize HTTP 响应后轮换，再保存落地 URL；请求在响应前发生传输异常时不会执行该轮换。
3. `register_password()` 总会尝试 GET `/create-account/password`，并在尝试结束后轮换；即使 GET 抛出异常也会轮换。
4. `send_otp()` 在 GET `/api/accounts/email-otp/send` 前轮换。
5. OTP validate 与 `create_account` 共用之后的 ID。

authorize 响应边界对齐不改变密码页分支差异：当前落点已是密码页时会跳过密码页 GET，而 OSS 仍会尝试该 GET，因此这类分支的总轮换次数及 `user/register` 使用的 ID 仍可能不同。密码页与 OTP 导航策略不在本次变更范围内。

依据：当前 `core/session.py:167-169,734-736`、`core/openai_auth.py:465-482,628-699`；OSS `协议/auth_flow.py:2384-2395,2695-2723,2792-2817`。

### 慢速 2FA 重认证

当前独立 2FA 与账号查活共用 `_follow_reauth_with_retry()`，轮换边界如下：

1. `/log-in` 顶层文档预热只建立/复用 Auth 文档 Cookie，不轮换后续 authorize 文档 ID。
2. 实际 authorize GET 每当返回 HTTP 响应，立即轮换 `document-navigation-id`，再进行状态检查或重试决策；因此 HTTP 错误响应也会推进 ID。
3. authorize GET 在收到响应前发生传输异常时不轮换；重试收到响应后才按同一边界轮换。
4. 最终成功导航的 ID 用于后续 `authorize_continue`；在没有新的文档级导航时，密码验证、邮箱 OTP 验证等 Auth JSON 请求复用当前 ID。若服务端指示 GET `/api/accounts/email-otp/send`，该后续文档导航自身仍按既有规则轮换 ID。

这与 OSS `auth_oauth_init()` 在 authorize 响应后的轮换时点一致。以上是客户端源码和确定性测试确认的请求序列，不等于服务端只接受该序列；尚未用同一账号、同一出口的实时 HTTP trace 验证线上接受结果。

依据：当前 `core/account_export.py:444-456`、`core/openai_auth.py:24-27,991-1028`；OSS `协议/auth_flow.py:2351-2437`。

## 7. 2FA 绑定流程

### 7.1 当前仓库注册后的 2FA

当 `ENABLE_2FA=False` 时跳过整个注册后 2FA；默认值见 `config/twofa.py:15`。

开启后，`main.py` 按以下顺序处理：

```text
已有 access_token
  |
  +-- GET /backend-api/accounts/mfa_info
  |
  +-- 已启用且本地有 secret：恢复 pending secret，跳过 enroll
  |
  +-- 未确认或未启用：setup_2fa_inline()
  |     +-- POST /backend-api/accounts/mfa/enroll
  |     +-- 保存 pending secret
  |     +-- POST /backend-api/accounts/mfa/user/activate_enrollment
  |     +-- GET /backend-api/accounts/mfa_info
  |
  +-- 快路径失败：新建 BrowserSession，走 setup_2fa() 慢路径
        +-- 可选 authenticated_bootstrap(access_token)
        +-- GET /api/auth/csrf
        +-- POST /api/auth/signin/openai?reauth=password&max_age=0
        +-- GET auth.openai.com authorize
        +-- authorize_continue 探测
        +-- 必要时 password/verify
        +-- 必要时 email OTP
        +-- GET callback + GET /api/auth/session，取得新 access_token
        +-- POST mfa/enroll
        +-- POST mfa/user/activate_enrollment
        +-- GET mfa_info
```

依据：`main.py:527-565`、`main.py:266-301`、`core/account_export.py:714-858`。

慢路径会通过 `on_access_token` 将重认证得到的新 access token 写回账号；这发生在 enroll 前。见 `core/account_export.py:834-840` 和 `core/twofa_service.py:158-174`。

### 7.2 OSS 2FA

OSS 由 `want_2fa` 控制。协议引擎正常情况下在拿到 access token、尚未进行 Codex 授权时执行 hook：

```text
GET  /backend-api/accounts/mfa_info
POST /backend-api/accounts/mfa/enroll
POST /backend-api/accounts/mfa/user/activate_enrollment
GET  /backend-api/accounts/mfa_info
```

快路径失败后，OSS fallback 新建独立 `AuthFlow`，重走 login 正式链；慢路径成功消费 callback、取得新的 access token 后再执行同一组 MFA API。见 OSS `协议/webui/registrar.py:339-375, 529-588`、`协议/webui/two_factor.py:113-199, 232-408`。

OSS 的浏览器引擎另有 `_bind_2fa_via_api()`，直接使用 Playwright API context 的 cookie 和 access token；这条路径不应与协议引擎的 `_common_headers()` 结果混为一谈。见 OSS `协议/browser_flow.py:1039-1121`。

### 7.3 快路径 API 对照

| 请求 | 当前仓库 | OSS |
|---|---|---|
| 查询状态 | GET `/backend-api/accounts/mfa_info` | 相同 |
| 申请 TOTP | POST `/backend-api/accounts/mfa/enroll`，JSON `{"factor_type":"totp"}` | 相同 |
| 激活 | POST `/backend-api/accounts/mfa/user/activate_enrollment` | 相同 |
| 复核 | GET `/backend-api/accounts/mfa_info` | 相同 |
| Authorization | Bearer access token | Bearer access token |
| device/language | 当前显式带 `oai-device-id`、`oai-language` | 协议 helper 依 `_common_headers()` 和调用参数；浏览器引擎使用简单头 |
| target headers | 当前 backend API 自动补 target path/route | 协议 helper 未显式传 `target_url`，不一定补同样字段 |
| Referer | 通常 `https://chatgpt.com/` | 使用具体 endpoint URL |
| 失败后复核 | 当前 `_verify_mfa_after_activation()` 只做状态诊断 | OSS 对 activate 非 200 会等待后检查 factor ID；activate 200 后等待再复核 |

当前实现：`core/account_export.py:570-663`、`core/openai_auth.py:1208-1232`、`core/session.py:748-783, 909-931`。

OSS 实现：`协议/webui/two_factor.py:74-199`。

### 7.4 已有账号登录时的 TOTP challenge

2026-11-01 浏览器抓包观察到已有账号链路 `password/verify -> mfa/issue_challenge -> mfa/verify`，MFA verify 返回结构化 `account_deactivated`。当前实现对应为：密码请求每次取 `password_verify` Sentinel；issue 请求不带 Sentinel；verify 请求每次取 `verify_mfa` Sentinel，且只在本次 challenge 要求时附带 SO。issue 和 verify 复用同一 factor ID；账号废弃错误作为不可重试结果结束查活或已有账号认证，不触发 OTP/signup fallback，也不释放已识别的 existing 邮箱。

OSS `login_password_verify()` 与 `submit_mfa_totp()` 使用实例缓存的 Sentinel/SO；TOTP 路径直接请求 `/api/accounts/mfa/verify`，没有对应的 `issue_challenge` 请求。

依据：当前 `core/openai_auth.py:1063-1129, 1216-1224`、`core/account_liveness.py:475-480, 746-749`、`main.py` 已有账号认证异常收尾；OSS `协议/auth_flow.py:2978-3023`。本文记录客户端实现与已提供抓包对照，不把离线测试等同于线上服务接受证明。

## 8. Session 与 token 刷新时机

### 当前仓库

1. 注册 callback 重定向完成后立即 GET `/api/auth/session`。
2. 如果 2FA 慢路径完成 reauth callback，再次 GET `/api/auth/session` 取得新 token。
3. 新 token 通过回调更新数据库。
4. 注册账号最终保存当前 `access_token`。

当前 callback/session 实现见 `core/account_export.py:192-254, 553-567`。

### OSS

1. `follow_redirect_chain()` 只捕获 callback，不主动消费 callback code。
2. `_consume_callback_for_session()` 主动 GET callback，使 NextAuth 设置 session cookie。
3. `get_auth_session()` 获取 access token。
4. 如果启用 2FA hook，在 hook 中使用当前 access token。
5. hook 后读取 `client_auth_session_dump`。
6. 可选执行 OAuth/Codex token exchange。
7. 如果发生 exchange，再次 GET `/api/auth/session`，以覆盖可能更新的 cookie/access token。

依据：OSS `协议/auth_flow.py:3346-3433, 3522-3624, 4116-4190`。

因此，OSS 的刷新策略更偏向“在后续 OAuth/2FA 操作前后反复确认登录态”；当前注册路径更偏向“callback 后取得一个可用 access token，必要时仅在慢速 2FA 重认证后刷新”。

## 9. 已确认差异与未确认项

### 已确认的源码差异

- OSS 新号 OTP 验证后有 `client_auth_session_dump`；当前注册主链没有对应调用。
- OSS 主流程在 callback/session、2FA hook、可选 OAuth exchange 后有额外 session 刷新；当前主流程没有同等数量的刷新。
- 当前 Auth JSON 头固定添加同源 Origin；OSS 按跨源条件添加。
- 当前发码导航 GET 不带 Sentinel；OSS `send_otp()` 会从实例字段带缓存 Sentinel。
- OSS 支持默认关闭的 Sentinel 跨 flow 复用实验；当前没有同名机制。
- 当前已有账号 TOTP 验证包含 `mfa/issue_challenge`；OSS 直接调用 `mfa/verify`。
- 当前 2FA 默认关闭；OSS 由 `want_2fa` 运行选项控制。

### 仍需实时抓包才能确认的内容

- 同一分支下两边最终发出的完整 header 字节是否一致；尤其是 HTTP client 自动补充的大小写、Cookie、Accept-Encoding 和 TLS 层行为。
- 慢速 2FA 重认证中服务端是否接受当前客户端按 authorize HTTP 响应边界生成的 document-navigation-id 序列，以及是否存在 HTTP client 未观察到的隐藏导航。
- 服务端在当前代码的状态循环中是否始终返回与 OSS 固定顺序一致的 `continue_url`。
- 启用 Sentinel 复用后，服务端是否按 flow 拒绝 OSS 的复用 token。
- callback 重定向链的实际 hop 数及是否进入 workspace/select。

## 10. 源码索引

### 当前仓库

- `main.py:307-645`：注册主流程、状态循环、注册后 2FA。
- `core/chatgpt_auth.py:77-162`：CSRF、signin、authorize 上下文。
- `core/openai_auth.py:333-513`：预检、authorize 导航。
- `core/openai_auth.py:516-818`：Sentinel、密码注册、OTP、create_account。
- `core/openai_auth.py:891-1232`：认证状态分类、登录 OTP、MFA challenge、MFA 状态查询。
- `core/session.py:663-871`：通用头、NextAuth、ChatGPT/Auth 导航头。
- `core/session.py:909-1060`：target headers、熔断、GET/POST 封装。
- `core/account_export.py:192-254`：OAuth callback 和 session。
- `core/account_export.py:666-858`：2FA 快/慢路径。
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

