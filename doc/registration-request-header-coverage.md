# 注册主链请求头：HAR 字段覆盖

覆盖统计只比较请求头的**字段名**，忽略大小写；下文单独列出 `Priority` 的非敏感值。浏览器参考为 [`har/chatgpt.com.har`](../har/chatgpt.com.har)，从正式 CSRF `#4` 到回调后返回 `accessToken` 的首页 `#280`（HAR 序号从 **0** 开始）。`#4` 响应的 `csrfToken` 与 `#5` 的 signin 表单匹配。源码范围为新账号密码注册从正式 CSRF 到回调后的 `/api/auth/session`；该 HAR 未记录独立的 `/api/auth/session` 或 `/oauth/token`。

不含预热、邮箱服务、已有账号分支、注册后 2FA、设置密码前的首次 `POST /api/accounts/passwordless/send-otp`（`#88`）、未匹配主链消费者的 Sentinel challenge，以及回调落地后的埋点、静态资源和登录后初始化。

## Request Headers

这是注册主链 **14 条 HAR 请求**的字段并集，不是单条请求。方括号标出 HAR 请求序号；`全部`指下方列出的 14 条请求，`未记录`不计入覆盖统计。

```text
Cache
    Cache-Control                          [#4, #5]
    Pragma                                 [#4, #5]
Client
    Accept                                 [全部]
    Accept-Encoding                        [全部]
    Accept-Language                        [全部]
    User-Agent                             [全部]
Cookies
    Cookie                                 [全部]
Entity
    Content-Length                         [#5, #128, #133, #236, #237, #270, #275]
    Content-Type                           [#5, #128, #133, #236, #237, #270, #275]
Miscellaneous
  已校验凭据
    openai-sentinel-so-token               [#237, #275]
    openai-sentinel-token                  [#133, #237, #275]
  来源上下文
    Referer                                [全部]
  动态关联
    traceparent                            [#133, #237, #275]
    tracestate                             [#133, #237, #275]
    x-access-flow-invocation-id            [#133, #237, #275]
    x-datadog-origin                       [#133, #237, #275]
    x-datadog-parent-id                    [#133, #237, #275]
    x-datadog-sampling-priority            [#133, #237, #275]
    x-datadog-trace-id                     [#133, #237, #275]
    x-openai-document-navigation-id        [#133, #237, #275]
Security
  来源上下文
    Origin                                 [#5, #128, #133, #236, #237, #270, #275]
  请求优先级
    Priority                               [全部]
  Client Hints / 低熵
    sec-ch-ua                              [全部]
    sec-ch-ua-mobile                       [全部]
    sec-ch-ua-platform                     [全部]
  Client Hints / 高熵
    sec-ch-ua-arch                         [未记录]
    sec-ch-ua-bitness                      [未记录]
    sec-ch-ua-full-version                 [未记录]
    sec-ch-ua-full-version-list            [未记录]
    sec-ch-ua-model                        [未记录]
    sec-ch-ua-platform-version             [未记录]
  Fetch Metadata
    Sec-Fetch-Dest                         [全部]
    Sec-Fetch-Mode                         [全部]
    Sec-Fetch-Site                         [全部]
Transport
    Connection                             [未记录]
    Host                                   [未记录]
导航专用（单列）
    Upgrade-Insecure-Requests              [#6, #7, #137, #138, #279, #280]
    Sec-Fetch-User                         [#137, #138]
```

- **`#4` / `#5`：** `GET /api/auth/csrf`、`POST /api/auth/signin/openai`。
- **`#6` → `#7`：** `GET /oauth/authorize` → `GET /email-verification`。
- **`#128` → `#133`：** Sentinel（`username_password_create`）→ `POST /api/accounts/user/register`。
- **`#137` → `#138`：** `GET /api/accounts/email-otp/send` → `GET /email-verification`。
- **`#236` → `#237`：** Sentinel（`email_otp_validate`）→ `POST /api/accounts/email-otp/validate`。
- **`#270` → `#275`：** Sentinel（`oauth_create_account`）→ `POST /api/accounts/create_account`。
- **`#279` → `#280`：** `GET /api/auth/callback/openai` → `GET /`。

