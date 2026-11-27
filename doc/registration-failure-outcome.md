# 注册失败后的去向：分类、重试与邮箱处理

本文说明一次注册任务失败后系统怎么处理：失败归为哪一类、哪一层会重试、领取的邮箱去哪里、服务重启后残留的任务怎么收口。依据是当前源码，文中的行号对应本文件落盘时的版本。

传输层的链路瞬断重试（判定表、实测数据、与 OSS 的逐项对照）单独写在 `transport-transient-retry.md`，本文只引用结论。

## 总览

一次任务从外到内经过四层，每层只处理自己认得的那类失败，其余原样上抛：

```
任务层      _run_one_job                 停止 / 失败落库 / 邮箱处理
  |
单轮        _run_registration_round      走完一轮注册；失败时分类、处理邮箱
  |
整轮重试    run_registration             换出口重来（仅限 user/register 之前的网络类失败）
  |
传输层      WreqSession.request          链路瞬断同会话重试
```

另有一步「正式 CSRF 的 403 重试」在步骤函数 `get_csrf_token` 内部，位于整轮重试之内、传输层之上。

## 失败类别

分类函数是 `classify_failure()`（`core/openai/register/registration_failure.py:96-120`）。它是纯函数：只看异常类型、异常对象上的结构化字段和响应头，不对异常消息做子串匹配。三个取值：

| 类别 | 含义 | 邮箱去向 | 是否可整轮重试 |
|---|---|---|---|
| `network` | 出口或链路问题，换出口重来有意义 | 回池（`available`），计入回池次数 | 是，另需满足下文的阶段条件 |
| `account` | 该邮箱对应的账号不可用 | 标 `failed` | 否 |
| `unknown` | 其余全部 | 标 `failed` | 否 |

判定表，自上而下先命中先得：

| 异常类型 | 附加条件 | 类别 |
|---|---|---|
| `AccountUnusableError` | 无 | `account` |
| `requests.ConnectionError` / `Timeout` / `ProxyError` | 无 | `network` |
| `requests.HTTPError` | `response.headers["cf-mitigated"]` 等于 `challenge` | `network` |
| `SentinelRequestError` | 无 | `network` |
| `SentinelSDKError` | `category` 属于求解类（`protocol_error`、`flow_mismatch`、`invalid_challenge`、`no_requirements`、`empty_main_token`、`token_context_mismatch`） | `network` |
| `RegistrationGeoError` | `category` 等于 `proxy` | `network` |
| 其余全部 | 无 | `unknown` |

几点说明：

- `requests.SSLError` 是 `ConnectionError` 的子类，归 `network`。
- `cf-mitigated` 只读响应头，不看状态码、不解析响应体。非 Cloudflare 的 403、含 `already exists` 的 409、429、5xx 都归 `unknown`。
- Sentinel SDK 的环境类失败（缺 Node、缺 runner 文件、子进程启动失败等）归 `unknown`：本地缺文件时换出口和换邮箱都不会改变结果。
- 地域校验的五种失败里只有 `proxy` 归 `network`，见下文「地域校验」；预检 trace 的 `requests.ConnectionError` / `Timeout` / `ProxyError` 会先被 `classify_geo_exception()` 按类型转成 `RegistrationGeoError(proxy)`，再归 `network`。

默认落 `unknown` 而不是 `network`。`unknown` 的邮箱处理与引入分类之前完全一致（标 `failed`），分类器漏掉或判错任何一种异常，行为都退回到引入之前。

## 哪一层会重试

### 传输层：链路瞬断

`WreqSession.request()`（`core/infra/wreq_session.py:148-225`）内对连接建立阶段的失败同会话重试，默认 2 次，间隔 1.5 秒、3 秒。对所有经过该方法的请求生效，不区分 GET 和 POST。

判定依据是 wreq 的异常类，不解析消息：`ConnectionError`、`TlsError`、`ProxyConnectionError`、`TimeoutError` 重试；请求已发出后才出现的 `ConnectionResetError`、`RequestError`、`DecodingError` 不重试。实测中握手失败抛出的都是 `ConnectionError`，`TlsError` 从未出现过。

这层重试的等待用 `time.sleep`，不可被用户的停止请求打断，最多多等 3 秒。

重试耗尽后抛出的异常类型与首次就失败时相同，上层无法区分。它统一是 `network` 类。

### 正式 CSRF：403

`get_csrf_token()`（`core/openai/auth/chatgpt_auth.py:90-132`）收到 403 时在同一会话内重试，默认共 3 次，间隔 5 秒、10 秒。每次重试前清除会话的本地熔断（`reset_retryable_circuit`），否则下一次请求会被熔断拦截。

只对 403 重试。其它状态码和传输层耗尽后抛出的异常原样上抛。等待用 `interruptible_sleep`，可被停止请求打断。

