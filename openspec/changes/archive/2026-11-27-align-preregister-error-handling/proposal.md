# Proposal

## Why

本地注册失败后的三个环节与 OSS 参考实现不一致。其一，单轮内的错误处理：正式 CSRF 遇到 403 没有任何恢复机会就整轮失败，而 authorize 对 403 做同会话重试，出口已被标记时重试只会多等几秒再失败；代理链路的瞬断本地直接失败，OSS 在传输层同会话重试，其注释记录 TLS 握手瞬断在 148 轮中发生率 5.4%，8 次事件原会话重试全部首次恢复。其二，失败后的邮箱处理：本地不看失败原因一律把邮箱标为 `failed`，不再领取；OSS 区分网络类与账号类失败，网络类失败邮箱回 `available`。其三，失败后的重试：OSS 自动换出口、用同一邮箱整轮重来；本地没有整轮重试，人工点「重试」时领的是新邮箱。

## What Changes

- **正式 CSRF 增加 403 重试**：`GET /api/auth/csrf` 收到 403 时保留同一会话重试，间隔 5s / 10s，共 3 次尝试；每次重试前清除本地熔断状态。
- **authorize 去掉全部同会话重试**：`follow_authorize` 遇到 403、其它 HTTP 错误码和网络错误都直接抛出，由上层决定放弃当前轮。
- **查活登录的外层预检不再对正式 CSRF 的 403 重试**：`_network_preflight_with_retry` 的外层循环对正式 CSRF 步骤抛出的 403 不再重试，403 的恢复完全由 `get_csrf_token` 内部负责，避免两层重试叠加。外层循环对其它步骤的错误和其它网络错误的处理保持不变。
- **传输层增加链路瞬断重试**：`WreqSession.request()` 对连接建立失败、请求超时、代理连接失败保留同一会话重试，间隔 1.5s / 3s，共 2 次，覆盖全仓所有 wreq 请求（含 POST）。请求已发出后的连接中断和响应体不完整不重试。重试判定按 wreq 异常类名，与 OSS 的 wreq 后端有一处差异：请求已发出后被对端重置（`ConnectionResetError`）OSS 会重试，本地不重试，理由见 design.md 的 D1。
- **POST 超时重发的已知后果**：传输层无法区分超时发生在连接建立阶段还是请求已发出之后，因此超时的 POST 会被重发。若首次请求已被服务端处理，重试会以重复提交的形式到达服务端，其响应由各端点既有的错误处理逻辑决定。本次不为重复提交增加识别逻辑，与 OSS 行为一致。
- **补齐异常映射**：wreq 的 `DecodingError`、`BodyError` 映射为 `requests.ConnectionError`，不再原样穿透到上层。
- **注册失败分类**：注册任务失败时按结构化信号判定失败类别（`network` / `account` / `unknown`），写入任务记录。分类依据是异常类型、异常对象已有的 `category` / `error_code` 字段，以及 HTTP 错误响应的 `cf-mitigated` 响应头，不做错误消息的关键词匹配。带 `cf-mitigated: challenge` 的 403 和 Sentinel 请求、求解失败归 `network`；其余 403、409、5xx 归 `unknown`。
- **预检出口观测的传输层失败保留网络证据**：注册预检对 `cloudflare.com/cdn-cgi/trace` 的请求遇到 `requests.ProxyError` / `ConnectionError` / `Timeout` 时，按异常类型归 `proxy`（最终 `network`），不再因消息不匹配现有关键词而落入 `geo_service` / `unknown`。trace 的 HTTP 非 200、响应解析失败和地域画像校验失败仍归 `unknown`；warmup 循环保持现状（它自带最多 20 次换出口尝试，耗尽后再整轮重试没有收益），见 design.md 的 D20。
- **网络类失败的邮箱回池**：失败类别为 `network` 时，邮箱池条目回到 `available`，可被再次领取；`account` 与 `unknown` 保持现状标 `failed`。已有账号登录失败时不回收邮箱，保持现状。
- **回池次数上限**：池条目记录因 `network` 类失败回池的次数，默认最多 3 次，达到后不再回池、改标 `failed`，可人工恢复并重新计数。上限可配置，0 表示不设上限。此项是本地新增，OSS 没有对应机制。
- **整轮重试**：注册在**账号创建之前**因网络类失败而终止时，自动换出口、用同一邮箱重新开始一轮，默认最多 3 轮。每轮重建完整会话（新设备 ID、新指纹上下文）。重试期间邮箱保持占用，不回池。
- **任务级重试优先用原邮箱**：人工对失败任务点「重试」时，若原失败类别为 `network` 且原邮箱可恢复，则恢复原邮箱并用它重试；否则保持现状领新邮箱。
- **服务启动时收口残留的注册任务**：WebUI 重启后，排队中和执行中的注册任务既不恢复执行也不被取消，其邮箱永久锁在 `registering`，人工无法恢复或删除。启动时把 `pending` 改标 `cancelled`，`running` 和 `stopping` 改标 `stopped`，随后邮箱池迁移把仍为 `registering` 的邮箱迁到 `failed`；已回池的邮箱保持可用。此项是本地既有缺口的修复，OSS 没有对应的任务表。
- signin 不变：步骤内全部错误直接抛出。
- 本地 HTTP 状态码熔断机制保留，不在本次范围内调整。

