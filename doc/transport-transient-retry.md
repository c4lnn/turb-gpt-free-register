# 传输层链路瞬断重试

本文记录 `WreqSession` 对链路瞬断的同会话重试：哪些失败会重试、依据是什么、与 OSS 的差别、未验证项。配置项在 `config/openai_protocol.py:69-72`，实现在 `core/infra/wreq_session.py`。

熔断（403 / 429 冷却）和正式 CSRF 的 403 重试不在本文范围，见 `registration-2fa-oss-comparison.md`。

## 适用范围

- 重试发生在 `WreqSession.request()`（`wreq_session.py:148`）内部，对所有经过该方法的请求生效，不区分 HTTP 方法，GET 和 POST 相同。
- 不经过 `WreqSession.request()` 的请求不受影响。
- 重试不换会话、不换出口、不换指纹，沿用同一个 `wreq.blocking.Client` 和 Cookie 容器。请求的方法、URL、头和体在重试之间不重新构造；读响应超时的用例在服务端比较过两次请求的原始字节，完全相同。
- 重试等待用 `time.sleep`（`wreq_session.py:213`），不是可中断的等待。传输层没有任务上下文，用户点停止时最多多等 3 秒。

## 判定规则

按 wreq 原生异常类判定，函数是 `_is_transient_failure()`（`wreq_session.py:73-87`），用 `isinstance` 匹配，子类同样算数；不解析异常消息。

| wreq 异常类 | 是否重试 | 含义 | 耗尽后上层收到的异常 |
|---|---|---|---|
| `ConnectionError` | 是 | 连接建立失败：握手前或握手被拒、证书校验失败、对端回明文 HTTP | `requests.ConnectionError` |
| `TlsError` | 是 | 实测中未出现过 | `requests.SSLError` |
| `ProxyConnectionError` | 是 | 代理在隧道建立前失败：拒绝连接、关闭、回 407 | `requests.ProxyError` |
| `TimeoutError` | 是 | 超时。握手挂起和请求已发出后等响应超时是同一个类 | `requests.Timeout` |
| `ConnectionResetError` | 否 | 请求已发出后被 RST | `requests.ConnectionError` |
| `RequestError` | 否 | 请求已发出后对端正常关闭（FIN） | `requests.ConnectionError` |
| `DecodingError` | 否 | 响应体不完整后对端关闭 | `requests.ConnectionError` |
| `BodyError` | 否 | 没有可稳定复现的注入方式，未实测；按 OSS 的集合归入不重试 | `requests.ConnectionError` |

`TlsError` 保留在重试集合里是为了与 OSS 的集合一致。`wreq/exceptions.py:36` 的说明称它覆盖握手失败和证书校验失败，但实测的握手失败抛的都是 `ConnectionError`，该分支和 `wreq_session.py:218-219` 的映射在实测场景下都没有命中。

wreq 其余异常类（`BuilderError`、`RedirectError`、`StatusError`、`UpgradeError`、`WebSocketError`、`RustPanic`）不在重试集合里，也不在 `request()` 的 `except` 链里，会以 wreq 原生类型穿透到上层。这些类在正常的 HTTP 请求路径上没有出现过。

## 实测结果

三轮本地故障注入，wreq 用 Chrome152 画像，服务端是本地自签证书的脚本化 TLS 监听，每个故障都在服务端确认发生。第三轮把前两轮的 15 行全部重跑，并补测了 RST。表共 17 行。三轮的原始记录在 `openspec/changes/align-preregister-error-handling/design.md` 的 Context 节和 D1；其中 12 行的异常类已由 `tests/test_wreq_session.py` 固化，可以重跑验证。