这个函数有两个调用方：注册（`registration_flow.py` 的单轮）和查活登录。两处行为相同。

### authorize：不重试

`follow_authorize()`（`core/openai/auth/openai_auth.py:347-393`）只导航一次，任何错误（403、网络错误）原样抛出，不在同会话内重试。是否换出口重来由整轮重试按阶段条件决定：authorize 发生在 `user/register` 之前，403 且带 `cf-mitigated: challenge` 时归 `network`，会重试；不带该头的 403 归 `unknown`，不重试。

### 整轮重试：换出口重来

`run_registration()`（`core/openai/register/registration_flow.py:257-317`）在单轮失败后决定是否换出口重来，默认最多 3 轮（`OPENAI_REGISTRATION_ROUND_MAX_ATTEMPTS`），轮间等待 5 秒 × 轮号（`OPENAI_REGISTRATION_ROUND_RETRY_DELAY`）。

每轮新建会话、设备 ID、Cookie 和熔断状态，出口从代理池重新抽取。是否重试由单轮返回的 `retryable_round` 决定，判定式在 `registration_flow.py:707-712`，四个条件同时成立才重试：

1. 失败类别是 `network`；
2. 账号类型尚不知晓或确定是新号（`account_kind` 为 `unknown`、`new_password`、`new_passwordless` 之一）；
3. 密码尚未提交（`password_state` 为 `not_attempted`）；
4. 密码尚未落库（`credential_persistence` 为 `not_attempted`）。

四条合起来是一句话：**`user/register` 请求还没有发出**。请求一旦发出，结果未知，重试可能用同一邮箱重复提交，所以任何失败都不重试。`password_state` 与 `credential_persistence` 两条互为备份。

轮间等待可被停止请求打断。被停止时不开始下一轮，把上一轮的结果交给任务层。

### 三层的嵌套关系

传输层在最内，整轮重试在最外。正式 CSRF 一次 `session.get()` 在传输层最多发 3 个请求，`get_csrf_token()` 最多调 3 次 `session.get()`，整轮重试最多 3 轮：

```
整轮重试              轮 1                  轮 2                  轮 3
              +------------------+  +------------------+  +------------------+
get_csrf      |403   403   403   |  |403   403   403   |  |403   403   403   |
              +------------------+  +------------------+  +------------------+
WreqSession   |x3    x3    x3    |  |x3    x3    x3    |  |x3    x3    x3    |
              +------------------+  +------------------+  +------------------+
```

每一格 `x3` 是传输层对一次 `session.get()` 的最多请求数。全部耗尽时是 3 轮 × 3 次 × 3 个 = 27 个请求。

这是上界，要同时满足三个条件才会达到：每一次都是 403；每一次 403 都带 `cf-mitigated: challenge`（否则归 `unknown`，不整轮重试）；传输层每次都先瞬断两次再拿到 403。

三层对熔断的处理不同：

- 传输层重试不经过熔断检查。它重试的是连接建立阶段的失败，没有 HTTP 响应，不触发熔断。
- 正式 CSRF 的 403 重试要先清熔断（`chatgpt_auth.py:123`）。熔断在响应被观察时设置，第三次 403 之后不清的话，同会话的下一个请求会被本地拦截。
- 整轮重试不需要清熔断。每轮在单轮内部新建 `BrowserSession`（`registration_flow.py:353`），熔断状态是会话的属性，上一轮的不会带进下一轮。

## 地域校验

每轮在认证之前做一次出口观测（`registration_preflight()`，`core/openai/register/registration_preflight.py:27-101`）：请求 `cloudflare.com/cdn-cgi/trace`，校验出口 IP、国家、时区，首次通过后冻结画像。校验不过直接抛 `RegistrationGeoError`，这一轮不再开始。

失败按 `category` 细分（`core/openai/register/registration_geo.py:24-28`）：

| `category` | 触发情形 | 分类 |
|---|---|---|
| `proxy` | 代理返回 407、连接代理失败，或 trace 请求的 `requests.ConnectionError` / `Timeout` / `ProxyError` | `network` |
| `geo_service` | 观测请求返回非 200，或抛出未被识别为传输层异常的错误 | `unknown` |
| `geo_parse` | trace 响应无法解析 | `unknown` |
| `geo_incomplete` | 缺出口 IP、国家不受支持、时区无法确认 | `unknown` |
| `geo_mismatch` | 出口与已冻结的画像不一致 | `unknown` |

只有 `proxy` 归 `network`：它要么是传输层失败，要么有确凿的代理证据，换出口有意义。观测服务返回非 200、响应无法解析或出口信息不完整时，换出口后结果未必改变，按 `unknown` 处理，邮箱标 `failed`。

## 邮箱的去向

