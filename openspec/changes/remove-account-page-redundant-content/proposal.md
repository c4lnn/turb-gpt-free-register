# Proposal

## Why

账号管理页面在主标题下方堆叠全局资源概览卡片和重复的页面说明，挤占账号列表可见空间。移除这两处冗余内容，让用户更快到达已注册账号及其筛选和批量操作。

## What Changes

- 仅在账号管理页面隐藏标题下的说明、资源概览卡片，以及账号内容区重复的标题和说明。
- 保留控制台主标题、自动刷新状态、已注册账号卡片及其现有功能；其他页面的概览卡片和说明不变。

## Capabilities

### New Capabilities
- `account-management-ui`: 规定账号管理页面的标题区域、概览内容和账号列表的呈现边界。

### Modified Capabilities

## Impact

- 影响 `webui/frontend/src/App.tsx` 的活动页面标题/描述与资源概览渲染，以及 `webui/frontend/src/pages/AccountsPage.tsx` 的账号页标题区。
- 不改变 API、账号数据、筛选、批量操作或其他页面行为。