| 故意注入的故障 | 夹具动作 | 发生阶段 | wreq 异常 | 消息前缀 | 本地是否重试 | OSS 是否重试 | 自动化用例 |
|---|---|---|---|---|---|---|---|
| 握手前对端关闭（FIN 或 RST） | `close_before_handshake[_rst]` | 连接建立 | `ConnectionError` | `is_connect error` | 是 | 是 | 阶段表；重试成功、重试耗尽、POST 重试前不达服务端、耗尽后保留最后一次消息、成功后才记录 Cookie |
| 收 ClientHello 后关闭（FIN 或 RST） | `close_after_clienthello[_rst]` | 连接建立 | `ConnectionError` | `is_connect error` | 是 | 是 | 阶段表 |
| 对端回明文 HTTP | 无 | 连接建立 | `ConnectionError` | `is_connect error` | 是 | 是 | 无 |
| 证书校验失败 | 无 | 连接建立 | `ConnectionError` | `is_connect error` | 是 | 是 | 无 |
| 经代理，隧道建好后对端关闭 | `proxy_tunnel` + `close_before_handshake` | 连接建立 | `ConnectionError` | `is_connect error` | 是 | 是 | 阶段表；重试成功 |
| 握手挂起 | `hang_handshake` | 连接建立 | `TimeoutError` | `is_timeout error` | 是 | 是 | 阶段表；重试成功、连续 3 次耗尽 |
| 经代理，隧道内握手挂起 | 无 | 连接建立 | `TimeoutError` | `is_timeout error` | 是 | 是 | 无 |
| 代理端口黑洞（accept 后不回应） | 无 | 代理连接 | `TimeoutError` | `is_timeout error` | 是 | 是 | 无 |
| 代理端口拒绝连接（无人监听） | 无 | 代理连接 | `ProxyConnectionError` | `is_proxy_connect error` | 是 | 是 | 无 |
| 代理 accept 后关闭 | `proxy_close` | 代理连接 | `ProxyConnectionError` | `is_proxy_connect error` | 是 | 是 | 阶段表；重试成功、连续 3 次耗尽 |
| 代理对 CONNECT 回 407 | `proxy_407` | 代理连接 | `ProxyConnectionError` | `is_proxy_connect error` | 是 | 是 | 阶段表；重试成功、连续 3 次耗尽 |
| 请求已发出，等待响应超时 | `hang_after_request` | 请求已发出 | `TimeoutError` | `is_timeout error` | 是 | 是 | 阶段表；重试成功 |
| POST 请求已发出，等待响应超时 | `hang_after_request` | 请求已发出 | `TimeoutError` | `is_timeout error` | 是 | 是 | 连续 3 次超时，服务端收到 3 个逐字节相同的 POST |
| 握手成功，请求已发出，无响应关闭 | `close_after_request` | 请求已发出 | `RequestError` | `is_request error` | 否 | 否 | 阶段表；不重试 |
| 握手成功，请求已发出，无响应 RST | `close_after_request_rst` | 请求已发出 | `ConnectionResetError` | `is_connection_reset error` | 否 | **是** | 阶段表；不重试 |
| 响应体不完整后关闭 | `truncated` | 响应接收 | `DecodingError` | `is_decode error` | 否 | 否 | 阶段表；不重试；映射为 `requests.ConnectionError` |
| 响应体不完整后 RST | `truncated_rst` | 响应接收 | `ConnectionResetError` | `is_connection_reset error` | 否 | **是** | 阶段表；不重试 |

「夹具动作」是 `tests/test_wreq_session.py` 里 `_FaultServer` 和 `_FaultProxy` 的脚本动作名；「无」表示该故障只用临时脚本测过，没有做进测试夹具。

「自动化用例」列：

- 「阶段表」指 `FaultFixtureSelfCheckTests.test_stage_table_fin_and_rst` 和 `test_stage_table_hang_yields_timeout`，用原生 `wreq.blocking.Client` 直接断言每个注入点抛出的异常类，绕过 `WreqSession`。这张表是判定规则的事实依据，wreq 升级后如果它变了，判定规则要重新评估。经代理的两种故障在 `test_proxy_actions` 里。
- 其余用例在 `WreqTransientRetryTests` 和 `WreqExceptionMappingTests`，断言经过 `WreqSession` 之后的重试次数、耗尽后的异常类型和映射。

表中有 12 行保留了自动化用例，另 5 行没有：对端回明文 HTTP、证书校验失败、经代理的隧道内握手挂起、代理端口黑洞、代理端口拒绝连接。这 5 种故障的异常类与表中有用例的行相同，但本身没有被固化为测试。

FIN 和 RST 抛的异常类不同，只在请求已发出之后有区别：

