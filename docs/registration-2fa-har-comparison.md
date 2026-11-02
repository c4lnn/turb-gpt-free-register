# 当前注册实现与两份独立 HAR 对照

## 范围与证据边界

本文把当前仓库分别与两份 HAR 对照；它们是独立捕获，不拼接为同一账号、同一会话或连续请求链。


| 捕获                               | HAR 时间范围 (UTC)                           | 捕获入口及边界                                                                                                                                                                |
| -------------------------------- | ---------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `har/passwordless2about-you.har` | 2026-11-01 19:14:05–19:15:48；83 entries  | 记录 signin 响应返回的 authorize URL，以及 passwordless OTP、注册和 OTP 验证。HAR 中没有实际 `GET /oauth/authorize`。                                                                         |
| `har/11.1-密码注册-快路径设置2fa.har`     | 2026-11-01 16:22:53–16:24:26；815 entries | 本次相关轨迹从 `GET /create-account` 开始，记录 `POST /api/accounts/authorize/continue`、密码注册、OTP、callback/session 和注册后 TOTP。没有 `GET /oauth/authorize`，也没有 `passwordless/send-otp`。 |


表格只列认证及 2FA 相关请求，不代表 HAR 中没有静态资源或其它请求。HAR 未捕获某个请求，只能说明它不在该捕获记录中，不能推断捕获窗口外没有发生。密码、邮箱、OTP、token、secret、session ID、cookie 值及个人标识均不在本文输出。

## `passwordless2about-you.har`

### HAR 记录


| 时间 (UTC) | 请求                                         | 状态  | 可见响应/头部证据                                                                                                                           |
| -------- | ------------------------------------------ | ---: | ----------------------------------------------------------------------------------------------------------------------------------- |
| 19:14:17 | `POST chatgpt.com/api/auth/signin/openai`  | 200 | `Referer` 路径为 `/auth/login_with`，存在 `Origin`；响应 JSON 的 `url` 指向 `auth.openai.com/oauth/authorize`。未捕获后续 `GET /oauth/authorize`。     |
| 19:14:25 | `POST /api/accounts/passwordless/send-otp` | 200 | `Referer: https://auth.openai.com/`；响应 `page.type=email_otp_verification`，继续地址为 `/email-verification`。                              |
| 19:14:55 | `POST /api/accounts/user/register`         | 200 | 请求字段为 `username`、`password`；`Referer: https://auth.openai.com/`；响应 `page.type=email_otp_send`，继续地址为 `/api/accounts/email-otp/send`。 |
| 19:15:14 | `POST /api/accounts/email-otp/validate`    | 200 | 请求字段为 `code`；`Referer: https://auth.openai.com/`；响应 `page.type=about_you`，继续地址为 `/about-you`。                                       |


上述四个相关 POST 均有 `Origin`。此 HAR 的相关请求中没有 `GET /oauth/authorize`、`GET /api/accounts/email-otp/send`、`POST /api/accounts/create_account`、ChatGPT callback 或 session 请求；因此证据止于 OTP 验证响应指向 `/about-you`，不能据此声称本捕获已完成资料提交、OAuth callback、session 建立或 2FA。

### 与当前仓库的对照

当前 `signin_openai()` 使用 `Origin: https://chatgpt.com`，并动态构造 `/auth/login_with` Referer；其中 email、`auth_session_logging_id` 和 device ID 来自当前调用上下文。该请求头形态与本 HAR 的 signin 证据相符；HAR 的响应 URL 只是返回值，不能替代缺失的 authorize GET 记录。

当前注册流程见 `main.py` 的 `run_registration()`：signin 后跟随返回的 authorize URL；直接分类失败时才调用 `probe_registration_state()`，向 `/api/accounts/authorize/continue` 探测。新密码注册会进入 `/create-account/password`，再提交 `user/register`。当前仓库没有 `passwordless/send-otp` 请求实现；成功设置密码且下一步为 OTP 时，按服务端返回的继续地址进行 OTP 导航，缺省地址为 `/api/accounts/email-otp/send`，随后验证 OTP 并按认证状态处理 profile/callback。因此，HAR 中“先 `passwordless/send-otp`，再 `user/register`”不是当前实现的同一请求顺序。

两边的 signin header 对齐不等于下游路径对齐：当前代码会跟随 authorize URL，而 HAR 没有实际记录该 GET；HAR 中 passwordless OTP 的先后顺序也与当前密码注册路径不同。此处只陈述代码与捕获差异，不推断任一请求是否为服务端接受所必需。

## `11.1-密码注册-快路径设置2fa.har`

### HAR 记录


| 时间 (UTC) | 请求                                         | 状态      | 可见响应/字段                                                                                                                            |
| -------- | ------------------------------------------ | -------: | ---------------------------------------------------------------------------------------------------------------------------------- |
| 16:22:56 | `GET auth.openai.com/create-account`       | 200     | 本次捕获的入口页面。                                                                                                                         |
| 16:23:06 | `POST /api/accounts/authorize/continue`    | 200     | 请求字段 `screen_hint`、`username`；响应 `page.type=email_otp_verification`，继续地址为 `/email-verification`。这是 POST，不是 `GET /oauth/authorize`。 |
| 16:23:17 | `POST /api/accounts/user/register`         | 200     | 请求字段 `username`、`password`；响应 `page.type=email_otp_send`，继续地址为 `/api/accounts/email-otp/send`。                                     |
| 16:23:19 | `GET /api/accounts/email-otp/send`         | 307、302 | 同一地址先后重定向，最终转到 `/email-verification`。                                                                                              |
| 16:23:20 | `GET /email-verification`                  | 200     | OTP 验证页。                                                                                                                           |
| 16:23:27 | `POST /api/accounts/email-otp/validate`    | 200     | 请求字段 `code`；响应 `page.type=about_you`，继续地址为 `/about-you`。                                                                           |
| 16:23:33 | `POST /api/accounts/create_account`        | 200     | 请求字段 `name`、`birthdate`；响应 `page.type=external_url`，继续地址为 ChatGPT OAuth callback。                                                  |
| 16:23:37 | `GET chatgpt.com/api/auth/callback/openai` | 302     | 重定向到 `https://chatgpt.com/`。                                                                                                       |
| 16:23:48 | `GET chatgpt.com/api/auth/session`         | 200     | session 请求成功；不记录响应中的凭据值。                                                                                                           |


