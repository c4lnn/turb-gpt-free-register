# Spec Delta

## Purpose

定义 OAuth 握手前三步（正式 CSRF、signin、authorize）在遇到 HTTP 错误码和网络错误时的重试与放弃规则，并划分步骤内重试与调用方外层重试的归属，避免同一错误被多层重复重试。

## ADDED Requirements

### Requirement: 正式 CSRF 收到 403 时在同一会话内重试
获取正式 CSRF token 的请求收到 HTTP 403 时，系统 SHALL 保留当前会话（Cookie、设备标识、指纹均不变）重试该请求，最多尝试 3 次，重试间隔依次为 5 秒和 10 秒。

#### Scenario: 首次 403 后重试成功
- **WHEN** 获取正式 CSRF token 的请求第 1 次返回 403，第 2 次返回 200
- **THEN** 系统返回第 2 次响应中的 csrfToken
- **AND** 两次请求使用同一会话，第 1 次 403 响应下发的 Cookie 被第 2 次请求携带
- **AND** 两次请求之间等待 5 秒

#### Scenario: 连续三次 403 后放弃
- **WHEN** 获取正式 CSRF token 的请求连续 3 次返回 403
- **THEN** 系统向调用方抛出表示 HTTP 403 的错误
- **AND** 共发出 3 次请求，不再重试

### Requirement: 正式 CSRF 重试前清除本地熔断状态
正式 CSRF 因 403 重试时，系统 MUST 在发出下一次请求前清除该 403 触发的本地熔断状态，使重试请求能够发出。

#### Scenario: 熔断状态下重试请求仍可发出
- **WHEN** 获取正式 CSRF token 的请求第 1 次返回 403 并触发本地熔断
- **THEN** 第 2 次请求正常发出并到达服务端，不被本地熔断拦截

### Requirement: 正式 CSRF 的非 403 错误直接抛出
获取正式 CSRF token 的请求遇到 403 以外的 HTTP 错误码或网络错误时，系统 SHALL 不做步骤内重试，直接向调用方抛出。

#### Scenario: 收到 429
- **WHEN** 获取正式 CSRF token 的请求返回 HTTP 429
- **THEN** 系统向调用方抛出错误，共发出 1 次请求

#### Scenario: 响应超时
- **WHEN** 获取正式 CSRF token 的请求在传输层重试耗尽后仍然超时
- **THEN** 系统向调用方抛出超时错误，步骤内不再重试

### Requirement: signin 的全部错误直接抛出
发起 OAuth signin 的请求遇到任何 HTTP 错误码或网络错误时，系统 SHALL 不做步骤内重试，直接向调用方抛出。

#### Scenario: signin 返回 403
- **WHEN** signin 请求返回 HTTP 403
- **THEN** 系统向调用方抛出错误，共发出 1 次 signin 请求

### Requirement: authorize 的全部错误直接抛出
跟随 OAuth authorize URL 的导航遇到 403、其它 HTTP 错误码或网络错误时，系统 SHALL 不在同一会话内重试，直接向调用方抛出，由调用方决定是否放弃当前轮次。

#### Scenario: authorize 返回 403
- **WHEN** authorize 导航返回 HTTP 403
- **THEN** 系统向调用方抛出错误，共发出 1 次 authorize 导航请求

#### Scenario: authorize 发生网络错误
- **WHEN** authorize 导航在连接已建立后连接被重置
- **THEN** 系统向调用方抛出错误，不在同一会话内重试

### Requirement: 查活登录预检不在外层重复重试正式 CSRF 的 403
查活登录的外层预检循环遇到正式 CSRF 步骤抛出的 403 时 SHALL 不在外层重试，403 的恢复只由正式 CSRF 步骤内的重试负责。外层循环对其它步骤的错误和 403 以外的网络错误 SHALL 保持原有重试行为。

#### Scenario: 正式 CSRF 连续 403 后不再外层重试
- **WHEN** 查活登录预检中正式 CSRF 步骤内 3 次尝试均返回 403 并抛出错误
- **THEN** 外层预检循环不重试，直接向调用方抛出该错误
- **AND** 本次预检共发出 3 次 CSRF 请求

#### Scenario: 外层对其它网络错误仍重试
- **WHEN** 查活登录预检中正式 CSRF 请求因代理错误失败
- **THEN** 外层预检循环按原有次数重试

#### Scenario: signin 返回 403 仍由外层重试
- **WHEN** 查活登录预检中 signin 请求返回 HTTP 403
- **THEN** 外层预检循环按原有次数重试

### Requirement: 握手前三步的重发只来自传输层
正式 CSRF、signin、authorize 三个步骤在步骤内 MUST NOT 重发任何请求。自动重发只由传输层的链路瞬断重试负责：连接建立阶段失败和超时会触发重发，请求发出后的连接中断不触发。

#### Scenario: signin 在连接建立阶段失败后自动重发
- **WHEN** signin 请求在连接建立阶段失败，请求体未发出
- **THEN** 该请求按传输层链路瞬断重试规则自动重发，成功后正常返回

#### Scenario: signin 在请求发出后连接中断
- **WHEN** signin 请求体已发出后连接被重置
- **THEN** 系统向调用方抛出错误，不重发该 signin 请求

#### Scenario: authorize 遇到网络错误
- **WHEN** authorize 导航在传输层重试耗尽后仍失败
- **THEN** 步骤内不再重试，直接向调用方抛出错误
