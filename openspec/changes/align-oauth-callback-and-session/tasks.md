# Tasks

## 1. 传输层：支持 `stream=True` 跳过成功响应正文读取

- [ ] 1.1 在 `core/infra/wreq_session.py` 的 `WreqSession.request()` 中消费 `stream = bool(kwargs.pop("stream", False))`（不传给 `self._client.request()`，保留其余未知参数抛 `TypeError` 的校验）；在 `with self._client.request(...) as raw:` 内完成重定向历史、Cookie、状态码、URL 与响应头构造后，仅当 `stream and response.status_code < 400` 时跳过 `bytes(raw.bytes())` 并设置 `response._content = b""`、`response._content_consumed = True`，自然退出 `with` 块而不调用 `raw.close()`；在 `tests/test_wreq_session.py` 新增用例覆盖：200 流式分块跳过正文时返回空正文且完整保留 Cookie、HTTP/2 经代理连接仍复用（`conns == 1`）且不触发截断错误、`stream=True` 且返回 403/500 时仍读取完整错误体、响应头前链路瞬断仍正常重试；运行 `.venv/Scripts/python.exe -m pytest tests/test_wreq_session.py -q` 通过

## 2. 认证辅助：工作区 `workspace_id` 解析与 `/workspace` 选区请求

- [ ] 2.1 在 `core/openai/auth/openai_auth.py` 扩展 `_step_from_continue_url`（`_AUTH_HOSTS` 下路径包含 `/workspace` 时返回 `"callback"`，冲突检查与非法域拦截不变），并新增共享辅助方法：`extract_workspace_id(session, *, url="", html_text="")`（依次从 `oai-client-auth-session` Cookie 前两段 base64url JSON 的 `workspace_id`/`workspaces[0].id`、URL query 的 `workspace_id`/`id`、HTML 正文正则提取）与 `select_workspace_continue_url(session, workspace_id, *, referer="https://auth.openai.com/", include_high_entropy=False)`（使用 `session.get_auth_headers(...)` 向 `https://auth.openai.com/api/accounts/workspace/select` 发送 `{"workspace_id": workspace_id}`，解析 `Location` 或 JSON `continue_url`/`redirect_url`/`url`/`next`/`location` 并相对 `https://auth.openai.com` 补全）；评估并复用 `core/openai/codex/codex_oauth.py` 中的 `oai-client-auth-session` 解码逻辑避免重复；在 `tests/test_openai_auth_registration_flow.py` 新增单测验证 `get_auth_step` 对 `/workspace` 与非法域的判定、Cookie/Query/HTML 三级提取及 `workspace/select` 请求头与跳转解析，运行 `.venv/Scripts/python.exe -m pytest tests/test_openai_auth_registration_flow.py -q` 通过

## 3. 回调主链与 2FA 慢路径：统一逐跳、首页省流、`/workspace` 200 拦截

