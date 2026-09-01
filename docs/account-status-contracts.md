# 账号状态契约迁移说明

## 兼容周期

旧字段和旧筛选参数至少保留一个完整发布周期。新消费者必须使用
`codex_auth_status`、`codex_operation_status`、`plan_category_code`、各领域
过程状态及 capabilities；`codex_status` 只作为授权事实的兼容投影，不再承载补跑过程。

账号列表和轻量状态接口的新筛选参数为 `plan_category`、`codex_auth_status`、
`codex_operation_status` 和 `live_check_status`。这些维度可组合使用；兼容参数
`codex_status` 不得与新状态参数同时提交，避免同名 code 被解释到错误领域。

## 迁移观测

服务启动时运行幂等迁移，并记录 `changed`、`unknown` 和 `total`。无法从历史
`retrying`、`stopped` 或 `deactivated` 恢复授权事实时使用 `unknown`，同时保留
`codex_status_legacy_raw`，不得猜测为成功。

迁移和测试只使用当前配置的 JSON/SQLite 存储边界。自动化测试必须使用 fixture、
mock 和临时存储，不读取真实账号凭据，也不调用真实上游协议。

## 回滚

回滚前必须停止 Codex、套餐、Checkout、提链和查活后台任务。新增规范字段和 raw
诊断字段应保留；旧版本可以继续读取兼容字段，但不得批量把 `unknown` 改为成功，
也不得把 `retrying` 或 `stopped` 重新写入授权事实字段。

按领域回滚消费者时，先回滚 WebUI，再回滚 API/服务写入。数据迁移本身不执行
破坏性反向迁移。
