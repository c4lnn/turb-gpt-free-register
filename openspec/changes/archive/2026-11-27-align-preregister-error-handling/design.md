# Design

## Context

动机见 proposal.md。本节只列约束实现方式的现状。

**本地现状**

- `get_csrf_token`（`chatgpt_auth.py:88`）只做 `raise_for_status()`，没有任何重试。
- `follow_authorize`（`openai_auth.py:347`）外层是一个 `max_attempts` 循环，对 403 和可恢复网络错误都同会话重试，重试前调用 `reset_retryable_circuit`。
- `_request_with_proxy_retry`（`openai_auth.py:292`）是既有的步骤级重试，最多 3 次、指数退避，按 `_is_retryable_authorize_error` 判定。调用方两处：登录预检（`:334`）和 Sentinel `/req`（`:443`）。
- `_network_preflight_with_retry`（`account_liveness.py:157`）是查活链 CSRF 和 signin 的外层循环，默认 4 次，用 `_is_retryable_network_error` 判定，其关键词表 `_RETRYABLE_NETWORK_HINTS` 含 `"403"`。
- 熔断：`BrowserSession.get()/post()` 入口先 `_raise_if_circuit_open()`，响应返回时 `_observe_response_for_circuit_breaker()` 对 403/429 设置 `blocked_until`。熔断设置发生在响应被观察到时，早于调用方的 `raise_for_status()`。
- `WreqSession.request()`（`wreq_session.py:118`）没有重试。参数处理在前，实际请求是 `:160` 一行 `with self._client.request(...)`，Cookie 记录与响应构造在 `with` 块内、请求成功之后。

**wreq 异常类型的实测结果**

三轮本地故障注入，wreq 用 Chrome152 画像，每种故障均在服务端确认发生，每轮各有正常响应的对照组成功。第三轮把前两轮的 15 行全部重跑（每行 2 次，OSS 列直接调用 OSS 的 `_is_retryable` 实跑）：12 行与前两轮一致；「请求已发出，无响应 RST」和「响应不完整后 RST」两行与前两轮不同，已按第三轮改正，见 D1「RST 的阶段」；「代理端口不通」一行拆成了两种形态，见表下说明。表共 17 行。

| 故意注入的故障 | 发生阶段 | wreq 异常 | 消息前缀 | OSS wreq 后端是否重试 |
|---|---|---|---|---|
| 握手前对端关闭 | 连接建立 | `ConnectionError` | `is_connect error` | 是 |
| 收 ClientHello 后关闭 | 连接建立 | `ConnectionError` | `is_connect error` | 是 |
| 对端回明文 HTTP | 连接建立 | `ConnectionError` | `is_connect error` | 是 |
| 证书校验失败 | 连接建立 | `ConnectionError` | `is_connect error` | 是 |
| 经代理，隧道建好后对端关闭 | 连接建立 | `ConnectionError` | `is_connect error` | 是 |
| 握手挂起 | 连接建立 | `TimeoutError` | `is_timeout error` | 是 |
| 经代理，隧道内握手挂起 | 连接建立 | `TimeoutError` | `is_timeout error` | 是 |
| 代理端口黑洞（accept 后不回应） | 代理连接 | `TimeoutError` | `is_timeout error` | 是 |
| 代理端口拒绝连接（无人监听） | 代理连接 | `ProxyConnectionError` | `is_proxy_connect error` | 是 |
| 代理 accept 后关闭 | 代理连接 | `ProxyConnectionError` | `is_proxy_connect error` | 是 |
| 代理对 CONNECT 回 407 | 代理连接 | `ProxyConnectionError` | `is_proxy_connect error` | 是 |
| 请求已发出，等待响应超时 | 请求已发出 | `TimeoutError` | `is_timeout error` | 是 |
| POST 请求已发出，等待响应超时 | 请求已发出 | `TimeoutError` | `is_timeout error` | 是 |
| 握手成功，请求已发出，无响应关闭 | 请求已发出 | `RequestError` | `is_request error` | 否 |
| 握手成功，请求已发出，无响应 RST | 请求已发出 | `ConnectionResetError` | `is_connection_reset error` | 是 |
| 响应体不完整后关闭 | 响应接收 | `DecodingError` | `is_decode error` | 否 |
| 响应体不完整后 RST | 响应接收 | `ConnectionResetError` | `is_connection_reset error` | 是 |

「代理不通」抛什么取决于不通的形态，两种形态各跑 3 次和 2 次，结果一致。前两轮记的「代理端口不通 → `TimeoutError`」对应黑洞形态；第三轮先用「端口绑定后关闭」注入，得到的是拒绝连接的 `ProxyConnectionError`，所以起初被当成与前两轮不一致。黑洞形态下 `s.timeout` 生效（设 3.0 秒，请求 3.0 秒返回），抛的是 `TimeoutError`，与握手挂起、请求已发出后等响应超时是同一个类，传输层对它的超时预算因此同样是 3 倍。两种形态都在重试集合里，OSS 列的结论不受影响。

四个结论：

1. `wreq.TlsError` 在所有握手故障中一次都没有出现。`exceptions.py:36` 的 docstring 声称它覆盖握手失败和证书校验失败，实际不走这条路。现有 `wreq_session.py:179-180` 的 `TlsError → SSLError` 分支在这些场景下不命中。
2. 连接建立阶段的失败与请求已发出后的连接中断，异常类型不同（前者 `ConnectionError`，后者 `RequestError` 或 `ConnectionResetError`），经现有映射后都是 `requests.ConnectionError`。在 `WreqSession` 内部按 wreq 原生类型即可区分。
3. **超时无法区分发生阶段**：握手挂起（请求未发出）和请求已发出后等响应超时，抛出的异常类型和消息完全相同，都是 `TimeoutError` + `is_timeout error ... source: TimedOut`。连接池已有空闲连接时新建连接握手失败，同样抛 `ConnectionError`、前缀 `is_connect error`。
4. 经代理的握手失败，异常类型和消息前缀与直连一致。

另：`DecodingError` 和 `BodyError` 不在现有 `except` 链中，会以 wreq 原生类型穿透到上层，已用真实 `WreqSession.request()` 确认。

**OSS 参考**

OSS 有两个 HTTP 后端：`http_client.py` 的 curl_cffi 版和 `http_client_wreq.py` 的 wreq 版。`_turb_bridge.py:256-258` 调 `install()` 切换，引擎开关 `TURB_HTTP_ENGINE` 默认 `wreq`，仓库内无覆盖。**实际运行的是 wreq 版**，以下均指该版本。

- 重试层在 `WreqSession._call`（`http_client_wreq.py:696-731`），重试 2 次，退避 1.5s × n。全部 `session.get/post` 都经过它，`_call` 不区分 HTTP 方法。
- 判定在 `_is_retryable`（`:101-108`），两层：先看异常类名，`_NO_RETRY_EXC`（`StatusError` `DecodingError` `RedirectError` `BodyError` `BuilderError`）直接不重试，`_RETRY_EXC`（`ConnectionError` `TimeoutError` `ConnectionResetError` `TlsError` `ProxyConnectionError`）直接重试，两个集合不相交，先后不影响结果；两者都不命中时再看消息是否含 `_RETRY_MARKERS`。
- 依据来自 curl_cffi 时代的注释（`http_client.py:131-149`）：148 轮中 TLS 握手瞬断发生率 5.4%，与指纹和域名无关；8 次事件原会话重试全部首次恢复。这组数据只针对握手瞬断，**超时的发生率 OSS 没有记录**。

OSS 的 CSRF 对 TLS 错误还有一条换指纹分支（`auth_flow.py:2186` `_rotate_impersonate_session`）。它做 4 件事：取下一个候选指纹、换 UA、同步 Client Hints、新建 session。但实际走不到：`_turb_bridge.py:141-172` 的 `_apply_safe_impersonate()` 把 `_CHROME_VERSIONS` 各档的 `impersonate` 改成同一个值（`:137`，默认 `chrome152`），`fingerprint.py:843` 按该字段去重后其他档为空，候选列表 `fallback_impersonates`（`:852`）只剩当前这一个；候选只有 1 个时 `_rotate_impersonate_session` 直接返回 `False`（`auth_flow.py:1661-1662`）。`_turb_bridge.py:838` 锁定浏览器家族为 `chrome`，只走这一条生成路径。用 2000 个随机种子调用 `_gen_chrome` 实跑，候选列表全部是单元素。

## Goals / Non-Goals

**Goals:**

- 正式 CSRF 的 403 恢复能力与 OSS 一致。
- authorize 的错误处理与 OSS 一致：任何错误直接抛。
- 链路瞬断在全仓范围内同会话恢复，判定按 wreq 异常类名，与 OSS 的 wreq 后端一致，仅 `ConnectionResetError` 一项不同（D1）。
- 传输库异常不穿透到上层。
- 注册失败按结构化信号分类，邮箱处理与重试共用同一个分类结果。
- 网络类失败的邮箱可再次领取，不再被永久剔除。
- 账号创建之前的网络类失败自动换出口整轮重来，人工重试优先使用原邮箱。
- 服务重启后，残留的注册任务被收口为终态，其邮箱不再锁在 `registering`，见 D19。

**Non-Goals:**

- 不改变熔断的触发条件、冷却时长和消费方。
- 不改变 callback、OTP、重认证阶段的任何步骤内重试行为。
- 不改 `_proxy_retry_config` / `_is_retryable_authorize_error`，它们仍服务于 `network_preflight`。
- 不为 POST 重复提交增加识别或恢复逻辑。
- 不对齐 OSS 的 TLS 专用错误消息和 Sentinel 异常放行，理由见 D8、D9。
- 不对账号创建之后的失败做整轮重试，理由见 D12。
- 不处理查活链的失败分类与邮箱状态。
- 不修复已有账号登录失败后池邮箱停在 `registering` 的既有缺口，见 D16。它与 D19 处理的是同一类现象（邮箱停在 `registering` 且无直接人工出口），区别在成因：D19 的是任务状态残留，启动时能确定任务已不在执行；D16 的任务已正常结束，缺口在流程里，修法不同。