### 领取

`run_registration` 收到空的 `email` 时才领取（`registration_flow.py:276-284`），否则直接使用传入的邮箱。领取后的池行是 `registering`，`registration_job_id` 指向当前任务。邮箱只在整轮重试之外领取一次，重试期间池行保持 `registering`，不会被其它任务领走。

单轮开始时先校验池行（`_prepare_persisted_email_registration()`，`registration_flow.py:122-139`，在 `:351` 调用，早于新建会话）：池行状态不是 `registering` 或 `used` 就抛错，这一轮不发出任何请求。

### 失败时

`settle_failed_registration_email()`（`core/infra/db.py:388-442`）一次性决定去向，只处理状态仍为 `registering` 的池行：

| 情形 | 池行 | 回池次数 |
|---|---|---|
| `network`，且未达上限 | `available` | 加 1 |
| `network`，回池次数达到上限（`EMAIL_NETWORK_RELEASE_MAX_COUNT`，默认 3） | `failed` | 加 1 |
| `account` 或 `unknown` | `failed` | 不变 |
| 单轮内部失败但本单轮不是最后一轮 | 不动，保持 `registering` | 不变 |

回池次数达到上限后不再自动回池，理由是同一个邮箱连续多次因网络失败，继续重试只是在消耗出口。

有几类失败不走上面的表，而是直接停用邮箱（`disabled`），`_should_disable_failed_registration_email()`（`registration_service.py:129-139`）按错误消息识别：最终 session 有 200 但拿不到 `accessToken` 且带警告横幅、邮箱提交后落到登录密码页。

### 用户停止

停止路径不分类，邮箱一律标 `failed`，回池次数不变。三条停止路径结果相同：

- 单轮内部的检查点抛出停止请求；
- 轮间等待时被停止（此时邮箱未被单轮处理，仍是 `registering`）；
- 停止位已置期间撞上网络类异常。

任务层在处理邮箱之前先检查停止位（`registration_service.py:367`），所以第三种情形不会把邮箱回池。

### 已有账号登录

账号类型是 `existing`（邮箱对应的账号已存在，走登录流程）时，登录失败或被停止都不改池行，邮箱保持 `registering`。这是一个已知缺口，见文末。

### 人工重试

`retry_job()`（`registration_service.py:572`）对失败任务创建子任务。原任务的失败类别决定子任务用哪个邮箱：

- 原任务是 `network` 类：优先复用原邮箱。`claim_specific_email()`（`db.py:444-492`）把该池行重新领取并归属子任务；回池次数清零。原邮箱已被停用、删除、被其它活动任务占用，或已有完整账号时，认领失败，子任务改领新邮箱。
- 其它类别，或原任务没有失败类别（例如服务重启收口出来的任务）：子任务领新邮箱。

认领发生在子任务提交到线程池之前，所以子任务排队期间别的任务领不走这个邮箱。

## 部分成功：账号已保存但 2FA 未完成

任务状态除 `success` / `failed` 外还有一个 `partial_success`，只在配置要求 2FA（`ENABLE_2FA` 为真）时出现：

| 情形 | 任务状态 | 邮箱 |
|---|---|---|
| 账号与登录态已保存，2FA 完成或未要求 | `success` | 已消耗，不回池 |
| 账号与登录态已保存，2FA 未完成 | `partial_success` | 已消耗，不回池 |
| 未取得登录态或账号未保存 | `failed` | 按失败类别处理（见上表） |

判定在 `_run_registration_round()`（`registration_flow.py:677-694`）：`account_saved` 为真且 `twofa_satisfied` 为假时记部分成功，任务 `error_message` 写「注册账号已保存，配置要求的 2FA 未完成」，`failure_category` 保持空。2FA 设置抛异常时先把 `registration.twofa_status` 写为 `failed`，再保存已有的登录态。

部分成功任务的邮箱已在账号保存时消耗，任务层不再调用回池逻辑。人工重试走账号分支：Codex 未完成则补跑 Codex；Codex 已完成则提示 2FA 需人工处理，当前没有自动补跑 2FA 的入口。

## 服务重启后的收口

WebUI 启动时先调 `recover_interrupted_registration_jobs()`（`db.py:895-922`），再调 `migrate_email_pool_statuses()`（`db.py:803-892`）。顺序不能反：迁移把处于 `pending`、`running`、`stopping` 的注册任务算作占用，先迁移会让它们领取的池行继续留在 `registering`。

收口规则（`webui/app.py:451-453`）：

| 任务原状态 | 收口后 | 任务说明 |
|---|---|---|
| `pending` | `cancelled` | WebUI 重启，任务未执行 |
| `running` 或 `stopping` | `stopped` | WebUI 重启，任务执行中断 |
| 其它 | 不变 | — |

