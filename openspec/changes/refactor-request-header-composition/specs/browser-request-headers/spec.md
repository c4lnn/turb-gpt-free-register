## Purpose

规范认证及相关浏览器请求场景的请求头输出、画像取值和跨请求状态边界，使正式 CSRF 至密码与 TOTP 校验的目标登录分支可按捕获证据验证，同时保持注册及其他非目标分支的既有语义，防止动态关联字段、凭据和高熵画像在场景之间泄漏。

## ADDED Requirements

### Requirement: 场景头部使用稳定画像且请求之间互不污染

系统 MUST 从同一会话的当前稳定画像取得 User-Agent、Accept-Language 和适用的低熵 Client Hints。各请求的头部结果 MUST 独立，后续附加凭据或覆盖场景字段 MUST 不改变已经构造的其他请求。场景输出 MUST 以字段名大小写无关的方式验证，MUST 区分缺失、空字符串及带引号的空模型值。

#### Scenario: 多种场景使用同一画像
- **WHEN** 同一会话先后构造正式 CSRF、Auth JSON、导航与 Sentinel 请求
- **THEN** User-Agent、Accept-Language 和适用的低熵 CH 来自同一会话画像，不从捕获样本复制固定版本或平台
- **AND** 对其中一次请求附加的凭据或头部覆盖不出现在其他请求中

#### Scenario: 非 Chromium 基础场景
- **WHEN** 未启用高熵的非 Chromium 画像构造基础请求头
- **THEN** 请求使用该画像的 UA 和语言，不伪造 Chromium Client Hints

### Requirement: 高熵 Client Hints 按场景完整且显式启用

系统 MUST 仅在请求场景明确启用时添加高熵 CH，且 MUST 从当前会话画像取得六个字段：`sec-ch-ua-full-version`、`sec-ch-ua-full-version-list`、`sec-ch-ua-platform-version`、`sec-ch-ua-arch`、`sec-ch-ua-bitness`、`sec-ch-ua-model`。启用时 MUST 不输出五字段子集，不以其他画像或独立默认常量弥补缺失值。隐式全局高熵开关 MUST 不影响场景结果。

#### Scenario: 完整 Chromium 画像启用高熵
- **WHEN** 请求场景启用高熵且当前 Chromium 画像完整
- **THEN** 六个字段全部出现，各值与当前画像对应
- **AND** 值为两个双引号字符的 `sec-ch-ua-model` 被保留为合法空模型，不视为缺失

#### Scenario: 缺少画像值或画像不支持高熵
- **WHEN** 场景要求高熵，但画像为非 Chromium，或六个所需画像值有缺失或无效空值
- **THEN** 系统在该请求发送前明确失败，不发送部分高熵字段、不切换画像且不静默降级

#### Scenario: 场景关闭高熵
- **WHEN** 场景未启用高熵，即使其他请求曾启用
- **THEN** 本次请求不携带上述六个字段，低熵画像保持其原有适用规则

### Requirement: 目标登录策略限于明确选择的密码登录前缀

系统 MUST 在目标 full-web 密码登录的正式 CSRF 之前明确选择目标头部策略，并使同一次流程的 CSRF、signin、OAuth 导航、密码验证、对应 Sentinel challenge 及实际触发的 MFA 请求一致消费该策略。系统 MUST 继续由既有密码输入决定是否尝试密码登录，并由服务端响应决定是否进入 MFA，MUST 不以 TOTP secret 是否存在提前改变登录分支。目标策略 MUST 不因调用同一个共享步骤而自动作用于注册、导出、激活或其他非目标入口。

#### Scenario: 目标 full-web 密码登录进入 TOTP
- **WHEN** full-web 登录入口已取得可用密码，随后服务端密码响应要求 TOTP
- **THEN** 正式 CSRF 至 MFA verify 使用目标头部合同，MFA 请求不会退回共享默认头部
- **AND** 密码输入与该次策略选择保持一致，不因流程中重新读取输入而漂移