本 HAR 的授权相关证据是入口页面和 `POST /api/accounts/authorize/continue`；未捕获 `GET /oauth/authorize`。它与上一份 HAR 的 signin 响应返回 authorize URL 是不同证据，不能把两个入口补成同一条 OAuth 请求链。

### 注册后 TOTP 轨迹


| 时间 (UTC)          | 请求                                                        | 状态      | 可见结果                                                                                  |
| ----------------- | --------------------------------------------------------- | -------: | ------------------------------------------------------------------------------------- |
| 16:23:53          | `GET /backend-api/accounts/mfa_info`                      | 200     | `mfa_enabled=false`、`mfa_enabled_v2=false`。                                           |
| 16:23:56          | `POST /backend-api/accounts/mfa/enroll`                   | 200     | body 包含 `factor_type=totp`、`source=settings`。                                         |
| 16:24:10          | `POST /backend-api/accounts/mfa/user/activate_enrollment` | 200     | body 包含 `code`、`factor_type`、`session_id`、`source=settings`；响应 `success=true`。敏感值不记录。 |
| 16:24:11          | `GET /backend-api/accounts/mfa_info`                      | 200     | `mfa_enabled=true`、`mfa_enabled_v2=true`。                                             |
| 16:24:11          | `POST /backend-api/accounts/mfa/factor_decision`          | 200     | body 的 `entrypoint=post_totp`。                                                        |
| 16:24:16、16:24:20 | `GET /backend-api/accounts/mfa_info`                      | 200、200 | 两次均保持启用状态。                                                                            |


每个相关 MFA 请求均列出 `Authorization` 和 `Cookie`，且有 33 个 HAR cookie entries；请求还包含 Codex Browser/Webview 上下文标识。HAR 证明这些材料与该次成功请求同时出现，不证明任一单独字段的因果必要性。

### 与当前仓库的对照

当前注册路径也会根据认证状态走 `user/register -> email-otp/send -> email-otp/validate -> create_account -> callback -> api/auth/session`，但 signin/authorize 通常在更早阶段发生。未知 authorize 落点才调用 `/api/accounts/authorize/continue`；该 POST 探测不是 `GET /oauth/authorize` 的替代名称。HAR 从 `/create-account` 开始，因而不记录当前注册主链的 signin/authorize 前置步骤。

HAR 的 `user/register`、`email-otp/validate` 和 `create_account` 使用 `https://auth.openai.com/` 作为 Referer；当前代码分别使用注册密码页、邮箱验证页和 `/about-you`。HAR 的邮箱验证码发码是 GET 并有 307/302 重定向；当前实现依据注册响应提供的继续状态导航到该端点。两者端点职责相近，但入口状态、Referer 和捕获的前置链并非逐请求相同。

当前 `main.py` 在 callback/session 后仅在 `ENABLE_2FA` 开启时查询并按需设置 TOTP；配置文件默认关闭。启用且需要新绑定时，快路径主要顺序为 `mfa_info -> enroll -> activate_enrollment -> mfa_info`。与本 HAR 相比，当前请求 body 不含 `source=settings`，也没有 `factor_decision` 和后续轮询；激活后的状态复核是 best-effort。HAR 的 MFA 请求明确有 Bearer Authorization、Cookie 和 33 个 cookie entries；当前代码显式设置 Bearer、`oai-device-id`、`oai-language`，会话 Cookie 由 `BrowserSession` 管理，实际 Cookie 集合取决于运行时会话。当前通用头不添加 Codex Webview 专属标识。

共同的 `email-otp/validate` `about_you` 响应只表示相同的页面类型/继续路径：第一份 HAR 在此结束相关认证轨迹；第二份 HAR 在独立捕获中继续创建资料、callback/session 和 2FA。它不是同一 session 的连续证据。

## 当前实现的落点判定链

本节按 `main.py:run_registration()` 的调用顺序描述代码实际做了什么，以及每一步用到的判定依据。图中的箭头只表示控制流，不表示已与任一 HAR 逐请求对齐。

### 阶段划分

```text
阶段 0  会话与预热
        network_preflight()            core/openai_auth.py:423
          -> _registration_preflight() core/openai_auth.py:342
             GET cloudflare.com/cdn-cgi/trace      冻结地域画像
             GET chatgpt.com/api/auth/csrf         绑定服务端 oai-did（有界重试）

阶段 1  ChatGPT 侧入口
        get_csrf_token()               core/chatgpt_auth.py:77
          GET chatgpt.com/api/auth/csrf
        signin_openai()                core/chatgpt_auth.py:114
          POST chatgpt.com/api/auth/signin/openai
          响应 {"url": ...} -> _ensure_authorize_context() 补齐设备上下文

阶段 2  authorize 跟随与分类
        follow_authorize()             core/openai_auth.py:455
          GET <authorize_url>，allow_redirects=True，只保留最后一次响应
        get_registration_state()       core/openai_auth.py:960
        probe_registration_state()     core/openai_auth.py:1004  仅 unknown 时

阶段 3  凭据阶段
        新号：navigate_create_account_password -> register_user
        老号：verify_login_password / request_login_otp

阶段 4  状态机循环（按服务端状态处理 otp / totp / profile / callback）
阶段 5  callback + session、可选 TOTP
```

