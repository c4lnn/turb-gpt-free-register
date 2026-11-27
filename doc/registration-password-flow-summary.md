# 密码注册链路总结

本文总结当前本地新账号密码注册主链：每一步发什么请求、请求头和请求体怎么构造、以及与抓包（`captures/har/chatgpt.com.har`、`captures/saz/1.saz`）和 OSS 实现有分歧时本地对齐哪一边。

**状态：请求形态已按抓包对齐，未在真实站点验证。** 文中「本地值」均来自源码构造或打到本地接收端的实发报文，没有对 `chatgpt.com` / `auth.openai.com` 发过真实注册请求。

## 范围

- 本地主链从正式 CSRF 开始，到 callback 后取回 `accessToken` 结束。
- HAR 序号从 **0** 开始；`1.saz` 的 session 编号不是时间顺序，按 `_m.xml` 时间定位。
- 正式 CSRF 之前的设备 warmup、trace、providers、入口页面不纳入主链。
- 已确认无需对齐、不再逐项审计的请求：本地多发的 callback 后 `GET /api/auth/session`；本地不发的设置密码前 `POST /api/accounts/passwordless/send-otp`（HAR `#88`、SAZ `#119`）。
- `1.saz` 的 authorize 403（`#108–#114`）是已知异常分支，不纳入比较。定位依据：`#197` signin 响应给出的 authorize URL 与 `#108` 请求 URL 逐字相同，可确认 `#196/#197` 是这次 authorize 之前的正式 CSRF/signin；`#108/#109` 两次 GET 均返回 403，`#114` 是 Cloudflare challenge 通过后的表单 POST，返回 302 后进入注册。捕获本身不能据此建立服务端拒绝的因果关系。

## 一、主链请求序列

下表是新号密码注册的正常路径。「本地位置」是 `core/openai/register/registration_flow.py` 的行号。

| # | 请求 | HAR | SAZ | 本地位置 |
| --- | --- | --- | --- | --- |
| 1 | `GET chatgpt.com/api/auth/csrf` | `#4` | `#196` | `:306` `get_csrf_token` |
| 2 | `POST chatgpt.com/api/auth/signin/openai` | `#5` | `#197` | `:312` `signin_openai` |
| 3 | `GET auth.openai.com/oauth/authorize` → 302 | `#6` | （403 分支，排除） | `:321` `follow_authorize` |
| 4 | `GET auth.openai.com/email-verification` → 200 | `#7` | `#115` | 同上，自动跟随 |
| — | `GET auth.openai.com/create-account/password` | **无** | **无** | `:400` `navigate_create_account_password` |
| 5 | `POST sentinel.openai.com/backend-api/sentinel/req` | `#128` | `#426` | `:406` `request_sentinel_token` |
| 6 | `POST auth.openai.com/api/accounts/user/register` | `#133` | `#147` | `:411` `register_user` |
| 7 | `GET auth.openai.com/api/accounts/email-otp/send` → 302 | `#137` | `#148` | `:430` `navigate_email_otp_send` |
| 8 | `GET auth.openai.com/email-verification` → 200 | `#138` | `#150` | 同上，自动跟随 |
| 9 | `POST sentinel.openai.com/backend-api/sentinel/req` | `#236` | `#432` | `registration_flow.py:163` |
| 10 | `POST auth.openai.com/api/accounts/email-otp/validate` | `#237` | `#168` | `:472` `_validate_registration_otp` |
| 11 | `POST sentinel.openai.com/backend-api/sentinel/req` | `#270` | `#433` | `:487` `request_sentinel_token` |
| 12 | `POST auth.openai.com/api/accounts/create_account` | `#275` | `#173` | `:491` `create_account` |
| 13 | `GET chatgpt.com/api/auth/callback/openai` → 302 | `#279` | `#223` | `:496` `_finalize_registration_session` |
| 14 | `GET chatgpt.com/` → 200（首页含 `accessToken`） | `#280` | `#226` | 同上，自动跟随 |
| — | `GET chatgpt.com/api/auth/session` | **无** | **无** | `:98` `fetch_session` |

两条标记「无」的请求是本地与两份捕获的差异，处理方式见第四节。

## 二、请求头构造

### 2.1 三类请求头模板

本地把请求头分成三类，由 `core/openai/transport/session.py` 统一构造。下表是每类包含的字段，「高熵」指六项高熵 Client Hints。

