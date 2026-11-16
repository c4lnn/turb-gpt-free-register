# Tasks

## 1. 正式 CSRF 与 signin 登录上下文

- [x] 1.1 核对共享认证 helper 的全部生产调用及现有上下文测试；若 LSP 可用则用 references 查询，否则记录不可用并定位调用。交付可复用的单一 login_with Referer 构造和显式上下文传递，注册在预检绑定设备后传入当前邮箱，非目标登录保留原语义；以 CSRF/signin Referer 完全相同且加号邮箱解码后保持原值验收。
- [x] 1.2 正式 CSRF GET 移除 Content-Type、补 cache-control/pragma no-cache，注册 signin 补两个 no-cache 头，保留 form/query/Origin 和正式 token 来源；更新 `tests/test_chatgpt_auth_context.py` 及相关既有用例，缺少覆盖时补充两次 CSRF 使用不同虚构 token、最终 signin 只消费正式 token 的回归场景，以请求参数和实际消费关系断言验收，首次 warmup 不增加邮箱、Referer 或 query/body。
- [x] 1.3 替换 `docs/registration-2fa-har-comparison.md` 中分支 A #4/#5 对应的当前构造描述，注明动态登录上下文来自当前邮箱/设备/logging ID、首次 warmup 独立且非 HAR #4；以更新后的字段表准确区分静态构造与尚未实测 wire、凭据值均脱敏验收，不复制 HAR 邮箱或 ID。

## 2. 注册 OTP 发送导航

- [x] 2.1 将新账号注册 OTP 发送导航 Referer 改为 auth 根，保留服务端 continue_url、same-origin、Sec-Fetch-User ?1、已有重定向和文档 ID 轮换位置；更新 `tests/test_openai_auth_registration_flow.py` 相关行为断言，以发送头正确、落点验证和既有文档 ID 分组不变验收，不新增独立页面请求。
- [x] 2.2 替换 HAR 对照文档分支 A #137/#138 的本地 Referer 差异描述，保留逐跳 wire 待实测边界；以文档仍按请求字段和来路对照、未重新审计已确认请求顺序验收。

## 3. 注册 callback 导航语义

- [x] 3.1 在 callback helper 和注册收尾调用之间显式传递 user_initiated 选择，新账号注册传 auth 根 Referer 与 false，已有账号及其他调用保留原语义；迁移共享接口全部调用，更新注册编排和 callback 的现有契约测试，未覆盖时补回归场景，以新注册不带 Sec-Fetch-User、非目标调用保持原行为、仍通过 session GET 取得 token 和元数据验收，不修改通用 builder 默认值。
- [x] 3.2 替换 HAR 对照文档分支 A #279/#280 与分支结论中的 callback 头差异描述，仍明确 accessToken 来源与 HAR 不同；以保留 Cookie/URL 来源和 wire 证据边界、未把 HAR B 捕获字段改写为 HAR A 行为验收。

## 4. 集成验收与证据收口

- [x] 4.1 在上述代码和相关测试完成后，一次运行受影响的定向测试集合（含认证上下文、OTP 导航、注册编排、callback 及确受影响的非目标调用），不运行无关全量套件；以所有目标行为及隔离场景通过验收，修复实际失败后只重跑受影响检查，不靠缩小断言规避失败。
- [x] 4.2 用临时本地接收端和真实 wreq 传输验证 OTP send→verification、callback→homepage 的每跳 Referer、Sec-Fetch-* 及 Cookie 传递，必要时覆盖两个本地 origin；使用测试专用虚构值且不连接真实注册端点，以接收端实际记录满足 spec 验收。若后端改写字段，在目标导航 helper 做最小跟随修正并重验受影响场景，不提前增加通用重定向框架；结束后删除临时脚本。
- [x] 4.3 根据实际定向测试和本地 wire 结果收口对照文档证据，修正文档中 HAR A 的现有文件定位（568 entries、捕获时间和序号吻合的 `har/chatgpt.com.har`），同步分支 A 的相关共用结论；以字段表列出 HAR 序号和实施后源码行号、明确本地测试不等于真实站点验收、未运行真实账号注册且无敏感值泄漏验收。
