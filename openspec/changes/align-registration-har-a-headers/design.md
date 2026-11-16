# Design

## Context

动机和四处行为范围见 `proposal.md`；外部契约见 `specs/registration-request-context/spec.md`。

- 正式 CSRF 的当前调用是 `get_csrf_token(session)`，signin 已在 `chatgpt_auth.py` 内构造完整 login_with Referer。注册入口在预检之前就持有 email，预检之后已绑定设备 ID，因此无需提前给 warmup 传邮箱，也无需新增网络请求。
- `account_liveness.py` 同时使用 CSRF、signin 和 callback helpers。共享 helper 的行为不能通过全局默认头修改后无差别外溢。
- OTP 发送 helper 当前使用密码页 Referer，调用已轮换 document-navigation ID；改变 Referer 不代表新建文档，不额外轮换 ID。
- callback helper 按目标域名调用现有导航 builder，但没有暴露 builder 已支持的 user_initiated 选择；注册收尾同时覆盖已有账号恢复，不能给所有 callback 一律取消用户触发头。
- wreq wrapper 交由后端自动处理重定向，当前没有每跳请求头重建逻辑。源码只能直接证明首跳头构造，不能证明每跳最终 wire headers。

此次涉及认证上下文、注册状态及共享 callback 等多个模块，并有非目标登录行为隔离要求，符合编写 design 的条件。

## Goals / Non-Goals

**Goals:**

- 一处构造动态 login_with Referer，让注册的正式 CSRF 与 signin 共享同一上下文，邮箱 URL 编码语义正确。
- 让注册调用显式选择端点行为，不以会话级 headers 或新策略类隐式改变其他请求。
- 使用现有导航 builder 与 CookieJar 完成四处同步，并在本地真实传输上验证重定向头部。

**Non-Goals:**

- 不统一所有认证分支的 Referer；不重新分类注册状态，不调整 OTP、文档 ID 或重试策略。
- 不引入全局 HAR 复现模式、浏览器画像切换或通用导航框架，不为此次变更新增配置开关。
- 不删除 session GET，不解析首页 bootstrap；不执行真实注册以推断服务端接受差异。

## Decisions

### 1. 正式 CSRF 显式接收登录上下文，warmup 完全隔离

复用 signin 现有字段集合，将 login_with Referer 的构造收敛为 `chatgpt_auth.py` 内单一逻辑。注册路径在 warmup 成功后，用当前 email、device_id、auth_session_logging_id 构造上下文，并显式传给正式 CSRF 与 signin；使用已有 URL 编码方式，避免二次编码和手工拼接。

共享请求 helper 若增加上下文参数，迁移全部生产调用，并让非注册登录继续明确使用原有 CSRF Referer 和缓存策略；不复制一套注册专用 signin 实现，也不通过在 session 上增加隐式登录邮箱状态传递上下文。参数的最终命名沿用仓库风格，行为以 spec 为准。

替代方案：两处独立拼接完整 Referer，容易因字段顺序或动态值差异破坏完全相同的要求；给 warmup 提前传邮箱则改变其职责，且当时设备身份尚未绑定，均不采用。

### 2. 只在目标端点应用缓存和 Content-Type 差异

正式 CSRF 构造完 NextAuth headers 后移除 Content-Type，并写入两个 no-cache 头。注册 signin 在设置 form Content-Type 和 Origin 的现有位置写入两个 no-cache 头。非目标调用通过显式上下文/端点选项保留原行为，不修改 `get_nextauth_headers()` 的全局默认值，不修改 fetch_session。

替代方案：全局删除 Content-Type 或全局附加缓存头会同时影响 session、存活检查等调用，超出范围。

### 3. OTP 发送修改注册导航上下文，不改变状态机

`navigate_email_otp_send()` 是新账号密码注册中消费服务端继续地址的步骤，将该步骤 Referer 改为 auth 根，复用既有 same-origin、user_initiated 和自动重定向设置。保留原 continue_url、最终落点验证及 document-navigation ID 轮换位置，不增加单独的 email-verification 请求。