| 发生阶段 | 对端 FIN 关闭 | 对端 RST |
|---|---|---|
| 握手前、收到 ClientHello 后 | `ConnectionError` | `ConnectionError` |
| 握手后，请求已发出 | `RequestError` | `ConnectionResetError` |
| 响应体传输中 | `DecodingError` | `ConnectionResetError` |

按 `openspec/changes/align-preregister-error-handling/design.md` D1 的记录，每个注入点的 FIN 和 RST 各测 3 次，结果一致。

## 重试参数

| 配置项 | 默认值 | 含义 |
|---|---|---|
| `WREQ_TRANSIENT_RETRY_MAX_ATTEMPTS` | `2` | 重试次数，不含首次请求。设为 `0` 关闭重试 |
| `WREQ_TRANSIENT_RETRY_BASE_DELAY` | `1.5` | 基准间隔（秒）。第 n 次重试前等待 `base × n`，即 1.5 秒、3 秒 |

默认配置下单次 `WreqSession.request()` 调用最多发 3 次请求，等待合计 4.5 秒。会话的默认超时是 30 秒（`config/browser.py:262` 的 `REQUEST_TIMEOUT`，在 `core/openai/transport/session.py:231` 赋给会话；调用方逐请求传入的 `timeout` 会覆盖它）。三次都超时时，这一次调用耗时约为 3 × 30 + 4.5 = 94.5 秒。

重试耗尽后抛出的是**最后一次**的异常，经 `wreq_session.py:214-225` 映射。它和「不重试、首次就失败」时上层收到的异常类型相同，上层无法区分两者。

直接构造 `WreqSession` 的调用点都不传重试参数，读取同一组默认值，行为与注册链一致：`core/mail/outlook_client.py:83`、`:399`，`core/openai/codex/codex_oauth.py:247`、`:312`、`:453`，`core/sms/sms_provider.py:63`，`core/sms/smsbower_provider.py:114`，以及 `core/openai/transport/session.py:218`、`:932`。也就是说邮箱、Codex OAuth、短信这些旁路模块的请求同样带这层重试。

## 与 OSS 的对照

OSS 实际运行的是 wreq 后端 `http_client_wreq.py`：`_turb_bridge.py:256-258` 调 `install()` 切换，引擎开关 `TURB_HTTP_ENGINE` 默认 `wreq`，仓库内无覆盖。

| 项 | OSS（`http_client_wreq.py`） | 本地（`wreq_session.py`） |
|---|---|---|
| 重试位置 | `WreqSession._call`，`:696-731` | `WreqSession.request`，`:190-213` |
| 重试次数与退避 | 2 次，1.5 秒 × n（`:86-87`，环境变量 `TURB_WREQ_RETRIES`、`TURB_WREQ_BACKOFF`） | 2 次，1.5 秒 × n |
| 判定函数 | `_is_retryable`，`:101-108` | `_is_transient_failure`，`:73-87` |
| 判定方式 | 先看类名，都不命中时再用消息关键词兜底 | 只看类名 |
| 按类名重试的集合 | `ConnectionError`、`TimeoutError`、`ConnectionResetError`、`TlsError`、`ProxyConnectionError`（`:92-93`） | `ConnectionError`、`TlsError`、`ProxyConnectionError`、`TimeoutError` |
| 按类名不重试的集合 | `StatusError`、`DecodingError`、`RedirectError`、`BodyError`、`BuilderError`（`:94-95`） | 其余全部 |
| 是否区分 HTTP 方法 | 否 | 否 |
| 等待方式 | `time.sleep`（`:730`） | `time.sleep` |

两处差别：

1. **`ConnectionResetError`**。OSS 重试，本地不重试。实测中这个类只在请求已发出之后出现，重试它等于重发一个可能已被服务端处理的请求。这是两边判定结果不同的唯一一个异常类；上面 17 行表里也只有两行因此不同。
2. **消息关键词兜底**。OSS 在类名都不命中时还会匹配 `is_connect`、`timed out`、`timeout`、`tls`、`handshake`、`connection reset`、`unexpected eof`、`broken pipe` 等关键词（`:96`）。本地不做。实测中的链路瞬断全部被类名覆盖，兜底只在 wreq 新增异常类时才会起作用，那种情况下不重试是保守的选择。