收口覆盖 `registration` 和 `codex_retry` 两类任务，只改状态、说明和完成时间，不写失败类别。

被中断任务领取的邮箱，由随后的迁移处理（`db.py:858-864`）：没有本地账号、也不再被活动任务占用的 `registering` 行转为 `failed`，说明为「邮箱池状态启动迁移」，回池次数保持被中断时的值。它不会自动回池：进程被中断时无法知道那一轮失败属于哪一类。

转为 `failed` 的邮箱可以人工恢复（`restore_email_pool_entry`，`db.py:532`），恢复后回池次数清零。

收口后的任务可以重试。这类任务没有失败类别，重试的子任务领新邮箱，不复用原邮箱。

命令行入口 `main.py` 不做收口。它不创建任务行，与 WebUI 共用的任务表里处于活动状态的任务只能来自正在运行的 WebUI，在命令行进程里收口会把那些正在跑的任务标成已停止。

## 与 OSS 的差异

| 项 | OSS | 本地 |
|---|---|---|
| 失败分类 | `webui/registrar.py:203-237` 的 `classify_error`，对错误消息做子串匹配，先账号类模式、再网络类模式 | 结构化判定，只看异常类型和字段 |
| Cloudflare 403 | 网络类模式含 `"just a moment"`、`"403 forbidden"`，任何消息带 403 字样都归网络类 | 只认响应头 `cf-mitigated: challenge`，其余 403 归 `unknown` |
| 是否整轮重试 | `_turb_bridge.py:564-576` 的 `_is_retryable_before_account`，同样是对消息做关键词匹配 | 按 `progress` 里的结构化状态判定 |
| 握手瞬断重试 | 重试，2 次 | 重试，2 次 |
| 请求发出后被重置 | 重试 | 不重试，请求可能已被处理 |
| Sentinel 环境类失败 | 消息含 `sentinel quickjs` 关键词，归网络类，邮箱回池 | 归 `unknown`，邮箱标 `failed` |
| 出口校验 | `check_proxy()`（`auth_flow.py:2027`）同样请求 cdn-cgi/trace 并比对出口，但校验不过时继续注册（`:3813-3814`），失败信息靠关键词归网络类 | 校验不过直接终止本轮；只有代理类失败归 `network` |
| 换出口 | 见下 | 见下 |
| HTTP 状态码熔断 | 没有 | 有，403 冷却 900 秒、429 冷却 300 秒，或按 `Retry-After` |

三点展开：

**结构化判定与关键词判定的取舍。** OSS 的两张模式表依赖错误消息的措辞，措辞一变就会漏判或误判；本地的判定不读消息。代价是本地只能识别有结构化信号的失败，信号之外的异常一律 `unknown`。

**出口校验的失败类型不与 OSS 对应。** OSS 把「浏览器出口环境校验失败」「任务出口 ip」这类消息归网络类，但它并不因此重试——校验不过时流程照常往下走。本地在预检处终止，并按 `category` 区分：代理问题归 `network`（可整轮重试），其余归 `unknown`。两边「出口校验失败」对应的不是同一组情形。

**换出口不探测。** 本地整轮重试换出口时直接从代理池取下一个候选，不预先验证新出口是否可用（`reset_preflight_transport()`，`core/openai/transport/session.py:934`，仅在 warmup 失败之间调用）。新出口是否可用由下一轮的预检和认证步骤实际请求来发现。

## 已知缺口

- **已有账号登录失败后邮箱停在 `registering`。** 账号类型为 `existing` 时，失败不改池行，邮箱不会被回池也不会标 `failed`，需要人工处理。这是有意的保守选择：这个邮箱对应的账号确实存在，自动把它标成失败或放回池里都不合适。
- **超时的 POST 会被传输层重发。** wreq 对「握手挂起」和「请求发出后等响应超时」抛同一个异常类，传输层无法区分。对注册主链的影响和与 OSS 的对照见 `transport-transient-retry.md` 的「超时重试对 POST 的影响」。
- **`TlsError` 的实际触发条件未知。** 实测中从未出现，重试集合里保留它只为与 OSS 一致。
- **线上代理池未覆盖。** 重试与分类的实测全部在本地回环完成，经真实代理时各故障的异常类是否一致没有数据。
- **预检 warmup 耗尽的网络失败记为 `unknown`。** warmup 在 `OPENAI_REGISTRATION_WARMUP_ATTEMPTS`（默认 20）次尝试内自行换出口；耗尽后抛 `RuntimeError`，分类为 `unknown`，邮箱标 `failed`，不再触发整轮重试。这是有意选择：20 个出口都不通时再叠加最多 3 轮整轮重试，收益低、耗时放大；单个 warmup 请求的传输层瞬断仍由 `WreqSession` 重试。
