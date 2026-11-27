# Spec Delta

## Purpose

定义注册任务失败后的处理：失败类别如何判定与记录、失败类别如何决定邮箱池状态、账号创建之前的网络类失败如何整轮重试、人工重试时邮箱的选取规则，以及服务启动时对残留注册任务的收口。

## ADDED Requirements

### Requirement: 注册失败时按结构化信号判定失败类别

注册任务失败时，系统 SHALL 判定失败类别并记录到任务记录。失败类别取值为 `network`、`account`、`unknown` 之一。判定 SHALL 只依据异常类型与异常对象携带的结构化字段，MUST NOT 对错误消息文本做关键词匹配。

#### Scenario: 传输层异常归为网络类

- **WHEN** 注册因 `requests.ConnectionError`、`requests.Timeout` 或 `requests.ProxyError` 失败
- **THEN** 失败类别为 `network`

#### Scenario: 预检出口观测的传输层异常归为网络类

- **WHEN** 注册预检对 `cloudflare.com/cdn-cgi/trace` 的请求因 `requests.ConnectionError`、`requests.Timeout` 或 `requests.ProxyError` 失败
- **THEN** 失败类别为 `network`，不是 `unknown`
- **AND** 该失败在账号创建之前发生，可以触发整轮重试

#### Scenario: 预检出口观测的 HTTP 非 200 仍归为未知类

- **WHEN** 预检的出口观测请求返回非 200 状态码（407 除外），或其响应无法解析
- **THEN** 失败类别为 `unknown`

#### Scenario: Sentinel 请求的网络失败归为网络类

- **WHEN** 注册因 category 为 `sentinel_network_failed` 的 `SentinelRequestError` 失败
- **THEN** 失败类别为 `network`

#### Scenario: Sentinel 服务返回错误或响应无效归为网络类

- **WHEN** 注册因 category 为 `sentinel_service_failed` 或 `sentinel_challenge_failed` 的 `SentinelRequestError` 失败
- **THEN** 失败类别为 `network`

#### Scenario: Sentinel 求解失败归为网络类

- **WHEN** 注册因 category 为 `protocol_error`、`flow_mismatch`、`invalid_challenge`、`no_requirements`、`empty_main_token` 或 `token_context_mismatch` 的 `SentinelSDKError` 失败
- **THEN** 失败类别为 `network`

#### Scenario: 代理连接失败归为网络类

- **WHEN** 注册因 category 为 `proxy` 的 `RegistrationGeoError` 失败
- **THEN** 失败类别为 `network`

#### Scenario: 账号已废归为账号类

- **WHEN** 注册因 `AccountUnusableError` 失败
- **THEN** 失败类别为 `account`

#### Scenario: 409 和 5xx 归为未知类

- **WHEN** 注册因 `requests.HTTPError` 失败，状态码为 409（含 `already exists`）或 5xx
- **THEN** 失败类别为 `unknown`

#### Scenario: CF 挑战的 403 归为网络类

- **WHEN** 注册因 `requests.HTTPError` 失败，状态码为 403，且响应头 `cf-mitigated` 的值为 `challenge`
- **THEN** 失败类别为 `network`

#### Scenario: 非 CF 的 403 归为未知类

- **WHEN** 注册因 `requests.HTTPError` 失败，状态码为 403，响应没有 `cf-mitigated: challenge` 头
- **THEN** 失败类别为 `unknown`

#### Scenario: 地域门禁失败归为未知类

- **WHEN** 注册因 category 为 `geo_mismatch`、`geo_incomplete`、`geo_service` 或 `geo_parse` 的 `RegistrationGeoError` 失败
- **THEN** 失败类别为 `unknown`

#### Scenario: 本地环境缺失归为未知类

- **WHEN** 注册因 category 为 `sdk_missing`、`node_missing`、`requirements_missing`、`sdk_exec_failed`、`closed` 或 `timeout` 的 `SentinelSDKError` 失败
- **THEN** 失败类别为 `unknown`

#### Scenario: 其余结构化失败归为未知类

- **WHEN** 注册因 `_RegistrationFailure` 或 `PasswordRejectedError` 失败
- **THEN** 失败类别为 `unknown`

#### Scenario: 失败类别写入任务记录

- **WHEN** 注册任务以失败结束
- **THEN** 任务记录的失败类别字段为本次判定结果
- **AND** 后续读取该任务记录的代码可以直接取得失败类别，不需要解析错误消息

### Requirement: 网络类失败的邮箱回到可领取状态

