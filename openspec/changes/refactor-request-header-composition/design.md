## Context

动机见 `proposal.md`。当前 `core/openai/transport/session.py:529–555` 的基础头函数可经全局开关注入五个高熵字段，而 `699–717` 的 Auth 专用附加函数严格从会话画像注入六个字段；NextAuth、Auth JSON、导航和 Sentinel 模板还各自重复 fetch/navigation 字段。`chatgpt_auth.py:100–106、159–166` 及 `openai_auth.py:987–1040` 在业务层继续覆盖内容类型、缓存、Accept 和凭据。

比较依据是 `doc/login-password-totp-403-request-details.md`：主链 #16 正式 CSRF、#17 signin、#18/#19 OAuth 导航、#34/#87 Sentinel challenge、#58 密码验证、#84 MFA issue、#93 MFA verify；范围截至 #93 的 403。正式 CSRF 按响应 token 被 signin 消费定义，不按请求次数定义。文档公共组用于还原捕获，不是可直接照搬的静态字典。

兼容性约束来自现有消费者及 `align-registration-har-a-headers` 的注册上下文 delta。尤其 `registration_flow.py:427` 也调用密码验证，账号导出与 Codex 也调用共享 Auth/NextAuth/导航函数，不能把目标登录策略写成共享函数的新全局默认。当前查活 full-web 登录在 `account_liveness.py:514–534` 串联 CSRF/signin、authorize 与密码/MFA；密码是否可用在此之前可从现有账号存储读取，无需新增网络请求。

## Goals / Non-Goals

**Goals:**

- 每类字段只有一处组合规则；每次返回独立字典，禁止请求间共享可变头部结果。
- 调用者表达业务场景和来源上下文，而非逐项维护常规 header。
- 使用可显式传递且默认保守的流程策略隔离目标分支；既能验证头部值，也能验证值的生产者与传递关系。
- 在同一变更中完成所有消费者迁移；保留有独立用途的场景函数，不以删除所有旧名字为目标。

**Non-Goals:**

- 不设计通用 HeaderBuilder、插件、端点注册表、配置 DSL 或第二套 browser profile。
- 不把“字段被记录”推导成浏览器内部生成算法，不把构造字典推导成最终 wire，不保证 403 消失。
- 不改变认证请求顺序、query/body 编码、预热步骤、PKCE/state、Cookie 存储与传输后端；不为对齐头部新增页面请求。

## Decisions

### 1. 小型私有字段组加现有场景入口，不引入另一套框架

继续以 `BrowserSession` 持有稳定画像和动态 ID。将 `_get_common_headers()` 收窄为 UA、语言和低熵 CH 的基础构造，不接收高熵开关。将 `_attach_auth_high_entropy_client_hints()` 改为域名无关的唯一高熵附加函数，替换所有调用，删除原实现而非保留转发别名。

对 fetch 的 mode/dest、document 导航的 Accept/mode/dest/upgrade 等确有重复的字段抽出小型私有 helper。来源上下文仍由场景函数按明确参数设置；不是每一列字段都必须有一个 helper。

公开组合入口包括端点专用 `get_nextauth_csrf_headers()`、`get_nextauth_signin_headers()`，以及仍有独立消费者的通用 NextAuth fetch、Auth JSON、页面导航、Sentinel challenge、ChatGPT 前端 API 场景。通用 NextAuth fetch 继续服务 providers/session/reauth 等，不作为 CSRF/signin 的兼容别名。Auth JSON 通过有语义的 `accept` 参数处理 issue_challenge 的例外，不再在业务层先构造默认值再重复覆盖。

每个入口从新字典开始，字段按“基础画像 → 显式高熵 → 请求语义与来源 → 当前请求动态关联 → 已校验凭据”的确定顺序组装。调用方不接收任意覆盖字典来绕过场景规则；个别真正的业务字段由对应场景参数表达。

备选：让每个请求自由合并六七组字典，会把漏组、覆盖顺序和生命周期责任重新交给调用点；一函数一端点则会重复相同的 Auth JSON 规则。选少量场景函数作为折中。

