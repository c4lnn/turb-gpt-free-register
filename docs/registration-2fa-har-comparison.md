# 当前注册实现与两份独立 HAR 对照

## 范围与证据边界

本文把当前仓库分别与两份 HAR 对照；它们是独立捕获，不拼接为同一账号、同一会话或连续请求链。

| 捕获 | HAR 时间范围 (UTC) | 捕获入口及边界 |
|---|---|---|
| `har/passwordless2about-you.har` | 2026-11-01 19:14:05–19:15:48；83 entries | 记录 signin 响应返回的 authorize URL，以及 passwordless OTP、注册和 OTP 验证。HAR 中没有实际 `GET /oauth/authorize`。 |
| `har/11.1-密码注册-快路径设置2fa.har` | 2026-11-01 16:22:53–16:24:26；815 entries | 本次相关轨迹从 `GET /create-account` 开始，记录 `POST /api/accounts/authorize/continue`、密码注册、OTP、callback/session 和注册后 TOTP。没有 `GET /oauth/authorize`，也没有 `passwordless/send-otp`。 |

表格只列认证及 2FA 相关请求，不代表 HAR 中没有静态资源或其它请求。HAR 未捕获某个请求，只能说明它不在该捕获记录中，不能推断捕获窗口外没有发生。密码、邮箱、OTP、token、secret、session ID、cookie 值及个人标识均不在本文输出。

## `passwordless2about-you.har`

### HAR 记录

| 时间 (UTC) | 请求 | 状态 | 可见响应/头部证据 |
|---|---|---:|---|
| 19:14:17 | `POST chatgpt.com/api/auth/signin/openai` | 200 | `Referer` 路径为 `/auth/login_with`，存在 `Origin`；响应 JSON 的 `url` 指向 `auth.openai.com/oauth/authorize`。未捕获后续 `GET /oauth/authorize`。 |
| 19:14:25 | `POST /api/accounts/passwordless/send-otp` | 200 | `Referer: https://auth.openai.com/`；响应 `page.type=email_otp_verification`，继续地址为 `/email-verification`。 |
| 19:14:55 | `POST /api/accounts/user/register` | 200 | 请求字段为 `username`、`password`；`Referer: https://auth.openai.com/`；响应 `page.type=email_otp_send`，继续地址为 `/api/accounts/email-otp/send`。 |
| 19:15:14 | `POST /api/accounts/email-otp/validate` | 200 | 请求字段为 `code`；`Referer: https://auth.openai.com/`；响应 `page.type=about_you`，继续地址为 `/about-you`。 |

上述四个相关 POST 均有 `Origin`。此 HAR 的相关请求中没有 `GET /oauth/authorize`、`GET /api/accounts/email-otp/send`、`POST /api/accounts/create_account`、ChatGPT callback 或 session 请求；因此证据止于 OTP 验证响应指向 `/about-you`，不能据此声称本捕获已完成资料提交、OAuth callback、session 建立或 2FA。

### 与当前仓库的对照

当前 `signin_openai()` 已按本次变更使用 `Origin: https://chatgpt.com`，并动态构造 `/auth/login_with` Referer；其中 email、`auth_session_logging_id` 和 device ID 来自本次调用上下文。signin 的 query、form body 和 authorize URL 处理保持原有实现。该请求头形态与本 HAR 的 signin 证据相符；HAR 的响应 URL 只是返回值，不能替代缺失的 authorize GET 记录。

当前注册流程见 `main.py:352-525`：signin 后跟随返回的 authorize URL；直接分类失败时才调用 `probe_registration_state()`，向 `/api/accounts/authorize/continue` 探测。新密码注册会进入 `/create-account/password`，再提交 `user/register`。当前仓库没有 `passwordless/send-otp` 请求实现；成功设置密码后按服务端返回的 OTP 状态导航到 `/api/accounts/email-otp/send`，随后验证 OTP 并按认证状态处理 profile/callback。因此，HAR 中“先 `passwordless/send-otp`，再 `user/register`”不是当前实现的同一请求顺序。

两边的 signin header 对齐不等于下游路径对齐：当前代码会跟随 authorize URL，而 HAR 没有实际记录该 GET；HAR 中 passwordless OTP 的先后顺序也与当前密码注册路径不同。此处只陈述代码与捕获差异，不推断任一请求是否为服务端接受所必需。

## `11.1-密码注册-快路径设置2fa.har`

### HAR 记录

| 时间 (UTC) | 请求 | 状态 | 可见响应/字段 |
|---|---|---:|---|
| 16:22:56 | `GET auth.openai.com/create-account` | 200 | 本次捕获的入口页面。 |
| 16:23:06 | `POST /api/accounts/authorize/continue` | 200 | 请求字段 `screen_hint`、`username`；响应 `page.type=email_otp_verification`，继续地址为 `/email-verification`。这是 POST，不是 `GET /oauth/authorize`。 |
| 16:23:17 | `POST /api/accounts/user/register` | 200 | 请求字段 `username`、`password`；响应 `page.type=email_otp_send`，继续地址为 `/api/accounts/email-otp/send`。 |
| 16:23:19 | `GET /api/accounts/email-otp/send` | 307、302 | 同一地址先后重定向，最终转到 `/email-verification`。 |
| 16:23:20 | `GET /email-verification` | 200 | OTP 验证页。 |
| 16:23:27 | `POST /api/accounts/email-otp/validate` | 200 | 请求字段 `code`；响应 `page.type=about_you`，继续地址为 `/about-you`。 |
| 16:23:33 | `POST /api/accounts/create_account` | 200 | 请求字段 `name`、`birthdate`；响应 `page.type=external_url`，继续地址为 ChatGPT OAuth callback。 |
| 16:23:37 | `GET chatgpt.com/api/auth/callback/openai` | 302 | 重定向到 `https://chatgpt.com/`。 |
| 16:23:48 | `GET chatgpt.com/api/auth/session` | 200 | session 请求成功；不记录响应中的凭据值。 |