## Decisions

### D1: 重试判定按 wreq 异常类名，与 OSS 一致（`ConnectionResetError` 除外）

**决定**：按 wreq 原生异常类名判定。`ConnectionError`、`TlsError`、`ProxyConnectionError`、`TimeoutError` 重试；`ConnectionResetError`、`RequestError`、`DecodingError`、`BodyError`、`BuilderError`、`RedirectError`、`StatusError` 不重试。与 OSS 的差别只有 `ConnectionResetError` 一项：OSS 重试，本地不重试，理由见下面「RST 的阶段」。

**理由**：实测表明 wreq 的异常类与失败阶段有对应关系——连接建立失败是 `ConnectionError`，代理失败是 `ProxyConnectionError`，请求已发出后对端关闭是 `RequestError`，响应体不完整后对端关闭是 `DecodingError`。按类名判定不需要解析消息文本。这个对应关系只在 FIN 关闭时成立，RST 的情况见下面「RST 的阶段」。

**与 OSS 的差异**：OSS 在类名都不命中时还会用消息关键词兜底（`_RETRY_MARKERS`）。本地不做消息兜底。实测中所有链路瞬断都被类名覆盖，消息兜底只在 wreq 新增异常类时才有作用，而那种情况下保守做法是不重试。其余差异只有 `ConnectionResetError` 一项。

**RST 的阶段**（复核时补测，4 个注入点 × FIN / RST 各 3 次，结果稳定）：对端用 FIN 关闭和用 RST 关闭，抛的异常类不同。

| 发生阶段 | 对端 FIN 关闭 | 对端 RST |
|---|---|---|
| 握手前、收到 ClientHello 后 | `ConnectionError`（`is_connect error`） | `ConnectionError`（`is_connect error`） |
| 握手后，请求已发出 | `RequestError`（`is_request error`） | `ConnectionResetError`（`is_connection_reset error`） |
| 响应体传输中 | `DecodingError`（`is_decode error`） | `ConnectionResetError`（`is_connection_reset error`） |

`ConnectionResetError` 在实测中只在请求已发出之后出现，握手阶段的 RST 抛的是 `ConnectionError`。所以把 `ConnectionResetError` 放进重试集合，重试的全部是「请求已发出后被重置」这一种情况，而传输 spec 要求这类失败 MUST NOT 自动重试。对 POST 来说，这是 D1a 讨论的重复提交的又一个入口，只是触发条件从超时变成了 RST；D1a 的「接受」是针对超时做的。OSS 的 `_RETRY_EXC` 同样含 `ConnectionResetError`，所以 OSS 也会重试这类。

**决定**：`ConnectionResetError` 不重试，与 OSS 不一致。区分靠的是类名，握手阶段的 RST 本来就不是这个类，所以不需要解析消息，与 D1「按类名判定」的原则一致。握手阶段的瞬断不受影响，仍由 `ConnectionError`、`TimeoutError` 覆盖。代价是请求发出后被 RST 的失败直接落到调用方；RST 在线上的发生频率没有记录。上面实测表里「无响应 RST」一行记的是 `RequestError`，本次没有复现，原注入条件无法确认，以本表为准。

**备选**：

- *用消息前缀 `is_connect error` 只重试握手失败*：放弃。这是只重试「请求必然未发出」的口径，更安全，但与 OSS 不一致，且无法覆盖超时。
- *用 `TlsError` 类型判定*：放弃。实测中它一次都没有出现。

### D1a: 超时重试的 POST 重复提交

**事实**：wreq 对「握手挂起」和「请求已发出后等响应超时」抛出的异常完全相同，传输层无法区分。选择重试超时，就必须接受超时的 POST 会被重发。

**对注册主链的影响**：`user/register`（`openai_auth.py:585`）超时后若首次请求已被服务端处理，重试会以同邮箱再次提交。`openai_auth.py:204` 的注释明确把 `already exists` 排除在可回退拒绝之外，因此该响应会走 `raise_for_status()` 抛出 `HTTPError`，`registration_flow.py:409` 的 `password_state` 停在 `submission_unknown`。按 D11 的分类，`HTTPError` 归 `unknown`，邮箱保持现状标记为 failed。结果是服务端账号已创建、本地记为失败。

**决定**：接受这一后果，不增加识别逻辑。与 OSS 行为一致。OSS 的 `classify_error` 同样不把 `already exists` 归入网络类，两边在这一点上结果相同。

**备选**：*增加重复提交识别（如收到 `already exists` 后用刚提交的密码尝试验证归属）*：放弃。这是需要独立设计的新功能，超出对齐 OSS 的范围。

### D2: 重试循环包住实际请求，不包参数处理和响应构造

```
 request(method, url, **kwargs)
     |
     +-- 参数处理 (headers 合并、proxy、timeout、redirect)
     |      无副作用，重试安全，放在循环外
     |
     +-- for attempt in 0..2:
     |       try:
     |           with self._client.request(...) as raw:
     |               记录重定向中间跳的 Cookie
     |               记录最终响应的 Cookie
     |               构造 requests.Response
     |           return response
     |       except 链路瞬断 且 attempt < 2:
     |           sleep(1.5s * (attempt + 1))
     |           continue
     |
     +-- 异常映射 (含新增 DecodingError / BodyError)
```

**理由**：链路瞬断时没有可用的响应，不会完成 `with` 块，Cookie 记录不会执行；重试成功后的处理路径与不重试时完全一致，无需单独保证。

**超时的特殊情况**：读响应超时时请求已发出，服务端可能已处理。传输层无法识别这一点，重试照常进行，见 D1a。

### D3: CSRF 的 403 重试写在 `get_csrf_token` 内部，每次重试前清熔断

```
 get_csrf_token
     |
     +-- for attempt in 1..3:
     |       resp = session.get(...)
     |       |
     |       v  _observe_response_for_circuit_breaker: 403 -> 设置 blocked_until
     |       |
     |       if resp.status_code == 403 且 attempt < 3:
     |           reset_retryable_circuit(session)      <-- 必须在下一次 session.get 之前
     |           sleep(5s * attempt)
     |           continue
     |       resp.raise_for_status()
     |       return csrfToken
```

**理由**：熔断在响应被观察到时设置，早于 `raise_for_status()`。不在重试前清除，下一次 `session.get` 会在 `_raise_if_circuit_open()` 处抛出「熔断冷却」而非发出请求。

用状态码判断而非捕获 `HTTPError`，避免依赖异常对象上的 `response` 属性。

**备选**：*在 `BrowserSession` 层对 CSRF 的 403 不设熔断*。放弃。熔断的消费方多，改触发条件影响面大，与本次缩小范围的目标相反。

### D4: `follow_authorize` 去掉外层循环，保留 document-navigation ID 轮换

去掉 `max_attempts` 循环，函数体只保留一次导航。**保留** `_rotate_document_navigation_id(session)` 及其在「收到 HTTP 响应之后、`raise_for_status()` 之前」的位置。这个边界是 HAR 对照确认过的浏览器行为，与重试无关。

重定向内部的 31 次循环是单次导航的组成部分，不动。

### D5: 查活链的 403 排除只改 `_network_preflight_with_retry` 一处

**决定**：在该循环的 `except` 中，先用状态判定排除 403，再走原有的 `_is_retryable_network_error`。

**理由**：`_is_retryable_network_error` 有 5 个使用点。`"403"` 这个关键词被 callback、session、OTP、重认证回退共用，直接从 `_RETRYABLE_NETWORK_HINTS` 删除会改变这 4 处的行为，超出确认范围。

**查活链改后的重试归属**：

```
 _network_preflight_with_retry  (外层, 4 次)
     |
     +-- get_csrf_token
     |       403          -> 内部重试 x3 (5s/10s)，耗尽后抛出，外层不再重试
     |       链路瞬断    -> 传输层先重试 x2，耗尽后抛出 -> 外层重试
     |
     +-- probe_auth_session / signin_openai
             全部错误     -> 直接抛出 -> 外层按原规则重试 (含 403)
```

signin 的 403 在查活链仍由外层重试，与现状一致。本次只消除 CSRF 403 这一步的叠加。

链路瞬断在查活链会经过两层重试：传输层 2 次，耗尽后抛出，外层按 `_is_retryable_network_error` 的关键词匹配决定是否再试（最多 4 次）。按实测消息文本核对，传输层耗尽后的各类异常都能命中外层关键词（`timeout`、`proxy`、`connection`、`reset` 之一），因此最坏 12 次请求。这个匹配依赖 wreq 消息的内部文本：消息中固定的 `proxy: ProxyIdentity {...}` 字段使所有带 `connect_info` 的异常都命中 `proxy`，即使请求未走代理；不带 `connect_info` 的（如响应体不完整后的 `is_connection_reset error`，`kind: Decode`）靠消息里的 `connection reset` 命中。所以外层重试对传输层不重试的 `RequestError`（请求已发出后关闭，`is_request error`，只命中 `proxy`）和响应体不完整同样生效：它们在传输层直接抛出，到了外层仍会被重试最多 4 次。外层重试是既有行为：改动前 `RequestError` 已被映射为 `requests.ConnectionError`，`DecodingError` 以 wreq 原生类型穿透，两者的 `str(exc)` 都是 wreq 原始消息，与改动后相同，命中结果不变，本次不动。

### D6: 重试次数与间隔做成配置项

沿用 `config/openai_protocol.py` 的现有约定：模块级常量 + `apply_env_overrides`，并在 `config/__init__.py` 的导出列表登记。

