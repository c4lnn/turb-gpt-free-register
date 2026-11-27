# Design

## Context

动机见 [proposal.md](proposal.md)。本节仅列实现所需的现有约束与本地实测结论。

**本地现状**

- `WreqSession.request()`（`core/infra/wreq_session.py:151-210`）：构造 `options` 传给 `wreq.blocking.Client.request()`，若有未消费的 `kwargs` 会抛 `TypeError("wreq 暂不支持请求参数: ...")`；收到响应后，先记录 `raw.history` 与 `raw.headers` 的 Cookie，再无条件调用 `bytes(raw.bytes())` 读完响应体。原生 `wreq.blocking.Response` 的响应体原本就是惰性的，且 `wreq` 不支持传给 `Client.request()` 的 `stream` 关键字参数。
- `follow_oauth_callback()`（`core/openai/account/account_export.py:193-236`）：循环最多 31 次；新号（`user_initiated=False`）手动逐跳 `allow_redirects=False`，按每跳域名前缀切 `get_chatgpt_navigate_headers` / `get_auth_navigate_headers`；`existing`（`registration_flow.py:561`）与查活（`account_liveness.py:263`）传 `user_initiated=True`，等价于 `allow_redirects=True`。所有跳转均整包读取响应体；不调用 `raise_for_status()`；无 `/workspace` 识别或 `workspace/select` 逻辑。
- `_exchange_new_token()`（`core/openai/account/account_export.py:583-598`）：在 `setup_2fa` 慢路径重认证后调用，直接用 `get_auth_navigate_headers(referer="https://auth.openai.com/email-verification")` + `allow_redirects=True` 请求 `continue_url`，调 `callback.raise_for_status()` 后再调用 `fetch_session(session)`。单测 `test_callback_failure_does_not_fetch_or_use_stale_session_token`（`tests/test_account_export_twofa.py:385-391`）断言回调 403 时不得调用 `fetch_session`。
- `get_auth_step()`（`core/openai/auth/openai_auth.py:777-824`）：`_step_from_continue_url` 对 `chatgpt.com` 或 `/authorize/continue` 返回 `"callback"`，尚未识别 `/workspace` 路径。
- `_finalize_registration_session()`（`core/openai/register/registration_flow.py:68-120`）：单层 `for attempt in range(1, 6)` 同时包裹 `follow_oauth_callback`、`human_delay("post_auth")`、`fetch_session` 和 `accessToken` 判空；后半段失败会整段重试、重放 `continue_url`。
- `fetch_session()`（`core/openai/account/account_export.py:239-285`）：调用 `session.get_nextauth_headers(referer="https://chatgpt.com/", include_high_entropy=include_high_entropy)`，`get_nextauth_headers()`（`session.py:765-773`）默认附带 `"content-type": "application/json"`，且 `tests/test_session_navigation_headers.py:322-324` 断言 `get_nextauth_headers()` 与 `get_nextauth_csrf_headers()` 返回值相等且含该字段。

**本地实测事实（作为决策 D1 / D2 的依据）**

1. **首页流式正文跳过与连接复用（`wreq` Chrome152 + 本地 TLS `CONNECT` 代理实测）**：
   - 原生抓包中 `GET https://chatgpt.com/` 使用 `br` 压缩，线上传输仅 `87,756 B`（`1.saz #226`，接收耗时 `~1.17s`）与 `140,521 B` / `141,687 B`（两份 HAR，接收耗时 `2.14s` / `3.61s`），属于流式 SSR 分片下发，而非 700 KB 整包直冲。
   - 对 HTTP/2 TLS 代理隧道仿真（`80ms RTT`，`141,687 B`）：不调用 `raw.bytes()` 直接退出 `with` 块（`exit_unread`）会触发 `RST_STREAM(CANCEL)` 并**保持连接复用（`conns=1`）**，在流式下发（1s 窗口）下代理入站从 `145,444 B` 降到 `19,723 B`（省 86.4%）；而额外调用 `raw.close()` 会废弃连接（`conns=2`），紧接着的 `/api/auth/session` 被迫重建 TLS 与 HPACK 表，在极端 `1 RTT` 首包冲完场景反而比整包读体多耗 `+1,683 B`。
   - 无论 `allow_redirects=False` 还是 `allow_redirects=True`，跳过 `raw.bytes()` 均不影响 `raw.history`、`raw.headers` 与 `WreqCookies` 对 `Set-Cookie` 的捕获，后续 `/api/auth/session` 正常携带全部 Cookie。