#### Scenario: 无密码的邮箱 OTP 或非目标共享调用
- **WHEN** 登录入口没有可用密码，或注册/账号导出调用共享密码或 MFA 步骤
- **THEN** 其请求仍采用各自既有场景语义，不自动获得目标登录的高熵、Priority 或导航用户触发变更

#### Scenario: 目标密码响应转向其他步骤
- **WHEN** 目标密码请求之后进入邮箱 OTP、成功回调或最终 session 查询
- **THEN** 已完成的目标请求不回滚、不重发，后续捕获范围外端点保持其原有场景头部

### Requirement: 目标正式 CSRF 与 signin 共享当前登录上下文

目标登录的正式 CSRF GET 与消费其 csrfToken 的 signin POST MUST 使用完全相同的完整 `https://chatgpt.com/auth/login_with?...` Referer，query 字段集合 MUST 为 `callback_path`、`screen_hint`、`login_hint`、`auth_session_logging_id`、`ext-oai-did`。值 MUST 分别来自固定 `/`、固定 `login_or_signup`、当前邮箱、当前会话 logging ID 和当前设备 ID，MUST 不额外请求该 Referer URL。两次请求 MUST 携带 `cache-control: no-cache`、`pragma: no-cache`、六个高熵 CH 以及 `same-origin/cors/empty` Fetch Metadata，Accept MUST 为 `*/*`，MUST 不显式添加 Priority。

#### Scenario: 正式 CSRF GET 对应捕获 #16
- **WHEN** 目标登录获取将被 signin 消费的正式 csrfToken
- **THEN** 请求仍是无 query/body 的 `/api/auth/csrf` GET，使用上述完整 Referer、禁缓存头和高熵 CH
- **AND** 请求不设置 Content-Type 或 Origin，不通过自定义 x-csrf-token 头传递 CSRF

#### Scenario: signin 表单对应捕获 #17
- **WHEN** 目标登录使用正式 CSRF 响应提交 signin
- **THEN** Referer 与本次正式 CSRF 相同，Content-Type 为 `application/x-www-form-urlencoded`，Origin 为 `https://chatgpt.com`
- **AND** 表单 csrfToken 精确来自该次正式响应；既有 callbackUrl、json、query 字段及 authorize URL 处理保持原语义

#### Scenario: 含加号邮箱的上下文一致传递
- **WHEN** 当前邮箱包含加号等需要编码的字符
- **THEN** 两次 Referer 解码后的 login_hint 均等于原始完整邮箱，且设备与 logging ID 均来自同一会话

### Requirement: 目标 OAuth 导航保留来源链语义

目标登录从 ChatGPT 进入 OAuth authorize 并重定向到密码页时，两次导航 MUST 使用 `https://chatgpt.com/` Referer、`sec-fetch-site: cross-site`、`sec-fetch-mode: navigate`、`sec-fetch-dest: document`、`upgrade-insecure-requests: 1`、既有 HTML 导航 Accept 和六个高熵 CH。请求 MUST 不设置 Content-Type、Origin、Sec-Fetch-User、Auth RUM 或 Sentinel 凭据，MUST 不显式添加 Priority。系统 MUST 不仅因重定向前后目标域名相同而将来源改为 same-origin。

#### Scenario: authorize 与密码落页对应捕获 #18/#19
- **WHEN** 目标登录访问 signin 返回的 authorize URL 并按响应重定向至密码页
- **THEN** 首跳与落页均保持上述来源和导航字段，不声明用户触发
- **AND** 不新增导航请求或改写服务端 URL、OAuth state、PKCE、Cookie 传递规则

### Requirement: 目标 Auth JSON 场景保留端点差异

目标密码验证、MFA issue_challenge 和 MFA verify MUST 使用 Auth Origin、Auth 根 Referer、`same-origin/cors/empty` Fetch Metadata、`application/json` Content-Type、六个高熵 CH 和当前请求的 Auth 动态关联字段，MUST 不显式添加 Priority。系统 MUST 保留三个端点的 Accept 与安全凭据差异，不通过统一默认值抹平差异。

