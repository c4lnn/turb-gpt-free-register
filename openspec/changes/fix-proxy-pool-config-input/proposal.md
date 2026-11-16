# Proposal

## Why

配置页中的 `PROXY_POOL` 有当前值时会被错误渲染为选择控件，用户不能直接编辑代理列表；空列表首次输入后也会切换为选择控件。修正这一行为，才能从 WebUI 添加、修改、移除或清空代理池。

## What Changes

- 对空值和非空值，代理池都使用可编辑的多行控件；每行表示一个代理 URL。
- 保留现有保存语义：忽略空行，并支持保存空列表以清空代理池。

## Capabilities

### New Capabilities
- `webui-configuration`: WebUI 配置页中代理池多行字段的编辑与保存行为。

### Modified Capabilities

无；当前项目尚无已定义的 OpenSpec capability。

## Impact

- 前端：`webui/frontend/src/pages/ConfigPage.tsx` 的配置字段控件选择逻辑。
- 后端配置 API 与 `.env` 存储格式不变；继续使用现有 `PROXY_POOL` 多行列表读写语义。