- `OPENAI_CSRF_403_RETRY_MAX_ATTEMPTS = 3`
- `OPENAI_CSRF_403_RETRY_BASE_DELAY = 5.0`
- `WREQ_TRANSIENT_RETRY_MAX_ATTEMPTS = 2`（重试次数，不含首次）
- `WREQ_TRANSIENT_RETRY_BASE_DELAY = 1.5`

**备选**：*硬编码*。放弃。现有重试参数全部可配置，单独这两个硬编码会形成两套约定。

注意 `WreqSession` 目前不依赖 `config` 模块，旁路模块（邮箱、接码）单独构造它。重试参数通过构造函数默认值注入，默认值在 `WreqSession` 内从 `config` 读取时需惰性导入，避免给旁路模块引入新的导入期依赖。

### D7: 等待使用可中断的 sleep

`openai_auth.py:314` 已使用 `job_context.interruptible_sleep`。CSRF 的 403 等待沿用，worker 收到停止请求时不会卡在 10 秒等待里。

传输层重试没有任务上下文，用普通 `time.sleep`。单次最长 3 秒，可接受。

### D8: 用结构化分类替代 OSS 的消息关键词分类

OSS 的 `registrar.classify_error` 按错误消息的关键词把失败分成 `network` / `account` / `unknown`。CSRF 的 TLS 错误耗尽后抛一条含「TLS 握手失败」字样的 `RuntimeError`（`auth_flow.py:2188-2192`），就是为了命中 `"tls"` 模式被判成 `network`，邮箱号放回 available。

本地不用消息关键词。理由是本地已经有 OSS 没有的结构化信号：`_RegistrationFailure.stage/category`、`RegistrationGeoError.category/stage`、`SentinelRequestError.category`、`AccountUnusableError.error_code`，以及 `requests` 的异常类型。`run_registration` 的 `except`（`registration_flow.py:626-630`）已经把 `category` 和 `stage` 收进 `error_code`、`error_stage`。

**现状的一个缺口**：`error_code` 只对带 `category` 的异常有值，`requests.ConnectionError`、`Timeout`、`ProxyError`、`HTTPError` 这四类没有 `category` 属性，`error_code` 为空。异常类型名只在 `:638` 的日志里出现，任务记录里只有消息文本。所以任务级重试无法从任务记录判断「原失败是不是网络类」。

**决定**：新增独立的失败类别字段，在失败时由分类器判定并写入任务记录，邮箱处理和重试共用。不复用 `error_code`，因为它的语义是「具体的错误码」，取值集合是开放的；失败类别是封闭的三值枚举。

### D9: 不对齐 OSS 的 Sentinel 异常放行

OSS 在 `sentinel.py:86-94` 和 `sentinel_quickjs.py:318-331` 的 catch-all 中专门放行 TLS 错误，避免网络瞬断被包成「QuickJS 异常」、掩盖真因。

本地 `request_sentinel_token`（`openai_auth.py:452-457`）的网络错误分支已经保留原始异常类型名，并用 `category = "sentinel_network_failed"` 与解析失败、求解失败区分开。结构化程度高于 OSS 的原样上抛。

**决定**：不改。本地已解决 OSS 当年修的那个问题。

### D10: 重试嵌套的位置

`_request_with_proxy_retry`（`openai_auth.py:292`）是步骤级重试，最多 3 次，有两个调用方：登录预检（`:334`）和 Sentinel `/req`（`:443`）。加上传输层重试后，这两个位置最坏 3 × 3 = 9 次请求。

**决定**：本次不调整 `_request_with_proxy_retry`。它服务的两个位置不在「正式 CSRF 到 authorize」的范围内，改它属于另一个决定。

### D11: 失败类别是封闭三值枚举，分类器只看类型和结构化字段

**决定**：失败类别 `failure_category` 取值 `network` / `account` / `unknown`。分类器是一个纯函数，输入异常对象，输出类别。判定顺序：

```
classify_failure(exc)
    |
+-- isinstance(exc, AccountUnusableError)                         -> account
|
+-- isinstance(exc, (requests.ConnectionError,
                    requests.Timeout, requests.ProxyError))     -> network
|       (requests.SSLError 是 ConnectionError 的子类，一并归 network)
|
+-- isinstance(exc, requests.HTTPError)
|       且 exc.response.headers["cf-mitigated"] == "challenge"  -> network
|       (J1': 只有 CF 挑战的 403; 其它 403/409/429/5xx 落到 unknown)
|
+-- isinstance(exc, SentinelRequestError)                       -> network
|       (三种 category 全部归 network, 见下)
|
+-- isinstance(exc, SentinelSDKError)
|       且 exc.category in 求解类六个值                          -> network
|
+-- isinstance(exc, RegistrationGeoError)
|       且 exc.category == "proxy"                              -> network
|
+-- 其余全部                                                   -> unknown
```

**`cf-mitigated` 的读取**：只读 `exc.response.headers`，不解析响应体，不匹配消息文本。响应头大小写不敏感，值精确等于 `challenge`（与 `core/infra/http_diagnostics.py:14` 一致）。`exc.response` 为 `None` 时按非 CF 处理。

**`SentinelRequestError` 为什么三种 category 全归 `network`**（B 对齐 OSS）：它的 `category` 有默认值（`openai_auth.py:38`），实际只有三种取值。OSS 两处判定都把求解失败当网络问题：邮箱回池的 `_NETWORK_ERROR_PATTERNS`（`webui/registrar.py:188-200`）含 `sentinel quickjs` 和 `/sentinel/req`，`sentinel.py:81` `:83` `:94` 三种消息全部命中；整轮重试的对应见下表。

| category | 抛出点 | OSS 对应 | 结论 |
|---|---|---|---|
| `sentinel_network_failed` | `:454`，`/req` 网络失败 | TLS 错误原样上抛（`sentinel.py:92-93`） | `network` |
| `sentinel_service_failed` | `:454`，`/req` 返回 HTTP 错误码 | `[INFERENCE]` `sentinel.py:94` 的 `Sentinel QuickJS 异常`，命中关键词 `sentinel quickjs` | `network` |
| `sentinel_challenge_failed`（默认） | `:432` `:461` `:463` `:485` `:495` `:499` `:507` `:511` `:513` | `:461` `:463` 对应 `sentinel.py:81` 主 token 缺失，命中 `sentinel quickjs` | `network` |

第三行里 `:485` 之后是本地的防御性校验（token 是否属于当前 challenge），OSS 没有对应物。它们没有独立 category，分类器无法与 `:461` `:463` 区分。一并归 `network` 是可接受的过宽：校验失败说明 challenge 上下文已失效，整轮重来会重建上下文。

**`SentinelSDKError` 的两组**（`sentinel_runner.py:91`）：

- 求解类归 `network`：`protocol_error`、`flow_mismatch`、`invalid_challenge`、`no_requirements`、`empty_main_token`、`token_context_mismatch`。对应 OSS `sentinel.py:81` `:94`。
- 环境类归 `unknown`：`sdk_missing`、`node_missing`、`requirements_missing`、`sdk_exec_failed`、`closed`、`timeout`。本地缺 Node 或 runner 文件，换出口和换邮箱都没用。

两组不是全集。`sentinel_runner.py` 的 20 处构造点里 category 字面量共 13 个，上面两组各 6 个，还有一个 `reuse`（`:170`，同一 `SentinelSDK` 实例被第二次调 `requirements()`，是调用契约被违反的本地编程错误）落「其余 → `unknown`」，不单列。另有 `:387` 的 category 是子进程消息里透传的动态值，同样落 `unknown`。

**与 OSS 的一处有意差异**：OSS 的 `sentinel.py:83` `Sentinel QuickJS 模块缺失` 同样命中 `sentinel quickjs` 关键词，归 `network`、邮箱回池。本地把对应的环境类归 `unknown`。理由是这条规则在本地的后果是邮箱被回池后反复领取、反复因缺文件失败；OSS 那边是消息文本的巧合命中，不是刻意设计。

**归 `unknown` 的几类及理由**：

- `requests.HTTPError` 中不带 `cf-mitigated: challenge` 的：403、409、429、5xx。其中 `already exists` 见 D1a。非 CF 的 403 里哪些是出口维度、哪些是邮箱维度，本地没有信号，OSS 也区分不了。
- `SentinelSDKError` 环境类：见上。
- `RegistrationGeoError` 的 `geo_*` 四类：本地预检阶段的地域校验（信息缺失、国家与时区不相容、观测响应无法解析、观测服务不可达）。OSS 有出口校验但失败类型不对应，见 Open Questions 的 K。其中 `geo_service` 只承载 HTTP 非 200 与未知异常；预检 trace 的传输层异常改由 `proxy` 类别承载（D20）。
- `_RegistrationFailure` 的 7 种 category（`registration_flow.py` 全部 7 处构造点，`:203` 是两个取值的条件表达式）：`classification_probe_failed`（`:351`）、`account_state_unknown`（`:371`）、`email_state_conflict`（`:375`）、`auth_step_unknown`（`:467`）、`existing_profile_unexpected`（`:484`）、`credential_write_failed` 与 `credential_write_unconfirmed`（`:203` `:211`）。都是流程判定失败：服务端状态与预期不符，或本地凭据保存出了问题，换出口重来不会改变结果。
- `PasswordRejectedError`：服务端明确拒绝密码，流程内部已有 OTP 回退，能抛到这里说明回退也失败。

**默认归 `unknown` 而不是 `network`**：`unknown` 的邮箱处理与现状完全一致（标 failed），所以分类器漏掉或判错任何一种异常，行为都退回现状，不会更差。

**备选**：*复用 `error_code`，把 `network` 写进去*。放弃。`error_code` 对带 category 的异常已经是具体错误码（如 `sentinel_challenge`、`geo_mismatch`），混入一个类别值会让同一字段的语义不唯一。