`get_providers()`（`/api/auth/providers`）不在注册链路调用，只有 `core/account_liveness.py:151` 使用。预检会请求一次或多次 `api/auth/csrf` 以绑定服务端设备 cookie；预检成功后，`get_csrf_token()` 再独立请求一次同一端点以取得 token。

### 分类输入

`follow_authorize()` 在重定向结束后写入 `session.registration_authorize_state`：

| 字段 | 来源 | 说明 |
| --- | --- | --- |
| `landing_url` | `resp.url` | 最终落点，含 query |
| `status` | `resp.status_code` | 仅记录，不参与分类 |
| `page` | `_try_response_json(resp)`（仅当响应体像 JSON） | HTML 落地时为空 `{}` |

`get_registration_state()` 优先使用显式传入的 `landing_url`，回退到 `registration_authorize_state["landing_url"]`；`page` 优先使用传入的 `page_state`，回退到该状态里的 `page`。

### `_classify_landing_path()` 的判定（`core/openai_auth.py:930`）

只做 URL 子串匹配，按顺序短路：

| 落地 URL 包含 | `kind` | `step` | `otp_delivery_required` |
| --- | --- | --- | --- |
| `/create-account` 或 `/about-you` | `new_password` | `password` | false |
| `/log-in/password` | `existing` | `password` | false |
| `/log-in`（其它） | `existing` | `otp` | **true** |
| `/email-verification` 且 `intent=signup` | `new_password` | `password` | false |
| `/email-verification` 且其它 intent | `existing` | `otp` | **true** |
| 其它（含 `chatgpt.com` 回调落地） | `unknown` | `unknown` | false |

`kind != unknown` 时 `classification_source=landing_url`，否则 `unclassified_landing`。该函数不读 `page.type`，因此 HTML 落地和 JSON 落地走同一条判定。

### 未知落点探测

仅当 `kind == "unknown"` 时调用一次 `probe_registration_state()`：

```text
request_sentinel_token(flow="authorize_continue")
POST auth.openai.com/api/accounts/authorize/continue
     {"username": {"kind": "email", "value": <email>}}
     allow_redirects=False，要求 200/204
```

`_classify_authorize_probe()`（`core/openai_auth.py:972`，注册路径 `strict=False`）：

| `page.type` / `continue_url` | `kind` | `step` | `otp_delivery_required` |
| --- | --- | --- | --- |
| `create_account_password` 或 `/create-account/password` | `new_password` | `password` | false |
| `email_otp_verification` 且 `payload.email_verification_mode=passwordless_signup` | `new_passwordless` | `otp` | false |
| `email_otp_verification` 且其他 mode | `existing` | `otp` | mode != `passwordless_login` |
| `login_password` 或 `/log-in/password` | `existing` | `password` | false |
| `mfa_challenge` | `existing` | `totp` | false |
| 其他 | `existing` | `otp` | **true**（注册兼容路径） |

解析失败或响应非 dict 时返回 `probe_parse_fallback`，直接按 `new_password/password` 处理。

判定链的兜底顺序：

```text
landing 分类成功           -> 用 landing 结果，不发 probe
landing unknown            -> probe -> 用 probe 结果
probe 抛异常               -> Sentinel 错误直接上抛；其它错误 -> _RegistrationFailure(classification_probe)
probe 后仍 unknown          -> 强制 new_password("probe_unknown_new_fallback")
kind 不在三类之内           -> _RegistrationFailure(account_state_unknown)
persisted_email == "used" 且 kind != existing
                           -> _RegistrationFailure(email_state_conflict)
```

### 以服务端响应为驱动的后续步骤

后续每一步都用 `get_auth_step()`（`core/openai_auth.py:904`）重新判型，判型来源是服务端返回的 `continue_url` + `page.type`，不再依赖落地 URL。

`get_auth_continue_url()`（`core/openai_auth.py:849`）的取值顺序：`continue_url`、`external_url`、`url`、`page.continue_url`、`page.external_url`、`page.url`。

`get_auth_step()` 的约束：

- `continue_url` 包含主机且不在 `_AUTH_HOSTS = {auth.openai.com, chatgpt.com}` 内，直接 `unknown`；相对路径可继续参与判定。
- `_step_from_page_type()` 与 `_step_from_continue_url()` 都判定成功但结论不同，判为证据冲突，返回 `unknown`。
- `_step_from_continue_url()` 的映射：`/mfa-challenge/` -> `totp`；`/email-verification`、`/email-otp/send`、`/email-otp/resend` -> `otp`；`/log-in/password`、`/create-account/password` -> `password`；`/about-you` -> `profile`；host 为 `chatgpt.com` 或含 `/authorize/continue` -> `callback`。

主状态机（`main.py:494-525`）只接受 `otp`、`totp`、`profile`、`callback` 四种转移，且同一 `step` 在一次注册内只允许出现一次；重复或越界一律 `_RegistrationFailure(auth_step_unknown)`。已有账号进入 `profile` 时也会终止，避免修改既有资料。`callback` 分支进入 `_finalize_registration_session()`（`main.py:96`），跟随 callback URL 后调用 `fetch_session()` 取 `accessToken`。

### 与两份 HAR 的可比点

| 观测点 | 当前代码 | `passwordless2about-you.har` | `11.1-…设置2fa.har` |
| --- | --- | --- | --- |
| 入口 | `chatgpt.com` 的 `POST signin/openai` | 同左（19:14:17） | `GET auth.openai.com/create-account`（16:22:56），无 signin |
| `GET /oauth/authorize` | `follow_authorize()` 实际发起 | 未捕获 | 未捕获 |
| `authorize/continue` 触发条件 | 仅落地分类为 `unknown` | 未捕获 | 入口页面后的第一步（16:23:06） |
| `authorize/continue` 请求体 | `{"username": {...}}` | — | `{"username": {...}, "screen_hint": "signup"}` |
| signin 响应 `url` 的目标 | 跟随并分类落地 | `auth.openai.com/oauth/authorize?...` | 无 signin 响应 |

