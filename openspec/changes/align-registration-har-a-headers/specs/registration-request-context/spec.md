# Spec Delta

## Purpose

约束账号注册链中设备预热与正式认证请求的上下文边界，使正式 CSRF、signin、OTP 发送导航和 OAuth 回调携带可核验的 Referer、缓存及用户触发字段，同时保留动态凭据来源并隔离不在变更范围内的登录行为。

## ADDED Requirements

### Requirement: 设备预热与正式 CSRF 上下文隔离

注册入口 MUST 保留第一次 CSRF warmup 作为设备 Cookie 初始化请求，不显式添加邮箱、login_hint、登录上下文 Referer 或登录上下文 query/body；正式 signin 使用的 csrfToken MUST 来自第二次正式 CSRF 响应。

#### Scenario: 首次设备 Cookie 预热
- **WHEN** 注册会话尚未绑定服务端设备 ID，并发起第一次 `/api/auth/csrf` 请求
- **THEN** 请求仍为无 query/body 的 GET，不增加 Referer 或显式邮箱字段
- **AND** 响应 Cookie 用于绑定当前会话设备 ID，不把该响应中的 token 替代正式 CSRF token

### Requirement: 正式 CSRF 与 signin 共享登录上下文

注册的正式 CSRF GET MUST 使用与紧随其后的 signin POST 完全相同的完整 `https://chatgpt.com/auth/login_with?...` Referer。Referer MUST 使用当前邮箱、已绑定设备 ID 和当前会话 logging ID；CSRF URL MUST 保持无 query，请求 MUST 保持无 body。

#### Scenario: 为当前注册输入构造完整 Referer
- **WHEN** 已绑定设备 ID 的注册会话为当前邮箱获取正式 csrfToken 并发起 signin
- **THEN** 两次请求的 Referer 相同，且 query 字段集合为 callback_path、screen_hint、login_hint、auth_session_logging_id、ext-oai-did
- **AND** callback_path 为 `/`，screen_hint 为 `login_or_signup`，其余三个值分别为当前邮箱、当前会话 logging ID 和已绑定设备 ID
- **AND** CSRF 请求目标仍为 `/api/auth/csrf`，无 query/body

#### Scenario: 编码邮箱而不改变其语义
- **WHEN** 当前邮箱包含加号等需要 URL 编码的字符
- **THEN** 从两次 Referer 解码得到的 login_hint 均等于完整当前邮箱，不因重复编码或字符串拼接而改变

### Requirement: 正式认证请求使用端点级内容类型和缓存头

注册的正式 CSRF GET MUST 不携带 Content-Type，且 MUST 携带 `cache-control: no-cache` 和 `pragma: no-cache`。注册 signin POST MUST 携带相同缓存头，并保留表单 Content-Type、Origin、既有 query/form 字段及正式 csrfToken 来源。

#### Scenario: 请求正式 CSRF
- **WHEN** 注册入口发出第二次正式 CSRF GET
- **THEN** 请求无 Content-Type，两个禁缓存头均为 no-cache

#### Scenario: 提交 signin 表单
- **WHEN** 注册入口使用正式 CSRF 响应发起 signin POST
- **THEN** 两个禁缓存头均为 no-cache，Content-Type 为 application/x-www-form-urlencoded，Origin 为 https://chatgpt.com
- **AND** form 保留 callbackUrl、csrfToken、json，query 保留 prompt、screen_hint、auth_session_logging_id、login_hint，csrfToken 来自该正式 CSRF 响应

### Requirement: 注册 OTP 发送导航使用 auth 根上下文

新账号注册中，服务端继续地址指向 OTP 发送步骤时，发送导航及其 email-verification 重定向落页 MUST 使用 `https://auth.openai.com/` Referer，并保留 `Sec-Fetch-Site: same-origin` 和 `Sec-Fetch-User: ?1`。请求 URL MUST 继续来自服务端 continue_url 和 Location。

#### Scenario: 密码注册后发送 OTP
- **WHEN** 新账号注册返回 email-otp/send 继续地址，随后重定向到 email-verification
- **THEN** 发送请求及验证页请求均使用 auth 根 Referer、same-origin 和 Sec-Fetch-User ?1
- **AND** 不增加页面 GET，不修改设备身份或 document-navigation ID 的既有轮换时机

### Requirement: 注册 OAuth 收尾导航不声明用户触发

新账号注册进入 OAuth 收尾时，callback 及随后 ChatGPT 首页请求 MUST 使用 `https://auth.openai.com/` Referer，MUST 不携带 Sec-Fetch-User，并保留 document/navigate/cross-site 导航字段。callback URL MUST 继续来自服务端继续地址及其重定向响应，Cookie MUST 继续由同一会话管理。

#### Scenario: 创建资料后完成 callback 并落到首页
- **WHEN** 新账号创建资料成功后按服务端继续地址访问 ChatGPT OAuth callback 并重定向到首页
- **THEN** callback 和首页请求均使用 auth 根 Referer，无 Sec-Fetch-User，sec-fetch-dest/mode/site 分别为 document/navigate/cross-site
- **AND** callback 响应设置的会话 Cookie 按域和路径规则用于后续首页及现有 session 查询

### Requirement: 请求头同步不外溢至非目标流程

此次请求头同步 MUST 不改变首次 warmup、已有账号登录和其他非目标导航调用的头部语义，MUST 不额外增加请求或改变业务请求顺序，MUST 保留现有 authorize URL 处理、画像、Sentinel 绑定、Cookie 管理、token 获取方式和 MFA 行为。

#### Scenario: 非目标调用保持原有导航语义
- **WHEN** 已有账号登录或非本次新账号注册收尾的调用方发起导航
- **THEN** 其显式 Referer、用户触发选择及通用 builder 默认行为不因本次同步被全局改变

#### Scenario: 注册仍通过 session 接口读取 token
- **WHEN** 注册 callback 完成并建立会话 Cookie
- **THEN** 注册仍使用现有 session 查询取得 accessToken 和账号元数据，不新增首页 bootstrap token 解析