### D12: 整轮重试的边界是 `user/register` 成功返回

**决定**：只有「账号创建之前」的网络类失败才整轮重试。边界判定用 `progress`：

```
可整轮重试 = failure_category == "network"
             AND account_kind in ("unknown", "new_password", "new_passwordless")
             AND password_state == "not_attempted"          <- register_user 尚未调用
             AND credential_persistence == "not_attempted"
```

`password_state` 在 `register_user` 调用前置为 `submission_unknown`（`:409`），成功后置 `confirmed`（`:422`）。所以 `password_state == "not_attempted"` 等价于「`user/register` 请求还没有发出」。

**为什么不在账号创建之后重试**：此时服务端可能已有账号，重试一轮会进入 `existing` 分支走登录。这条路本身是通的（`:384` 和 `:436-453`），但 `existing` 分支失败时邮箱不动（`:642`，见 D16），重试会让一个状态不确定的邮箱多走几遍。保守做法是不重试，邮箱按失败类别处理。

**`account_kind == "existing"` 不重试**：已有账号登录失败只影响任务，这是现状（`:642` 的注释）。整轮重试不改变这个语义。

**Sentinel 求解失败在三个位置发生，边界判定对三处都成立**：`request_sentinel_token` 在 `registration_flow.py` 有三个调用点。

| 位置 | flow | 所在步骤 | 调用时的 `password_state` | 归 `network` 后 |
|---|---|---|---|---|
| 1 | `:406` | `username_password_create`，`register_user` 之前 | `not_attempted` | 整轮重试 |
| 2 | `:163` | `email_otp_validate`，在 `_validate_registration_otp`（`:154`）内，由 `:472` 调用；受 `SEND_SENTINEL_ON_EMAIL_OTP_VALIDATE` 控制，默认开启（`config/openai_protocol.py:45`） | `confirmed` | 不整轮重试，邮箱回池 |
| 3 | `:487` | `oauth_create_account`，OTP 验证之后、`create_account` 之前 | `confirmed` | 不整轮重试，邮箱回池 |

边界由 `password_state` 判定，不按调用点枚举，所以三个位置不需要分别写规则。

**CF 挑战的 403 与正式 CSRF 的同会话重试是两层**：正式 CSRF 的 403 先由 `get_csrf_token` 同会话重试（D3，最多 3 次）。三次都返回带 `cf-mitigated: challenge` 的 403 后抛出 `HTTPError`，分类器归 `network`，再触发整轮重试（最多 3 轮）。最坏 3 × 3 = 9 次 CSRF 请求，中间穿插两轮换出口。两层的目的不同：同会话重试应对瞬时的 CF 拦截，整轮重试应对出口被标记，不是重复重试。authorize 的 403 没有同会话重试（D4），带 `cf-mitigated` 时直接进整轮重试。

**账号创建之后网络类失败仍然回池**：这类邮箱回到 `available` 后再被领取，服务端会返回 `existing`，流程走登录分支把它登进来。本地 `is_complete_account`（`db.py:856`）只把 `registration_state == "password_set"` 的中间凭据视为不完整，所以只存了密码的邮箱不算「已有账号」，回池不受阻。

**备选**：*`password_state == "submission_unknown"` 时排除重试和回池*。放弃，用户已否决。理由是服务端有账号不等于邮箱不可用，本地流程本身支持已有账号登录。

### D13: 整轮重试放在 `run_registration` 内层，单轮拆成独立函数

```
run_registration(email, name, birthday, proxy, otp_code, batch_dir, on_email_acquired)
    |
    +-- 领取邮箱 (email 为空时)                          <-- 只做一次
    |
    +-- for round in 1..OPENAI_REGISTRATION_ROUND_MAX_ATTEMPTS:
    |       result = _run_registration_round(email, name, birthday, proxy, ...)
    |       |                                           每轮: 新建 BrowserSession
    |       |                                           结束: close_browser_session
    |       |
    |       +-- result.success -> return result
    |       +-- 不可整轮重试 (D12) -> return result
    |       +-- StopRequested -> return result
    |       +-- 可整轮重试 且 round < max:
    |               interruptible_sleep(间隔)
    |               continue
    |
    +-- return 最后一轮的 result
```

**理由**：

- 邮箱领取在循环外，所以重试期间邮箱一直是 `registering`，天然不会被其它任务领走（E）。不需要在轮次之间做任何邮箱状态操作。
- 单轮函数自带 `try/except/finally`，每轮新建 `BrowserSession(proxy=proxy)`，所以设备 ID、Cookie、熔断状态都是新的。`proxy=None` 时 `_proxy_pool_selected` 为 True（`session.py:124`），新会话从 `config.proxy.PROXY_POOL` 重新选出口；池只有一个候选或固定代理时沿用原路由。
- 失败分类和邮箱处理都留在单轮函数的 `except` 里，外层循环只看 `failure_category` 和 `progress` 决定是否继续。
- `run_registration` 的签名和返回结构不变（只在返回字典里多一个 `failure_category` 键），`registration_service._run_one_job` 不需要知道重试存在；但任务层要把 `failure_category` 写进任务记录，见 D14 末尾。

**单轮函数的 `except` 中邮箱处理的改动**：现状是无条件调 `release_email_if_unconsumed`，只对 `account_kind != "existing"` 执行（`:642`）。改为：

```
category = classify_failure(e)
progress["failure_category"] = category
if email and progress["account_kind"] != "existing":
    if 可整轮重试(category, progress) 且 不是最后一轮:
        不动邮箱 (保持 registering)
    else:
        settle_email_on_failure(email, reusable=(category == "network"), note=...)   <- 见 D14
```

**停止不是失败类别**：`StopRequested` 继承 `RuntimeError`（`job_context.py:16`），会被这个 `except Exception` 接住。它没有失败类别，落进分类器会得到 `unknown`，结果是 `reusable=False`、邮箱标 `failed`，与改动前一致。不给它单独加回池分支，也不累计回池计数。`existing` 分支的守卫保持在最外层，停止和失败都不动它的邮箱。

**轮次之间不清熔断**：新会话的 `blocked_until` 初始为 0。

**备选**：*在 `_run_one_job` 层做整轮重试，失败后重新调 `run_registration`*。放弃。`run_registration` 失败时已经处理了邮箱（现状标 failed），外层再调一次会重新领取邮箱，变成领新号，与「用同一邮箱重来」矛盾。要避免这一点就得让 `run_registration` 知道「这次调用不要处理邮箱」，参数会变复杂。

### D14: 失败时邮箱的目标状态在一次迁移内决定

**现状的两个约束**：

- `release_unconsumed_outlook`（`db.py:2938`）对 `failed` 状态直接返回 False。如果先标 `failed` 再调恢复，第二次调用什么都不做。所以目标状态必须一次决定。
- `_transition_pool_row` 遵守 `can_transition`（`core/infra/contracts/email_pool.py:88-101`）：`registering -> available` 不在允许集合里。

**决定**：新增 `settle_failed_registration_email(email, *, reusable: bool, note)`，替代注册失败路径上的 `release_email_if_unconsumed` 调用。四个持久化池（outlook、generic_api、cloudflare_domain、icloud）共用这一个函数：持锁后用 `_pool_rows_for_email_locked`（`db.py:349`）按邮箱定位池和行，再按返回的 `kind` 调对应的保存函数。`release_unconsumed_*` 四个函数保留，其它调用方（如有）不受影响。

```
settle_failed_registration_email(email, reusable, note)
    with _LOCK:
        if is_complete_account(该邮箱的账号):  return False       <- 已有完整账号不动
        row = 按邮箱找池行
        if row is None:  return False
        if row.status != "registering":                              <- 被 set_email_pool_status 等
            logger.warning(...)                                      <- 人工路径改过
            return False
        if reusable:  回池计数加一, target 按上限取 "available" 或 "failed"     <- 伪码见 D18
        else:         target = "failed"
        _transition_pool_row(row, target, force=(target == "available"), note=note, ...)
        保存
        return True
```

`reusable=True` 时用 `force=True` 绕过 `can_transition`，与 `restore_email_pool_entry:447` 的做法一致。这是 `registering -> available` 的唯一一处自动迁移，只由失败类别触发。

**临时邮箱**（cloudflare / remail）：没有池行，继续走现有 `release_email_if_unconsumed` 的清理分支，`reusable` 对它无意义。

**`force=True` 的影响面**：`can_transition` 的注释是「禁止失败条目被复活」。本次的迁移是 `registering -> available`，起点不是 `failed`，不违反这条契约。`failed -> available` 仍然只能经 `restore_email_pool_entry`。

**备选**：*先标 `failed`，再用 `restore_email_pool_entry(source="retry")` 恢复*。放弃。两次持锁，中间有一个 `failed` 窗口；且 `release_unconsumed_outlook` 对 `registering -> failed` 之后紧接着的恢复没有原子保证。

**`existing` 守卫必须在改写后保留**：`_release_unconsumed_job_email`（`registration_service.py:88-104`）入口第一行是 `if job_context.is_current_account_task(): return`，它读 `progress["account_kind"]`（`job_context.py:69-71`）。这和 `registration_flow.py:642` 是同一道守卫的两层。现有测试 `test_existing_login_failure_or_stop_does_not_poison_claimed_email`（`tests/test_registration_pipeline.py:730-769`）对失败、停止、异常三种结局都断言 `existing` 分支的池行停在 `registering`。改调 `settle_email_on_failure` 时这个守卫要原样留在 `_release_unconsumed_job_email` 里，不进 `settle_email_on_failure`，因为后者在 `registration_flow.py` 的单轮函数里也被调用，那里的 `existing` 判断由调用方负责。