差异陈述到此为止。`screen_hint` 是否必需、未知落点是否必须探测、`/email-verification` 在 `intent=signup` 下判成 `password` 是否优于按 `page` 判 `otp`，本文不作结论：两份 HAR 都不包含可回答这些问题的完整链。

## 代码索引与验证边界

- signin：`core/chatgpt_auth.py` 的 `signin_openai()`。
- authorize 导航及未知落点探测：`core/openai_auth.py` 的 `follow_authorize()`、`get_registration_state()`、`probe_registration_state()`。
- 注册密码、OTP 和资料创建：`main.py` 的 `run_registration()` 及 `core/openai_auth.py` 中相应的注册函数。
- callback/session：`core/account_export.py` 的 `follow_oauth_callback()`、`fetch_session()`。
- 注册后 TOTP：`main.py` 的 `_setup_registration_totp()` 及 `core/account_export.py` 的 `setup_2fa_inline()`、`setup_2fa()`。

本地与固定 OSS 快照的对照见 [注册与 2FA OSS 对照](registration-2fa-oss-comparison.md)。

本文对照 HAR 静态证据与当前源码，不包含真实账号 smoke，也不证明服务端对候选差异的接受度。未捕获的请求、动态值和客户端自动生成的头均不作推断。

 
## 注册与 2FA 请求头、设备与 Sentinel 逐端点对照

**结论：**两份 HAR 中主要注册端点的 Sentinel flow 与本地注册主链基本对应；本地注册状态探测不发送 SO，与 B 样本未记录 SO 头的观察相符，其余差异仍值得核查：

1. 两份样本的 `authorize_continue` challenge 均标记 `so.required=true`，对应业务请求未记录 SO 头且响应为 200；本地仅对注册状态探测不要求、不发送 SO，不将此策略推广到其他端点。
2. 注册 Auth POST 的 `Referer` 路径、本地 `authorize/continue` 的 `screen_hint`、signin 的 `callbackUrl` 与样本不同。
3. 2FA 样本同时携带 `oai-did` 请求头、`oai-device-id` 请求头和 `oai-did` Cookie；本地代码没有显式设置 `oai-did` 请求头，且两边还有多组前端上下文头差异。
4. 该对照未使用本地实际请求抓包。本文中的“本地”是**源码显式构造结果**，不能等同于 HTTP 客户端最终发出的完整报文。

### 1. 范围和记号

| 记号 | 证据 |
| --- | --- |
| **A** | `har/passwordless2about-you.har`，83 entries |
| **B** | `har/11.1-密码注册-快路径设置2fa.har`，815 entries |
| `#N` | HAR `log.entries` 的 **0-based** 索引 |
| `未记录` | HAR 中未看到该字段；不等于证明网络上没有发送 |
| `未显式设置` | 所读本地请求构造和包装代码未设置；HTTP 客户端可能自动补充 |
| `运行时决定` | 取决于 Cookie Jar、HTTP 客户端或服务端响应，静态源码无法确认最终值 |

A、B 是独立捕获，**不拼成同一账号或同一会话**。本文不输出邮箱、密码、验证码、Cookie 值、Token、设备 ID、授权码或 TOTP secret。

Fingerprint 不逐端点展示具体值。选取的 A 中三个、B 中四个 Auth POST 的 `User-Agent`、低熵 `sec-ch-ua*`、`Accept-Language` 在各自样本内均稳定；但 B 的高熵 Client Hints 在不同请求中的**出现情况不同**，不能假设所有请求的头集合完全一样。

### 2. 端点与请求流程

| 端点 | A | B | 本地注册主链 |
| --- | --- | --- | --- |
| `GET chatgpt.com/api/auth/csrf` | `#11`，200 | 未捕获 | 预热有界重试；成功后 `get_csrf_token()` 再请求一次 |
| `POST chatgpt.com/api/auth/signin/openai` | `#12`，200 | 未捕获 | 发起 signin，读取响应 `url` |
| `GET auth.openai.com/oauth/authorize` | 未捕获 | 未捕获 | 跟随 signin 返回的 URL，允许重定向 |
| `GET auth.openai.com/create-account` | 未捕获 | `#1`，200；是相关轨迹入口 | 可能作为 authorize 重定向落点；没有独立的对应入口调用 |
| `POST /api/accounts/authorize/continue` | 未捕获 | `#125`，200 | 仅在 authorize 最终落点无法分类时探测 |
| `POST /api/accounts/passwordless/send-otp` | `#18`，200 | 未捕获 | 所查注册主链没有对应实现 |
| `POST /api/accounts/user/register` | `#45`，200 | `#153`，200 | 新号尝试设置密码 |
| `GET /api/accounts/email-otp/send` | 未捕获 | `#157` 307、`#158` 302，随后 `#159` 验证页 200 | 根据注册响应导航发码 |
| `POST /api/accounts/email-otp/validate` | `#70`，200 | `#257`，200 | 验证邮箱 OTP |
| `POST /api/accounts/create_account` | 未捕获 | `#292`，200 | profile 阶段创建账号 |
| `GET chatgpt.com/api/auth/callback/openai` | 未捕获 | `#294`，302 | 跟随服务端 callback 地址 |
| `GET chatgpt.com/api/auth/session` | 未捕获 | `#471`，200 | callback 后获取 session 和 `accessToken` |
| `GET /backend-api/accounts/mfa_info` | 未捕获 | `#592`、`#619` 等，200 | 开启 2FA 时查询；激活后复核 |
| `POST /backend-api/accounts/mfa/enroll` | 未捕获 | `#609`，200 | 2FA 快路径 enroll |
| `POST /backend-api/accounts/mfa/user/activate_enrollment` | 未捕获 | `#618`，200 | 提交 TOTP 激活 |
| `POST /backend-api/accounts/mfa/factor_decision` | 未捕获 | `#621`，200 | 所查注册 2FA 快路径没有对应调用 |