| 字段 | fetch（CSRF / signin / Auth JSON / Sentinel） | Auth 导航 | 通用 |
| --- | --- | --- | --- |
| `accept` | `*/*`（Auth JSON 同） | HTML 类型列表 | — |
| `content-type` | 按请求：`application/json` / `x-www-form-urlencoded` / `text/plain;charset=UTF-8` | 不带 | — |
| `origin` | 按目标域 | 不带 | — |
| `referer` | 调用方传入 | 调用方传入 | — |
| `sec-fetch-site` | `same-origin` | 按目标域与 Referer 推断 | — |
| `sec-fetch-mode` | `cors` | `navigate` | — |
| `sec-fetch-dest` | `empty` | `document` | — |
| `sec-fetch-user` | 不带 | 仅 `user_initiated=True` 时 `?1` | — |
| `upgrade-insecure-requests` | 不带 | `1` | — |
| `priority` | `u=1, i` | `u=0, i` | — |
| `cache-control` / `pragma` | 仅 `no_cache=True` 时 `no-cache` | 不带 | — |
| UA、`accept-language`、低熵 CH | ✓ | ✓ | ✓ |
| 六项高熵 CH | 按开关 | 按开关 | 按开关 |
| trace / Datadog / invocation / document ID | 仅 Auth JSON | 不带 | 不带 |
| Sentinel 凭据 | 按请求附加 | 不带 | 不带 |

模板位置：`_attach_fetch_headers` `:712-724`、`_attach_navigation_headers` `:726-743`、`_attach_auth_rum_headers` `:601-610`。

`accept-encoding`、`content-length`、`cookie`、HTTP 协议字段由 wreq 传输层和 Cookie Jar 管理，业务代码不构造。

### 2.2 逐请求的头

| # | 请求 | Referer | `sec-fetch-site` | `sec-fetch-user` | `priority` | 高熵 | Sentinel 凭据 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | CSRF | 完整 `login_with?...` | same-origin | — | `u=1, i` | 6 | — |
| 2 | signin | 同上 | same-origin | — | `u=1, i` | 6 | — |
| 3 | authorize | `https://chatgpt.com/` | cross-site | — | `u=0, i` | 6 | — |
| 4 | email-verification（authorize 落页） | `https://chatgpt.com/` | cross-site | — | `u=0, i` | 6 | — |
| — | create-account/password | authorize 落页 URL | same-origin | `?1` | `u=0, i` | 6 | — |
| 5 | Sentinel `/req` | frame.html `?sv=...` | same-origin | — | `u=1, i` | **0** | — |
| 6 | user/register | `https://auth.openai.com/` | same-origin | — | `u=1, i` | 6 | 主凭据 |
| 7 | email-otp/send | `https://auth.openai.com/` | same-origin | `?1` | `u=0, i` | 6 | — |
| 8 | email-verification（发码后落页） | `https://auth.openai.com/` | same-origin | `?1` | `u=0, i` | 6 | — |
| 9 | Sentinel `/req` | 同 #5 | same-origin | — | `u=1, i` | **0** | — |
| 10 | email-otp/validate | `https://auth.openai.com/` | same-origin | — | `u=1, i` | 6 | 主凭据 + SO |
| 11 | Sentinel `/req` | 同 #5 | same-origin | — | `u=1, i` | **0** | — |
| 12 | create_account | `https://auth.openai.com/` | same-origin | — | `u=1, i` | 6 | 主凭据 + SO |
| 13 | callback | `https://auth.openai.com/` | cross-site | — | `u=0, i` | 6 | — |
| 14 | 首页 | `https://auth.openai.com/` | cross-site | — | `u=0, i` | 6 | — |
| — | `/api/auth/session` | `https://chatgpt.com/` | same-origin | — | `u=1, i` | 6 | — |

说明：

- 第 1、2 行的禁缓存头：两条都带 `cache-control: no-cache` 和 `pragma: no-cache`。
- 第 7、8 行的 `?1`：本地导航函数默认 `user_initiated=True`，与两份捕获一致。
- Sentinel 凭据的 SO：注册 flow 不带，验码和建号 flow 带，与两份捕获一致（HAR `#133/#237/#275`、SAZ `#147/#168/#173`）。
- 第 3 行本地带高熵，HAR `#6` 不带；属目标合同，见第三节。

## 三、请求体、query 与跨请求传递

### 3.1 query 与请求体