本 HAR 的授权相关证据是入口页面和 `POST /api/accounts/authorize/continue`；未捕获 `GET /oauth/authorize`。它与上一份 HAR 的 signin 响应返回 authorize URL 是不同证据，不能把两个入口补成同一条 OAuth 请求链。

### 注册后 TOTP 轨迹

| 时间 (UTC) | 请求 | 状态 | 可见结果 |
|---|---|---:|---|
| 16:23:53 | `GET /backend-api/accounts/mfa_info` | 200 | `mfa_enabled=false`、`mfa_enabled_v2=false`。 |
| 16:23:56 | `POST /backend-api/accounts/mfa/enroll` | 200 | body 包含 `factor_type=totp`、`source=settings`。 |
| 16:24:10 | `POST /backend-api/accounts/mfa/user/activate_enrollment` | 200 | body 包含 `code`、`factor_type`、`session_id`、`source=settings`；响应 `success=true`。敏感值不记录。 |
| 16:24:11 | `GET /backend-api/accounts/mfa_info` | 200 | `mfa_enabled=true`、`mfa_enabled_v2=true`。 |
| 16:24:11 | `POST /backend-api/accounts/mfa/factor_decision` | 200 | body 的 `entrypoint=post_totp`。 |
| 16:24:16、16:24:20 | `GET /backend-api/accounts/mfa_info` | 200、200 | 两次均保持启用状态。 |

每个相关 MFA 请求均列出 `Authorization` 和 `Cookie`，且有 33 个 HAR cookie entries；请求还包含 Codex Browser/Webview 上下文标识。HAR 证明这些材料与该次成功请求同时出现，不证明任一单独字段的因果必要性。

### 与当前仓库的对照

当前注册路径也会根据认证状态走 `user/register -> email-otp/send -> email-otp/validate -> create_account -> callback -> api/auth/session`，但 signin/authorize 通常在更早阶段发生。未知 authorize 落点才调用 `/api/accounts/authorize/continue`；该 POST 探测不是 `GET /oauth/authorize` 的替代名称。HAR 从 `/create-account` 开始，因而不记录当前注册主链的 signin/authorize 前置步骤。

HAR 的 `user/register`、`email-otp/validate` 和 `create_account` 使用 `https://auth.openai.com/` 作为 Referer；当前代码分别使用注册密码页、邮箱验证页和 `/about-you`。HAR 的邮箱验证码发码是 GET 并有 307/302 重定向；当前实现依据注册响应提供的继续状态导航到该端点。两者端点职责相近，但入口状态、Referer 和捕获的前置链并非逐请求相同。

当前 `main.py` 在 callback/session 后仅在 `ENABLE_2FA` 开启时查询并设置 TOTP；默认配置为关闭。启用时，主要顺序为 `mfa_info -> enroll -> activate_enrollment -> mfa_info`。与本 HAR 相比，当前请求 body 不含 `source=settings`，也没有 `factor_decision` 和后续轮询；激活后的状态复核是 best-effort。HAR 的 MFA 请求明确有 Bearer Authorization、Cookie 和 33 个 cookie entries；当前代码显式设置 Bearer、`oai-device-id`、`oai-language`，会话 Cookie 由 `BrowserSession` 管理，实际 Cookie 集合取决于运行时会话。当前通用头不添加 Codex Webview 专属标识。

共同的 `email-otp/validate` `about_you` 响应只表示相同的页面类型/继续路径：第一份 HAR 在此结束相关认证轨迹；第二份 HAR 在独立捕获中继续创建资料、callback/session 和 2FA。它不是同一 session 的连续证据。

## 代码索引与验证边界

- signin：`core/chatgpt_auth.py:114-171`。
- authorize 导航及未知落点探测：`core/openai_auth.py:455-513, 956-1035`。
- 注册密码、OTP 和资料创建：`main.py:439-525`、`core/openai_auth.py:637-784`。
- callback/session：`core/account_export.py:192-254`。
- 注册后 TOTP：`main.py:528-570`、`core/account_export.py:573-666`。

`docs/registration-2fa-oss-comparison.md` 的第 1–10 节按本变更要求保留为原文；其中第 4.2 节关于 signin 移除 Origin 的描述是本次 signin 变更前的历史记录，不代表当前实现。当前 signin 行为以本文和 `core/chatgpt_auth.py` 为准。

本次对照是 HAR 静态证据与当前源码的比对，不包含真实账号 smoke，也不证明服务端对候选差异的接受度。未捕获的请求、动态值和客户端自动生成的头均不作推断。