**停用判定优先于失败类别**：`_run_one_job` 在调 `_release_unconsumed_job_email` 之前先过 `_should_disable_failed_registration_email`（`registration_service.py:330-333`），命中则走 `_disable_job_email`（`:135-156`），后者经 `db.disable_registration_email` 直接改池状态，不经过 `release_email_if_unconsumed`。这条路径的判据是四个错误文本关键词（`:122-132`）：`/api/auth/session` 等待超时、邮箱提交后进入登录密码页、两类 `/log-in/password` 落地。与回池不冲突的理由是执行顺序：停用判定在 `:330`，分类和邮箱处理在它之后，命中停用关键词的失败根本不进入分类器。这里不能靠「这些失败的类别一定是 `unknown`」来保证：前两个关键词在 `core/`、`webui/`、`main.py`、`config/` 的源码里只有判定函数自己这一处，没有找到抛出这些文本的代码，它们实际由哪种异常类型携带无法确认。

**决定**：保留停用判定在失败类别之前。命中停用关键词的失败走原路径置 `disabled`，不进入分类器和 `settle_failed_registration_email`。`_release_unconsumed_job_email` 的内部实现改调 `settle_email_on_failure`，所以它的每个调用点都要显式传 `reusable`，包括 `request_stop_job` 的那一处。

**任务层写入失败类别**：`run_registration` 把 `failure_category` 放进返回字典和 `progress` 快照。`_run_one_job` 的失败分支（`registration_service.py:317-328`）调 `db.update_job` 时新增 `failure_category` 参数，`update_job`（`db.py:3400-3442`）新增同名关键字写入任务行。

**任务层的邮箱处理路径**。`_run_one_job` 调 `_release_unconsumed_job_email` 共四个调用点，对应五种情况，改后各自的 `reusable`：

| 位置 | 触发条件 | 任务终态 | `reusable` |
|---|---|---|---|
| `:317-333` 失败分支 | `run_registration` 返回 `success` 为假 | `failed` | `(result or {}).get("failure_category") == "network"` |
| `:351-376` 外层 `except Exception` | `run_registration` 没接住的异常，停止位未置 | `failed` | 对 `exc` 调 `classify_failure`，结果为 `network` 时为真 |
| `:351-376` 同上 | 同上，但 `:357` 的停止位已置 | `stopped` | 固定 `False` |
| `:290-291` `is_stop_requested` 分支 | 返回后才发现停止请求 | `stopped` | 固定 `False` |
| `:341-342` `except StopRequested` | 停止检查点抛出 | `stopped` | 固定 `False` |

**停止本身不回池、不累计回池次数**：三条停止路径都传 `reusable=False`。它们只在池行仍是 `registering` 时生效；`settle_failed_registration_email` 对非 `registering` 行返回 False，所以上一轮已经回池的邮箱不会被停止改成 `failed`。

**`_run_one_job` 之外的第五个调用点**：`request_stop_job`（`registration_service.py:688`）在「任务状态是 running 或 stopping，但找不到运行中的任务实例」时把任务直接标 `stopped`，并调 `_release_unconsumed_job_email` 回收任务行上的邮箱。它是停止路径，`reusable` 固定为 `False`，不分类。任务 12.1 改的是 `_release_unconsumed_job_email` 的内部实现，这个调用点必然受影响，`reusable` 必须在这里显式传入，不能漏。

停止时的邮箱结果取决于停止发生在哪，三行停止路径不改变这个事实：

| 停止点 | 邮箱是否已被单轮函数处理 | 最终邮箱 |
|---|---|---|
| 单轮内部，`check_stop_requested` 抛 `StopRequested` | 是，`flow:642` 以 `reusable=False` 处理 | `failed`，计数不变 |
| 轮次之间，`interruptible_sleep` 抛 `StopRequested` | 否。能走到轮次之间，说明上一轮可整轮重试且不是最后一轮，邮箱没有处理 | `failed`，计数不变 |
| `run_registration` 返回后才发现停止位 | 是，最后一轮已按失败类别处理 | 网络类为 `available`，计数已加一；否则 `failed` |

第一种和第二种的结果相同：停止发生在重试期间时，邮箱还没有回池，停止路径以 `reusable=False` 把它标 `failed`。整轮重试期间邮箱保持 `registering`（D13），这是「不被其它任务领走」的前提，代价是停止时该邮箱不会回池，需要时由人工恢复。进程在轮次之间被中断时结果相同，见 D19。

外层 `except Exception` 需要调整语句顺序：现状是先处理邮箱（`:353-356`）、再查停止位（`:357`）。改后 `:357` 的检查要提到邮箱处理之前，否则停止期间撞上网络类异常时，邮箱会先回池并计入回池次数，任务再标 `stopped`，与「停止期间的单轮内部停止 → `failed`」不一致。

前两条失败路径的邮箱处理先过停用判定（`:330`、`:353`），命中则走 `_disable_job_email`，不进分类器。任务行的 `failure_category` 字段：失败分支由 `run_registration` 的返回值提供，`:317-328` 调 `db.update_job` 时新增该参数；外层异常分支对 `exc` 调 `classify_failure` 后写入。`update_job`（`db.py:3400-3442`）新增同名关键字。外层异常分支不做整轮重试。

**与 OSS 的一处差异：换出口前不探测**。OSS 的 `_pick_clean_proxy`（`_turb_bridge.py:490-529`）换出口前对每个候选做一次探测（`_probe_grade`），被 CF 拦的跳过，按「最久没用过」排序，最多探 6 个。本地的 `_next_pool_proxy`（`session.py:916-927`）是从 `PROXY_POOL` 随机取一个不同于当前的，不探测。所以本地的整轮重试可能连续换到同一个或同样被标记的出口，重试成功率会低于 OSS。探测是独立功能，本次不做。

### D15: 人工重试的原邮箱恢复与领取是一个原子动作（I1）

**现状**：`retry_job`（`registration_service.py:553`）对注册类重试传 `email=None`（`:581` `email=email if action == "codex" else None`），`create_retry_job`（`db.py:3349`）只是把这个值透传进子任务行，子任务执行时 `acquire_email()` 领新号。`_run_one_job` 没有读任务行邮箱的逻辑。

**决定**：

1. `db.restore_email_pool_entry` 的来源枚举增加 `"retry"`，记录 `manual_reactivated_by = "retry-job-<新任务 id>"`。函数内部逻辑不变。
2. 新增 `db.claim_specific_email(email, *, job_id) -> bool`：持锁，按邮箱找池行；状态为 `available` 则直接迁到 `registering`；状态为 `failed` 则先走恢复（`source="retry"`）再迁到 `registering`；其它状态返回 False。恢复和领取在同一把锁内，没有 `available` 窗口。
3. `retry_job` 在 `create_retry_job` 创建子任务**之后**、提交线程池**之前**调 `claim_specific_email`。成功则把邮箱写进子任务行；失败则子任务行的邮箱保持创建时的 `None`，子任务按现状领新号。
4. `_run_one_job` 读取任务行上的邮箱：非空时传给 `run_registration(email=...)`，跳过 `_prepare_registration_args` 的邮箱部分。
5. 子任务执行时 `_prepare_persisted_email_registration`（`registration_flow.py:135`）会校验池行状态是 `registering` 或 `used`，不是则抛错。这就是「指定了原邮箱但执行时已不可用」的失败路径，不回退到领新号。

**为什么在第 3 步才领取，而不是在创建子任务时**：`create_retry_job` 持的是任务表的锁，`claim_specific_email` 持的是邮箱池的锁。两个锁的获取顺序在全仓必须一致。`_active_registration_conflict_locked`（`db.py:378`）在持有邮箱池锁时调 `_load_jobs()`，所以顺序是「邮箱池锁 -> 任务表读」。如果先领邮箱再建任务，`registration_job_id` 登记不到；如果在一个锁里做两件事，要改 `create_retry_job` 的锁结构。分两步做，中间的窗口由「子任务行已存在且 pending」保护：`_active_registration_conflict_locked` 把 pending 任务算作活动任务，其它恢复路径不会动这个邮箱。

**不恢复的条件**（任一命中则领新号）：

- 原任务 `failure_category` 不是 `network`
- 原邮箱不在四个持久化池中（临时邮箱、手动输入）
- 池行状态是 `used` 或 `disabled`
- `is_complete_account` 为 True
- `_active_registration_conflict_locked` 返回非 None

**与 OSS 的差异**：OSS 没有人工重试入口，这条是本地新增行为，没有对齐对象。

**与 D18 上限的交互**：因达到回池上限而标 `failed` 的邮箱，原任务的 `failure_category` 仍是 `network`，所以人工点「重试」满足恢复条件，恢复后计数清零，邮箱重新从 0 开始。上限拦的是自动循环，人工显式决定重试就重新计数；代价是人工每点一次「重试」，这个邮箱可以再白跑最多 9 轮。不另加限制。实现上注意 `claim_specific_email` 是 `restore_email_pool_entry` 的调用方，清零要写在它自己持锁加载的 `row` 上，见 D18 的「清零的四处」。

**备选 I2**：*`failed -> registering` 直接迁移，绕过 `available`*。用户已选 I1，放弃。

### D16: 已有账号登录失败后池邮箱停在 `registering` 是既有缺口

**事实**：`registration_flow.py:642` 和 `registration_service.py:90` 都对 `account_kind == "existing"` 跳过邮箱回收。池邮箱被领取后是 `registering`，如果走 `existing` 分支并失败，进程运行期间没有任何路径把它迁走：

- 不能被领取（`claim_next_*` 只领 `available`）
- 不能被恢复（`restore_email_pool_entry:439` 对 `registering` 抛 `registration_busy`）
- 不能被人工改状态：`set_email_pool_status` 对目标是 `available` 的请求转交 `restore_email_pool_entry`（`db.py:507-514`）；对其它目标在 `:522` 被 `_active_registration_conflict_locked` 拦下，`registering` 返回 `registration_busy`
- 也不能被删除：`check_email_pool_delete`（`db.py:570-572`）经 `_active_registration_conflict_locked` 拦截，后者对 `registering` 状态无条件返回 `registration_busy`（`db.py:381-382`），不看任务是否还在；`force=True` 只绕过账号保护（`:574`），绕不过它