| # | 请求 | 位置 | 字段 | 来源 |
| --- | --- | --- | --- | --- |
| 2 | signin | query | `prompt`、`screen_hint`、`auth_session_logging_id`、`login_hint` | 固定值 + 会话值 + 当前邮箱 |
| 2 | signin | form | `callbackUrl`、`csrfToken`、`json` | 固定值 + #1 响应的 `csrfToken` |
| 3 | authorize | query | `audience`、`auth_session_logging_id`、`ccaps`、`client_id`、`code_challenge`、`code_challenge_method`、`device_id`、`login_hint`、`prompt`、`redirect_uri`、`response_type`、`scope`、`screen_hint`、`state` | 以 #2 响应的 URL 为底稿 |
| 5/9/11 | Sentinel `/req` | body | `p`、`id`、`flow` | SDK 采集 + 设备 ID + 步骤名 |
| 6 | user/register | JSON | `password`、`username` | 生成的密码 + 当前邮箱 |
| 7 | email-otp/send | — | 无 body、无 query | URL 来自 #6 响应的 `continue_url` |
| 10 | email-otp/validate | JSON | `code` | 邮箱取到的验证码 |
| 12 | create_account | JSON | `name`、`birthdate` | 调用方传入或生成 |
| 13 | callback | query | `code`、`scope`、`state` | URL 来自 #12 响应的 `continue_url` |

JSON 请求体均为紧凑序列化，键序与两份捕获一致：`password, username`、`code`、`name, birthdate`。

### 3.2 跨请求传递

| 值 | 来源 | 消费者 |
| --- | --- | --- |
| CSRF token | #1 响应 `csrfToken` | #2 表单 |
| authorize URL | #2 响应 `url` | #3；本地会校正 `device_id` 等上下文 |
| OAuth `state` / PKCE | #2 响应的 URL；同时由 Cookie 保存 | #13 callback |
| 设备 ID | 设备 warmup 后从 `oai-did` Cookie 绑定 | #1/#2 的 Referer、#3 的 `device_id`、Sentinel 的 `id`、凭据校验 |
| 登录上下文 logging ID | 会话创建时生成或复用传入值 | #1/#2 的 Referer、#2 query、#3 query |
| 验证页 URL | #6 响应 `continue_url` | #7 |
| callback URL | #12 响应 `continue_url` | #13 |
| Sentinel 主凭据 | #5/#9/#11 响应 `token`，SDK 求解 | #6/#10/#12 的 `openai-sentinel-token`，`c` 字段与响应 `token` 匹配 |
| SO 凭据 | 同上，服务端要求时 SDK 返回 | #10/#12 的 `openai-sentinel-so-token` |
| session Cookie | #13/#14 响应 `Set-Cookie` | 后续所有 `chatgpt.com` 请求 |

### 3.3 Auth JSON 的动态关联头

每次 Auth JSON 请求（#6、#10、#12）生成：

- 新的 64-bit `x-datadog-trace-id` 和 `x-datadog-parent-id`；`traceparent` 的 trace/parent 是这两个值的十六进制形式。
- 新的 UUID `x-access-flow-invocation-id`。
- 当前的 `x-openai-document-navigation-id`。

Sentinel `/req` 和所有导航请求不带这些头，与两份捕获一致。

## 四、分歧与对齐

下表列出 HAR、SAZ、OSS 三者不一致的地方，以及本地对齐哪一边。序号是 HAR 序号。

### 4.1 请求头