**源码：**`main.py:341-525`、`core/openai_auth.py:342-488`、`core/account_export.py:192-254,573-665`。

### 3. ChatGPT NextAuth 请求头

#### 3.1 逐字段对照

| 请求头 | A `#11` CSRF | A `#12` signin | B `#471` session | 本地代码 |
| --- | --- | --- | --- | --- |
| `Accept` | `*/*` | `*/*` | `*/*` | `*/*` |
| `Content-Type` | 未记录 | `application/x-www-form-urlencoded` | 未记录 | CSRF/session 显式 `application/json`；signin 为 form |
| `Origin` | 未记录 | `https://chatgpt.com` | 未记录 | signin 显式设置；两个 GET 未显式设置 |
| `Referer` | `/auth/login_with?...` | `/auth/login_with?...` | `https://chatgpt.com/` | CSRF 为 `/auth/login`；signin 为 `/auth/login_with?...`；session 为 `/` |
| `Cache-Control` | `no-cache` | `no-cache` | `no-cache` | 未显式设置 |
| `Pragma` | `no-cache` | `no-cache` | `no-cache` | 未显式设置 |
| `Sec-Fetch-Site` | `same-origin` | `same-origin` | `same-origin` | `same-origin` |
| `Sec-Fetch-Mode` | `cors` | `cors` | `cors` | `cors` |
| `Sec-Fetch-Dest` | `empty` | `empty` | `empty` | `empty` |
| `Sec-Fetch-User` | 未记录 | 未记录 | 未记录 | 未显式设置 |
| `Priority` | `u=1, i` | `u=1, i` | `u=1, i` | `u=1, i` |
| `x-openai-target-path` | 未记录 | 未记录 | `/api/auth/session` | `/api/auth/*` 不由包装器补充 |
| `x-openai-target-route` | 未记录 | 未记录 | `/api/auth/session` | `/api/auth/*` 不由包装器补充 |
| `Cookie` | 有 | 有 | 有 | Cookie Jar 管理，运行时决定 |
| `Authorization` | 未记录 | 未记录 | 未记录 | 未显式设置 |
| `openai-sentinel-token` | 未记录 | 未记录 | 未记录 | 这些调用未显式设置 |

**源码：**`core/session.py:785-797,909-931`、`core/chatgpt_auth.py:77-97,114-171`、`core/account_export.py:224-240`。

#### 3.2 Query、body 和响应关联

| 项目 | HAR 证据 | 本地代码 |
| --- | --- | --- |
| signin query | A `#12` 包含 `prompt`、`screen_hint`、`auth_session_logging_id`、`login_hint` | 构造相同字段集合 |
| signin Referer query | A `#12` 包含 `callback_path`、`screen_hint`、`login_hint`、`auth_session_logging_id`、`ext-oai-did` | 构造相同字段集合 |
| signin form 字段 | `callbackUrl`、`csrfToken`、`json` | 字段集合相同 |
| `callbackUrl` | A 为 `https://chatgpt.com/` | `/` |
| CSRF 关联 | A `#11` 响应的 `csrfToken` 与 `#12` form 值一致 | 使用 `get_csrf_token()` 的返回值 |
| authorize URL | A `#12` 响应 JSON 的 `url` 指向 `auth.openai.com/oauth/authorize` | 读取 `url`，补齐设备和授权上下文后跟随 |
| OAuth cookie | A `#12` 响应设置 NextAuth `state` 和 PKCE verifier Cookie | 由同一 `BrowserSession` 的 Cookie Jar 管理 |

### 4. Auth JSON POST 请求头

下表覆盖 A `#45` `user/register`、A `#70` `email-otp/validate`、B `#125` `authorize/continue`、B `#153` `user/register`、B `#257` `email-otp/validate`、B `#292` `create_account`。

#### 4.1 共用头逐字段对照

| 请求头 | 六个 HAR 请求 | 本地对应 Auth 请求 |
| --- | --- | --- |
| `Accept` | 均为 `application/json` | `application/json` |
| `Content-Type` | 均为 `application/json` | `application/json` |
| `Origin` | 均为 `https://auth.openai.com` | `https://auth.openai.com` |
| `Referer` | 均为 `https://auth.openai.com/` | 按端点使用不同页面路径，见下表 |
| `Sec-Fetch-Site` | 均为 `same-origin` | `same-origin` |
| `Sec-Fetch-Mode` | 均为 `cors` | `cors` |
| `Sec-Fetch-Dest` | 均为 `empty` | `empty` |
| `Sec-Fetch-User` | 均未记录 | 未显式设置 |
| `Priority` | 均为 `u=1, i` | `u=1, i` |
| `Cookie` | 均有 | Cookie Jar 管理 |
| `Authorization` | 均未记录 | 未显式设置 |
| `oai-device-id` 请求头 | 均未记录 | 未显式设置 |
| `oai-did` 请求头 | 均未记录 | 未显式设置 |
| `oai-language` | 均未记录 | 未显式设置 |
| `openai-sentinel-token` | 均有 | 对应 flow 的 token；OTP 受配置控制 |
| `openai-sentinel-so-token` | A `#70`、B `#257/#292` 有；其余未记录 | 注册状态探测不发送；其他端点按各自调用点的 SO 校验和发送策略执行 |
| `traceparent` | 均有 | 显式构造 |
| `tracestate` | 均为 `dd=s:1;o:rum` | 使用当前 Datadog origin 构造 |
| `x-access-flow-invocation-id` | 均有 | 每次 Auth 请求生成 |
| `x-openai-document-navigation-id` | 均有 | 使用当前 document navigation ID |
| `x-datadog-origin` | 均为 `rum` | 默认 `rum` |
| `x-datadog-parent-id` | 均有 | 每次 Auth 请求更新 |
| `x-datadog-sampling-priority` | 均为 `1` | `1` |
| `x-datadog-trace-id` | 均有 | 每次 Auth 请求更新 |
| `Cache-Control` | 均未记录 | 未显式设置 |
| `Pragma` | 均未记录 | 未显式设置 |