### 2. 唯一高熵入口，六字段整体启用，不做静默回退

完整字段集合为 `sec-ch-ua-full-version`、`sec-ch-ua-full-version-list`、`sec-ch-ua-platform-version`、`sec-ch-ua-arch`、`sec-ch-ua-bitness`、`sec-ch-ua-model`。只从当前冻结 `browser_profile` 读取；`sec-ch-ua-full-version` 映射当前既有 `chrome_full_version` 字段。显式启用时，非 Chromium 画像、缺字段或无效空值在发送前失败，不再混入独立配置常量兜底。

`sec-ch-ua-model` 的合法空模型是包含两个引号的字符串 `""`，不是缺失字段或长度为零的字符串；校验和测试必须区分。版本、品牌、平台沿用会话画像，不固化 SAZ 的 Edge 157/Windows 值。

关闭高熵的场景从基础头开始构造，不通过“先加再删”实现。删除 `SEND_HIGH_ENTROPY_CLIENT_HINTS` 的定义、导出和引用，非目标场景保持当前默认关闭行为；现有注册等显式启用点继续显式启用。画像字段常量如仍被画像构建消费则保留，不进行无关配置清理。

备选：保留五字段兼容模式或让通用函数偷偷读取全局开关，会再次出现两种语义；本方案不支持全局隐式高熵注入。

### 3. 目标分支采用显式、局部传播的流程策略

引入仅有两个固定值的内部 `HeaderProfile`：`STANDARD` 和 `PASSWORD_TOTP_LOGIN`。它只承载本次已有两种行为选择，不提供任意端点配置或可变字段表。共享业务步骤通过 keyword-only `header_profile` 接收，默认 `STANDARD`；场景函数统一解析，不通过 URL、是否传入 Referer、session 上的可变开关或设备 ID 模式猜测流程。

查活 full-web 入口使用已有密码来源选择策略：有可用密码时，在正式 CSRF 之前选择 `PASSWORD_TOTP_LOGIN`，将该次密码输入作为局部快照继续传给密码步骤，避免在同一次流程中重新查询后发生策略与输入漂移；无密码的纯邮箱 OTP 流程使用 `STANDARD`。不提前要求 TOTP secret，也不改变服务端决定是否进入 MFA 的业务分支。

策略显式贯穿 `_network_preflight_with_retry()` 的正式 CSRF/signin 部分、`follow_authorize()`、密码验证、该次流程的 Sentinel challenge 及 MFA issue/verify。预热函数不消费目标策略。共享 `verify_login_totp()` 等中间包装如承接策略也必须继续向下传递，不能在包装层丢失。账号导出、注册内已有账号密码验证等调用显式使用 `STANDARD`，仅迁移头部组合方式。

若密码响应转向邮箱 OTP，已发出的 CSRF/signin/authorize/密码请求属于选定的目标前缀，不回滚或重发；后续未被捕获覆盖的邮箱 OTP 端点仍使用其原场景语义。成功回调及最终 session 同样不继承目标 profile 的头部覆盖。这是范围边界，不是失败回退机制。

备选：增加多个散落布尔参数会产生无意义组合；在 `BrowserSession` 上设置可变 flow 标记会污染预热、回调、复用会话和其他调用。选择显式两值 profile，并保持场景参数只描述真实例外。

### 4. 目标场景表明确列出行为，而非机械复用文档组

下表仅适用于 `PASSWORD_TOTP_LOGIN` 对应端点；所有值来源仍按当前上下文生成。