| 项目 | HAR | SAZ | OSS | 本地对齐 | 依据 |
| --- | --- | --- | --- | --- | --- |
| 六项高熵 Client Hints | 全程无 | 除 authorize 外全有 | fetch 模板按指纹值带其中五项（无 `sec-ch-ua-full-version`），导航模板同样；Sentinel 请求不带 | **SAZ** | 目标合同，非捕获事实；本地 Sentinel `/req` 不带 |
| `priority` | fetch `u=1, i`，导航 `u=0, i` | 未记录 | 不发 | **HAR** | — |
| 初始 authorize 的 `sec-fetch-user` | 无 | `?1`（403 分支） | 去掉 | **HAR** | SAZ 两条均为 403，不据此认定 `?1` 是原因 |
| 导航 `accept` 的 `image/jxl` | 无 | 有（Chrome 157） | — | **HAR** | 与本地 chrome152 画像自洽 |
| CSRF / signin 的 Referer | 完整 `login_with?...` | 根路径 | 固定 `/auth/login` | **HAR** | 字段：`callback_path`、`screen_hint`、`login_hint`、`auth_session_logging_id`、`ext-oai-did` |
| signin 的禁缓存头 | 有 | 无 | — | **HAR** | CSRF 两份都有 |
| 同源 fetch 的 `Origin` | signin、`user/register`、`email-otp/validate`、`create_account` 均带 | 同 HAR | **不带**：`_common_headers()` 规定同源不发，称其抓包中四个同源 POST 均无此头 | **HAR / SAZ** | 仓库全部 6 份抓包零例外，见 4.4 |
| Sentinel `/req` 的 trace 头 | 不带 | 不带 | 全请求注入 Datadog trace 头 | **HAR / SAZ** | — |
| `user/register` 的 Referer | `https://auth.openai.com/` | `https://auth.openai.com/` | `/create-account/password` | **HAR / SAZ** | `register_user` 固定根路径 |
| `email-otp/validate` 的 Referer | `https://auth.openai.com/` | `https://auth.openai.com/` | `/email-verification` | **HAR / SAZ** | `_validate_registration_otp` 传根路径 |
| document-navigation ID 的取值 | 文档响应头下发 | 文档响应头下发 | 自生成 UUID | **OSS** | 暂不改，见 4.4 |
| 浏览器版本 / 平台 / 语言 | Chrome 154 / Windows / `en-PH` | Chrome 157 / Windows / `zh-CN` | 指纹池 | **不照抄** | 固定 `wreq/chrome152` + macOS，语言按时区 / 出口 |

### 4.2 请求序列

| 项目 | HAR | SAZ | OSS | 本地对齐 | 依据 |
| --- | --- | --- | --- | --- | --- |
| `GET /create-account/password` | 无 | 无 | 发 | **OSS** | 见 4.4 |
| 设置密码前 `passwordless/send-otp` | 有（`#88`） | 有（`#119`） | 发 | **都不对齐** | 已确认跳过；本地先设密码 |
| callback 后 `/api/auth/session` | 无 | 无 | 发 | **OSS** | 已确认保留；本地不解析首页 token |
| 落点分类 | 无 `authorize/continue` | 无 | 按落地 URL，未知才探测 | **OSS** | `get_registration_state` |
| signin 的 `ext-oai-did` query 与 `callbackUrl` | 无 `ext-oai-did`；`callbackUrl=https://chatgpt.com/` | 同 HAR | 有 `ext-oai-did`；`callbackUrl=/` | **HAR / SAZ** | — |

### 4.3 Cookie

| 项目 | HAR | SAZ | OSS | 本地对齐 | 依据 |
| --- | --- | --- | --- | --- | --- |
| `unified_session_manifest` 名与域 | `__Host-`，host-only | 无前缀，`Domain=auth.openai.com` | — | **跟随各自响应** | 服务端 `Set-Cookie` 决定，本地不构造 |
| `oai-login-csrf_dev_*` | 有 | 无 | — | **跟随各自响应** | 同上 |
| Sentinel 请求带的 CF Cookie | `_cfuvid` | `cf_clearance` | — | **跟随各自响应** | SAZ 有 challenge 通过记录，本地无 challenge 处理 |
| 捕获开始前已存在的 Cookie | 24 个 | 16 个 | 不补种 | **OSS** | 两份捕获里多数无更早 `Set-Cookie`；本地与 OSS 都不补，见 4.4 |

### 4.4 保留差异的说明

共五项。每项写明本地与哪一边对齐，以及证据边界。

**同源 fetch 的 `Origin`：与 HAR / SAZ 对齐，带。**

- OSS 的 `_common_headers()`（`auth_flow.py:1759-1762`）只在跨源时发 `Origin`，注释称其抓包里 signin、`user/register`、`email-otp/validate`、`create_account` 四个同源 POST 都没有这个头。
- 本仓库的抓包结论相反：4 份 HAR + 2 份 SAZ 中，这三个 Auth JSON POST、signin 和 Sentinel `/req` **全部带 `Origin`**，没有一条例外。`captures/har/chatgpt.com.har` 为 `#5/#133/#237/#275/#128/#236/#270`，`1.saz` 为 `#197/#147/#168/#173/#426/#432/#433`。
- OSS 引用的是另一份抓包（`captures/revive-20260920-reg`），无法核对。两边证据相反时以本仓库抓包为准。

本地实现：`_attach_fetch_headers` 按请求显式传入 `origin`（`core/openai/transport/session.py:684-687,721`）。