2. **自动重定向的跨站头实测（`BrowserSession` 双主机 `127.0.0.1` -> `localhost`）**：
   - `allow_redirects=True` 从 `auth` 跳往另一主机 `/cb` 再落到 `/` 时，`wreq` 把第 1 跳的 `sec-fetch-site: same-origin` 原样带给了跨站的第 2、3 跳，同时把 `sec-fetch-user: ?1` 保留到所有 302 跟随跳。

## Goals / Non-Goals

**Goals:**

- 用显式 `stream=True`（跳过 `< 400` 读体而不调用 `raw.close()`）为 `chatgpt.com/` 回调落地页节省代理流量，同时保持 `WreqSession` 默认行为、错误响应诊断体与 HTTP/2 连接复用不变。
- 将 `follow_oauth_callback`（含注册新号、`existing`、查活与 2FA 慢路径）统一到同一套手动逐跳与 `/workspace` 选区兜底逻辑上，修复跨站 `Sec-Fetch-Site` 与 302 `Sec-Fetch-User` 语义。
- 在注册收尾阶段隔离回调消费与 `/api/auth/session` 拉取的失败重试，并去掉 `GET /api/auth/session` 上多余的 `Content-Type` 头。

**Non-Goals:**

- 不拆分“捕获 callback URL”与“消费 callback”（保持一次跟随到落地页的现行结构）。
- 不新增 OSS 的 `client_auth_session_dump` 与收尾第二次 `GET /api/auth/session`。
- 不实现 4b（`existing` 缺失 `continue_url` 时去掉 `prompt=login` 重发 `/oauth/authorize` 的 `_reauthorize_for_session`）。
- 不跳过登录预热 `https://chatgpt.com/auth/login`（该页需供 `observe_chatgpt_document` 读取前端 build 元数据）；也不改变 `get_nextauth_headers()` 的默认返回值。

## Decisions

### D1: `WreqSession.request()` 仅跳过 `raw.bytes()`，不透传 `stream` 也不调用 `raw.close()`

**决定**：在 `WreqSession.request()` 的参数提取段 `stream = bool(kwargs.pop("stream", False))`；不把 `stream` 写入 `options`。在 `with self._client.request(...) as raw:` 块中记录重定向链、Cookie、状态码、URL、响应头之后，若 `stream and response.status_code < 400`，直接跳过 `raw.bytes()` 并设 `response._content = b""`、`response._content_consumed = True`，正常退出 `with` 块。

**理由**：`wreq.blocking.Client.request()` 本身不接收 `stream`，且响应体原本就惰性读取；自然退出 `with` 块在 HTTP/2 下发 `RST_STREAM(CANCEL)` 且允许同一连接继续服务后续 `GET /api/auth/session`，避免了 `raw.close()` 强制断开连接导致的额外 TLS 握手损耗。状态码 `>= 400` 仍读全正文，避免吞掉 403/5xx 错误响应体；未消费的未知 `kwargs` 仍按原逻辑抛 `TypeError`。

**备选**：*模仿 OSS 在调用处调 `_r.close()`*：放弃，本地代理实测证明它破坏 HTTP/2 复用并倒赔握手流量；*给 `Response` 暴露生成器流读取*：放弃，当前所有调用方只区分“需要正文”与“只要头和 Cookie”，无需引入额外生命周期复杂度。

### D2: `follow_oauth_callback` 统一 `allow_redirects=False` 手动逐跳，解耦 `user_initiated` 与跳转模式

**决定**：移除 `allow_redirects=user_initiated` 分支，始终传 `allow_redirects=False` 逐跳跟随（上限 30 次跳转 / 31 次请求）。首跳（`redirect_count == 0`）沿用入参 `user_initiated`；所有由 3xx `Location` 驱动的后续跳（`redirect_count > 0`）固定传 `user_initiated=False`。每跳按 `current_url` 前缀选择 `get_chatgpt_navigate_headers` 或 `get_auth_navigate_headers`，从而依据 `referer` 自动重算 `Sec-Fetch-Site`（跨入 `chatgpt.com` 切为 `cross-site`）。同时保持入参 `referer`（如新号传 `"https://auth.openai.com/"`、`existing` 传 `"https://auth.openai.com/email-verification"` 等）按既有测试契约传递，保留 `include_high_entropy` 透传。

**理由**：既彻底消除了 `wreq` 自动重定向把 `same-origin` 错带到 `chatgpt.com` 的问题，又让每一跳发请求前都明确知道目标路径（供 D3 决定是否 `stream=True`、供 D4 拦截 `/workspace` `200` 页面），且不破坏现有单测对第 1 跳 `sec-fetch-user` 与多跳 `referer` 的断言。

