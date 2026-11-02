# OSS 实现与两份独立 HAR 对照

## 范围与证据边界

本文以当前仓库同级的 `../turb-gpt-free-register-oss-20260928` 为固定 OSS 只读快照，分别将其中 `vendor/gpt-register-tool` 的协议实现与两份 HAR 静态对照；源码索引的路径均相对于该快照根目录，不包含真实账号 smoke。两份 HAR 是独立捕获，不能拼接为同一账号、同一 session 或一条连续请求链。本文不输出邮箱、密码、OTP、token、secret、session ID、cookie 值或个人标识。

| 捕获 | 入口及授权证据 | 记录到的结果 |
|---|---|---|
| `har/passwordless2about-you.har` | 捕获 signin POST 和响应中的 `/oauth/authorize` URL；未捕获 `GET /oauth/authorize`。 | passwordless OTP → `user/register` → OTP 验证响应指向 `/about-you`；没有捕获资料创建、callback/session 或 2FA。 |
| `har/11.1-密码注册-快路径设置2fa.har` | 认证相关轨迹从 `GET /create-account` 开始，之后捕获 `POST /api/accounts/authorize/continue`；未捕获 OAuth authorize GET。 | 密码注册/OTP → 资料创建 → callback/session → 注册后 TOTP。 |

HAR 的缺项只代表捕获记录中没有对应请求，不表示捕获窗口外绝未发生。两个 HAR 的共同 `/about-you` 响应仅是可比结果，不是连续轨迹证据。下文 `#N` 为各自 HAR 中 `log.entries` 的从零开始索引。

## OSS 源码中的通常注册路径

OSS 快照中 `vendor/gpt-register-tool/协议/auth_flow.py::run_register()` 的通常链路为：warmup/CSRF → `get_auth_url()` POST signin → 补充授权上下文及可选 PKCE 后由 `auth_oauth_init()` GET authorize URL → 按 authorize 落地分类 → 新号设置密码并 POST `user/register` → GET `email-otp/send` → POST `email-otp/validate` → `client_auth_session_dump` → POST `create_account` → redirect/callback/session。未提供落地 URL 或无法分类时，`signup()` 才回退调用 POST `/api/accounts/authorize/continue`。

注册器在 `want_2fa` 开启时为协议引擎挂接 session-ready hook，在取得 access token 后尝试绑定；若尚未取得绑定结果，后续还会尝试快路径，并在有密码时回退到重新登录的慢路径。

这只是源码路径，不代表两份 HAR 中的请求都属于同一轨迹或每轮都会走完所有分支。错误处理、已有账号路径、配置开关和 OAuth/Codex 后续操作会改变实际请求数量。

## `passwordless2about-you.har` 对照

### HAR 与 OSS 的端点顺序

| 阶段 | HAR 证据 | OSS 源码行为 | 差异 |
|---|---|---|---|
| signin | `#12`：`POST /api/auth/signin/openai` 为 200；URL query 字段为 `prompt`、`screen_hint`、`auth_session_logging_id`、`login_hint`，`Referer` 是 `/auth/login_with` 且包含 `callback_path`、`screen_hint`、`login_hint`、`auth_session_logging_id`、`ext-oai-did`，有 `Origin`；响应 `url` 指向 `/oauth/authorize`。 | `get_auth_url()` 使用 `/auth/login` Referer、同源不发送 Origin；URL query 包含 `prompt`、`ext-oai-did`、`auth_session_logging_id`、`screen_hint`、`login_hint`，并处理返回的 authorize URL。 | Referer route 与 Origin presence 不同；device ID 在 OSS signin URL query、而在 HAR 的 Referer query。HAR 只记录返回的 URL，未捕获对应 `GET /oauth/authorize`。 |
| authorize GET | 捕获中没有 `GET /oauth/authorize`。 | `run_register()` 通常调用 `auth_oauth_init()`，跟随处理后的 authorize URL 发起 GET，并按最终落地 URL 分类；未知落点才发 authorize/continue 探测 POST。 | 无法从这份 HAR 验证 OSS authorize GET 或落地分类的表现。 |
| 发起 OTP | `#18`：`POST /api/accounts/passwordless/send-otp` 为 200，响应为 `email_otp_verification`，继续地址 `/email-verification`。 | OSS 有 `send_passwordless_otp()`，但典型成功的新号路径先 `register_password()`，成功后再 GET `/api/accounts/email-otp/send`。`send_passwordless_otp()` 可由 OTP 投递 fallback 调用；其存在不表示通常成功路径会先执行它。 | HAR 的 passwordless POST 位于 `user/register` 之前；OSS 通常成功路径的设置密码 POST 在前，之后使用 GET `email-otp/send`。 |
| 设置密码 | `#45`：`POST /api/accounts/user/register` 为 200，body 字段 `username`、`password`；响应指向 `email-otp/send`。 | `register_password()` 尝试 GET `/create-account/password` 后 POST `user/register`；成功后 `run_register()` GET `/api/accounts/email-otp/send`。 | HAR 记录的 passwordless 发码顺序不同；两边后续都存在密码注册及 OTP 端点。 |
| OTP 验证 | `#70`：`POST /api/accounts/email-otp/validate` 为 200，响应 `page.type=about_you`、继续地址 `/about-you`。 | `verify_otp()` POST 验码；新号通常在验证后 GET `client_auth_session_dump`，再 `create_account()`。 | HAR 记录的验证响应结果可比较；本 HAR 没有记录后续 dump、create_account 或 callback。 |