#### Scenario: 密码验证对应捕获 #58
- **WHEN** 目标登录提交密码验证请求
- **THEN** Accept 为 `application/json`，主 Sentinel 凭据属于当前设备、当前 challenge 和 `password_verify` flow
- **AND** 密码仍来自既有输入，body 结构与编码规则不变

#### Scenario: MFA issue 对应捕获 #84
- **WHEN** 服务端要求 TOTP，目标登录发出 MFA issue_challenge
- **THEN** Accept 为 `*/*`，请求不携带 `openai-sentinel-token` 或 `openai-sentinel-so-token`
- **AND** factor ID、type 和 force_fresh_challenge 保持既有来源与值

#### Scenario: MFA verify 对应捕获 #93
- **WHEN** 目标登录提交 TOTP 验证请求
- **THEN** Accept 为 `application/json`，主 Sentinel 凭据属于当前设备、当前 challenge 和 `verify_mfa` flow，不复用密码验证凭据
- **AND** factor ID 与 code 保持既有输入来源，不因头部重构改变 TOTP 生成或时钟行为

### Requirement: 目标 Sentinel challenge 使用独立 frame fetch 语义

目标登录的 Sentinel challenge 请求 MUST 使用 `Accept: */*`、`Content-Type: text/plain;charset=UTF-8`、Sentinel Origin、带当前 SDK 版本的 frame Referer 以及 `same-origin/cors/empty` Fetch Metadata。请求 MUST 不携带六个高熵 CH、Auth RUM、Auth 请求的 Sentinel 主/SO 凭据，也 MUST 不显式添加 Priority。即使 body 可解析为 JSON，Content-Type 也 MUST 不改成 application/json。

#### Scenario: 密码和 MFA challenge 对应捕获 #34/#87
- **WHEN** 目标登录分别为 password_verify 与 verify_mfa 获取 challenge
- **THEN** 两次请求均使用上述 frame fetch 语义，设备 ID 来自当前会话，flow 对应当前步骤，版本来自现有 SDK 配置
- **AND** body proof、SDK Cookie 输入、求解机制和 challenge 处理不因头部组合改变

### Requirement: Auth 动态关联字段遵守各自生命周期

每次 Auth fetch MUST 使用独立的 trace ID、span/parent ID 和 invocation ID；同一次请求的 Datadog 与 W3C trace 标识 MUST 保持一致关联。document-navigation ID MUST 在同一文档上下文的相关 fetch 中保持一致，并仅在既有文档轮换时机变化；头部组合 MUST 不新增轮换时机或复用完整动态头部模板。

#### Scenario: 同一文档内的密码和 MFA 请求
- **WHEN** 同一文档上下文依次发出密码验证、MFA issue 和 MFA verify
- **THEN** 三次请求的 trace/span/invocation 各自独立，document-navigation ID 保持当前文档值
- **AND** 各次 traceparent 与 Datadog trace/parent 保持相应数值关联，采样与 tracestate 保持既有语义

#### Scenario: authorize 响应与无响应异常
- **WHEN** authorize 导航已取得 HTTP 响应，或在取得响应前发生传输异常
- **THEN** 前者按既有时机轮换 document-navigation ID，后者不额外轮换，后续头部读取相应当前值

### Requirement: 安全凭据必须先校验再附加

头部组合 MUST 仅消费当前步骤已经校验的凭据，不执行 Sentinel challenge 请求或求解。系统 MUST 保留 flow、device、challenge token、必需 proof 与 SO 的归属及完整性校验；校验失败 MUST 阻止消费者请求发送。SO 头 MUST 仅在当前 challenge 明确要求且通过校验时附加，不得将 SO 元数据当作 token，也不得由本次捕获缺少 SO 头推导全局禁止规则。