- [ ] 3.1 重写 `core/openai/account/account_export.py` 的 `follow_oauth_callback()`：移除 `allow_redirects=user_initiated` 差异，全部路径统一 `allow_redirects=False` 手动逐跳（保留 30 次跳转 / 31 次上限）；仅首跳（`redirect_count == 0`）使用传入的 `user_initiated`，3xx 后续跳固定 `user_initiated=False`；按当前目标 URL 前缀切换 `get_chatgpt_navigate_headers` / `get_auth_navigate_headers`，仅在目标主机为 `chatgpt.com` 且路径不含 `/workspace`、`/consent`、`/sign-in-with-chatgpt/` 时传 `stream=True`；当落到含 `/workspace` 且状态码为 200 的页面时提取 `workspace_id` 调用 `workspace/select` 并以返回地址继续循环；增加 `raise_for_status: bool = False`（启用且 `status_code >= 400` 时优先调用 `resp.raise_for_status()` 否则抛 `RuntimeError`）；同步更新 `_exchange_new_token()` 改调 `follow_oauth_callback(session, continue_url, referer="https://auth.openai.com/email-verification", user_initiated=True, raise_for_status=True)`
- [ ] 3.2 在 `tests/test_account_export_twofa.py`、`tests/test_request_header_consumers.py` 与 `tests/test_account_liveness.py` 更新并补充单测：验证 `existing` / 查活 `user_initiated=True` 经 `auth.openai.com -> chatgpt.com` 多跳时始终 `allow_redirects=False`、第 1 跳带 `?1`、后续跳清零 `sec-fetch-user` 且切为 `cross-site`；验证 `chatgpt.com/` 跳传 `stream=True` 而 `auth.openai.com` 与 `/workspace` 不传 `stream=True`；验证 `/workspace` 200 页提取 `workspace_id` 调用 `workspace/select` 后完成跳转；验证 `_exchange_new_token` 跨站头正确且回调 403 仍中断不调用 `fetch_session`；运行 `.venv/Scripts/python.exe -m pytest tests/test_account_export_twofa.py tests/test_request_header_consumers.py tests/test_account_liveness.py -q` 通过

## 4. `accessToken` 获取优化：去掉 GET `Content-Type` 与拆解收尾重试

- [ ] 4.1 在 `core/openai/account/account_export.py` 的 `fetch_session()` 中，调用 `session.get_nextauth_headers(referer="https://chatgpt.com/", include_high_entropy=include_high_entropy)` 后执行 `headers.pop("content-type", None)`（不改动 `BrowserSession.get_nextauth_headers` 默认行为），确保 `GET /api/auth/session` 不带 `Content-Type`；验证 `tests/test_session_navigation_headers.py` 与 `tests/test_request_header_consumers.py` 全部通过并补齐 `fetch_session` 请求头无 `content-type` 断言
- [ ] 4.2 更新 `core/openai/register/registration_flow.py`：在 `create_account(...)` 返回后若缺少 `continue_url`（4a 方案 B），先尝试从 Cookie 提取 `workspace_id` 并调用 `workspace/select` 补齐 `continue_url`，仍缺失或无 `workspace_id` 时按原边界失败（`existing` 不补 `_reauthorize_for_session`）；将 `_finalize_registration_session()` 拆为「`follow_oauth_callback`（`raise_for_status=True`）有限重试 → `human_delay("post_auth")` → `fetch_session` 有限重试」两段循环，两段均对 `StopRequested`、空 `continue_url` 与 `TooManyRedirects` 立即抛出，重试可恢复异常前调用 `reset_retryable_circuit(session)`，回调成功后绝不因 `fetch_session` 失败重放 `continue_url`，耗尽异常语义与既有一致；在 `tests/test_registration_pipeline.py` 补充单元测试覆盖上述分阶段重试、停止信号穿透与 `create_account` 缺失 `continue_url` 的 `workspace/select` 兜底，运行 `.venv/Scripts/python.exe -m pytest tests/test_registration_pipeline.py -q` 通过

## 5. 全量验证与文档同步

- [ ] 5.1 同步更新 `doc/registration-2fa-oss-comparison.md` 与 `doc/registration-2fa-har-comparison.md` 中关于“`existing` 自动重定向”及“缺少 `/workspace` 200 兜底”的旧描述，确保文档与代码事实一致
- [ ] 5.2 运行全量关联测试套件 `.venv/Scripts/python.exe -m pytest tests/test_wreq_session.py tests/test_session_navigation_headers.py tests/test_request_header_consumers.py tests/test_openai_auth_registration_flow.py tests/test_account_export_twofa.py tests/test_registration_pipeline.py tests/test_account_liveness.py tests/test_codex_oauth.py -q`（若有遗漏测试文件则一并跑通 `pytest -q`），确认全部通过且无残留临时改动

## Workflow follow-up

- 待全部实现任务完成并验收通过后，可执行 `/openspec-archive-change` 将本变更归档至主规范并校验归档结果。
