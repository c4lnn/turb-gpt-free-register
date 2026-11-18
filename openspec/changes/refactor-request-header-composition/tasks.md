## 1. 迁移准备

- [x] 1.1 确认项目本地虚拟环境已创建并激活，列出当前画像字段与全部请求头实际消费者（含 Codex 对基础头的直接调用）；验收：六个高熵字段有明确画像映射，消费者清单覆盖设计所列认证、查活、注册、导出、Codex、bootstrap/commerce 路径，不发起真实认证。
- [x] 1.2 将设计中的九条目标请求及非目标注册/导出/其他导航行为映射到现有合同测试，确定需要更新的断言；验收：每条目标请求均有字段集合、取值来源和缺失状态检查位置，非目标保留项及共享密码/MFA 路径有对应测试位置，不以源码文本或内部调用次数作为合同。

## 2. 字段组与场景组合

- [x] 2.1 收窄基础画像构造为 UA、语言与低熵 CH，将高熵生成统一为唯一域名无关入口；验收：实际返回头满足六字段整体启用、合法空模型有效、缺画像/非 Chromium 显式启用失败、关闭无高熵的合同，版本与平台取当前会话画像。
- [x] 2.2 抽取重复 fetch/navigation 语义，整理来源参数与独立字典组合；验收：导航不混入 fetch 的 RUM/凭据，同一会话交错构造不同场景不会修改先前请求头，不引入通用 builder/插件或任意覆盖字典。
- [x] 2.3 增加专用 NextAuth CSRF/signin 场景入口，并保留有独立用途的通用 NextAuth、Auth JSON、导航、Sentinel、ChatGPT 前端 API 场景；验收：CSRF GET 与 signin 表单的内容类型、缓存、Origin/Referer 和 Auth issue 的 Accept 由场景参数一次决定，无调用点重复覆盖常规字段。
- [x] 2.4 增加默认保守的两值内部流程策略，明确目标场景启用规则；验收：目标场景的高熵、Priority、导航用户触发和来源规则符合 design 场景表，STANDARD 保持原有语义，策略不依赖 session 可变标志或 URL/Referer 猜测。
- [x] 2.5 统一校验后 Sentinel 凭据的附加逻辑，同时保留现有 Auth RUM 生产入口及 navigation ID 轮换位置；验收：凭据附加不发网络请求或求解，issue 无主/SO 凭据，SO 仅在 required 时附加，动态 trace/span/invocation 不缓存复用。

## 3. 目标登录分支端到端接入

- [x] 3.1 在 full-web 入口按既有密码来源选择流程策略并保存当前密码局部快照，将参数传递到正式 CSRF、signin、authorize、密码与 MFA 步骤；验收：有密码入口的合成调用链使用目标策略，无密码走原邮箱 OTP；注册/导出共享调用不会自动切换策略，不提前以 TOTP secret 决定分支。
- [x] 3.2 为目标登录在正式 CSRF 前构造并复用完整 login_with Referer，接入新的 CSRF/signin 场景函数；验收：捕获到的两次 Referer 相同且解码输入匹配当前邮箱/device/logging ID，signin 消费本次正式响应 token，既有 query/form 和请求数量顺序不变。
- [x] 3.3 接入目标 authorize 导航与重定向的头部策略，保留既有 URL 来源和 navigation ID 轮换规则；验收：两跳均使用 ChatGPT 根 Referer、cross-site/navigate/document、六个高熵，无 Sec-Fetch-User/显式 Priority，收到响应与无响应异常的 ID 行为符合原合同。
- [x] 3.4 为目标密码验证、MFA issue/verify 及对应 Sentinel challenge 完整传递策略，迁移共享 TOTP 包装的相关参数；验收：password_verify/verify_mfa 凭据各自绑定当前 challenge，issue Accept 为 */* 且无凭据，Sentinel 使用 text/plain 且无高熵/RUM，捕获范围外 OTP/回调/session 保持原语义。

## 4. 全部调用方切换与兼容性

- [x] 4.1 迁移注册与注册预检路径（包括注册内已有账号密码验证），保持正式上下文、OTP 导航、OAuth 收尾和首次 warmup 原语义；验收：相关注册合同仍验证既有 Referer/缓存/内容类型/显式高熵/Priority/用户触发选择，不自动应用目标登录覆盖，token 获取方式和请求顺序不变。
- [x] 4.2 迁移账号导出/激活及 Codex 的 NextAuth、Auth JSON、导航和基础画像消费者；验收：非目标入口保持 STANDARD 与各自业务头，高熵仅沿用原显式选择，不因共享密码/MFA 步骤改变请求语义。
- [x] 4.3 迁移 ChatGPT bootstrap、commerce 和清单中其余消费者，收拢重复头部操作；验收：各实际消费者仍收到所需 oai/Datadog/target/Authorization 等其原有业务字段，目标认证头不会泄漏到其他域或场景。
- [x] 4.4 删除被替代的高熵实现、参数用法、无效重复代码及 `SEND_HIGH_ENTROPY_CLIENT_HINTS` 定义/导出/引用，不添加 shim 或转发别名；验收：符号引用或必要的定向搜索无遗留消费者，仍有独立用途的场景入口和画像构建常量保留。

## 5. 合同验证与交付

- [x] 5.1 更新现有场景/调用合同测试，补充确实缺失的高熵完整性、目标策略传播、CSRF 传递、跨场景污染及非目标分支回归；验收：覆盖 spec 的相关 Scenario，以真实构造头或消费者请求为断言对象；旧目标登录差异断言替换为新合同，只锁实现细节的用例删除而不重新钉住新写法。
- [x] 5.2 补齐或复用动态关联与 Sentinel 凭据边界用例；验收：通过受控随机值确定性验证逐 fetch trace/span/invocation、同文档 navigation ID、响应/异常轮换差异、flow/device/challenge 错配阻止消费者发送，以及 SO required/非 required 和 issue 无凭据，不运行真实账号认证。
- [x] 5.3 全部代码与测试迁移完成后，在项目本地虚拟环境中一次性运行覆盖本次变更的定向测试集合；验收：记录实际命令和结果，范围涵盖 session headers、ChatGPT auth context、OpenAI auth、account liveness、受影响非目标消费者及 Wreq 适配合同，不以全库检查替代针对性证明。
- [x] 5.4 使用合成输入通过现有 Wreq 适配器访问隔离本地接收端（测试端口 5100），完成代表性 fetch/导航及两跳重定向 smoke；验收：实际接收头证明所测场景的字段值/缺失、重定向来源与 Content-Length，无真实 Cookie/token/邮箱/密码；报告只声明本地已观察结果，并删除临时脚本和关闭本次接收端。
- [x] 5.5 更新 `doc/login-password-totp-403-request-details.md`、`doc/login-password-totp-403-analysis.md`、`doc/registration-2fa-oss-comparison.md` 以及实际受影响现有文档的当前行为章节和有效源码引用；验收：旧构造差异被当前行为替换，捕获事实/范围/脱敏与无法判断边界保留，不将本地 smoke 写成目标站接受或 MFA 403 已解决，不另增普通报告文档。