OSS 重试握手瞬断的依据来自 curl_cffi 时代的记录（`http_client.py:131-149`）：148 轮扫描中发生率 5.4%（8 次），与指纹和域名无关，8 次原会话重试全部在第 1 次重试恢复。这组数据只针对握手瞬断。

OSS 对正式 CSRF 的 TLS 错误还有一条换指纹分支（`auth_flow.py:2186`），但实际走不到。原因：`_turb_bridge.py:141-172` 的 `_apply_safe_impersonate()` 把 `_CHROME_VERSIONS` 里每条 Chrome 档的 `impersonate` 都改成同一个值（默认 `chrome152`，`:137`），`fingerprint.py:843` 按该字段去重得到的其他档因此为空，`fallback_impersonates` 只剩当前这一个（`fingerprint.py:852`）。`_rotate_impersonate_session()`（`auth_flow.py:1647-1676`）在候选只有 1 个时直接返回 `False`（`auth_flow.py:1661-1662`）。`_turb_bridge.py:838` 把浏览器家族锁为 `chrome`，所以只会走这一条生成路径。用 2000 个随机种子调用 `_gen_chrome` 实跑，候选列表全部是单元素。

## 超时重试对 POST 的影响

wreq 对「握手挂起」和「请求已发出后等响应超时」抛出的异常类型和消息完全相同，都是 `TimeoutError`，消息以 `is_timeout error` 开头、来源为 `TimedOut`。传输层无法区分这两种情况。选择重试超时，就必须接受超时的 POST 会被重发。

对注册主链，受影响的是 `POST /api/accounts/user/register`（`core/openai/auth/openai_auth.py:548`）：

- 首次请求超时后重试。如果首次请求其实已被服务端处理，重试会以同一邮箱再次提交。
- 服务端返回的 `already exists` 不属于可回退的拒绝（`openai_auth.py:204` 的说明），会走 `raise_for_status()` 抛 `HTTPError`。
- `registration_flow.py:409` 把 `password_state` 记为 `submission_unknown`。

结果是服务端账号已创建、本地记为失败。本地不增加识别逻辑。OSS 的 `classify_error`（`webui/registrar.py:203-237`）按错误文本匹配模式表：先匹配账号类模式，再匹配 `_NETWORK_ERROR_PATTERNS`（`:188-200`），都不命中归 `unknown`。`already exists` 不在两张表里，OSS 对这条错误的分类同样是 `unknown`，两边结果相同。这是按模式表得出的判断，没有用 OSS 实际抛出的异常文本验证。

其他 POST 的超时重发没有逐个分析。重试集合里的 `ConnectionError`、`ProxyConnectionError` 以及握手阶段的 `TimeoutError` 发生时请求必然没有发出，重发是安全的；有重复提交风险的只有「请求已发出后超时」一种。

## 未验证项

- **线上代理池未覆盖**。全部实测在本地回环上完成，代理是本地脚本化的 HTTP 代理（CONNECT 隧道）。线上代理池使用的代理协议、鉴权方式和链路质量没有在本地复现，经真实代理时各故障抛出的异常类是否与上表一致，没有数据。
- **超时的线上发生率没有数据**。OSS 记录的 5.4% 只针对握手瞬断，超时没有统计。超时重试的收益和风险因此无法量化。
- **RST 的线上发生频率没有记录**。
- **5 种故障没有自动化用例**：对端回明文 HTTP、证书校验失败、经代理的隧道内握手挂起、代理端口黑洞、代理端口拒绝连接。它们只在实测时用临时脚本跑过，脚本已删除，没有固化为测试夹具。其中「代理端口拒绝连接」是第三轮才补测的形态，前两轮记录的「代理端口不通」对应的是黑洞形态。
- **连接复用下的异常类**。`openspec/changes/align-preregister-error-handling/design.md` 的 Context 节记录了一项实测：连接池里已有空闲连接时新建连接握手失败，同样抛 `ConnectionError`、前缀 `is_connect error`。复用已有连接时被对端关闭的表现没有单独测试。
- **`TlsError` 从未在实测中出现**，它的实际触发条件无法判断。