注册任务最终以 `network` 类别失败，且该邮箱来自持久化邮箱池、未达到回池次数上限时，系统 SHALL 把邮箱池条目置为 `available`。以 `account` 或 `unknown` 类别失败时，系统 SHALL 保持现状把条目标记为 `failed`。

#### Scenario: 网络类失败后邮箱可被再次领取

- **WHEN** 池中邮箱的注册任务以 `network` 类别失败，且该邮箱的回池次数未达上限
- **THEN** 该邮箱池条目状态为 `available`
- **AND** 该条目可被后续任意注册任务领取

#### Scenario: 未知类失败后邮箱保持不可领取

- **WHEN** 池中邮箱的注册任务以 `unknown` 类别失败
- **THEN** 该邮箱池条目状态为 `failed`，与改动前一致

#### Scenario: 账号类失败后邮箱保持不可领取

- **WHEN** 池中邮箱的注册任务以 `account` 类别失败
- **THEN** 该邮箱池条目状态为 `failed`，与改动前一致

#### Scenario: 命中停用判定的失败优先于失败类别

- **WHEN** 注册因 `/api/auth/session` 等待 accessToken 超时而失败
- **THEN** 邮箱池条目状态为 `disabled`，与改动前一致
- **AND** 该失败不参与失败类别对邮箱状态的判定

#### Scenario: 已有账号登录失败不动邮箱

- **WHEN** 注册流程按已有账号登录分支执行后失败
- **THEN** 系统不改变该邮箱池条目的状态，与改动前一致

#### Scenario: 临时邮箱不受失败类别影响

- **WHEN** 使用临时邮箱（cloudflare / remail）的注册以 `network` 类别失败
- **THEN** 系统按现有方式清理该临时邮箱，不尝试恢复

#### Scenario: 本地已有完整账号的邮箱不回池

- **WHEN** 注册以 `network` 类别失败，但本地已存在该邮箱的完整账号记录
- **THEN** 系统不改变该邮箱池条目的状态，与改动前一致

### Requirement: 网络类回池有次数上限

邮箱池条目 SHALL 记录该邮箱因 `network` 类别失败回池的次数。每次因 `network` 类别失败处理邮箱时，系统 SHALL 先把该次数加一，再判断是否回池：次数达到上限（默认 3，SHALL 可配置）时，系统 SHALL 把条目标记为 `failed` 且不回池；未达上限时置为 `available`。上限配置为 0 时 SHALL 视为不设上限。

人工把条目恢复为 `available`、重试任务复用该邮箱、显式重新导入、注册成功这四种情形，系统 SHALL 把该次数清零。

#### Scenario: 第一次和第二次网络类失败回池

- **WHEN** 池中邮箱先后两次以 `network` 类别失败，上限为 3
- **THEN** 两次处理后条目状态都为 `available`
- **AND** 第二次处理后条目记录的回池次数为 2

#### Scenario: 第三次网络类失败不再回池

- **WHEN** 回池次数为 2 的邮箱再次以 `network` 类别失败，上限为 3
- **THEN** 条目状态为 `failed`
- **AND** 条目记录的回池次数为 3，备注说明已达上限
- **AND** 该条目不会被后续注册任务领取

#### Scenario: 未知类失败不累计回池次数

- **WHEN** 回池次数为 1 的邮箱以 `unknown` 类别失败
- **THEN** 条目状态为 `failed`，回池次数保持为 1

#### Scenario: 人工恢复后重新计数

- **WHEN** 回池次数为 3、状态为 `failed` 的邮箱被人工恢复为 `available`
- **THEN** 条目的回池次数为 0

#### Scenario: 显式重新导入后重新计数

- **WHEN** 回池次数为 3、状态为 `failed` 的邮箱被显式重新导入并开启恢复已有条目
- **THEN** 条目恢复为 `available`，回池次数为 0

#### Scenario: 注册成功后计数清零

- **WHEN** 回池次数为 2 的邮箱注册成功
- **THEN** 条目状态为 `used`，回池次数为 0

#### Scenario: 上限配置为 0 时不设上限

- **WHEN** 上限配置为 0，邮箱已连续多次以 `network` 类别失败
- **THEN** 每次处理后条目状态都为 `available`

#### Scenario: 进程重启不累计回池次数

- **WHEN** 回池次数为 2 的邮箱处于注册中，进程重启后该条目被启动迁移标记为 `failed`
- **THEN** 条目的回池次数保持为 2

### Requirement: 失败类别在一次状态迁移内决定邮箱池状态