HAR 中 `auth.openai.com` 上的三个相关 POST（`passwordless/send-otp`、`user/register`、`email-otp/validate`）Referer 均为 `https://auth.openai.com/`；signin POST 的 Referer 则为 `https://chatgpt.com/auth/login_with`。OSS 的设置密码、OTP 验证等代码使用相应密码页/验证页 Referer。这里只比较源码明示的 Referer，不据此推断服务端是否要求某一种值。

## `11.1-密码注册-快路径设置2fa.har` 对照

### 注册入口与 authorize 相关请求

| 阶段 | HAR 证据 | OSS 源码行为 | 差异 |
|---|---|---|---|
| 入口 | 捕获中认证相关轨迹从 `#1` 的 `GET auth.openai.com/create-account` 开始。 | `run_register()` 通常从 warmup、CSRF 和 ChatGPT signin 开始。 | HAR 未捕获 OSS 通常流程的前置 signin。 |
| 邮箱/状态提交 | `#125`：`POST /api/accounts/authorize/continue` 为 200，body 字段 `screen_hint`、`username`，响应为 email OTP 验证状态。 | `signup()` 优先按 authorize 落地 URL 分类；只有落地未知时才调用 authorize/continue fallback，并为该探测惰性尝试获取 Sentinel token。 | 两边出现同一 endpoint，但 HAR 将它记录为本次入口后的实际 POST；OSS 正常可分类路径会跳过该 POST。这个 POST 也不是 `GET /oauth/authorize`。 |
| 密码和 OTP | `#153` 的 `user/register` 后 GET `email-otp/send`（`#157`、`#158` 先后为 307、302，最终转至 `/email-verification`），然后 `#257` validate。 | OSS 通常先尝试 GET 密码页、POST `user/register`，然后 GET `email-otp/send`、POST validate。 | 主要端点顺序相近；HAR 的捕获窗口没有展示 OSS 通常的 signin/authorize 前置链。 |
| 验证后建号 | `#257` validate 返回 `about_you`；之后 `#292` POST `create_account`。 | validate 后通常额外 GET `client_auth_session_dump`，再 POST `create_account`。 | HAR 本次关注轨迹中未记录 `client_auth_session_dump`；源码中的额外请求不能被反向写入 HAR 证据。 |
| callback/session | `#292` 的 `create_account` 返回 callback 继续地址；随后 `#294` GET ChatGPT callback（302）和 `#471` GET `/api/auth/session`（200）。 | OSS 手动跟随 redirect chain，在非 refresh-only 模式下若取得 callback 则尝试显式消费，以建立 NextAuth session cookie，然后 GET `/api/auth/session`。若有 callback 或 continue URL 且非 refresh-only，在后续可选 OAuth/Codex 分支之后会再次 GET session，即使没有执行交换也是如此。 | 两边均有 callback/session，但 redirect 跟随与额外刷新策略不同。HAR 捕获的一次 callback/session 不证明 OSS 的可选后续路径。 |