## Capabilities

### New Capabilities

- `openai-auth-handshake-error-handling`: OAuth 握手前三步（正式 CSRF、signin、authorize）在收到 HTTP 错误码和网络错误时的重试与放弃规则，以及注册链与查活链两层重试的归属划分。
- `transport-handshake-retry`: 传输会话对链路瞬断（连接建立失败、超时、代理连接失败）的识别、同会话重试，以及传输库异常到 `requests` 异常的映射。
- `registration-failure-outcome`: 注册任务失败后的处理：失败类别的判定与记录、失败类别对邮箱池状态的影响、账号创建之前的整轮重试，以及人工重试时邮箱的选取。

### Modified Capabilities

无。项目当前没有已归档的主规范。

## Impact

- `core/openai/auth/chatgpt_auth.py`：`get_csrf_token` 增加 403 重试。
- `core/openai/auth/openai_auth.py`：`follow_authorize` 去掉重试循环。`_proxy_retry_config` 与 `_is_retryable_authorize_error` 仍被 `network_preflight` 的 `_request_with_proxy_retry` 使用，保留。
- `core/openai/account/account_liveness.py`：`_network_preflight_with_retry` 的重试判定排除正式 CSRF 步骤抛出的 403。`_RETRYABLE_NETWORK_HINTS` 与 `_is_retryable_network_error` 另有 4 个使用点（callback、session、OTP、重认证回退），保持不变。
- `core/infra/wreq_session.py`：`request()` 增加重试循环，`except` 链补两个分支。该函数是全仓所有 wreq 请求的出口：除 `BrowserSession`（注册、查活、2FA、套餐查询、Sentinel）外，Outlook 邮箱、Codex OAuth、短信接码也直接构造 `WreqSession`，链路瞬断重试对这些旁路模块同样生效。
- 重试嵌套：Sentinel `/req` 与登录预检已经过 `_request_with_proxy_retry`（步骤级，最多 3 次）。加上传输层重试后，这两个位置的最坏情况是 3 × 3 = 9 次请求。本次不调整 `_request_with_proxy_retry`。
- 行为变化：`follow_authorize` 的两个调用方（注册 `registration_flow.py:321`、查活 `account_liveness.py:537`）遇错后不再有同会话重试。查活的 authorize 之后没有外层重试，403 或网络错误将直接导致本次查活失败。
- `core/openai/register/registration_geo.py`：`classify_geo_exception` 先按 `requests.ProxyError` / `ConnectionError` / `Timeout` 类型判定 `proxy`，消息关键词保留为兜底；`CATEGORY_PROXY` 的注释与文案从「代理连接/认证失败」扩为「网络连接/代理失败」。`core/openai/register/registration_preflight.py` 的 warmup 循环不改。
- `core/openai/register/registration_flow.py`：`run_registration` 拆出单轮执行函数，外层增加整轮重试循环；失败返回增加失败类别；邮箱回收点（`:641-647`）按失败类别分支。
- `core/openai/register/registration_service.py`：任务失败时把失败类别写入任务记录；`_run_one_job` 把任务行上的邮箱传给 `run_registration`；`_release_unconsumed_job_email` 按失败类别分支；`retry_job` 增加原邮箱优先逻辑。
- `core/infra/db.py`：任务记录新增失败类别字段；池行新增回池计数字段，`settle_failed_registration_email` 按计数决定回池还是标 `failed`；`restore_email_pool_entry` 的来源枚举增加人工重试来源 `retry`，整轮重试期间邮箱保持占用，不经过恢复；人工恢复、重新导入、人工重试复用、注册成功四处清零计数；新增 `recover_interrupted_registration_jobs` 收口残留任务。
- `webui/app.py`、`main.py`：启动段在邮箱池迁移之前调用 `recover_interrupted_registration_jobs`。
- `core/infra/job_context.py`：进度快照增加失败类别，供任务层读取。
- 行为变化：网络类失败的邮箱从「标记 `failed`、不再领取」变为「回到 `available`、可被任何任务领取」，同一邮箱默认最多回池 3 次，之后标 `failed`。
- 行为变化：账号创建之前的网络类失败会自动重来，单次任务的耗时上限从一轮变为最多三轮；非池模式（固定代理或直连）下换出口不生效，重试沿用原路由。
- 行为变化：服务重启后，残留的 `pending` 注册任务变为 `cancelled`，`running` / `stopping` 变为 `stopped`，其邮箱池条目从永久停在 `registering` 变为 `failed`。部署后首次启动会对当时残留的任务执行一次。
- 与 OSS 的差异：OSS 在每轮结束时按错误分类决定邮箱是否回池，重试期间邮箱可能被其它任务领走；本地在整轮重试期间保持邮箱占用，全部轮次结束后再按最终失败类别处理。
- 测试：`tests/test_openai_auth_registration_flow.py` 中有 2 个测试断言 authorize 同会话重试（403 后重试成功、传输异常后重试），需按新行为改写；`tests/test_account_liveness.py` 的 `test_preflight_retries_with_same_session_when_csrf_is_blocked` 断言外层对 403 重试，需改写。`tests/test_registration_pipeline.py`（6 处引用 `run_registration`，含 `_run_one_job` 的失败路径）和 `tests/test_email_pool_status.py`（`_release_unconsumed_job_email`、`retry_job`）需按新的邮箱处理行为核对改写。4 个把启动段 `db.*` 调用逐个打成桩的测试文件（`test_account_filters.py`、`test_archive_removal.py`、`test_codex_export_filter.py`、`test_agent_removal_contract.py`）需给新增的启动调用补桩。
- 文档：`doc/registration-2fa-oss-comparison.md` 中涉及 authorize 重试的措辞需同步更新；其中 document-navigation ID 的轮换边界保持不变。
- 配置：CSRF 重试、链路瞬断重试、整轮重试、回池次数上限的配置项落点在 design.md 中确定。
- 不在范围内：换指纹、2FA 重认证、callback 与 OTP 阶段的步骤内重试、POST 重复提交的识别、Sentinel 的异常分类（现状已区分网络失败，无需对齐）、账号创建之后的失败重试、查活链的失败分类与邮箱处理、已有账号登录失败后池邮箱停在 `registering` 的既有缺口（见 design.md 的记录）。