唯一的出口是进程重启：`migrate_email_pool_statuses`（`db.py:743`）把没有活动任务的 `registering` 行迁到 `failed`，之后可人工恢复。

**什么时候会发生**：`submit_registration` 每个任务从池里领号（`registration_service.py:457`）。如果领到的邮箱在服务端已被注册，authorize 后 `get_registration_state` 返回 `existing`，就进入这个分支。登录成功则 `save_account_data` 把池行标 `used`；登录失败则卡住。

**决定**：不在本次范围内。整轮重试对 `existing` 不生效（D12），失败分类不改变 `existing` 的邮箱处理，所以本次改动不会让这个缺口更频繁。记录在此供后续立项。

**对 D15 的影响**：`registering` 状态不能被人工改动，所以重试子任务排队期间该邮箱的状态是稳定的。

### D17: 整轮重试的次数、间隔与配置

- `OPENAI_REGISTRATION_ROUND_MAX_ATTEMPTS = 3`（总轮数，含首轮）
- `OPENAI_REGISTRATION_ROUND_RETRY_DELAY = 5.0`（轮次之间的基础等待。第 N 轮失败后等「基础 × N」，即第 1 轮后 5s、第 2 轮后 10s；最后一轮失败后不等）

等待用 `job_context.interruptible_sleep`，用户点停止时不会卡在等待里。

设为 1 等价于关闭整轮重试，行为回到只有传输层和步骤层重试。

**备选**：*总轮数默认 6，与 OSS 的 `MAX_IP_TRIES` 一致*。放弃。OSS 的 6 轮覆盖的是从 warmup 到账号创建的整个区间，且每轮重建成本更低；本地的预检已经有自己的换出口循环（`registration_preflight.py:73-101`，默认次数由 `OPENAI_REGISTRATION_WARMUP_ATTEMPTS` 控制），两层叠加后 3 轮已经不少。

### D18: 网络类回池有次数上限，达到后标 `failed`

**决定**：池行新增字段 `network_release_count`，记录该邮箱因网络类失败回池的次数。`settle_failed_registration_email(reusable=True)` 时先加一，再判断：

```
settle_failed_registration_email(email, reusable=True, ...)
    ...
    n = int(row.get("network_release_count") or 0) + 1
    row["network_release_count"] = n
    if EMAIL_NETWORK_RELEASE_MAX_COUNT > 0 and n >= EMAIL_NETWORK_RELEASE_MAX_COUNT:   # 默认 3
        target = "failed"          # 不再回池, 可人工恢复
        note = f"网络类失败已达 {n} 次, 停止自动回池"
    else:
        target = "available"
    _transition_pool_row(row, target, force=(target == "available"), ...)   # 只有回池需要绕过 can_transition
```

阈值 3 与整轮重试的轮数对齐：一个邮箱最多为网络问题白跑 3 × 3 = 9 轮。达到阈值的这一次仍然计入，所以第 3 次网络类失败时邮箱直接标 `failed`，不经历第 3 次回池。

**为什么标 `failed` 而不是 `disabled`**：`failed` 可经 `restore_email_pool_entry` 人工恢复，`disabled` 是审计终态（`migrate_email_pool_statuses:729` 对它不做任何改动）。达到上限说明出口持续有问题，不是邮箱本身有问题，留人工恢复的口子。

**字段兜底**：`_normalize_pool_row`（`db.py:243-271`）加 `row.setdefault("network_release_count", 0)`。四个池的加载路径和启动迁移都经过这个函数，旧数据读出来就是 0。

**清零的四处**：

| 入口 | 位置 | 说明 |
|---|---|---|
| 人工改状态为 `available` | `set_email_pool_status:507-514` → `restore_email_pool_entry` | 在 `restore_email_pool_entry:456-458` 那组重置里加一行 |
| 任务重试复用原邮箱 | `claim_specific_email`（D15） | 它自己就是 `restore` 的调用方，属于下面「调用方覆盖」那一类：持锁加载的 `row` 是恢复前读的旧字典，必须在这个 `row` 上显式写 0 再迁 `registering` |
| 显式重新导入 | `db.py:2676` `:3014` `:3768` `:3897` | **调用方在 `restore` 返回后会用自己的旧字典再保存一次**（`:2685-2689`、`:3023-3026`、`:3776-3779`、`:3906-3909`），把 `restore` 里写的字段覆盖掉。所以这四处必须在调用方字典上手动清零，只改 `restore_email_pool_entry` 不够。domain 池那处是 `claim_next_domain_email`，清零后直接迁 `registering`，不停在 `available` |
| 注册成功 | `_mark_pool_row_used`（`db.py:274`） | 池行变 `used`，计数不再有意义，一并清零。`mark_registration_success`（`:630`）和导入已注册账号（`:1741` `:1751` `:2799` `:2832` `:2852`）都经过它，改这一处即可 |

**不触发清零的路径**：`migrate_email_pool_statuses:743` 在进程重启时把无活动任务的 `registering` 行迁到 `failed`，这条路径不经过 `settle`，计数不变。行为：一个已回池 2 次的邮箱在第 3 次注册时进程重启，被迁到 `failed`，计数停在 2；人工恢复后清零。

**显式重新导入能否绕过上限**：能，但需要人工开启 `reactivate_existing` 并重新导入（`:3010`）。按 D15 的同一规则处理，人工显式决定重用就重新计数。

**配置**：`EMAIL_NETWORK_RELEASE_MAX_COUNT = 3`，放在 `config/email.py`（与 `OTP_MAX_WAIT` 同处），登记到 `apply_env_overrides`。设为 0 等价于关闭上限，回到「无限回池」，判定里要带上 `> 0` 的前提，否则 0 会让第一次网络类失败就标 `failed`。

**备选**：*不加上限，与 OSS 一致*。用户已否决。理由是出口持续不通时，一个坏出口能把池里的邮箱逐个拖进循环。

**备选**：*把计数放在任务表而不是池行*。放弃。上限约束的是邮箱，不是任务链；人工重试会新建子任务，按任务链计数会被绕过。

### D19: 启动时收口残留的注册任务

**事实**（已核实）：WebUI 重启后，任务表里残留的注册任务不会恢复执行，也不会被取消，它们领取的邮箱会永久锁在 `registering`。

- 写任务表的注册任务只有两处提交点，都在用户动作触发的函数里：`submit_registration`（`registration_service.py:459`）、`retry_job`（`:612` `:614`）。全仓另有两处 `executor.submit`，与任务表无关：`webui/app.py:2094`（Codex 补跑批处理）、`main.py:230`（CLI 批量入口，直接跑 `run_one_batch_item`，不写任务行，进程被杀后没有残留任务可收口）。启动段 `webui/app.py:450-468` 的恢复覆盖邮箱池、账号状态、Codex 补跑、套餐查询、查活、2FA，没有注册任务。
- `shutdown_executor`（`registration_service.py:205`）是 `shutdown(wait=..., cancel_futures=False)`，不处置排队中的任务。`cancel_pending_jobs`（`:639`）只有一个调用点，是用户点「取消排队任务」（`webui/app.py:2347`）。
- `migrate_email_pool_statuses`（`db.py:696-709` `:743`）把 `pending` / `running` / `stopping` 算作活动任务，每次启动和每次 `submit_registration`（`:448`）都据此把对应池行保留在 `registering`。
- 锁住后没有直接的人工出口：`_active_registration_conflict_locked`（`db.py:381-382`）对 `registering` 无条件返回 `registration_busy`；`get_retry_info`（`registration_service.py:512`）对 `pending` 返回不可重试。用户要先点「取消排队任务」，再触发一次迁移（提交任意新任务）才能解开。`running` / `stopping` 残留只能逐个点「停止」，走 `request_stop_job:676-694` 的「任务实例不存在」分支。

这个缺口在改动前就存在。D15 的重试子任务在 `claim_specific_email` 领走原邮箱之后重启，会多一个进入它的入口。

**决定**：新增 `db.recover_interrupted_registration_jobs() -> int`，照 `recover_interrupted_live_checks`（`db.py:2527-2544`）的惯例，在服务启动时把残留的注册任务改成终态，不恢复执行。

| 残留状态 | 收口为 | 理由 |
|---|---|---|
| `pending` | `cancelled` | 没有执行过，与用户手动取消排队的结果一致 |
| `running` | `stopped` | 执行中被中断，与用户手动停止的结果一致 |
| `stopping` | `stopped` | 已请求停止、等线程退出时被中断 |

`error` 写「WebUI 重启，任务未执行」（`pending`）或「WebUI 重启，任务执行中断」（另两种），`completed_at` 取当前时间。处理任务表里全部类型：`registration` 和 `codex_retry`（`create_job:457` 用默认值，`create_retry_job` 经 `registration_service.py:579` 取这两个值之一，全仓没有第三种）。`codex_retry` 的任务行此前同样没人收口：`recover_interrupted_codex_operations`（`db.py:1828-1852`）只改账号表的 `codex_operation_status`，不碰任务行。收口它不需要释放补跑占位，`codex_retry_service._RETRYING` 和 `operation_lock._OWNERS` 都是进程内状态（`codex_retry_service.py:15`、`operation_lock.py:8`），重启即空。

**邮箱**：收口函数不改邮箱池。它跑在 `migrate_email_pool_statuses` 之前，迁移随后看到这些任务已不是活动任务，按池行当时的状态处理，不区分任务被中断时走到了哪一步（进程被杀时没有可靠信号，`progress` 快照只在内存里）：