**document-navigation ID：与 OSS 对齐，暂不改。**

- 两份捕获中该值由文档响应头下发：HAR `#7`、`#138`，SAZ `#115`、`#150`（均为 `/email-verification`），之后的 Auth JSON 请求逐字使用。
- 本地与 OSS 都是 UUID4 自生成，按「整页文档导航换一个、同一文档内复用」的规则在固定点轮换，不读取响应头。
- 轮换点（本地）：初始化、authorize 响应后、密码页 GET 后、发码导航后、重发前、callback 前。
- 不改的理由：捕获只有成功样本，没有证据表明服务端校验该值；OSS 的自生成是现有可对应的先例。

OSS 的规则（`_new_document_navigation()` 注释 `:1810-1822`、`_auth_json_headers()` `:1824-1840`）：

1. 整页文档导航换一个新 ID；同一文档内的多个 Auth JSON POST 共用；SPA 路由切换（`Page.navigatedWithinDocument`）不换。
2. 该头只随 Auth JSON POST 下发，文档导航 GET 请求本身不带。

本地与 OSS 的逐点对照：

| 轮换点 | OSS | 本地 | 差异 |
| --- | --- | --- | --- |
| 初始 ID | `:210` | `core/openai/transport/session.py:169` | 无 |
| authorize 响应后 | `:2389` | `core/openai/auth/openai_auth.py:377` | 时点一致：都在收到 HTTP 响应之后、状态检查之前 |
| 密码页后 | `:2723` | `core/openai/auth/openai_auth.py:530` | 已落在密码页时本地跳过 GET 和轮换，OSS 仍 GET 并轮换；OSS 在 GET 抛异常时也轮换 |
| 发码 | `:2812` | `core/openai/auth/openai_auth.py:586` | OSS 在 GET 之前轮换，本地在导航之后轮换 |
| 重发、登录 follow-send、回调前 | 无单独规则 | `openai_auth.py:606,1074`、`core/openai/account/account_export.py:483` | 本地多出的轮换点 |
| Auth JSON 读取当前值 | `:1838-1839` | `core/openai/transport/session.py:608` | 一致 |
| 文档 GET 不带该头 | `_navigation_headers()` `:1842-1879` | 导航头构造不附加 | 一致，两份捕获亦相同 |

OSS 总结的「validate 与 create_account 同值、两次验证页之间切换」与两份捕获一致。SAZ 另有 `#111 /favicon.ico` 响应下发过一个值，但没有请求使用它，属 403 恢复分支，不纳入比较。

**`GET /create-account/password`：与 OSS 对齐，保留。**

- 两份捕获都没有这个请求。浏览器在 `/email-verification` 页内直接 POST `user/register`，密码通过 SPA 切换填写。
- OSS 和本地都没有浏览器 UI，用一次文档 GET 建立页面状态。
- 该 GET 是整页请求而非 SPA 切换，按规则必须轮换 document ID；捕获里 `user/register` 用的仍是 authorize 落页下发的值。这是模拟方式的副作用，不代表服务端要求更换。
- OSS 注释称「需要此步建立服务端状态」，但其引用的 HAR 不是本仓库这份，无法核对；真实站点未验证。

**callback 后 `/api/auth/session`：与 OSS 对齐，保留。**

- 两份捕获都是从首页 HTML 取得 `accessToken`，没有独立的 session 请求。
- 本地从 `/api/auth/session` 的 JSON 读取，不解析首页。
- HAR 首个携带该 Bearer 的请求是 `#325`（`/backend-api/wham/accounts/check`），仅作 token 来源佐证。

**预先存在的 Cookie：与 OSS 一致，不补种。**

- 两份捕获的正式 CSRF 请求上，多数 Cookie 在捕获内找不到更早的 `Set-Cookie`（HAR 24 个中 23 个，SAZ 16 个全部），说明捕获开始前就在浏览器里。
- OSS 不补种，只消费流程中服务端下发的 Cookie；本地相同。
- 本地新会话的 Cookie 背景与两份捕获都会不同，是否有影响两边都没有证据。

### 4.5 authorize URL 校正规则

本地以 #2 signin 响应的 URL 为底稿，不原样使用，会做以下处理（`core/openai/auth/chatgpt_auth.py:34-55,195-201`）：