#### Scenario: 主凭据属于其他步骤或设备
- **WHEN** 主凭据的 flow、device 或 challenge 归属与当前消费者不匹配
- **THEN** 消费者请求发送前明确失败，不移除凭据后继续发送，也不复用先前有效凭据

#### Scenario: SO 条件性要求
- **WHEN** 当前 challenge 分别为要求 SO 与不要求 SO
- **THEN** 前者只附加已校验的所需 SO token，后者不附加 SO 头；两者均保留当前有效主凭据

### Requirement: 传输和会话字段保持其原有生产者

系统 MUST 不将捕获的 Host、Content-Length、Connection、Accept-Encoding、Cookie 或动态标识固化为场景模板。Host 和长度 MUST 继续由目标 URL、最终 body 与现有传输机制决定；Cookie MUST 继续由同一会话按既有域、路径、响应和重定向规则管理。目标九条请求 MUST 不因设备或 CSRF 存在而新增 Authorization、oai-device-id、oai-language 或 x-csrf-token 请求头。

#### Scenario: body 长度或 Cookie 在响应后变化
- **WHEN** body 的最终字节数变化或同一会话收到更新 Cookie 的响应
- **THEN** 后续发送使用实际 body 对应长度和会话当前适用 Cookie，不复用捕获中的长度或 Cookie 快照

#### Scenario: 目标九条请求不额外声明身份字段
- **WHEN** 目标会话已经有设备 ID、CSRF、Cookie 与 OAuth 上下文
- **THEN** 九条对应请求仍按原位置传递这些状态，不自动新增 Authorization、oai-device-id、oai-language 或 x-csrf-token

### Requirement: 非目标场景保留既有行为与请求顺序

除上述目标登录头部变化外，系统 MUST 保持注册（含注册内已有账号密码验证）、设备 warmup、纯邮箱 OTP、账号导出/激活、Codex、ChatGPT bootstrap/commerce 及其他导航场景的既有头部语义。重构 MUST 不增加请求、不改变既有请求顺序、URL/query/body、Cookie/CSRF/OAuth/PKCE 来源、Sentinel 求解或传输指纹。

#### Scenario: 注册正式认证和 OTP 导航
- **WHEN** 注册发出正式 CSRF/signin，或在注册密码后进行 OTP 发送导航
- **THEN** 正式认证继续共享完整 Referer、禁缓存和端点内容类型，注册 OTP 导航继续使用 Auth 根 Referer、same-origin 与 Sec-Fetch-User ?1
- **AND** 不自动引入目标登录 profile 的高熵或 Priority 变化，首次 warmup 与既有 navigation ID 轮换规则不变

#### Scenario: 注册收尾与其他消费者
- **WHEN** 注册 OAuth 收尾或其他非目标消费者使用导航、NextAuth、Auth JSON、ChatGPT API 或基础画像头
- **THEN** 各自既有的显式高熵选择、Priority、来源上下文、用户触发、缓存、动态关联及业务头仍保持原语义
- **AND** 注册收尾仍使用既有 Auth 根 Referer、cross-site/document/navigate 且无 Sec-Fetch-User，最终 token 取得方式不变

### Requirement: 对齐结论区分构造证据和传输证据

验收与相关文档 MUST 区分捕获记录、源码显式构造、隔离本地实际发送及无法判断的浏览器/服务端行为。动态凭据、邮箱、密码、OTP、Cookie 和设备/session 标识 MUST 脱敏；不得从字段一致推断浏览器指纹等同或服务端接受，不得将捕获中的 MFA 403 标记为本次变更已解决。

#### Scenario: 本地发送观测与目标站结论分开
- **WHEN** 隔离接收端观察到目标场景头部与预期一致，包含受控重定向的首跳和落页
- **THEN** 报告仅声明对应本地传输已观察的结果，不宣称已证明目标站登录成功、完整浏览器行为或 TLS 指纹一致
- **AND** 使用合成敏感字段，不在日志和文档复述实际账号凭据