HAR 记录的 `email-otp/validate` 响应同样指向 `/about-you`。与前一 HAR 不同，本 HAR 随后继续 `create_account` 和 callback/session；该差异只描述两份独立捕获的覆盖范围，不将其解释为同一账号的两个阶段。

### 注册后 TOTP

HAR 记录的成功链为 `mfa_info -> enroll -> activate_enrollment -> mfa_info -> factor_decision -> mfa_info -> mfa_info`，对应 `#592`、`#609`、`#618`、`#619`、`#621`、`#701`、`#733`。初次状态为未启用；enroll/activate body 有 `source=settings`；activation 成功后状态为启用；`factor_decision` 的 `entrypoint` 为 `post_totp`。每个相关 MFA 请求同时列出 Bearer Authorization、Cookie 和 33 个 cookie entries，并带有 Codex Browser/Webview 标识。敏感字段值不记录。

OSS `vendor/gpt-register-tool/协议/webui/two_factor.py::_enroll_and_activate()` 的协议引擎成功路径为：先查询 `mfa_info`，若成功确认已绑定 TOTP 则跳过；再 POST enroll、POST activate；随后查询一次 `mfa_info`。enroll body 为 `factor_type=totp`，activate body 使用 TOTP code、factor type 和 enrollment session，不包含 `source=settings`。该函数单次执行不调用 `factor_decision`，成功激活后只查询一次状态；若该次复核响应非 200 或解析后显示未启用，则仅告警、不改变返回结果。activation 非 200 时会额外查询状态并按 factor ID 判断是否已成功启用。

OSS 明确为请求设置 Bearer token；协议引擎复用其 session/CookieJar，但源码并不固定 HAR 的 33 个 cookie entries，也不构造 Codex Webview 专属头。单份 HAR 只能证明认证材料与成功请求同时出现，不能证明它们单独必要或适用于其它客户端。OSS 2FA hook 在注册器启用 `want_2fa` 且已取得 access token 时触发；注册器后续 fallback 可能产生额外请求，不能把上述函数的一次成功调用当作整个注册过程的请求总数。

## 结论与不确定项

- `passwordless2about-you.har` 与 OSS 的 signin header 和成功注册 OTP 顺序存在可见差异；特别是 HAR 中的 passwordless POST 不能与 OSS 通常成功分支中的 GET `email-otp/send` 混为同一请求。
- `11.1-密码注册-快路径设置2fa.har` 的 `POST /authorize/continue` 与 OSS 的未知落点 fallback 使用同一 endpoint，但该 endpoint 不是 OAuth authorize GET；HAR 的认证相关轨迹从 `/create-account` 开始，未捕获 OSS 通常的 signin/OAuth GET 链。
- 两份 HAR 都有 OTP 验证返回 `about_you` 的证据；只有第二份记录了后续 `create_account`、callback/session 和 2FA。共同页面结果不构成共用 session 的证据。
- 本文记录的是 OSS 静态实现与 HAR 的请求差异，不证明线上服务接受或拒绝任何差异。`source=settings`、`factor_decision`、额外状态查询、Codex Webview 头及 HAR cookie 的因果必要性均未由这些捕获单独证明。

## 源码索引

OSS 只读对照文件：

- `vendor/gpt-register-tool/协议/auth_flow.py:2291-2428`：signin、authorize GET 与落地 URL。
- `vendor/gpt-register-tool/协议/auth_flow.py:2471-2537`：authorize/continue 探测与 Sentinel token。
- `vendor/gpt-register-tool/协议/auth_flow.py:2581-2693`：注册状态分类及 authorize/continue fallback。
- `vendor/gpt-register-tool/协议/auth_flow.py:2695-2913`：设置密码、OTP 投递和 fallback。
- `vendor/gpt-register-tool/协议/auth_flow.py:3034-3182`：OTP 验证与 `create_account`。
- `vendor/gpt-register-tool/协议/auth_flow.py:3347-3410,3522-3624`：重定向跟随、session 获取和 callback 消费。
- `vendor/gpt-register-tool/协议/auth_flow.py:3810-4199`：注册主流程、callback/session 和 2FA hook。
- `vendor/gpt-register-tool/协议/webui/registrar.py:339-354,425-430,530-564`：2FA hook 的条件与后续 fallback。
- `vendor/gpt-register-tool/协议/webui/two_factor.py:71-208`：注册后 MFA API。