**备选**：*仅在 `existing` 保留 `allow_redirects=True`、对它再补额外重拉分支*：放弃，跨站 `Sec-Fetch-Site` 无法在自动跳转中修复，且会在 `/workspace` 200 页上额外多发一次重复请求。

### D3: 回调中仅对非解析目标的 `chatgpt.com` 页面跳启用 `stream=True`

**决定**：在 `follow_oauth_callback` 内判断当前跳 `current_url`：当且仅当主机为 `chatgpt.com` 且路径不含 `/workspace`、`/consent`、`/sign-in-with-chatgpt/` 时传 `stream=True`；其余跳（包括 `auth.openai.com` 页面及 `/workspace` 等可能需要读 HTML 的页面）保持 `stream=False`。

**理由**：3xx 重定向响应体本身为空，`/api/auth/callback/openai` 通常直接 302 到 `https://chatgpt.com/`；对目标站常规跳转启用 `stream=True` 可省下 `chatgpt.com/` 流式 SSR HTML，同时在 D4 触发 `/workspace` `200` 页时确保 `resp.text` 完整可读。若遇到 `chatgpt.com` 返回 403/5xx，由 D1 保证 `>= 400` 依然读出完整错误正文。

### D4: 工作区选择（`/workspace`）三点兜底方案（4a 方案 B）

**决定**：

1. **提取与提交复用**：在认证侧新增或抽离共享辅助函数（避免与 `codex_oauth.py` 各自复制 JWT Cookie 解码细节，可考虑让 `codex_oauth._decode_jwt_segment` / Cookie 解析共复用或提供独立辅助）：
   - Cookie 提取：遍历/读取 `oai-client-auth-session`，对前 2 段 base64url JSON 解码，依次查找 `workspace_id` 或 `workspaces[0].id`；
   - Query 提取：尝试读取 URL query 参数 `workspace_id` / `id`；
   - HTML 提取：对 `/workspace` HTTP `200` 页正文做正则匹配（`workspaces` 内 `id`、`"workspace_id"`、`"workspaceId"`）。
   - 选区提交：调用 `POST https://auth.openai.com/api/accounts/workspace/select`，请求头使用 `session.get_auth_headers(referer=referer, include_high_entropy=include_high_entropy)`，载荷 `{"workspace_id": workspace_id}`，`allow_redirects=False`；状态码 `>= 400` 时报错，下一跳优先取 `Location` 头，否则取响应 JSON 的 `continue_url` / `redirect_url` / `url` / `next` / `location`，相对路径相对 `https://auth.openai.com` 补全。
2. **三个接入点**：
   - `openai_auth.py` 的 `_step_from_continue_url` 识别 `_AUTH_HOSTS` 下路径含 `/workspace` 的地址为 `"callback"`；
   - `registration_flow.py` 在 `create_account(...)` 返回后若缺少 `continue_url`，先尝试用 Cookie 提取 `workspace_id` 并调用 `workspace/select` 补齐 `continue_url` 再进入回调；
   - `follow_oauth_callback` 在进入含 `/workspace` 的 `current_url` 返回 `200` 时（或启动即含 `/workspace`），按 `Cookie -> query -> HTML` 顺序取 `workspace_id` 调 `workspace/select`，拿到 `next_url` 后继续循环（不提前终止，共用 30 跳上限，防止无限循环）。
3. **4b 边界**：不实现 `_reauthorize_for_session`。`existing` 或认证终步无 `continue_url` 且无法通过 Cookie/workspace 取得地址时直接报错。

**备选**：*只在 `follow_oauth_callback` 里拦截 `/workspace` 200*：不够，若 `create_account` 直接缺 `continue_url` 或返回 `/workspace` 入口链接，会在进入 `follow_oauth_callback` 前就在 `get_auth_step` 报错退出。

### D5: 2FA 慢路径 `_exchange_new_token` 收敛至 `follow_oauth_callback(..., raise_for_status=True)`

**决定**：为 `follow_oauth_callback` 增加关键字参数 `raise_for_status: bool = False`。当 `raise_for_status=True` 且最终（或遇不可跟随的）响应 `status_code >= 400` 时，若响应对象具有可调用的 `raise_for_status()` 则调用它，否则抛出 `RuntimeError(f"OAuth callback 失败: HTTP {status_code}")`。`_exchange_new_token(session, continue_url)` 改调 `follow_oauth_callback(session, continue_url, referer="https://auth.openai.com/email-verification", user_initiated=True, raise_for_status=True)`，随后再调 `fetch_session(session)`。

**理由**：默认 `raise_for_status=False` 保持既有无 `raise_for_status` 方法的 `SimpleNamespace` 测试桩兼容性；`_exchange_new_token` 开启 `True` 后既修掉了跨站 `Sec-Fetch-Site: same-origin`、补上了 `/workspace` 兜底与首页省流，又严格守住“回调 403 不得继续读取旧 `session-token`”的单测红线。

