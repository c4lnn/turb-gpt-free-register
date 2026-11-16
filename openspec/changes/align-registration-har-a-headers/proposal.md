# Proposal

## Why

当前注册链与 HAR A 在正式 CSRF、signin、OTP 发送导航及 OAuth 回调的请求头上存在已确认差异。此次只对齐这四处可观察行为，保留首次设备预热及既有状态传递，不把静态头构造等同于最终网络报文一致或注册成功率提升。

## What Changes

- 第二次正式 CSRF GET 使用与随后 signin 相同的完整 `/auth/login_with?...` Referer，包含当前邮箱、已绑定设备 ID 和会话 logging ID；移除该 GET 的 Content-Type，增加 `cache-control: no-cache` 和 `pragma: no-cache`。
- signin POST 增加上述两个禁缓存头，保留现有 form、query、Origin 及 CSRF token 来源。
- 新账号注册的 `email-otp/send` 导航使用 `https://auth.openai.com/` Referer，保留服务端继续地址、`same-origin` 和 `Sec-Fetch-User: ?1`。
- 新账号注册的 OAuth 收尾导航使用 auth 根 Referer，不发送 `Sec-Fetch-User`；验收 callback 及其首页重定向落点的头部行为。
- 首次 CSRF warmup 不显式传邮箱、不增加 Referer，仍只承担设备 Cookie 初始化；不全局改变通用 NextAuth 或导航 builder 的默认行为，不改变已有账号登录及其他回调调用方的导航语义。
- 实现后直接更新 HAR 对照文档相关行，保留“源码构造”和“最终 wire 观测”的证据边界。

## Capabilities

### New Capabilities

- `registration-request-context`：定义注册请求的登录上下文 Referer、端点级缓存/内容类型及导航用户触发语义，并约束设备预热与正式认证请求之间的隔离。

### Modified Capabilities

无。当前主规格目录没有已登记 capability。

## Impact

- 涉及 `core/openai/auth/chatgpt_auth.py`、`core/openai/auth/openai_auth.py`、`core/openai/account/account_export.py`、`core/openai/register/registration_flow.py`；如需为调用方传递显式选项，迁移受影响调用，不保留废弃接口或别名。
- 保持 `core/openai/transport/session.py` 中通用 builder 的默认行为，以及 `core/openai/register/registration_preflight.py` 的首次 warmup 行为；按需复用现有可选参数。
- 验证范围包括动态邮箱/设备/logging ID 传递、端点头字段集合、注册与非目标调用的隔离，以及 wreq 的真实本地重定向行为；不运行真实账号注册。
- 文档更新对象为 `docs/registration-2fa-har-comparison.md` 的分支 A 相关表格及结论，不重新审计已确认的本地请求顺序，不改写 HAR B 的历史捕获结论。
- 参考为 HAR A 零基 `#0–#280`，截止 callback 后首页 200。文档旧路径 `har/落点明确-passwordless-at.har` 当前不存在；已核对 `har/chatgpt.com.har` 的 568 entries、起止时间和相关序号与文档相符，实施时记录这一文件定位事实，不混入其他捕获。
- 不增加依赖；不调整画像、authorize URL 改写、Sentinel、token 获取方式、注册顺序、重试策略或 MFA 流程。