上述六个请求的 `traceparent` 与相应 `x-datadog-trace-id`、`x-datadog-parent-id` 的数值关联均可验证；不要求不同请求之间的 ID 相等。

**源码：**`core/session.py:705-731,816-832`、`core/openai_auth.py:666-679,733-760,791-817,1004-1018`。

#### 4.2 各端点差异

| 端点 | HAR `Referer` | 本地 `Referer` | body 与响应 |
| --- | --- | --- | --- |
| B `#125` `authorize/continue` | `https://auth.openai.com/` | `https://auth.openai.com/create-account` | HAR body 有 `username`、`screen_hint=signup`；本地只构造 `username`。HAR 响应为 `email_otp_verification`，继续地址 `/email-verification` |
| A `#45`、B `#153` `user/register` | `https://auth.openai.com/` | `https://auth.openai.com/create-account/password` | 双方 body 字段均为 `username`、`password`；HAR 响应 `email_otp_send`，继续地址 `/api/accounts/email-otp/send` |
| A `#70`、B `#257` `email-otp/validate` | `https://auth.openai.com/` | `https://auth.openai.com/email-verification` | 双方 body 字段均为 `code`；HAR 响应 `about_you`，继续地址 `/about-you` |
| B `#292` `create_account` | `https://auth.openai.com/` | `https://auth.openai.com/about-you` | 双方 body 字段均为 `name`、`birthdate`；HAR 响应 `external_url`，继续地址为 ChatGPT callback |

A `#18 passwordless/send-otp` 是另一条请求：body 为空，`Accept: */*`，携带 `authorize_continue` Sentinel 主 token，未记录 SO、`traceparent` 或 `x-datadog-*`；所查本地注册主链没有同名端点，因此没有对应的逐字段本地请求可比。

### 5. Sentinel 请求与 flow 关联

#### 5.1 `POST sentinel.openai.com/backend-api/sentinel/req` 请求头

对照 A `#17`、B `#122/#256/#285`：

| 请求头 | HAR | 本地代码 |
| --- | --- | --- |
| `Accept` | `*/*` | `*/*` |
| `Content-Type` | `text/plain;charset=UTF-8` | 相同 |
| `Origin` | `https://sentinel.openai.com` | 相同 |
| `Referer` | `/backend-api/sentinel/frame.html?sv=20260810913b` | 使用 `SENTINEL_SV` 构造；当前配置文件默认值相同 |
| `Sec-Fetch-Site` | `same-origin` | `same-origin` |
| `Sec-Fetch-Mode` | `cors` | `cors` |
| `Sec-Fetch-Dest` | `empty` | `empty` |
| `Priority` | `u=1, i` | `u=1, i` |
| `Cookie` | 有；包含 `oai-did` | Cookie Jar 管理，运行时决定 |
| `Authorization` | 未记录 | 未显式设置 |
| `traceparent` | 未记录 | 未显式设置 |
| `tracestate` | 未记录 | 未显式设置 |
| `x-datadog-*` | 未记录 | 未显式设置 |
| `openai-sentinel-token` | 未记录 | 未显式设置；这是获取 challenge 的请求 |

请求 body 字段均为 `p`、`id`、`flow`。本地同样使用这三个字段构造请求。

#### 5.2 每个业务端点使用的 challenge

通过业务请求主 token 的 `c` 与 Sentinel 响应中的 `token` 做关联，而不是取时间上最近的 Sentinel 请求。

| 样本业务请求 | 匹配 challenge | flow | challenge `so.required` | 业务请求记录 SO 头 | 与本地注册 flow |
| --- | --- | --- | --- | --- | --- |
| A `#18 passwordless/send-otp` | A `#17` | `authorize_continue` | `true` | 未记录 | 注册主链无对应端点 |
| A `#45 user/register` | A `#41` | `username_password_create` | 未提供 | 未记录 | 相同 |
| A `#70 email-otp/validate` | A `#69` | `email_otp_validate` | `true` | 有 | 相同；当前配置文件默认开启 |
| B `#125 authorize/continue` | B `#122` | `authorize_continue` | `true` | 未记录 | 相同 |
| B `#153 user/register` | B `#150` | `username_password_create` | 未提供 | 未记录 | 相同 |
| B `#257 email-otp/validate` | B `#256` | `email_otp_validate` | `true` | 有 | 相同；当前配置文件默认开启 |
| B `#292 create_account` | B `#285` | `oauth_create_account` | `true` | 有 | 相同 |

七组中：

- Sentinel 请求 body 的 `id`、主 token 的 `id`、业务请求的 `oai-did` Cookie 均一致。
- 主 token 的 `flow` 与对应 Sentinel 请求的 `flow` 一致。
- 主 token 的 `c` 与对应 challenge 响应 `token` 一致。
- 有 SO 头的三组，其 SO 的 `flow`、`id`、`c` 与主 token 一致。
- 七组 challenge 的 `proofofwork.required` 均为 `true`；对应主 token 的 `p`、`t` 均非空。具体值未输出。

**关联示例：**B `#292 create_account` 对应 `#285 oauth_create_account`，虽然两者之间还有 `#291 email_otp_validate`。不能把最近的 challenge 自动认作当前请求使用的 challenge。

#### 5.3 SO 样本与本地策略

A `#17 -> #18` 和 B `#122 -> #125` 的 challenge 都记录 `so.required=true`，对应业务请求捕获中均未见 SO 头，响应均为 200。