| 场景及证据 | Accept / Content-Type | 来源与 Fetch Metadata | 高熵 / 动态关联 / 凭据 |
|---|---|---|---|
| 正式 CSRF #16 | `*/*` / 不设置 | 完整 login_with Referer；same-origin/cors/empty；无 Origin；两项 no-cache | 六个高熵；无 RUM、无 Sentinel 凭据 |
| signin #17 | `*/*` / `application/x-www-form-urlencoded` | 与 CSRF 相同 Referer；Origin 为 ChatGPT；same-origin/cors/empty；两项 no-cache | 六个高熵；无 RUM、无 Sentinel 凭据 |
| authorize 及密码落页 #18/#19 | HTML 导航 Accept / 不设置 | Referer 为 ChatGPT 根；cross-site/navigate/document；upgrade=1；无 Origin、无 Sec-Fetch-User | 六个高熵；无 RUM、无 Sentinel 凭据 |
| Sentinel challenge #34/#87 | `*/*` / `text/plain;charset=UTF-8` | Sentinel Origin、当前 SDK 版本 frame Referer；same-origin/cors/empty | 无高熵；无 RUM、无 Auth 凭据 |
| password verify #58 | `application/json` / `application/json` | Auth Origin、Auth 根 Referer；same-origin/cors/empty | 六个高熵；当前请求 RUM；password_verify 凭据 |
| MFA issue #84 | `*/*` / `application/json` | 同上 | 六个高熵；当前请求 RUM；无 Sentinel/SO 凭据 |
| MFA verify #93 | `application/json` / `application/json` | 同上 | 六个高熵；当前请求 RUM；verify_mfa 凭据 |

这九条对应请求均不显式设置 Priority、Authorization、oai-device-id、oai-language、x-csrf-token；设备 ID 与 CSRF 仍通过原有 Cookie/query/body 传递。SO 是否附加只由当前已校验 challenge 的 required 条件决定；捕获没有 SO 头，不是全局禁止其他响应要求 SO 的证据。

正式 CSRF 与 signin 在调用前使用 `build_login_referer()` 构造同一个局部 Referer，使用当前邮箱、device ID 和 logging ID；不访问该 URL 以“获取”Referer。csrfToken 继续来自本次正式响应，signin query/form 与 authorize URL 处理保持不变。

#18/#19 必须逐跳保留最初导航的来源上下文。5100 隔离接收端实测表明，wreq 自动跟随重定向时会在后续导航请求移除调用方设置的 Referer；因此 `follow_authorize()` 显式关闭底层自动跟随，按 Location 逐跳发送同一组导航头，不增加请求数或改变 URL 顺序，并在最终响应后沿用既有 navigation ID 轮换时机。此选择只针对 authorize 重定向链，不推广到其他导航；验证必须观察每一跳的实际接收头。

非目标 `STANDARD` 场景保留现有 Priority、用户触发默认值、Referer、缓存策略、内容类型和显式高熵选择。注册正式 CSRF/signin 虽也有完整 Referer 和 no-cache，但不能因复用新端点函数就自动获得目标登录 profile 的高熵或 Priority 变更。

### 5. 动态关联与安全凭据分别维持生产、校验、附加边界

`_attach_auth_rum_headers()` 继续每个 Auth fetch 创建独立 trace/span/invocation；同一请求的 Datadog 与 W3C 标识关系一致，document-navigation ID 读取当前会话值。继续保留 `follow_authorize()` 既有“取得 HTTP 响应后轮换、无响应异常不轮换”的时机，不在字段 helper 内轮换 navigation ID。不缓存含动态关联字段的完整模板。

Sentinel 的 challenge 请求、SDK solve 仍在 `request_sentinel_token()` 中进行，`build_sentinel_header()` 保留对 flow、device、challenge、proof 和 SO 的校验。新增小型附加 helper 接受校验后的主 token 与条件性 SO token，不发网络请求、不求解、不生成凭据，也不捕获校验错误后降级发送。password 与 MFA verify 调用统一 helper，issue_challenge 根本不附加凭据而非先加再 pop。

备选：让头函数自动请求 Sentinel 或缓存完整 Auth 头，会隐藏副作用、重用旧凭据并打乱业务请求顺序，拒绝采用。

### 6. 传输字段和 Cookie 不进入静态场景模板

Host、Content-Length、Connection、Accept-Encoding 留给现有 URL、body 与传输后端能力；本次不添加 Cookie 字符串模板，不复制捕获的 Cookie 值。仍由同一 Cookie Jar 处理域、路径、重定向响应和更新；SDK 所需 Cookie 输入来源也保持不变。

