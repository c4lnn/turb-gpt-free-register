# Spec Delta

## Purpose

规范 OAuth 回调重定向链的手动逐跳导航、跨站请求头重建、工作区选择（`/workspace`）兜底、首页 HTML 正文跳过策略、2FA 重认证回调收口以及 `accessToken` 拉取的分阶段重试契约。

## ADDED Requirements

### Requirement: OAuth 回调统一采用逐跳手动重定向
新号注册、已有账号登录、查活重登及 2FA 重认证的 OAuth 回调流程 SHALL 关闭底层自动重定向，统一按 301/302/303/307/308 状态码和 `Location` 头手动逐跳跟随，上限为 30 次重定向。每一跳 MUST 按当前目标站重建导航请求头与 `Sec-Fetch-Site`（面向同源目标为 `same-origin`，跨站切入另一域为 `cross-site`）；仅首跳根据调用入口决定是否携带 `Sec-Fetch-User: ?1`，由 3xx 重定向触发的后续跳 MUST NOT 携带 `Sec-Fetch-User`。

#### Scenario: 新号回调经 auth 中转跨站落到 chatgpt.com 首页
- **WHEN** 新号以非用户主动点击语义从 `https://auth.openai.com/authorize/continue` 开始跟随，先后收到 302 指向 `https://chatgpt.com/api/auth/callback/openai` 与 `https://chatgpt.com/` 并最终返回 HTTP 200
- **THEN** 系统使用 `allow_redirects=False` 手动发出全部 3 跳请求，每跳都不带 `Sec-Fetch-User`
- **AND** 第 1 跳 `Sec-Fetch-Site` 为 `same-origin`，第 2、3 跳跨站进入 `chatgpt.com` 后 `Sec-Fetch-Site` 更新为 `cross-site`

#### Scenario: 已有账号与查活回调首跳带激活语义且后续跳转清零并修正跨站头
- **WHEN** 已有账号或查活流程以用户主动发起语义从 `https://auth.openai.com/authorize/continue`（同源 Referer）出发，经 302 跨站跳转到 `https://chatgpt.com/api/auth/callback/openai` 再落到 `https://chatgpt.com/`
- **THEN** 系统关闭自动重定向并手动跟随全部 3 跳
- **AND** 第 1 跳携带 `Sec-Fetch-User: ?1` 且 `Sec-Fetch-Site` 为 `same-origin`，第 2、3 跳去掉 `Sec-Fetch-User` 且 `Sec-Fetch-Site` 更新为 `cross-site`

#### Scenario: 回调重定向超过 30 次上限
- **WHEN** 回调链连续返回超过 30 次 3xx 重定向与有效 `Location`
- **THEN** 系统中止跟随并抛出重定向超限错误

### Requirement: OAuth 回调跳过常规 ChatGPT 落地页正文且不影响元信息页面
在逐跳跟随 OAuth 回调时，针对指向 `chatgpt.com` 且路径不属于 `/workspace`、`/consent`、`/sign-in-with-chatgpt/` 的跳转，系统 SHALL 启用流式跳过正文模式，在收到响应状态码小于 400 的响应头及 Cookie 后放弃读取剩余响应正文；对于状态码 `>= 400` 的响应、上述需解析页面以及登录预热的 `https://chatgpt.com/auth/login`，系统 MUST 保留完整响应正文。

#### Scenario: 回调落地 chatgpt.com 根页跳过 HTML 正文并建立会话 Cookie
- **WHEN** OAuth 回调最终跳到 `https://chatgpt.com/` 并返回 HTTP 200、`Set-Cookie` 与流式 HTML 正文
- **THEN** 系统在记录响应头与全部会话 Cookie 后跳过剩余正文读取，并返回最终落点 URL
- **AND** 若底层复用 HTTP/2 连接，后续 `GET /api/auth/session` 可在同一连接上携带已种下的 Cookie 正常请求

#### Scenario: 回调命中错误页时保留错误响应正文
- **WHEN** 回调请求（含流式跳）收到 HTTP 403 或 5xx 响应
- **THEN** 系统读取完整响应正文用于异常诊断

#### Scenario: 登录预热首页文档不受省流跳过影响
- **WHEN** 注册或查活前的登录预热请求 `https://chatgpt.com/auth/login`
- **THEN** 系统不启用流式跳过，仍完整读取响应正文以提取前端构建版本号等页面元数据