三组 Sentinel 均为 `POST sentinel.openai.com/backend-api/sentinel/req`，已按 challenge 响应与消费者请求头的内容匹配。`Cookie` 和 Sentinel 头只列字段名，不记录值。

这 14 条请求共记录 **30 个普通请求头字段**。当前注册主链显式构造字段并集为 **33 个**，HAR 覆盖其中 **27/33**，缺少的六项为高熵 Client Hints；本地目标业务请求启用，Sentinel `/req` 不启用。HAR 的其余 3 个字段由传输层或 Cookie Jar 管理：`Accept-Encoding`、`Content-Length`、`Cookie`。四个 HTTP/2 伪头（`:authority`、`:method`、`:path`、`:scheme`）不计入；字段名并集覆盖不代表逐端点一致，也未核验面向真实站点的本地实际发包。

### 本地构造职责

上面的方括号只标记 HAR 观察，下面是本地源码的生产者和附加条件。`未记录` 不等于传输时一定不存在，也不等于其他场景不构造。

| 字段组 | 本地生产者与取值来源 | 附加条件 |
| --- | --- | --- |
| UA、语言 | [`_get_browser_profile_headers()`](../core/openai/transport/session.py#L542-L550) 从会话画像读取，缺值时保留各自回退。 | 基础画像入口负责组合；`Accept` 和 `Accept-Encoding` 不是画像字段。 |
| 低熵 / 高熵 Client Hints | [`_build_low_entropy_client_hints()`](../core/openai/transport/session.py#L552-L566) 和 [`_attach_high_entropy_client_hints()`](../core/openai/transport/session.py#L745-L763) 共用会话画像；低熵保留逐字段回退，高熵不使用该回退。 | 注册[正式认证与 authorize](../core/openai/register/registration_flow.py#L304-L335) 显式启用六项高熵；新号后续业务、条件性探测／OTP 重发及[回调/session](../core/openai/register/registration_flow.py#L491-L501) 延续选择。低熵保持原适用条件，Sentinel `/req` 不启用高熵，预热和其他共享消费者默认行为不变。 |
| 缓存 | [`_attach_cache_headers()`](../core/openai/transport/session.py#L663-L667) 使用固定的 `no-cache`。 | 只有 fetch 场景启用 `no_cache` 时成对附加 `Cache-Control` 和 `Pragma`。 |
| 请求语义：`Accept`、`Content-Type` | [请求语义组](../core/openai/transport/session.py#L669-L677) 使用场景提供的媒体类型；导航提供 HTML `Accept`。 | `Accept` 按场景设置；仅在 fetch 的 `Content-Type` 不为 `None` 时附加。[正式 CSRF 和 signin](../core/openai/transport/session.py#L775-L803) 的设置不同，高熵选择不改变媒体类型。 |
| 来源上下文：`Origin`、`Referer` | [来源上下文组](../core/openai/transport/session.py#L679-L687) 消费场景传入的值；正式 CSRF 和 signin 共用[登录 Referer](../core/openai/auth/chatgpt_auth.py#L21-L30)。 | fetch 保留 Referer（包括空值），仅在 Origin 不为 `None` 时附加；导航省略空 Referer 且不设置 Origin。 |
| Fetch Metadata | [`_attach_fetch_metadata()`](../core/openai/transport/session.py#L689-L695) 消费各场景的 site/mode/dest；导航继续[按目标与来源判定 site](../core/openai/transport/session.py#L572-L581)。 | fetch 使用其原有固定值；导航使用 document/navigate 及导航判定结果。 |
| 导航专用 | [导航专用组](../core/openai/transport/session.py#L697-L705) 生成固定的 `Upgrade-Insecure-Requests` 和有条件的 `Sec-Fetch-User`。 | 只由导航组合入口附加；`Sec-Fetch-User` 仍取决于该场景的 `user_initiated`。 |
| `Priority` | [`_attach_priority_header()`](../core/openai/transport/session.py#L707-L710) 消费 fetch 和导航各自提供的优先级，见下节。 | 由现有场景决定是否添加，不从 Client Hints 推断。 |
| trace、RUM、invocation、document-navigation ID | [`_attach_auth_rum_headers()`](../core/openai/transport/session.py#L601-L610) 组合 W3C / Datadog 关联字段和当前文档 ID。 | Auth JSON 按现有规则附加；trace/span/invocation 按请求生成，文档 ID 继续使用当前文档上下文。 |
| Sentinel 主凭据与 SO | [`attach_sentinel_credentials()`](../core/openai/transport/session.py#L43-L49) 只附加本步骤已经校验的输入。 | 只在既有消费者附加；SO 以当前 challenge 的条件为准，不把抓包里有或无 SO 当成通用规则。 |
| `Cookie`、传输字段及 HTTP/2 伪头 | [`WreqSession`](../core/infra/wreq_session.py#L83-L89) 的原生 Jar 及 [HTTP 传输](../core/infra/wreq_session.py#L139-L174)，使用实际 URL、请求体和会话状态。 | 业务层不显式补齐 `Accept-Encoding`、`Host`、`Content-Length`、`Connection` 或伪头；`Cookie` 只由 Jar 按请求目标自动筛选。 |

### Cookie 字段

取上述 14 条请求的 `Cookie` 请求头，按 Cookie 名去重，共 **37 个字段、7 种字段组合**。方括号标出出现该字段的 HAR 请求序号；只比较字段是否存在，不比较值，也不据此推断 Cookie 来自哪个响应。动态名称后缀已脱敏。

```text
全部请求共有
    __cf_bm                                  [全部]
    __cflb                                   [全部]
    _cfuvid                                  [全部]
    oai-did                                  [全部]
    oai-sc                                   [全部]
跨域共有
    __oailb                                  [#4, #5, #6, #7, #133, #137, #138, #237, #275, #279, #280]
    cf_clearance                             [#4, #5, #6, #7, #133, #137, #138, #237, #275, #279, #280]
    oaicom-stable-id                         [#6, #7, #128, #133, #137, #138, #236, #237, #270, #275]
仅 chatgpt.com
    __Host-next-auth.csrf-token              [#4, #5, #279, #280]
    __Secure-next-auth.callback-url          [#4, #5, #279, #280]
    __Secure-next-auth.pkce.code_verifier    [#4, #5, #279]
    __Secure-next-auth.state                 [#4, #5, #279]
    __Secure-next-auth.session-token.0       [#280]
    __Secure-next-auth.session-token.1       [#280]
    __stripe_mid                             [#4, #5]
    _account                                 [#4, #5, #279, #280]
    _account_is_fedramp                      [#280]
    _dd_s_v2                                 [#4, #5]
    codex_sidebar_width                      [#4, #5, #279, #280]
    g_state                                  [#4, #5, #279, #280]
    oai-asli                                 [#4, #5, #279, #280]
    oai-client-session-epoch                 [#4, #5, #279, #280]
    oai-hlib                                 [#4, #5, #279, #280]
    oai-ll                                   [#4, #5, #279, #280]
    oai-login-page-load-pending              [#280]
    oai-mweb-auth-pending                    [#4, #5, #279, #280]
    oai-mweb-origin                          [#4, #5, #279, #280]
    oai-mweb-route                           [#4, #5, #279, #280]
    oai_pl_permission_state                  [#4, #5, #279, #280]
仅 auth.openai.com
    __Host-unified_session_manifest          [#6, #7, #133, #137, #138, #237, #275]
    _dd_s                                    [#133, #137, #138, #237, #275]
    auth-session-minimized                   [#6, #7, #133, #137, #138, #237, #275]
    auth-session-minimized-client-checksum   [#6, #7, #133, #137, #138, #237, #275]
    iss_context                              [#6, #7, #133, #137, #138, #237, #275]
    oai-client-auth-info                     [#137, #138, #237, #275]
    oai-login-csrf_dev_<后缀>                  [#6, #7, #133, #137, #138, #237, #275]
    rg_context                               [#6, #7, #133, #137, #138, #237, #275]
```

- **`chatgpt.com`：** CSRF 与 signin `#4/#5` 字段相同（24 个）。回调 `#279` 比它们少 `__stripe_mid`、`_dd_s_v2`（22 个）；首页 `#280` 比回调少 PKCE verifier 和 state，多两个 session-token 分片、`_account_is_fedramp`、`oai-login-page-load-pending`（24 个）。
- **`auth.openai.com`：** authorize 与初次落页 `#6/#7` 字段相同（14 个）。提交注册密码 `#133` 多 `_dd_s`（15 个）；发码起 `#137/#138/#237/#275` 再多 `oai-client-auth-info`（16 个）。
- **`sentinel.openai.com`：** 三次 challenge `#128/#236/#270` 字段相同，均为 5 个共有字段加 `oaicom-stable-id`；没有该域独有字段。

## HAR 字段差异与抓包补证

本次主链未记录 `Host`、`Connection` 或上述六个高熵 Client Hints。六个高熵字段计入[当前注册业务场景显式构造字段](../core/openai/transport/session.py#L745-L851)，但不计入 HAR 已记录字段。与 HAR 独立对照的 [`saz/1.saz`](../saz/1.saz) 不计入本次覆盖统计；其 13 条业务请求中 12 条带齐六项，首次 authorize `#108` 未记录，同 URL `#109` 带齐，两条均返回 403，不能据此确定 Cloudflare challenge 的原因。`1.saz` 的三条匹配 Sentinel `/req`（`#426/#432/#433`）均不带高熵。

| 字段名 | 注册源码在哪些请求设置 | 抓包证据 |
| --- | --- | --- |
| 六项高熵 Client Hints | [注册编排](../core/openai/register/registration_flow.py#L304-L335) 在正式 CSRF 起的业务请求显式启用，[新号回调及 session](../core/openai/account/account_export.py#L193-L254) 传递该选择；值来自当前会话画像，不复制捕获值。 | HAR 业务请求 `#4/#5/#6/#7/#133/#137/#138/#237/#275/#279/#280` 均未记录，构成本地显式字段差异；Sentinel `#128/#236/#270` 与本地都不带。这是目标合同，不代表历史捕获一致或服务端已接受。 |
| `Priority` | [fetch 与导航组合入口](../core/openai/transport/session.py#L712-L743) 在各自场景启用时调用共享优先级组。 | 本次 HAR 的 14 条主链请求均记录，取值见下节。`1.saz`（462 条请求）和 `login-password-totp-403.saz`（98 条请求）均未记录。 |
| `Sec-Fetch-User` | [导航专用组](../core/openai/transport/session.py#L702-L705) 仅在用户触发时附加，例如[发码](../core/openai/auth/openai_auth.py#L602-L622)；[注册初始 authorize](../core/openai/register/registration_flow.py#L321-L323) 显式禁用。 | 本次 HAR 仅在发码及落页 `#137/#138` 记录 `?1`，初始 authorize `#6` 未记录。`1.saz` 的发码与落页 `#148/#150` 也记录该字段。 |

`1.saz` 的验码 `#168` 和建号 `#173` 独立记录了 `openai-sentinel-so-token`。

### `Priority` 在 HAR 中的值

| 请求类型 | HAR 观察值 | 请求示例 |
| --- | --- | --- |
| API fetch | `u=1, i` | CSRF/signin `#4/#5`、提交注册密码 `#133`、验证邮箱 OTP `#237`、创建账号 `#275`；对应 Sentinel 请求 `#128/#236/#270` 同值。 |
| 页面导航 | `u=0, i` | OAuth authorize 及落页 `#6/#7`、OTP 发送及落页 `#137/#138`、OAuth 回调及首页 `#279/#280`。 |

[当前源码](../core/openai/transport/session.py#L712-L743)的标准 fetch、导航分别构造 `u=1, i` 和 `u=0, i`，与上述样本同值；HAR 无法证明浏览器内部如何选择优先级。即使是同一端点也不是必带：`GET /api/accounts/email-otp/send` 在本次 HAR `#137` 有 `u=0, i`，但在 `11.1-密码注册-快路径设置2fa.har` `#157/#158` 未记录该字段。

四份 HAR 全档共有 951 条请求记录 `Priority`，原样值为 `u=1, i`（507）、`u=0, i`（28）、`u=1`（280）、`u=0`（65）、`u=4, i`（50）、`i`（21）。全档包含脚本、样式等资源请求，不能把所有取值套用到注册主链。`u` 表示紧急度（数值越小越紧急），`i` 是增量传输标志。