保持 Wreq 逐请求 `default_headers=False` 的既有隔离，不通过启用浏览器默认头补齐字段。实际 wire 是否额外出现某头需要观测，不将“源码未构造”写成“最终绝不发送”。

### 7. 验证使用消费者合同和隔离传输观测

优先扩展 `test_session_navigation_headers.py`、`test_chatgpt_auth_context.py`、`test_openai_auth_registration_flow.py`、`test_account_liveness.py`、`test_wreq_session.py` 等已有路径；测试实际返回头或实际捕获请求，不检查 helper 名称、源码文本、内部调用次数等实现细节。

- 用固定画像和合成非敏感输入覆盖九条对应请求的字段集合、值来源及缺失状态，额外覆盖 profile 从 full-web 入口贯穿至 MFA 的完整调用链。
- 验证 CSRF 响应到 signin form、两次 Referer 相同且解码输入正确；query/body 与原合同不变。
- 验证启用高熵时缺失画像失败、合法空模型有效，关闭时不混入高熵；交错构造 STANDARD/目标场景不互相污染。
- 验证多次 Auth fetch 的 trace/span/invocation 更新、navigation ID 稳定与既有轮换条件；通过受控随机值避免概率性断言。
- 验证主 token 与 flow/device/challenge 绑定、SO required/非 required、issue 无凭据、Sentinel fetch 无 Auth/RUM 污染。
- 非目标注册、导出、Codex、bootstrap/commerce 选取实际受影响合同，证明仍保留各自语义。目标登录原有“保留旧差异”的断言按新合同替换；仅锁实现细节的测试删除，不改成锁新写法。
- 使用合成数据通过原 Wreq 适配器向隔离本地测试接收端（测试端口 `5100`）发送代表性 fetch、导航及重定向，观察实际头集合和计算后的 Content-Length；不携带真实 Cookie/token，不发起真实账号认证。该证据仅证明本地传输，不证明目标站接受、TLS 指纹一致或完整浏览器行为。

所有 Python 命令必须在项目本地虚拟环境内执行；先完成相关实现再一次性运行针对性检查。不新增通用抓包平台；临时验证脚本用后删除，保留有消费者价值的确定性回归用例。

## Risks / Trade-offs

- [共享步骤污染非目标流程] → profile 默认 STANDARD，入口明确选取并逐层传递；注册内已有账号密码验证也纳入兼容性验证。
- [删除全局开关导致依赖旧隐式注入的调用遗漏] → 实现时枚举实际引用与场景，迁移 Codex 对基础头的直接调用，删除配置导出前完成全部消费者切换；不声称支持旧全局开关的自定义外部用法。
- [严格画像校验暴露不完整测试桩或画像] → 复用既有画像构建结果，测试桩按消费者需要补全；不为让测试通过而增加字段兜底或绕过失败。
- [重定向时下层更改头部] → 以本地两跳接收证明区分构造与 wire，不能用首次请求字典冒充落页证据。
- [捕获以 403 结束，头部一致仍不能保证登录成功] → 验收以指定字段、来源与隔离为准；不调整重试、Sentinel 算法或失败判断来掩盖结果。
- [当前行为文档与历史捕获混淆] → 登录分析、请求明细及注册对照文档只更新源码/当前行为章节，捕获值和“无法判断”边界保留，并同步有效行号。

## Migration Plan

1. 按本设计冻结目标/非目标场景合同和全部实际消费者清单；确认可用画像具备六个高熵字段。
2. 实现字段组与场景入口，随后在同一变更中迁移认证、查活、注册、导出、Codex、bootstrap/commerce 等调用；仅替换真正被取代的入口。
3. 打通显式 profile 与正式登录 Referer/密码局部快照传递，删除调用点重复字段、旧高熵实现及全局开关定义/导出；保留独立场景的行为。
4. 更新合同测试并完成针对性回归与本地 wire smoke；同步现有文档当前行为章节，删除临时脚本。
5. 作为一个完整切换交付，不支持新旧入口混用。需要回滚时整体回滚本次代码、配置导出、调用点及对应文档，不通过重新引入别名或开关热修复。