### D6: `_finalize_registration_session` 拆分为回调与 `fetch_session` 两阶段独立重试

**决定**：参照 `account_liveness._follow_continue_and_fetch` 将 `_finalize_registration_session` 拆为两段：

1. **阶段 1（`follow_oauth_callback`）**：最多尝试 `_FINALIZE_SESSION_MAX_ATTEMPTS`（5）次，指数退避 `_FINALIZE_SESSION_BACKOFF_BASE ** (attempt - 1)`。遇到 `StopRequested`、空 `continue_url` 等明确输入错误或 `TooManyRedirects` 时立即抛出不重试；可重试失败（如传输超时/连接中断或回调状态异常）在重置可重试熔断后仅重试 `follow_oauth_callback`。此阶段考虑同样开启 `raise_for_status=True`（或保持原策略但建议在回调返回 403/5xx 时当场重置熔断并重试回调，避免带着未落地 Cookie 空跑一次 `fetch_session`——但注意需同步确认现有单测桩：现有 `_finalize_registration_session` 单测里的 `SimpleNamespace` 无 `raise_for_status`，采用与 D5 相同的安全判断 `getattr(resp, "raise_for_status", None)` 即可完全兼容 `status_code=200` 桩并在真实 403/5xx 时准确重试阶段 1）。
2. **过渡延迟**：`human_delay("post_auth")` 仅在阶段 1 成功进入阶段 2 前执行一次。
3. **阶段 2（`fetch_session`）**：最多尝试 `_FINALIZE_SESSION_MAX_ATTEMPTS`（5）次；遇到 `StopRequested` 立即抛出，遇到网络异常或 `accessToken` 暂空时调用 `reset_retryable_circuit(session)` 并按同样指数退避仅重试 `fetch_session`，**不再重放 `follow_oauth_callback` / `continue_url`**。
4. **外层异常边界**：重试耗尽后仍以 `RuntimeError(f"OAuth 回调/拉取 Token 重试耗尽：{email}，...") from last_exc` 抛出，保持账号创建之后阶段在 `classify_failure` 中的既有分类边界不变。

### D7: `fetch_session()` 局部移除 `Content-Type`，不动共享 `get_nextauth_headers()`

**决定**：在 `account_export.fetch_session()` 内拿到 `headers = session.get_nextauth_headers(...)` 后执行 `headers.pop("content-type", None)`。

**理由**：`GET /api/auth/session` 无请求体，不应带 `Content-Type: application/json`；同时 `tests/test_session_navigation_headers.py:322-324` 与其他复用 `get_nextauth_headers()` 的场景显式断言其返回包含 `"content-type": "application/json"`。在 `fetch_session` 消费点局部移除，既消除了 GET 报文的协议偏差，又不影响共享头构造函数的契约与全仓其余调用点。

## Risks / Trade-offs

- **流式跳过在真实 CDN 首包即发完场景下收益递减** → Loopback 仿真表明，即使 `1 RTT` 内服务端把 ~142 KB 全部冲完，`exit_unread` 仅收益归零（`+16 B` HTTP/2 控制帧开销），不像 `raw.close()` 那样多花 TLS 握手；而在原生抓包观测到的 `1.17s–3.61s` 流式 SSR 场景可省 ~86% 流量，总体只赚不赔。
- **`follow_oauth_callback` 在注册收尾改为校验 `>= 400` 时的重试与测试桩风险** → 通过 `getattr(resp, "raise_for_status", None)` + 回落 `RuntimeError` 检查 `status_code >= 400`，确保 `SimpleNamespace(status_code=200, ...)` 单测桩零报错；同时注册收尾 403 时需先调 `reset_retryable_circuit(session)` 再重试阶段 1，防止本地熔断拦截同会话重试。
- **`_exchange_new_token` 复用 `follow_oauth_callback` 后的测试桩调用变化** → 原本只调一次 `session.get` 与 `get_auth_navigate_headers`，改动后会按 URL 域调 `get_chatgpt_navigate_headers` / `get_auth_navigate_headers`。需核对并补充 `_exchange_new_token` 对应单测（如真实 `BrowserSession` 替身验证跨站头与 403 阻断）。
- **`_finalize_registration_session` 与 `account_liveness._follow_continue_and_fetch` 重试次数的轻量差异** → 注册保留既有 `_FINALIZE_SESSION_MAX_ATTEMPTS = 5`，查活保留各自 3 次与已有重试逻辑，只保证阶段分离一致，不强行改动查活现有重试预算。