注册任务失败处理邮箱时，系统 SHALL 依据失败类别在一次池状态迁移内把条目置为 `available` 或 `failed`，MUST NOT 先置为 `failed` 再另行恢复。迁移 SHALL 在持有邮箱池锁时重新确认条目当前状态为 `registering`。

#### Scenario: 失败处理过程中邮箱被其它路径改动

- **WHEN** 任务失败，开始处理邮箱时该条目状态已不是 `registering`
- **THEN** 系统不改变该条目状态，并记录一条告警

### Requirement: 账号创建之前的网络类失败整轮重试

注册在账号创建之前因 `network` 类别失败时，系统 SHALL 换出口、用同一邮箱重新执行完整注册流程。总轮数默认 3 轮，SHALL 可配置。每轮 SHALL 重建完整会话，MUST NOT 复用上一轮的设备 ID、Cookie 或熔断状态。

账号创建的边界是 `user/register` 请求成功返回。该请求成功之前发生的失败属于账号创建之前；之后发生的失败不属于。

#### Scenario: 首轮网络类失败后换出口重来

- **WHEN** 第 1 轮在正式 CSRF 步骤因 `requests.ConnectionError` 失败
- **THEN** 系统以同一邮箱开始第 2 轮，使用新会话
- **AND** 第 2 轮的设备 ID 与第 1 轮不同
- **AND** 代理池有多个候选时，第 2 轮使用的出口与第 1 轮不同

#### Scenario: 三轮全部失败后放弃

- **WHEN** 3 轮都在账号创建之前因网络类失败终止
- **THEN** 任务以失败结束，失败类别为 `network`
- **AND** 邮箱池条目置为 `available`，除非已达到回池次数上限

#### Scenario: 第二轮成功

- **WHEN** 第 1 轮网络类失败，第 2 轮注册成功
- **THEN** 任务以成功结束
- **AND** 任务记录不保留第 1 轮的失败类别

#### Scenario: 后一轮以非网络类失败终止

- **WHEN** 第 1 轮网络类失败，第 2 轮因不带 `cf-mitigated: challenge` 头的 HTTP 403 失败
- **THEN** 任务以失败结束，失败类别为 `unknown`
- **AND** 邮箱池条目置为 `failed`

#### Scenario: 账号已创建后的失败不重试

- **WHEN** 第 1 轮在 `user/register` 请求已成功返回之后因网络类失败
- **THEN** 系统不开始新的一轮，任务以失败结束，失败类别为 `network`
- **AND** 邮箱池条目置为 `available`，除非已达到回池次数上限

#### Scenario: 非网络类失败不重试

- **WHEN** 第 1 轮因不带 `cf-mitigated: challenge` 头的 HTTP 403 失败（类别为 `unknown`）
- **THEN** 系统不开始新的一轮，任务以失败结束

#### Scenario: CF 挑战的 403 触发整轮重试

- **WHEN** 第 1 轮在正式 CSRF 的同会话重试耗尽后，因带 `cf-mitigated: challenge` 头的 HTTP 403 失败
- **THEN** 系统以同一邮箱开始第 2 轮
- **AND** 第 2 轮使用新会话和新出口

#### Scenario: 账号创建之前的 Sentinel 求解失败触发整轮重试

- **WHEN** 第 1 轮在 `user/register` 请求发出之前，因 `SentinelRequestError` 或求解类 `SentinelSDKError` 失败
- **THEN** 系统以同一邮箱开始第 2 轮

#### Scenario: 账号创建之后的 Sentinel 求解失败不重试

- **WHEN** 第 1 轮在 `user/register` 请求已成功返回之后，因 `SentinelRequestError` 或求解类 `SentinelSDKError` 失败
- **THEN** 系统不开始新的一轮，任务以失败结束，失败类别为 `network`
- **AND** 邮箱池条目置为 `available`，除非已达到回池次数上限

#### Scenario: 已有账号登录失败不重试

- **WHEN** 流程按已有账号登录分支执行后因网络类失败
- **THEN** 系统不开始新的一轮

#### Scenario: 重试期间邮箱保持占用

- **WHEN** 第 1 轮失败、第 2 轮尚未开始
- **THEN** 邮箱池条目保持 `registering`、回池次数不变，不被其它任务领取

#### Scenario: 单轮执行中用户停止

- **WHEN** 某一轮执行过程中用户请求停止，该轮尚未因失败结束
- **THEN** 系统不开始新的一轮，任务按现有方式标记为 `stopped`
- **AND** 邮箱池条目置为 `failed`，回池次数不变