本地 `probe_registration_state()` 对 `authorize/continue` 显式不消费 SO：即使 challenge 标记 `so.required=true`，也不因 SO 结果缺失而中止，不发送 SO 头；主 token 的 flow、设备、challenge 和必需 proof 仍会校验。注册未知落点和 2FA 重认证共用该探测函数，但须先有有效设备上下文；注册后慢路径的初始化限制见 8.1 节。

策略按调用点而非 flow 区分。Codex 邮箱提交及其使用 `authorize_continue` 的邮箱 OTP 验证，使用严格 SO 校验和发送逻辑；注册主链邮箱 OTP 验证使用 `email_otp_validate`。A 的 `passwordless/send-otp` 在本地注册主链没有对应实现。

仅凭这两份 HAR 不能证明服务端在其他上下文或未来始终接受无 SO 请求；业务请求失败仍按服务端状态处理。探测路径的离线验证使用合成 challenge、SDK 结果及请求接收器，不能代表真实服务端或真实 SDK challenge 的表现。

### 6. 页面导航、重定向与 callback

| 字段或行为 | B HAR | 本地代码 |
| --- | --- | --- |
| `GET /create-account` | `#1` 为地址栏式导航：`Sec-Fetch-Site: none`，无 Referer 记录 | 可能由 authorize 重定向落地；不能把本地首次 authorize 请求直接视为同一请求 |
| `GET /email-otp/send` | `#157`、`#158` 分别 307、302 | `navigate_email_otp_send()` 跟随重定向 |
| 发码导航 Referer | `#157/#158/#159` 记录为 `https://auth.openai.com/` | 发码导航显式使用 `/create-account/password` |
| 发码导航其他头 | `#157/#158/#159` 只记录了少量头，`headersSize=-1` | 构造 HTML 导航头；无法据此断言 HAR 中其他头确实未发送 |
| callback `Accept` | B `#294` 为 HTML 导航，包含 `image/jxl` | HTML 导航 `Accept` 中未显式包含 `image/jxl` |
| callback `Referer` | B `#294` 为 `https://auth.openai.com/` | 按前一状态决定；从 profile 创建账号后使用 `/about-you` |
| callback `Sec-Fetch-Site` | `cross-site` | 从 Auth 回到 ChatGPT 时构造 `cross-site` |
| callback `Sec-Fetch-Mode` | `navigate` | `navigate` |
| callback `Sec-Fetch-Dest` | `document` | `document` |
| callback `Sec-Fetch-User` | `?1` | 默认 `?1` |
| callback `Upgrade-Insecure-Requests` | `1` | `1` |
| callback 响应 | B `#294` 为 302，转向 `https://chatgpt.com/` | 跟随服务端 callback 及重定向 |

B `#157` 的 HAR 标记为 `http/1.1`，而多个完整记录的业务请求标记为 `http/2.0`；该条目头部不完整，不能仅据此推断本地应使用何种协议或哪些头未发送。

### 7. 2FA 请求头逐字段对照

B `#592/#619` 为 `GET mfa_info`；B `#609/#618` 分别为 enroll、activate POST。

| 请求头 | HAR：`mfa_info` GET | HAR：enroll/activate POST | 本地对应请求 |
| --- | --- | --- | --- |
| `Accept` | `*/*` | `*/*` | `*/*` |
| `Content-Type` | 未记录 | `application/json` | GET 和 POST 均显式为 `application/json` |
| `Origin` | 未记录 | `https://chatgpt.com` | 构造代码未显式设置 |
| `Referer` | `https://chatgpt.com/` | 相同 | 相同 |
| `Sec-Fetch-Site` | `same-origin` | `same-origin` | `same-origin` |
| `Sec-Fetch-Mode` | `cors` | `cors` | `cors` |
| `Sec-Fetch-Dest` | `empty` | `empty` | `empty` |
| `Sec-Fetch-User` | 未记录 | 未记录 | 未显式设置 |
| `Priority` | `u=1, i` | `u=1, i` | `u=1, i` |
| `Authorization` | Bearer，有 | Bearer，有 | 使用 session 获得的 access token |
| `Cookie` | 有 | 有 | Cookie Jar 管理 |
| `oai-device-id` | 有 | 有 | 显式使用 `session.device_id` |
| **`oai-did` 请求头** | 有 | 有 | 未显式设置 |
| `oai-language` | `pt-BR` | `pt-BR` | 使用当前会话的 `navigator_language()` |
| `chatgpt-account-id` | 有 | 有 | 未显式设置 |
| `originator` | `Codex Browser` | `Codex Browser` | 未显式设置 |
| `x-openai-codex-window-type` | `browser` | `browser` | 未显式设置 |
| `x-openai-rate-limit-join-workspace` | `1` | `1` | 未显式设置 |
| `x-openai-web-frontend` | `codex_webview` | `codex_webview` | 未显式设置 |
| `oai-client-build-number` | 未记录 | 未记录 | 显式添加 |
| `oai-client-version` | 未记录 | 未记录 | 显式添加 |
| `oai-session-id` | 未记录 | 未记录 | 显式添加 |
| `x-openai-target-path` | 未记录 | 未记录 | 包装器对 `/backend-api/*` 添加 |
| `x-openai-target-route` | 未记录 | 未记录 | 包装器对 `/backend-api/*` 添加 |
| `x-datadog-origin` | 未记录 | 未记录 | 显式添加 |
| `x-datadog-parent-id` | 未记录 | 未记录 | 显式添加 |
| `x-datadog-sampling-priority` | 未记录 | 未记录 | 显式添加 |
| `x-datadog-trace-id` | 未记录 | 未记录 | 显式添加 |
| `traceparent` | 未记录 | 未记录 | 该 ChatGPT 头构造路径未显式设置 |
| `tracestate` | 未记录 | 未记录 | 该 ChatGPT 头构造路径未显式设置 |
| `openai-sentinel-token` | 未记录 | 未记录 | 未显式设置 |
| `openai-sentinel-so-token` | 未记录 | 未记录 | 未显式设置 |