替代方案：在通用 Auth 导航 builder 中统一根 Referer，会改变其他页面导航，且不能表达不同调用的真实上下文。

### 4. callback 用户触发语义由注册分支显式传递

为 callback helper 及注册收尾调用传递 user_initiated 选择，直接交给现有导航 builder。新账号注册收尾显式传 auth 根 Referer 与 false；已有账号分支继续采用已有 callback_referer 和用户触发语义；`account_liveness.py` 等非目标调用保持原行为。若改变共享签名，迁移全部调用和相关契约测试，不保留旧接口别名。

不通过全局改变导航 builder 的默认 true 达成目标，也不仅在临时 headers 字典里删除字段而遗漏另一目标域名的 builder 分支。

### 5. 先使用既有自动重定向，按实际 wire 证据处理后端行为

OTP 发送保留后端自动重定向与同一 CookieJar。已用临时可信证书、5100 端口 HTTPS 接收端和真实 wreq Chrome152 验证 OTP 两跳、直达 callback 两跳、两个本地 origin 的三跳，以及 auth→callback→首页；仅做 URL 本地路由适配，使用虚构凭据和测试 Cookie，不连接真实站点。HTTP 接收端的首次验收发现 HTTPS Referer 在降级跳转后被去掉，因此最终以 HTTPS 场景验收，不以降级结果修改业务逻辑。

HTTPS 实测进一步确认：后端自动跳转不会将 auth 的 same-origin 重算为 ChatGPT 的 cross-site。因此新注册的非用户触发 callback 在 helper 内显式逐跳请求，每跳按目标使用现有导航 builder，保留 auth 根 Referer、同一 CookieJar、相对 Location 解析及原 30 次跳转上限；已有账号和其他用户触发 callback 保留自动重定向。此为实测触发的目标路径修正，不修改全局 wreq wrapper，也不新增通用逐跳跟随框架。修正后本地 HTTPS 场景全部通过，临时脚本及证书已清理。

### 6. 优先更新现有行为测试，再做一次定向验收

现有 `tests/test_chatgpt_auth_context.py` 覆盖 signin 上下文，`tests/test_openai_auth_registration_flow.py` 覆盖 OTP 导航和文档 ID，`tests/test_registration_pipeline.py` 覆盖注册编排。实施时确认这些测试的实际覆盖，优先更新已有契约；只有动态邮箱编码、两次 CSRF token 选择、注册/非目标隔离或 callback 重定向存在未覆盖风险时新增对应回归用例。

完成全部修改后，一次运行涉及的定向测试和本地真实传输场景，不跑无关全量套件。验收脚本为临时文件，结束后删除。文档逐项记录构造结论及实际观察边界，不把本地 wire 观测写成真实服务端接受证明。

## Risks / Trade-offs

- [邮箱和会话上下文进入 Referer] → 仅用于正式登录请求，使用已有字段范围；测试及日志脱敏，不打印完整 Referer、邮箱、设备 ID、token 或 Cookie。
- [共享 helper 修改影响存活检查或已有账号恢复] → 查找所有调用并显式保留非目标语义，用能区分注册与非目标请求的行为断言验证，避免全局默认头修改。
- [自动重定向与浏览器行为不同] → 以本地真实接收结果验证每跳，发现不符时在目标路径修正；本地验证不证明真实跨站网络和服务端接受行为。
- [头部变化被误当作文档导航] → 不增加请求、不改变既有 document-navigation ID 的轮换分组，保留 OTP 成功进入 profile 的状态处理。
- [HAR 路径已变化] → 文档记录现有文件与捕获元数据的核对结果，仍严格限定零基 #0–#280，不拼接其他 HAR 或暴露敏感内容。

## Migration Plan

无数据库或配置迁移。实施时在同一变更中完成共享 helper 调用迁移、注册分支调整、定向验证与分支 A 当前行为文档替换；非目标行为保持不变。回退时整体撤回此次请求头及调用参数变更，恢复匹配的测试与当前行为文档，不触碰账号数据、Cookie 存储或用户其他修改。