- 删除 `ext-passkey-client-capabilities`、`ext-oai-did`、`ui_locales`、`auth_return_target_category`。
- 强制 `device_id` 使用当前已绑定的设备；设备未初始化时直接失败。
- 缺失时补 `auth_session_logging_id`、`screen_hint`、`login_hint`、固定的 `ccaps`。
- 解析并重新序列化 query；保留服务端给的 OAuth `state`、PKCE 等参数，不自行替换。
- 仅当目标主机是 `auth.openai.com` 时处理，其他 URL 原样返回。

HAR `#5 → #6` 支持「消费 signin 返回的 URL」，不证明浏览器也执行这些删除或补写。用 HAR `#5` 和 SAZ `#197` 返回的 URL 及同次上下文回放本地逻辑，解析后的 query 集合和取值没有变化，只有完整字符串存在重新序列化差异；该结果不外推到其他输入。

**性质：保留的上下文兼容策略，不是抓包的必然要求。**

### 4.6 落点分类与 unknown fallback

本地按 authorize 落地 URL 分类，已知落点不请求 `authorize/continue`，未知才探测（`core/openai/auth/openai_auth.py:826-842`；编排 `core/openai/register/registration_flow.py:326-362`）。注册入口固定 `intent="signup"`，按以下顺序匹配，命中即止：

| 顺序 | 落地 URL 包含 | 分类为 | 步骤 |
| --- | --- | --- | --- |
| 1 | `/create-account` 或 `/about-you` | 新号 | 密码 |
| 2 | `/log-in/password` | 已有账号 | 密码 |
| 3 | `/log-in`（不含上一行） | 已有账号 | OTP |
| 4 | `/email-verification` | 新号 | 密码 |
| — | 都不包含 | 未知 → 发一次 `authorize/continue` 探测 | — |

探测请求带 `authorize_continue` flow 的 Sentinel 主凭据，不消费 SO（`consume_so=False`，`:941-943`）。这是窄范围例外，不代表该 flow 都不消费 SO。

探测响应按 `page.type` 映射（`_classify_authorize_probe` `:898-927`）。注册入口用 `strict=False`：

| `page.type` 或 `continue_url` | 分类为 | 步骤 |
| --- | --- | --- |
| `create_account_password`，或 URL 含 `/create-account/password` | 新号 | 密码 |
| `email_otp_verification`，mode 为 `passwordless_signup` | 新号（无密码） | OTP |
| `email_otp_verification`，其他 mode | 已有账号 | OTP |
| `login_password`，或 URL 含 `/log-in/password` | 已有账号 | 密码 |
| `mfa_challenge` | 已有账号 | TOTP |
| 其他 | 已有账号 | OTP（需发码） |

注册入口的失败与回退（`registration_flow.py:327-362`）：

- 探测请求本身失败（含 Sentinel 失败、非 200/204）：注册终止，不回退。
- 响应无法解析或不是对象：回退为新号密码状态。
- 探测后分类仍未知：按新号密码处理。
- 仓库里标记为「已使用」的邮箱若被分类为非已有账号：注册终止。

**性质：工程兼容策略，借用 OSS 的规则。** 「无法解析后按新号继续」比「未知就停止」更主动，不应写成服务端已确认账号类型。HAR 的某个成功落点不能证明这些规则覆盖所有服务端状态。

## 五、已验证与未验证

### 已验证
| 项目 | 方式 | 结果 |
| --- | --- | --- |
| 请求头字段集合 | 三方逐字段比对 | 本地没有多出 HAR / SAZ 都没有的头 |
| CSRF / signin 实发报文 | 本地接收端 | Referer、禁缓存头、`priority`、6 项高熵与决定一致 |
| 高熵分布 | 源码构造 + 本地接收端 | 目标业务请求 6 项，Sentinel `/req` 0 项 |
| 重定向落页 | 本地接收端 | 保留高熵和导航头 |
| JSON body 键序与紧凑序列化 | 与两份捕获比对 | 一致 |
| Sentinel challenge → 消费者 | 内容匹配 | 三组 `c` 均匹配对应 challenge 响应 `token` |
| trace / Datadog 派生关系 | 4 份 HAR + 2 份 SAZ 全量扫描，共 2457 条请求 | 15 条三字段齐全的请求，trace 与 parent 共 30/30 对数值匹配，无反例；统计范围见下 |
| authorize URL 校正 | 用 HAR 和 SAZ 样本回放 | 解析后 query 完全相同 |
| 请求顺序 | 已确认 | 完整，不再审计 |
| 单元测试 | 见下 | 通过 |
| 同源 fetch 的 `Origin` | 4 份 HAR + 2 份 SAZ 全量扫描 | signin、三个 Auth JSON POST、Sentinel `/req` 全部带 `Origin`，无例外 |

