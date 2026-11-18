## Why

本地协议流程对 signin 的 HTTP 403 不应自动重试；重试可能重复提交同一 signin 请求并掩盖明确的拒绝结果。让 403 立即失败，可使本地行为与预期错误处理一致，并保留原始 HTTP 错误供上层处理。

## What Changes

- signin 请求收到 HTTP 403 时直接抛出错误，不重发该请求；包含 CSRF → signin 的重试编排不得因该 signin 403 再次启动整段认证链。
- 保持 CSRF 请求及其他请求当前的重试策略不变；CSRF 403 的重试不在本次改动范围内。
- 增加回归覆盖，确认 signin 403 只发送一次并向调用方传播错误，同时不改变 CSRF 403 的既有重试行为。

## Capabilities

### New Capabilities
- `auth-signin`: 定义 signin 请求遇到 HTTP 403 时立即失败且不重试的行为。

### Modified Capabilities

## Impact

- 影响 `signin_openai()` 的调用方重试边界，尤其是 `core/openai/account/account_liveness.py` 中覆盖 CSRF、探测与 signin 的网络预检重试；对应测试验证 signin 403 不重试，CSRF 403 仍可按既有策略重试。
- 不改变 signin 请求成功路径、CSRF 获取策略或 authorize 重定向处理。