`oai-did` 不只是 Cookie 名；B 的 MFA 请求还有独立的 `oai-did` **请求头**。

B `#592/#609/#618/#619` 中，`oai-did` 请求头、`oai-device-id` 请求头和 `oai-did` Cookie 的值在各请求内一致。本地是否最终在网络层附带 `oai-did` 请求头，需要实际请求证据；源码构造及 `BrowserSession` 包装器中未见显式添加。

#### 7.1 2FA body 和认证关系

| 项目 | B HAR | 本地 |
| --- | --- | --- |
| enroll body | `factor_type=totp`、`source=settings` | 只构造 `factor_type=totp` |
| activate body | `code`、`factor_type`、`session_id`、`source=settings` | 构造前三项，没有 `source` |
| Bearer 来源 | `#592/#609/#618/#619` 的 Bearer 与 `#471` session 响应的 `accessToken` 一致 | 从注册 session 获得 access token 后传入 2FA |
| enrollment session | `#618` 的 `session_id` 与 `#609` 响应一致 | 从 enroll 响应取得后传给 activate |
| 后续决策 | `#621 factor_decision` body 包含 `entrypoint=post_totp` | 所查快路径没有对应调用 |

**源码：**`core/session.py:748-755,799-814,909-931`、`core/openai_auth.py:1225-1235`、`core/account_export.py:573-625`。

### 8. 设备、Cookie 与会话连续性

#### 8.1 设备 ID

| 比较位置 | 捕获结果 | 本地实现 |
| --- | --- | --- |
| A signin Cookie `oai-did` 与 Referer `ext-oai-did` | 相同 | Referer 使用 `session.device_id` |
| A signin Cookie `oai-did` 与响应 authorize URL `device_id` | 相同 | `_ensure_authorize_context()` 写入 `session.device_id` |
| Sentinel body `id`、业务 token `id`、业务请求 `oai-did` Cookie | 七组均相同 | 构造及校验时使用当前 `session.device_id` |
| B MFA 的 `oai-did` 头、`oai-device-id` 头、`oai-did` Cookie | 所核验四个请求内均相同 | 显式设置 `oai-device-id`；Cookie 和最终网络头需运行时确认 |

注册会话以 `server_device_id=True` 创建：预热从 ChatGPT 服务端 Cookie 读取并绑定设备 ID，不在该模式下主动补种设备 Cookie。2FA 快路径失败且有密码时，注册流程会创建同样采用 `server_device_id=True` 的新会话进行重认证，但未对新会话执行设备绑定预检。若此前重认证请求成功、执行到状态探测，`request_sentinel_token()` 会因 `device_id` 仍为空而中止。离线验证使用真实新会话并模拟前置重认证成功；尚无真实账号或网络验证。

#### 8.2 Cookie 和认证状态

| 阶段 | HAR 证据 | 本地可确认内容 |
| --- | --- | --- |
| signin | A `#12` 响应设置 NextAuth state、PKCE verifier Cookie | 保留同一 Cookie Jar |
| Auth 入口 | B `#1` 请求已带 `oai-did`；响应另有 `oai-did` Set-Cookie，Domain 为 `openai.com` | 注册预热从 `chatgpt.com` Cookie 绑定设备；Auth 域 Cookie 的实际集合需运行时确认 |
| Auth POST | B `#125/#153/#257/#292` 均记录 Auth 会话和设备相关 Cookie | 交由 Cookie Jar 按域与路径发送 |
| callback | B `#294` 清除 state/PKCE Cookie，并设置分片 session-token Cookie | 跟随 callback，依赖 Cookie Jar 接收响应 |
| session | B `#471` 请求携带 session-token 分片，响应返回 `accessToken` | `fetch_session()` 要求响应中存在 `accessToken` |
| MFA | B 的相关请求同时记录 Bearer 和 Cookie | 显式设置 Bearer，Cookie 由会话管理 |

A signin 和 B callback 属于**不同 HAR、不同会话证据**，不能用来证明同一次 OAuth state 的端到端关联。

#### 8.3 document 和 trace 标识

B 中 `#125/#153` 使用同一个 document navigation ID，`#257/#292` 使用另一个；四次请求的 `x-access-flow-invocation-id` 各不相同。六个 Auth JSON POST 样本的 `traceparent` 与各自 Datadog trace/parent ID 关联一致。

本地 Auth 请求每次生成新的 access-flow invocation ID 和 trace/parent ID；document navigation ID 在代码识别到文档导航时轮换。是否与真实本地运行逐次一致，仍需实际抓包。

### 9. 网络层字段和验证边界

| 字段 | HAR | 本地静态证据 |
| --- | --- | --- |
| `Accept-Encoding` | 所列完整业务样本记录 `gzip, deflate, br, zstd` | 请求构造函数未显式设置；实际值由客户端决定 |
| `Content-Length` | 完整 POST 样本记录 | 请求体编码后由客户端处理，不能跨不同动态 body 比较字面值 |
| `:authority` | HTTP/2 完整样本有 | HTTP/2 传输层伪头；不等同于应用手工设置的普通头 |
| `:method` | HTTP/2 完整样本有 | 由请求方法和传输层决定 |
| `:path` | HTTP/2 完整样本有 | 由路径和 query 决定；本文不输出敏感 query 值 |
| `:scheme` | HTTP/2 完整样本有 | 由 URL 和传输层决定 |

**敏感数据提示：**本地源码中 `main.py:444` 会记录密码候选，`core/account_export.py:647` 在提供 `on_pending_secret` 回调时会记录 TOTP secret。日志及 HAR 应按凭据材料保护；本文没有复制这些值。