| 被中断时池行状态 | 迁移后 | 依据 |
|---|---|---|
| `registering` | `failed`，回池次数不变 | `:743`，可人工恢复 |
| `registering`，且本地已有该邮箱的完整账号 | `used` | `:735` 的优先级高于 `:743` |

整轮重试期间池行保持 `registering`，所以在轮次之间被中断的任务落在第一行，与单轮执行中被中断的结果一致。用户停止和进程中断在轮次之间的效果相同。

**调用位置**：两个启动入口各加一行，放在 `migrate_email_pool_statuses()` 之前：`webui/app.py:451` 和 `main.py:118`。`submit_registration:448` 那次迁移不需要改，服务启动时已经收口过。顺序只有这两个入口需要保证，配合效果由用例覆盖（收口前迁移保留 `registering`，收口后迁移落到 `failed`）。

**备选**：*新增 `db.recover_on_startup()`，把收口和迁移包成一个函数，两个入口只调它*。放弃，虽然它把顺序封装在了一处。有 4 个测试文件把启动段的 `db.*` 调用逐个打成桩（`tests/test_account_filters.py:144`、`test_archive_removal.py:49`、`test_codex_export_filter.py:16`、`test_agent_removal_contract.py:54`），桩的是 `migrate_email_pool_statuses` 这个名字。换成新函数后这些桩全部失效，启动段的真实迁移会在这些测试里执行，返回值形状也要跟着改。保持两个独立函数，只需给这 4 处各追加一行对新函数的桩。

**为什么直接收口，不恢复执行**：重启后任务丢失了内存里的 `progress` 和会话，无法判断走到了哪一步。`running` 的任务可能已过 `user/register`，重新执行会按 D12 讨论过的情况落到 `existing` 分支；`pending` 的任务虽然可以安全重提，但和 `running` 用两套处理会让启动逻辑变复杂，而它们都有「重试」按钮作为出口。

**收口后的重试**：三种终态都 `retryable`（`registration_service.py:512`）。`stopped` 的任务 `failure_category` 为空（进程被杀时没走到分类器），按 D15 领新号，不复用原邮箱。原邮箱是 `failed`，用户要复用需先在邮箱池页面人工恢复它。

**备选**：*启动时把 `pending` 任务重新提交执行*。放弃。见上一段的理由，且重提需要重建线程池与任务的对应关系，超出本次范围。

**备选**：*全部标 `failed`，与其它 `recover_interrupted_*` 完全一致*。放弃。`pending` 的任务没有失败过，标 `failed` 会让失败统计里多出一批重启导致的记录，语义也不对。

### D20: 预检出口观测的传输层失败按类型归 proxy，warmup 保持现状

**决定**：`classify_geo_exception()` 先按异常类型判断，再退回现有的消息关键词：

```
isinstance(exc, (requests.ProxyError, requests.ConnectionError, requests.Timeout))
    -> (CATEGORY_PROXY, "网络连接/代理失败")
否则按现有 marker（407 / proxy auth / proxy connect / socks）兜底
```

`CATEGORY_PROXY` 在 `classify_failure` 里映射为 `network`。trace 的 HTTP 非 200（407 除外）、响应解析失败、画像校验失败仍归 `geo_*` / `unknown`。

**为什么**：同一个 `requests.ProxyError`，发生在 CSRF 阶段由 `classify_failure` 判 `network`，发生在预检 trace 阶段却被 `classify_geo_exception` 按消息判成 `geo_service`。wreq 的 `ProxyConnectionError` 映射后消息是 `is_proxy_connect error`，下划线形式不命中 `proxy connect` 等带空格的 marker；实测三种坏代理形态（回 407、RST、close）都会落到 `unknown`。这让「代理在第一个请求就坏」的任务不回池、不整轮重试，而它在 `user/register` 之前，本应满足 D12 的重试条件。

**warmup 为什么不改**：warmup 循环有 `OPENAI_REGISTRATION_WARMUP_ATTEMPTS`（默认 20）次尝试，第 2 次起 `reset_preflight_transport()` 换出口。耗尽后说明 20 个出口都不通；再让任务层整轮重试（最多 3 轮），最坏请求数变成 3 × 20 × 3，收益很低。保持耗尽后抛 `RuntimeError`（`unknown`），邮箱标 `failed`，把「全部出口都不通」当作环境故障而不是可重试的网络抖动。warmup 阶段对单个请求的传输层瞬断仍由 `WreqSession` 正常重试。

**影响**：预检 trace 失败从 `unknown` 移入 `network`，邮箱回池并计入回池次数，任务最多再跑 2 轮；回池上限 3 次（D18）仍然生效。

**测试边界**：现有 `test_initial_trace_network_failure_stops_before_warmup` 用的是内置 `ConnectionError`，不是 `requests.exceptions.ConnectionError`，修复后仍走 `geo_service`，保留；新增用例用 `requests.ProxyError` / `requests.Timeout` 钉住 `proxy`。`test_initial_trace_non_200_stops_before_warmup`（403）与 `test_initial_trace_407_stops_before_warmup`（407）不变。

**备选**：*在 `registration_preflight` 的 `except` 里直接 re-raise 传输层异常*。放弃。`classify_failure` 能判对，但会丢 `stage=detect`，任务记录的错误阶段退化为 `initialize`；把类型判断放进 `classify_geo_exception` 能保留 `RegistrationGeoError` 的 stage。

## Risks / Trade-offs

**[POST 超时重发造成状态不一致]** → 见 D1a。`user/register` 超时后重发，若首次已被处理，服务端账号已建而本地记为失败并剔除邮箱。缓解：无代码缓解，按用户决定接受。上线后若日志中出现 `password_state=submission_unknown` 且随后 `user/register` 失败，即为该情况；其发生率取决于线上超时率，OSS 未记录该数据。

**[超时重试拉长失败路径]** → 出口拥堵时，每个请求的超时预算变为原来的 3 倍加退避。按请求各自的超时算：默认超时的请求从 30 秒变为最多约 94.5 秒（30 + 1.5 + 30 + 3 + 30）；预检的 warmup 请求超时是 40 秒（`OPENAI_REGISTRATION_WARMUP_TIMEOUT`），变为最多约 124.5 秒。这个膨胀按请求累计，而 authorize 之前的请求数不固定：预检先发 1 个 trace 请求（超时 15 秒），再进入最多 20 次尝试的 warmup 循环，每次尝试 1 个请求；正式 CSRF 1 个、signin 1 个；authorize 是最多 31 跳的重定向链（`openai_auth.py:367`），每跳一个请求。坏出口下这条链的总耗时可能是改动前的数倍。缓解：与 OSS 行为一致；`WREQ_TRANSIENT_RETRY_MAX_ATTEMPTS` 可配置为 0 关闭重试。

**[三层重试叠加拉长单任务耗时]** → 传输层 ×3、步骤层（CSRF 403）×3、整轮 ×3 叠加，预检自身还有 `OPENAI_REGISTRATION_WARMUP_ATTEMPTS`（默认 20，`config/openai_protocol.py:61`）次尝试，其中第 2 次起每次换出口。出口完全不通时，单任务最坏耗时约为改动前的 3 倍：整轮重试是新增的乘数，传输层和步骤层的重试只在各自的触发条件下生效，已在各自的风险条目里计算。缓解：所有等待都是可中断的，用户可停止；三个次数都可配置。整轮重试的轮数和间隔上线后按实际触发率调整。

**[网络类邮箱回池后被反复领取]** → 出口持续不通时，同一邮箱会被领取 → 网络失败 → 回池 → 再领取，循环消耗。每次循环的代价是一次完整任务（整轮重试最多 3 轮，每轮含预检的 warmup 循环），期间占着一个 worker。缓解：D18 的自动上限，默认 3 次后标 `failed`，可人工恢复并清零。OSS 没有这个机制（`release_unused`，`webui/db.py:433-442`，无条件改回 `available`）。残余风险：上限按邮箱计数，出口整体不通时池里的邮箱仍会被逐个消耗到上限，只是每个最多 3 次；这种情况是代理池问题，不是邮箱问题。

**[分类器判错导致不该回池的邮箱回池]** → 分类器只把传输异常、CF 挑战的 403、Sentinel 请求与求解失败、代理连接失败归 `network`，其余全部归 `unknown`。对绝大多数分支，判错的方向只会是「把网络类判成 unknown」，行为退回现状（标 failed），不会更差。**有一条例外**：`cf-mitigated: challenge` 头只证明这次 403 是 Cloudflare 的挑战页，不证明挑战是因出口 IP 被标记而触发。如果 Cloudflare 是因邮箱或账号维度的信号对该请求发起挑战，按 J1' 邮箱会被回池。缓解：D18 的上限让这种邮箱最多回池 2 次、第 3 次标 `failed`，之后需人工介入；人工若恢复它，计数清零后可能再次循环。本地没有信号能区分这两种成因，OSS 同样不能（它把所有 403 一律归 `network` 且无上限）。上线后若发现同一邮箱反复出现在「达到回池上限」的 `failed` 里，即为该情况。

**[wreq 升级改变异常类与阶段的对应关系]** → 判定按类名，依赖三条对应关系：`ConnectionError` 只对应连接建立失败；`ConnectionResetError` 只在请求已发出后出现；`RequestError` 只在请求已发出后出现。wreq 升级若改变对应关系，有两个方向的风险。一是「请求已发出」的失败被重试，例如握手失败开始抛别的类；二是瞬断不再被重试，例如握手阶段的 RST 开始抛 `ConnectionResetError`，按 L1 它不重试，这种瞬断就会直接失败。超时不在其中：`TimeoutError` 在两个阶段都会出现，这是已知并已接受的（D1a）。缓解：对 `wreq.exceptions` 逐类断言返回值的单元测试只能守住判定表本身，wreq 升级后类名不变，它照样通过，发现不了对应关系的变化；能发现的是故障注入用例（握手前关闭、握手前 RST 后重试成功，请求发出后的关闭与 RST 只发 1 次），任务 3.1、3.4、3.5。wreq 升级时必须重跑这组用例。

