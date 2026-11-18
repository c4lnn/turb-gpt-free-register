## Why

现有请求头已经按域名和用途局部封装，但浏览器画像、高熵 Client Hints、请求语义与端点覆盖仍交叉分布在 `BrowserSession` 和业务调用点；两套高熵构造的字段集合及取值校验也不一致。`doc/login-password-totp-403-request-details.md` 已提供正式 CSRF 至 MFA verify 的分支证据，现在可以收拢这些职责，并显式修正该登录分支的已知头部差异，而不把一次捕获推广为所有流程的默认策略。

## What Changes

- 采用“底层字段组＋上层场景函数”：统一浏览器基础画像、高熵 CH、fetch/navigation 语义、来源上下文、动态关联字段及凭据附加职责；调用方选择场景并提供业务上下文，不再重复拼装常规字段。
- 将高熵 CH 收敛为唯一构造入口，使用当前会话画像完整生成六个字段，显式启用时拒绝不完整或非 Chromium 画像；基础画像函数不再受全局高熵开关影响。
- 整理 NextAuth CSRF、signin 表单、Auth JSON、页面导航、Sentinel challenge 场景，并保留 ChatGPT 前端 API 等现有消费者需要的场景；迁移全部受影响调用点，删除重复逻辑和被替代入口，不增加兼容 shim。
- 对密码＋TOTP full-web 登录分支采用显式策略：正式 CSRF/signin 共享完整登录 Referer 与禁缓存头，CSRF GET 无 Content-Type；相关 NextAuth、OAuth 导航及密码/MFA 请求携带六个高熵 CH，Sentinel challenge 不携带；九条捕获对应场景不显式添加 Priority；OAuth 导航不声明用户触发。
- 保留 MFA issue_challenge 的 `Accept: */*` 与无 Sentinel 凭据语义；密码验证和 MFA verify 使用其各自 flow 校验后的凭据，SO 仅按实际 challenge 条件附加。
- 保持逐请求 trace/span/invocation 与文档级 navigation ID 的既有生命周期，Sentinel 请求/求解/归属校验继续独立于头部组装。
- 注册、注册内已有账号分支、账号导出/激活、Codex、commerce、bootstrap、其他登录和导航调用仅迁移组合方式，保持既有请求语义；不以全局修改默认值实现目标登录分支对齐。
- 更新现有相关文档，区分捕获事实、当前构造行为、最终 wire headers 与无法判断的浏览器内部机制；不将捕获中的敏感值或固定版本写入模板。

## Capabilities

### New Capabilities

- `browser-request-headers`：定义浏览器请求头的场景组合、画像与动态凭据边界，规定密码＋TOTP 登录分支的头部行为以及非目标分支的兼容性。

### Modified Capabilities

无。当前仓库未建立 `openspec/specs/` 主规范目录；现有 `align-registration-har-a-headers` change 的注册上下文约束作为兼容性依据保留，不在本 change 中改写其历史产物。

## Impact

- 主要代码区域：`core/openai/transport/session.py`、`core/openai/auth/chatgpt_auth.py`、`core/openai/auth/openai_auth.py` 和 `core/openai/account/account_liveness.py`。
- 调用方迁移涉及 `core/openai/account/account_export.py`、`core/openai/auth/chatgpt_bootstrap.py`、`core/openai/register/`、`core/openai/codex/codex_oauth.py`、`core/openai/commerce/chatgpt_plan.py` 及仓库内其余实际消费者；受影响接口是仓库内部 Python 调用，不新增 WebUI 或外部服务 API。
- 配置：清理只服务于旧隐式高熵注入的 `SEND_HIGH_ENTROPY_CLIENT_HINTS` 定义、导出和引用；画像字段与构造来源继续复用现有配置，不引入依赖或第二套画像。
- 验证：更新已有场景/调用合同测试；补齐跨场景污染、完整高熵字段、正式 CSRF 传递、动态 ID 生命周期和条件性凭据等实际消费者可见边界。最终发送头可用隔离本地接收端观察，不能由字典断言替代 wire 证明。
- 文档：同步已有登录 SAZ 分析与请求明细、注册对照文档中受影响的当前行为描述；仅在实现阶段更新，保留捕获事实及必要历史背景。
- 不改变请求数量/顺序、URL/query/body 构造、CSRF/OAuth/PKCE 来源、Cookie Jar、Sentinel 求解算法或 TLS/HTTP 传输指纹；允许为正确传递场景策略增加内部参数。捕获终点为 MFA 403，本变更不承诺解决 403 或证明服务端接受差异。
