# Proposal

## Why

当前 OAuth 回调与 ChatGPT `accessToken` 获取链路在已有账号登录、查活重登、2FA 慢路径重认证和注册收尾四处存在结构分裂与两类实际缺陷。其一，已有账号与 2FA 慢路径仍把 `auth.openai.com -> chatgpt.com` 交给底层客户端自动跟随跳转，跨站时不会重算 `Sec-Fetch-Site`（会把 `same-origin` 错带到 `chatgpt.com`），也缺少中途落入 `/workspace` 页面时的 `workspace/select` 兜底。其二，回调落地 `chatgpt.com/` 时始终完整下载并丢弃流式 HTML 正文，注册收尾把回调跟随与 `fetch_session` 绑在同一重试循环内（`/api/auth/session` 瞬断后会重放已消费的 OAuth `code`），且 `fetch_session` 的 `GET` 请求沿用了带 `Content-Type: application/json` 的请求头。

## What Changes

- **传输层支持惰性流式省流**：`WreqSession.request()` 支持 `stream=False` 可选参数；当 `stream=True` 且响应状态码小于 `400` 时跳过响应体读取（不调用 `raw.close()`，保留 HTTP/2 连接复用与 Cookie 记录）；状态码 `>= 400` 仍读取完整响应体以保留错误诊断上下文。
- **回调链统一为手动逐跳**：`follow_oauth_callback` 对新号、`existing` 和查活链统一关闭自动重定向并逐跳重建导航头，跨站切入 `chatgpt.com` 时更新 `Sec-Fetch-Site` 为 `cross-site`；`user_initiated` 仅控制第 1 跳是否发送 `Sec-Fetch-User: ?1`，302 后续跳不带 `Sec-Fetch-User`。
- **跳过 `chatgpt.com/` 首页 HTML 正文**：对非 `/workspace`、`/consent`、`/sign-in-with-chatgpt/` 的 `chatgpt.com` 回调落地页启用 `stream=True`，避免下载约 88–142 KB 压缩流式 SSR HTML 正文。
- **补齐 `workspace/select` 兜底（4a 方案 B）**：当认证终步（如 `create_account`）缺失 `continue_url`、`continue_url` 指向 `/workspace`，或回调重定向链中途落到 `/workspace`（返回 `200`）时，优先从 `oai-client-auth-session` Cookie、次选 query 或 `/workspace` HTML 中提取 `workspace_id`，调用 `POST https://auth.openai.com/api/accounts/workspace/select` 取得下一跳地址继续完成回调。缺失 `continue_url` 且无可用 `workspace_id` 时，以及 `existing` 缺 `continue_url` 时（4b）保持现状直接失败，不新增去 `prompt` 重发 `/oauth/authorize` 兜底。
- **2FA 慢路径回调收敛**：`setup_2fa` 重认证阶段的 `_exchange_new_token` 改用统一的 `follow_oauth_callback`，同时要求回调状态码 `>= 400` 时立刻报错中断，防止后续 `fetch_session` 误读残留旧 `session-token`。
- **拆分 AT 收尾重试粒度并清理 `GET /api/auth/session` 头**：`_finalize_registration_session` 将“跟随回调”与“拉取 `/api/auth/session`”拆为两段独立重试循环，回调一旦成功不再重放 `continue_url`；`fetch_session` 的 `GET` 请求移除冗余 `Content-Type: application/json`（保持 `get_nextauth_headers()` 原有默认合同不变）。
- **保持现状不改项**：不拆分“跟重定向到 callback”与“消费 callback”的时机；不增加 OSS 的 `client_auth_session_dump` 与收尾第二次 `GET /api/auth/session`。

## Capabilities

### New Capabilities

- `openai-oauth-callback-session`: OAuth 回调逐跳导航与跨站头重建、`/workspace` 选区兜底、首页正文跳过策略、2FA 重认证回调对齐，以及 `accessToken` 拉取与分段重试契约。

### Modified Capabilities

- `transport-handshake-retry`: 显式传入 `stream=True` 且响应状态码小于 `400` 时，传输层跳过响应体读取与响应体阶段截断校验；默认（非流式）请求与错误响应的现有重试及异常映射规则保持不变。

## Impact

- `core/infra/wreq_session.py`：`WreqSession.request()` 增加 `stream` 参数支持并更新未知参数校验。
- `core/openai/auth/openai_auth.py`、`core/openai/account/account_export.py`：`get_auth_step` / `follow_oauth_callback` / `_exchange_new_token` / `fetch_session` 实现统一逐跳、`/workspace` 提取与 `workspace/select` 兜底、按路径启用 `stream=True`、可选状态码报错及去掉 `fetch_session` 冗余 `Content-Type`。
- `core/openai/register/registration_flow.py`：`_finalize_registration_session` 拆分回调与 `session` 两阶段重试，并在 `create_account` 之后衔接 `/workspace` 路径与缺失 `continue_url` 的 `workspace/select` 兜底。
- 已有调用与兼容：`core/openai/account/account_liveness.py` 的 `_follow_continue_and_fetch` 自动继承统一逐跳、`/workspace` 兜底和首页省流；登录预热的 `chatgpt.com/auth/login`（需 `observe_chatgpt_document` 提取前端版本元数据）保持整包读取不受影响。
- 测试：`tests/test_wreq_session.py`、`tests/test_account_export_twofa.py`、`tests/test_request_header_consumers.py`、`tests/test_registration_pipeline.py`、`tests/test_openai_auth_registration_flow.py`、`tests/test_account_liveness.py` 需核对并补充对应单测。