**[查活 authorize 失去同会话重试]** → 查活链在 `follow_authorize` 之后没有外层循环，403 或网络错误会直接导致本次查活失败。这是与 OSS 对齐的预期行为，但查活成功率可能下降。缓解：查活失败后由 `live_check_service` 按既有策略走独立直连会话兜底，不新增逻辑；上线后观察查活失败率。

**[链路瞬断重试对旁路模块生效]** → Outlook、Codex OAuth、短信接码的请求也会被重试，其 POST 同样受超时重发影响。接受：与 OSS 行为一致，OSS 同样是 session 层全量覆盖且不区分调用方。

**[`DecodingError` / `BodyError` 改为映射后，上层行为变化]** → 此前这两类异常以 wreq 原生类型穿透，上层 `except requests.exceptions.ConnectionError` 接不住。映射后会被接住。这是修正而非回归，但若上层有依赖「接不住」的代码路径，行为会变。缓解：实现时全局搜索对 `ConnectionError` 的捕获点，逐个确认。

**[CSRF 403 重试延长失败路径的耗时]** → 出口确定被标记时，注册链多等 15 秒才失败。接受：与 OSS 一致，换取真实的瞬断恢复。

**[人工重试恢复原邮箱时的竞态]** → `retry_job` 在创建子任务后才领取邮箱。如果领取失败（邮箱已停用、已有完整账号、被其它任务占用），子任务行的邮箱保持 `None`、走领新号。子任务排队期间邮箱是 `registering`，人工路径无法改动它（见 D16），子任务执行时 `_prepare_persisted_email_registration` 还会再校验一次池行状态（D15 第 5 点）。进程重启后子任务不会恢复执行，由 D19 的启动收口把它标成终态，邮箱随之迁到 `failed`。

## Migration Plan

无 schema 迁移。改动是代码和配置默认值，回滚即还原这几个文件。

**部署后首次启动的一次性变更**：D19 的启动收口会把任务表里残留的 `pending` 注册任务改标 `cancelled`，`running` 和 `stopping` 改标 `stopped`。这些任务在旧版本里同样不会执行，只是状态停在原处；它们的邮箱池条目随后被 `migrate_email_pool_statuses` 从 `registering` 迁到 `failed`（旧版本里会永久停在 `registering`）。两者都是把已经不可用的状态改成可人工处理的终态。

已有的任务记录没有 `failure_category` 字段，读取时按 `None` 处理，等同 `unknown`。人工重试这些旧任务会走现状领新号，行为与改动前一致。

已有的邮箱池条目没有网络类回池计数字段，`_normalize_pool_row` 在加载时兜底为 0。已处于 `available` 的旧条目从 0 开始计数。

建议顺序：先做传输层（链路瞬断重试 + 异常映射），观察一个批次，重点看超时重试的触发次数；再做步骤层（CSRF 重试、authorize 去重试、查活外层）；最后做失败分类、邮箱回池、整轮重试、人工重试用原邮箱、启动收口。前两部分和第三部分没有代码依赖，可以分开提交。第三部分内部有依赖顺序：分类器 → 邮箱状态迁移 → 整轮重试 → 人工重试 → 启动收口。启动收口放在最后，因为它重试分支的邮箱处理要等 D15 的规则定下来。

## Open Questions

无未决项。J、K 两个归类在定稿前有过反复，保留 OSS 对照证据；L 记录的是 `ConnectionResetError` 的取舍，依据在 D1「RST 的阶段」。

### J: CF 挑战的 403 归 `network` — 已决定（J1'）

OSS 对整轮重试和邮箱回池用两套独立的关键词判定，都把 403 当网络问题：

- 整轮重试（`_turb_bridge.py:564-576` `_is_retryable_before_account`）：可重试的关键词含 `just a moment`、`403`、`cf`、`warmup`、`环境`、`出口`，以及 `connection`、`timeout`、`ssl`、`socks`；不可重试的关键词是 `otp`、`验证码`、`invalid_auth_step`、`该号已生成密码`。
- 邮箱回池（`webui/registrar.py:188-200` `_NETWORK_ERROR_PATTERNS`）：`network` 含 `tls`、`ssl`、`connection`、`timeout`、`proxy`、`socks`、`dns`、`cloudflare`、`just a moment`、`403 forbidden`、`csrf token 获取失败`、`/sentinel/req`、`sentinel quickjs`、`invalid_state`、`connection reset`。

**决定**：只有带 `cf-mitigated: challenge` 头的 403 归 `network`，整轮重试且回池；其余 403 归 `unknown`。实现见 D11。

**比 OSS 更精确的地方**：CF 拦截的 403 响应带 `cf-mitigated: challenge` 头，本地已有识别（`core/infra/http_diagnostics.py:14`、`account_export.py:523-524`）。分类器读 `exc.response.headers`，不解析响应体，不匹配消息文本。OSS 靠 `"just a moment"` / `"403 forbidden"` 关键词把所有 403 一律当网络问题。

**本地做不到的**：非 CF 的 403 里，哪些是出口维度、哪些是邮箱维度，本地没有信号。OSS 也区分不了。这部分归 `unknown`，邮箱标 failed。

**已知差距**：本地换出口不探测（见 D14 末尾），所以 CF 挑战的 403 触发整轮重试后，换到的可能仍是坏出口，重试成功率低于 OSS。接受：出口持续被标记是代理池问题，任何邮箱都会失败；回池后的邮箱若反复落到上限会标 `failed`（D18），不会无限循环。

### K: 地域门禁 `geo_*` 失败归 `unknown` — 已按默认处理，可否决

OSS 对出口校验失败的处理是「整轮重试但不回池」，这一点属实。但它不能直接搬到本地的 `geo_*` 上，因为两边的失败类型不对应。核实结果如下：

- OSS 有出口校验失败这个类型，但与本地的 `geo_*` 不对应。`auth_flow.py` 的 `check_proxy`（`:2027`）自 2026-09-21 起区分两种变化：出口 IP 变化只打 warning、保留观测、流程继续（`:2100-2107`）；出口国家变化仍然硬失败（`:2108-2116`，`任务出口国家发生变化`）。后者命中整轮重试关键词 `出口`（`_turb_bridge.py:575`），不命中邮箱回池关键词（`webui/registrar.py:199` 只有 `任务出口 ip`）。本地对这两种变化都只记录、不抛（`_revalidate_session_geo`，`registration_geo.py:542-548`）。
- 本地的四类与 OSS 的出口校验不是同一回事。`geo_mismatch` 是出口国家与观测到的时区不相容（`resolve_timezone`，`registration_geo.py:379-382`）或显式画像的内部不一致（`:523`）；`geo_incomplete` / `geo_parse` / `geo_service` 是地域信息缺失、观测响应无法解析、观测服务不可达。OSS 没有这些检查。把本地 16 处 `RegistrationGeoError` 的消息逐条拿 OSS 两组关键词实跑：邮箱回池那组全部不命中；整轮重试那组只有 `geo_incomplete` 的两条命中关键词 `出口`（`:461` `缺少有效出口 IP`、`:464` `出口国家缺失或不受支持`），其余都不命中。`browser_flow.py:316` 的 `浏览器出口环境校验失败` 是浏览器引擎的消息，本地不会产生。
- 地域失败全部发生在预检阶段（`registration_preflight.py:41-62`），早于正式 CSRF，不在本提案「正式 CSRF 到 authorize」的起点之内。

**默认**：`geo_*` 归 `unknown`，不整轮重试、不回池。理由是地域失败发生在预检阶段，早于正式 CSRF，不在本提案的范围之内；OSS 对出口校验失败的处理（整轮重试、不回池）针对的是另一种失败，不能直接套用。`geo_incomplete`（缺时区观测、国家不受支持）换出口可能有用，但预检自身的 warmup 循环已经承担换出口，整轮重试不重复这一层。若要改这个默认，只需在分类器里给 `geo_incomplete` 加一个分支。

### L: `ConnectionResetError` 是否重试 — 已决定：不重试

**问题**：D1 原先把 `ConnectionResetError` 放在重试集合里（与 OSS 一致）。补测表明这个类只在请求已发出之后出现，所以这一项重试的全部是「请求已发出后被对端 RST」。传输 spec 的「请求发出后的连接中断不重试」要求这类 MUST NOT 自动重试；任务 3.2、3.4 和 spec 三者按原写法互相矛盾：3.4「响应体不完整后 RST，断言只收到 1 个连接」在 3.2 实现后会失败，实际会收到 3 个。

**两个选项**：

| | L1：`ConnectionResetError` 不重试 | L2：保持重试，修改 spec |
|---|---|---|
| 改动 | 3.2 的重试集合去掉 `ConnectionResetError`；D1 决定同步 | 传输 spec 的「请求发出后的连接中断不重试」把 RST 排除在外，改成只约束 FIN；D1a 的接受范围从「超时」扩大到「超时和 RST」 |
| 与 spec 现文 | 一致 | 需要改 spec |
| 与 OSS | 不一致，OSS 会重试这类 | 一致 |
| 握手阶段的 RST | 不受影响，仍抛 `ConnectionError`，仍重试 | 同左 |
| 对 POST 的后果 | 请求已发出后被 RST 直接失败，不会重复提交 | 请求已发出后被 RST 会重发，与超时同样存在重复提交 |
| 本地已有数据 | 无。RST 在线上发生的频率、其中 POST 占多少，都没有记录 | 同左 |

**L1 不需要解析消息**：区分靠的是类名，握手阶段的 RST 本来就不是这个类。这与 D1「按类名判定」的原则一致，不引入消息匹配。

**决定**：L1。D1、传输 spec、任务 3.2 和 3.4 已按 L1 同步。