测试命令与结果：

```
.venv/Scripts/python.exe -m pytest -q tests/test_session_navigation_headers.py tests/test_chatgpt_auth_context.py tests/test_openai_auth_registration_flow.py tests/test_registration_pipeline.py tests/test_registration_preflight.py tests/test_account_export_twofa.py tests/test_request_header_consumers.py tests/test_protocol_registration_compatibility.py
→ 231 passed, 363 subtests passed

.venv/Scripts/python.exe -m pytest -q tests/test_wreq_session.py
→ 6 passed
```

trace / Datadog 补证的统计范围。比较使用完整数值，不只截取 trace 的低 64-bit：`int(traceparent.trace, 16) == int(x-datadog-trace-id, 10)`，parent 同理。

| 捕获 | 三个字段齐全的请求 | 结果 |
| --- | --- | --- |
| `captures/har/chatgpt.com.har` | `#133/#237/#275` | 3 条全部匹配 |
| `captures/har/11.1-密码注册-快路径设置2fa.har` | `#125/#153/#257/#292` | 4 条全部匹配 |
| `captures/har/11.4-passwordless2about-you.har` | `#196/#305` | 2 条全部匹配 |
| `captures/har/预热.har` | 无 | 未记录这三个字段 |
| `captures/saz/1.saz` | `#147/#168/#173` | 3 条全部匹配 |
| `captures/saz/login-password-totp-403.saz` | `#58/#84/#93` | 3 条全部匹配 |

`1.saz #398/#410` 是浏览器扩展请求，只带 `traceparent`、没有两个 Datadog ID，属于缺字段，不是不匹配。该补证不说明所有请求都带这些字段，也不能证明浏览器内部随机源与本地相同。

### 未验证

| 项目 | 说明 |
| --- | --- |
| 真实站点发包 | 从未对 `chatgpt.com` / `auth.openai.com` 发过本地注册请求 |
| callback 与 session 的实发报文 | 本地接收端 smoke 只覆盖到发码重定向，callback 和 `/api/auth/session` 未实测 |
| `create-account/password` 是否必需 | 未在真实站点对比发与不发 |
| document-navigation ID 是否被校验 | 捕获只有成功样本 |
| 预先存在的 Cookie 是否影响注册 | 两份捕获回答不了 |
| Sentinel `p` 字段与浏览器环境是否等价 | 本地 SDK 在 Node VM 中运行，无法由捕获证明 |
| Cloudflare 403 | SAZ 中 authorize 被拦两次后恢复，本地无 challenge 处理逻辑 |
| Cookie 来源 | 哪些由 JS 写入、哪些由更早响应下发，捕获内看不到 |

### 本地补全、无法与浏览器判同的规则

以下规则抓包只能支持字段和部分传递现象，不能证明浏览器内部算法与本地相同。它们不是已确认的缺陷。