#### Scenario: 轮次之间用户停止

- **WHEN** 第 1 轮网络类失败后，系统在等待下一轮期间用户请求停止
- **THEN** 系统不开始新的一轮，任务按现有方式标记为 `stopped`
- **AND** 邮箱池条目置为 `failed`，回池次数不变。非最后一轮的失败不处理邮箱，所以停止时没有已完成的回池可撤销

#### Scenario: 非池模式下重试沿用原路由

- **WHEN** 代理池只有一个候选或配置为固定代理
- **THEN** 重试的新一轮仍执行，出口与上一轮相同

#### Scenario: 服务端已有账号的邮箱回池后再次注册

- **WHEN** 邮箱因 `network` 类别失败回到 `available`，之后被任意任务领取并执行注册
- **THEN** 注册流程按服务端返回的账号类型进入已有账号登录分支或新号分支
- **AND** 本地不为该邮箱预先创建账号记录

### Requirement: 人工重试优先使用原邮箱

用户对失败的注册任务发起重试时，若原任务失败类别为 `network` 且原邮箱满足可恢复条件，系统 SHALL 恢复原邮箱、把它领取给重试任务，并用它执行；否则 SHALL 保持现状从邮箱池领取新邮箱。

可恢复条件：邮箱来自持久化邮箱池、条目状态为 `failed` 或 `available`、本地没有该邮箱的完整账号、没有活动注册任务占用该邮箱。恢复与领取 SHALL 是一个原子动作。

#### Scenario: 网络类失败后重试使用原邮箱

- **WHEN** 用户对 `network` 类别失败的任务发起重试
- **THEN** 重试任务使用原邮箱执行，不从邮箱池领取其它邮箱
- **AND** 重试任务开始执行时，原邮箱条目状态为 `registering` 且归属该重试任务

#### Scenario: 未知类失败后重试领取新邮箱

- **WHEN** 用户对 `unknown` 或 `account` 类别失败的任务发起重试
- **THEN** 重试任务保持现状，执行时从邮箱池领取新邮箱

#### Scenario: 原邮箱已回池时不重复恢复

- **WHEN** 用户对 `network` 类别失败的任务发起重试，原邮箱条目状态为 `available`
- **THEN** 系统直接把原邮箱领取给重试任务，不调用恢复路径

#### Scenario: 原邮箱已被停用时不恢复

- **WHEN** 用户对 `network` 类别失败的任务发起重试，原邮箱条目状态为 `disabled`
- **THEN** 系统不恢复该邮箱，重试任务执行时从邮箱池领取新邮箱

#### Scenario: 原邮箱已有完整账号时不恢复

- **WHEN** 用户对 `network` 类别失败的任务发起重试，本地已存在该邮箱的完整账号记录
- **THEN** 系统不恢复该邮箱，重试任务执行时从邮箱池领取新邮箱

#### Scenario: 原邮箱被活动任务占用时不恢复

- **WHEN** 用户对 `network` 类别失败的任务发起重试，该邮箱正被另一个活动注册任务占用
- **THEN** 系统不恢复该邮箱，重试任务执行时从邮箱池领取新邮箱

#### Scenario: 原邮箱不属于持久化池时不恢复

- **WHEN** 用户对 `network` 类别失败的任务发起重试，原邮箱是临时邮箱或未入池的手动输入地址
- **THEN** 系统不恢复该邮箱，重试任务执行时按现有规则获取邮箱

#### Scenario: 重试任务执行时原邮箱不可领取

- **WHEN** 重试任务被指定使用原邮箱，但执行时该邮箱条目已不是 `registering` 且不属于该任务
- **THEN** 重试任务以失败结束并说明原因，不改为领取其它邮箱

### Requirement: 账号已保存但 2FA 未完成时任务为部分成功

当配置要求 2FA（`ENABLE_2FA` 为真）且注册已完成账号创建与登录态保存、但 2FA 未完成时，系统 SHALL 把任务状态记为 `partial_success`，MUST NOT 记为 `failed` 或 `success`。任务记录 SHALL 说明 2FA 未完成；`failure_category` SHALL 为空。该任务领取的邮箱 SHALL 视为已消耗，MUST NOT 回池。

账号未保存或未取得登录态的失败仍记为 `failed`；配置不要求 2FA 时 2FA 不参与成功判定。

#### Scenario: 2FA 设置失败但账号已保存