### Requirement: 认证终步与回调中的工作区选择兜底
当账号创建或后续认证终步未返回 `continue_url`、返回的 `continue_url` 包含 `/workspace`，或回调逐跳过程中落到路径包含 `/workspace` 的 HTTP 200 页面时，系统 SHALL 按“先从 `oai-client-auth-session` Cookie 解码，其次从 URL 查询参数，再次从 `/workspace` 200 页面 HTML”的顺序提取 `workspace_id`，向 `https://auth.openai.com/api/accounts/workspace/select` 发送 JSON POST 请求获取下一跳地址后继续完成回调。若仍无法得到可用下一跳地址，系统 MUST 显式失败；当已有账号分支缺少 `continue_url` 时，系统 MUST NOT 去掉 `prompt=login` 重发 `/oauth/authorize` 进行静默重授权。

#### Scenario: 账号创建响应缺失 continue_url 但 Cookie 携带 workspace_id
- **WHEN** `create_account` 返回成功但响应缺少 `continue_url`，且会话 Cookie `oai-client-auth-session` 中已带有 `workspace_id` 或 `workspaces[0].id`
- **THEN** 系统向 `https://auth.openai.com/api/accounts/workspace/select` 提交 `{"workspace_id": "<id>"}` 并使用响应返回的下一跳 URL 继续完成 OAuth 回调

#### Scenario: 回调重定向中途落入 /workspace 200 页面
- **WHEN** 回调逐跳过程中某跳请求路径包含 `/workspace` 且服务端返回 HTTP 200 HTML（Cookie 缺失或存在工作区信息）
- **THEN** 系统读取该 `/workspace` 页面的完整 HTML 正文，提取 `workspace_id` 后调用 `POST /api/accounts/workspace/select` 取得下一跳地址并继续逐跳直到完成回调

#### Scenario: 无法提取 workspace_id 或已有账号缺少 continue_url 时直接失败
- **WHEN** 认证终步或 `/workspace` 页面无法取得可用 `continue_url` 且无法提取有效 `workspace_id`（或 `workspace/select` 未返回下一跳）
- **THEN** 系统显式抛出错误中止流程，且不对已有账号重发静默 `/oauth/authorize`

### Requirement: 2FA 慢路径重认证复用统一回调并在错误状态码下阻断旧态读取
2FA 慢路径重认证在完成 OTP 验证后交换新 token 时，SHALL 复用统一的 OAuth 回调逐跳链路（包含跨站导航头重建、`/workspace` 兜底与首页省流），且 MUST 在回调链最终返回 HTTP 400 及以上状态码时立即报错中止，不得继续执行 `/api/auth/session`。

#### Scenario: 2FA 重认证回调返回 403 时不拉取旧 session
- **WHEN** 2FA 重认证的回调请求返回 HTTP 403
- **THEN** 系统立即抛出 HTTP 错误，MUST NOT 接着调用 `GET /api/auth/session` 读取重认证前的旧会话 Cookie

#### Scenario: 2FA 重认证经跨站或 /workspace 回调成功后刷新 AT
- **WHEN** 2FA 重认证从 `auth.openai.com` 回调路径跨站跳转（或经 `/workspace` 选区）最终以 HTTP 200 落到 `chatgpt.com`
- **THEN** 每跳导航头符合目标站 `Sec-Fetch-Site` 约定，且随后 `GET /api/auth/session` 返回新的 `accessToken`

### Requirement: 注册收尾回调与 Session 拉取独立重试且 GET 不带 Content-Type
注册收尾建立登录态时，系统 SHALL 将“跟随 OAuth 回调”与“拉取 `GET /api/auth/session`”拆分为两个互不重叠的有界重试阶段：回调阶段成功完成后，后续 `/api/auth/session` 重试 MUST NOT 重放原始 `continue_url` 或消费同一 OAuth 回调地址。`GET /api/auth/session` SHALL 仅在成功后执行一次（不附加 `client_auth_session_dump` 或收尾第二次 `/api/auth/session`），且请求头 MUST NOT 携带 `Content-Type`（同时不改变共享 NextAuth 请求头构造函数的默认 `Content-Type` 返回值）。

#### Scenario: 回调成功但 session 临时失败时只重试 session
- **WHEN** 注册收尾的回调跟随已成功返回，但随后的 `GET /api/auth/session` 因临时 502、网络抖动或未及时出现 `accessToken` 而失败
- **THEN** 系统在退避等待后仅重新调用 `GET /api/auth/session`，不再重新请求 `continue_url`

#### Scenario: 回调与 session 遇停止信号立即中止
- **WHEN** 注册收尾任一阶段抛出任务停止信号
- **THEN** 系统不进入重试退避，立即向上抛出停止信号

#### Scenario: GET /api/auth/session 请求头不含 Content-Type 且单次成功即止
- **WHEN** 系统发起 `GET /api/auth/session` 获取 `accessToken`
- **THEN** 发出的请求头不包含 `Content-Type` 字段，而调用方直接查询通用 NextAuth 头构造器的默认返回值仍不受影响
- **AND** 获取到非空 `accessToken` 后不再发起 `client_auth_session_dump` 或第二次 `GET /api/auth/session`