| 项目 | 本地做法 | 抓包能支持什么 |
| --- | --- | --- |
| 浏览器版本、平台与设备池 | 固定 `wreq/chrome152`，对齐 Chrome / macOS / `MacIntel`，设备规格从画像池选择并在会话内稳定。`core/openai/transport/session.py:70-85,214-251`、`core/openai/transport/protocol_profiles.py:23-46`、`config/browser.py:264-283`。 | HAR `#133/#237/#275` 为 Windows / Chrome 154。本地 UA、CH、SDK 自洽有工程理由，但不是复刻该样本；也不能由 HTTP 值推断 TLS 指纹等同浏览器。 |
| 语言与时区 | 自动模式使用观测国家；默认时区按画像国家选择，显式值校验后冻结。`core/openai/register/registration_geo.py:327-426,449-548`。HTTP 与 SDK 共用当前画像。 | HAR 的语言头为 `en-PH,en;q=0.9`，不能证明浏览器按出口国家选语言，也不能确认 JS 时区。 |
| invocation 与 trace 随机生成 | 每次构造 Auth JSON 头时新生成 UUID4 invocation 和两个 64-bit 随机数；零值用 1 替换。`core/openai/transport/session.py:592-610`。 | 支持逐请求区分及数值关联；不能证明随机源、位宽、零值处理相同。粒度是每次构造头，不保证传输层重发时重新生成。 |
| 导航上下文 | 按 Referer 前缀粗判 `Sec-Fetch-Site`，由调用方布尔参数选择 `Sec-Fetch-User`。authorize 沿用同一组显式头，新号 callback 逐跳重建。`core/openai/transport/session.py:572-581,689-743`、`core/openai/auth/openai_auth.py:347-377`、`core/openai/account/account_export.py:214-234`。 | HAR `#6/#7`、`#137/#138`、`#279/#280` 可支持对应样本值；不等同完整浏览器的 site、重定向链和用户激活算法。 |
| Sentinel 运行宿主 | SDK 在 Node VM 中执行，采集与求解共用一次宿主上下文，由 Python 发送 `/req`；不为形式对齐先请求 SDK 或 frame 页面。`core/openai/auth/sentinel_runner.py:60-114,165-227,477-530`、`core/openai/auth/openai_auth.py:396-434`。 | 三个 challenge 及其消费者支持最终字段与消费匹配，不能证明 VM 的 JS 环境等同浏览器。`SENTINEL_SV` 与 HAR 三个请求 frame Referer 的版本标记相符，不证明本地 SDK 字节相同或仍是线上最新。配置：`config/openai_protocol.py:27-31`。 |
| SDK Cookie 投影 | 把当前 Jar 的 Cookie 投影给 VM；JS 按名称去重，缺少时可将当前 `oai-did` 补入 VM 输入。`core/openai/transport/session.py:426-462`、`core/openai/auth/openai_auth.py:401-417`、`core/openai/auth/sentinel/sentinel-runner.js:1225-1262`。 | Cookie 头与 `Set-Cookie` 支持部分传递关系，不证明 SDK 实际可见的 `document.cookie` 集合。投影不复现 Domain / Path / HttpOnly 可见性；VM 补值不是向 HTTP CookieJar 补种。 |
| 操作节奏与 OTP 恢复 | 按动作类别配置随机等待；无效或过期的 OTP 最多提交三次，失败后重发并取当前验证码。`config/humanize.py:16-29`、`core/openai/register/registration_flow.py:154-180`。 | 成功捕获推不出自动化的等待区间或重试上限。这是运行策略，不是浏览器行为，也不证明降低风控风险。 |

SO 凭据不能按某一份捕获一刀切：HAR `#237/#275`、SAZ `#168/#173` 独立记录了 SO；challenge 响应里的 `so.required` 等只是元数据，不是 SO token。

## 六、相关文档

本文覆盖新号密码注册主链。其他分支和更细的字段证据由以下文档维护，它们与本文不重叠：

| 文档 | 内容 |
| --- | --- |
| [registration-2fa-har-comparison.md](registration-2fa-har-comparison.md) | HAR 三个分支的逐字段五列对照：分支 B 为注册后 TOTP 快路径，分支 C 为 passwordless |
| [registration-2fa-oss-comparison.md](registration-2fa-oss-comparison.md) | 与 OSS 的流程对照：已有账号登录、2FA 快 / 慢路径、session 刷新时机 |
| [registration-request-header-coverage.md](registration-request-header-coverage.md) | 请求头字段覆盖统计、逐请求 Cookie 字段数、`Priority` 全档取值分布 |
| [login-password-totp-403-analysis.md](login-password-totp-403-analysis.md) | 密码 + TOTP 登录分支对照 |
| [login-password-totp-403-request-details.md](login-password-totp-403-request-details.md) | 密码 + TOTP 登录分支的逐请求字段明细 |

## 七、重新核查条件

本轮对照按「处理决策已明确，保留上述验证限制」收口，不表述为「所有实现细节完全一致」。后续仅在实际失败或发包证据指出具体错配时，重新核查对应项；不因实现路径不同而无证据地增加页面请求、补凭据或复制捕获值。

讨论某一项时应区分三种情况：

1. **已有捕获支持的字段或传递规则**：不能仅因内部实现未知而反复改写。
2. **明确保留的本地策略**：可以讨论取舍，但不能写成浏览器的必然行为。
3. **确实未验证的行为**：明确写无法判断，不以相同字段名、相邻请求或某次 403 推导因果。

没有任何证据证明某项保留差异会导致或已经解决 Cloudflare / MFA 403。静态构造差异不能直接视为服务端拒绝原因，本地测试通过也不等于真实站点已接受。

OSS 源码位置：`../turb-gpt-free-register-oss-20260928/vendor/gpt-register-tool/协议/auth_flow.py`。