- **WHEN** 配置要求 2FA，注册已完成账号创建与登录态保存，但 2FA 设置失败
- **THEN** 任务状态为 `partial_success`，任务记录说明 2FA 未完成
- **AND** 任务的失败类别为空
- **AND** 该任务的邮箱不回池

#### Scenario: 配置不要求 2FA

- **WHEN** `ENABLE_2FA` 为非真，且账号与登录态已保存
- **THEN** 任务状态为 `success`

#### Scenario: 账号未保存

- **WHEN** 未取得登录态或账号未保存
- **THEN** 任务状态为 `failed`，不记为部分成功

#### Scenario: 部分成功任务的重试能力

- **WHEN** 用户对 `partial_success` 任务查询或发起重试
- **THEN** 已有完整账号时按账号能力处理：Codex 未完成则补跑 Codex，Codex 已完成则说明 2FA 需人工处理且不提供自动补跑入口
- **AND** 不改动该任务的邮箱

### Requirement: 服务启动时收口残留的注册任务

服务启动时，系统 SHALL 把上次进程留下的未结束注册任务改为终态，MUST NOT 恢复执行它们。状态为 `pending` 的任务 SHALL 改标 `cancelled`；状态为 `running` 或 `stopping` 的任务 SHALL 改标 `stopped`。任务记录 SHALL 写明原因是服务重启。

收口 SHALL 在邮箱池状态迁移之前执行，收口本身 MUST NOT 改动邮箱池。迁移随后按池条目当时的状态处理，不区分该任务被中断时进行到哪一步：仍为 `registering` 的条目置为 `failed`，不累计回池次数；已回到 `available` 的条目保持 `available`，回池次数不变。整轮重试期间条目保持 `registering`（见「账号创建之前的网络类失败整轮重试」），在轮次之间被中断的任务落在前一种情况。

#### Scenario: 排队中的任务在重启后被取消

- **WHEN** 服务重启时任务表里有状态为 `pending` 的注册任务
- **THEN** 该任务状态为 `cancelled`，记录的原因说明是服务重启、任务未执行

#### Scenario: 执行中的任务在重启后标记为已停止

- **WHEN** 服务重启时任务表里有状态为 `running` 的注册任务
- **THEN** 该任务状态为 `stopped`，记录的原因说明是服务重启、执行中断

#### Scenario: 停止中的任务在重启后标记为已停止

- **WHEN** 服务重启时任务表里有状态为 `stopping` 的注册任务
- **THEN** 该任务状态为 `stopped`，记录的原因说明是服务重启、执行中断

#### Scenario: 残留任务领取的邮箱不再被占用

- **WHEN** 被收口的任务领取了持久化邮箱池中的邮箱，该条目状态为 `registering`
- **THEN** 启动迁移完成后该条目状态为 `failed`
- **AND** 该条目的回池次数不变
- **AND** 该条目此后可被人工恢复或删除

#### Scenario: 被中断时服务端已创建账号但本地未保存

- **WHEN** 状态为 `running` 的任务被中断时已过账号创建步骤，服务端已有该邮箱的账号，本地没有保存账号记录，邮箱池条目仍为 `registering`
- **THEN** 该任务收口为 `stopped`，邮箱池条目置为 `failed`，不回池

#### Scenario: 整轮重试的轮次之间被中断

- **WHEN** 状态为 `running` 的任务在第 1 轮网络类失败后、下一轮开始前被中断，当时邮箱池条目仍为 `registering`、回池次数为 0
- **THEN** 该任务收口为 `stopped`
- **AND** 启动迁移完成后邮箱池条目置为 `failed`，回池次数仍为 0

#### Scenario: 被中断前账号已保存

- **WHEN** 状态为 `running` 的任务被中断前本地已保存该邮箱的完整账号记录，邮箱池条目仍为 `registering`
- **THEN** 该任务收口为 `stopped`，邮箱池条目按现有启动迁移规则置为 `used`，不置为 `failed`

#### Scenario: 已结束的任务不受影响

- **WHEN** 服务重启时任务表里有状态为 `success`、`failed`、`stopped` 或 `cancelled` 的注册任务
- **THEN** 系统不改变这些任务的状态和记录

#### Scenario: Codex 补跑任务同样被收口

- **WHEN** 服务重启时任务表里有状态为 `pending`、`running` 或 `stopping` 的 Codex 补跑任务
- **THEN** 该任务按与注册任务相同的规则收口

#### Scenario: 收口后的任务可以重试

- **WHEN** 用户对被收口为 `stopped` 的任务发起重试
- **THEN** 该任务没有失败类别，重试任务按现有规则领取新邮箱，不复用原邮箱
