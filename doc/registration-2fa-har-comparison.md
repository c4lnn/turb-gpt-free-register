# HAR、本地仓库与 OSS 注册流程分支对照

## 范围与证据约定

本文按流程分支对照 HAR 与本地的请求字段及来路，统一使用五列差异表。最新 HAR 是浏览器捕获参考，本地是主要对照对象，OSS 仅辅助解释构造。三份捕获独立，不拼接账号或 session。本地为当前工作区；OSS 为 `../turb-gpt-free-register-oss-20260928/` 固定快照，`协议/...` 相对于其中的 `vendor/gpt-register-tool/`。除 OSS 前缀和显式 Markdown 链接外，本文 `har/`、`core/`、`config/`、`main.py`、`doc/` 等路径均相对于仓库根目录，不相对于本文所在目录；本文落盘位置为 `doc/registration-2fa-har-comparison.md`。

- HAR A（最新）：当前文件为 `har/chatgpt.com.har`（原记录名 `har/落点明确-passwordless-at.har` 当前不存在），完整文件 568 entries，entry 开始时间覆盖 2026-11-12 09:32:02.730Z–09:33:37.079Z；条目数、起止时间及相关请求序号与原记录吻合。分支 A 从零基 `#4` 正式 CSRF（响应 `csrfToken` 被 `#5` signin 消费）开始，截止 `#280` callback 后 ChatGPT 首页 200；不使用其他捕获补接，不展开截止点之后的请求。
- HAR B：`har/11.1-密码注册-快路径设置2fa.har`，2026-11-01 16:22:53–16:24:26 UTC，815 entries。未捕获 providers、正式 CSRF 或 signin，亦无此前 authorize 导航；相关已知页面从 `#1 GET /create-account` 开始，不补写前段。注册/session 比较截止 `#471` session；同会话 TOTP 子分支从 `#592` MFA 状态查询至 `#733` 后续状态复查，均只用本捕获证据。
- HAR C：`har/11.4-passwordless2about-you.har`，339 entries，entry 开始时间覆盖 2026-11-03 21:38:33.814Z–21:40:32.992Z。正式主链从 `#63` CSRF → `#64` signin 开始，本文截止 `#305` OTP 验证响应的 `about_you` 状态；不以 A/B 补接 profile、callback、session 或 2FA。
- 正式 CSRF 之前的 trace、设备 warmup、providers、入口页面不作为主链节点，不逐项审计顺序、头或响应；只有必要字段来源才标为“辅助请求”引用。正式 CSRF 指 token 被随后 signin 消费的那次请求，不以“第二次 CSRF”统一编号。省略不代表这些流程一致或请求未发生。
- 只展开相关请求的字段差异和来源未明项，一致项合并说明；响应只保留证明字段来路与消费关系所需内容。
- 比较只对 HAR 实际捕获内容作结论；区分 HAR 未记录、源码未显式构造、条件性请求未触发和没有对应实现。HAR 未记录或源码未显式构造均不证明最终网络报文绝对不存在；静态头构造不等于最终 wire headers。
- 同端点重复捕获可合并并注明序号，合并不表示连续；共用字段只解释一次，不要求逐端点重复判定。
- Cookie、Bearer、Sentinel token、OTP、邮箱、密码、session ID、设备 ID、TOTP secret 和动态 query 值均不复述。响应只列与流程判断相关的字段。

## 指纹与来源判定口径

版本值允许不同，字段增减仍须逐请求核对，平台、品牌、语言等值差异也须记录。**值相同不等于来源一致。** HAR 能证明报文与内容匹配，不能直接证明浏览器的随机源、画像选择或 JS 内部算法；这些情况写“无法判断”。本地静态构造也不等于最终 wire headers。

## 分支 A：请求字段与来源对照（HAR A）

主链只使用 HAR A 零基 #4–#280，从 #4 正式 CSRF → #5 signin 至 callback 后首页 200；完整文件 568 entries。**到 about-you 的本地顺序按用户确认视为完整，不再审计顺序。** #237 的 about_you 是响应状态，不补写独立页面 GET。

**不展开：** 正式 CSRF 前的 mweb handoff/events、login_with GET、providers，以及后续 passwordless/send-otp、SDK/frame 下载与无匹配消费者的 challenge；仅在必要字段来路处标明辅助证据并引用序号。原先确认不补 login_with、providers、passwordless/send-otp 的决策不变。省略不代表请求一致、未发生或 token 未被消费。

### 指纹字段

覆盖 #4/#5/#6/#7/#128/#133/#137/#138/#236/#237/#270/#275/#279/#280；已逐条核对指纹头集合，共用结论不重复抄写。

| 请求 | 位置/字段 | HAR 值或来源证据 | 本地值及构造来源 | 结论 |
| --- | --- | --- | --- | --- |
| 上述请求 | 头 user-agent、sec-ch-ua、sec-ch-ua-mobile、sec-ch-ua-platform | Chrome 154 / Windows，mobile=?0；品牌为 Chromium、Google Chrome、GREASE；画像选择过程未记录 | wreq 固定 chrome152，align_browser_profile() 同步 UA/品牌版本并设 macOS，mobile 取会话画像。`core/openai/transport/session.py:201-235,529-555`、`core/openai/transport/protocol_profiles.py:23-46` | **字段集合对应；版本、平台、GREASE 品牌/版本/顺序不同。** HAR 的画像选择来源无法判断 |
| 上述请求 | 头：六项高熵 CH | sec-ch-ua-full-version、sec-ch-ua-full-version-list、sec-ch-ua-platform-version、sec-ch-ua-arch、sec-ch-ua-bitness、sec-ch-ua-model 均未记录 | 本链默认均不发；Auth opt-in 未使用，其他 builder 的普通高熵开关默认关。`core/openai/transport/session.py:529-555,683-773`、`config/browser.py:68-69` | **当前集合对应。** opt-in 能力不等于本链已发送 |
| 上述请求；Sentinel JS | 头 accept-language；JS language/languages/timezone | 头为 en-PH,en;q=0.9；navigator 与时区实际值、选择来源未直接记录 | 自动模式按预检出口国家选语言/时区，手动画像经校验后冻结；HTTP 与 SDK 共用会话画像。`core/openai/register/registration_geo.py:392-426,458-539`、`core/openai/transport/session.py:529-535`、`core/openai/auth/openai_auth.py:414-419`、`core/openai/auth/sentinel_runner.py:491-499` | **头存在，值取决于本地画像；来源无法与 HAR 判同。** 不能反推 HAR 也按出口国家选择 |
| #128/#236/#270 及消费者 | Sentinel p 的 JS 指纹环境 | HAR 有 SDK 请求与不透明 proof，不能据此直接确认内部 navigator/screen/硬件/WebGL 值 | Node VM 执行仓库 SDK，从会话画像传 UA、platform、语言、时区、screen、硬件、CH、WebGL 等；cookie 输入取 Auth CookieJar。`core/openai/auth/openai_auth.py:414-443`、`core/openai/auth/sentinel_runner.py:165-227,477-530` | **本地 HTTP/SDK 共用画像，不等于真实浏览器。** JS 值、可见 cookie 与传输指纹是否等价，无法由 HAR 判定 |

### 标识、Cookie 与凭据来路

“Auth JSON”指 #133 user/register、#237 email-otp/validate、#275 create_account。动态值只核对传递关系，不要求不同 session 字面相等。

| 请求 | 位置/字段 | HAR 值或来源证据 | 本地值及构造来源 | 结论 |
| --- | --- | --- | --- | --- |
| #6、三个 challenge、Auth JSON；辅助证据 #2 | query ext-oai-did/device_id；Cookie oai-did；body/凭据 id | #2【辅助请求，仅用于设备 ID 来源】query 与 #6 device_id 同值；后续 Cookie、challenge id、主 token id 匹配。初始设备 Cookie 已存在，首次生成未记录 | 注册初始 ID 为空；设备 warmup【辅助请求，仅说明设备 ID 来源】后从有效且唯一的 ChatGPT oai-did Cookie 绑定，复用于正式 CSRF/signin 的 Referer、authorize/SDK/凭据校验，不本地补种。`core/openai/transport/session.py:137-145,253-295`、`core/openai/register/registration_preflight.py:64-101`、`core/openai/auth/chatgpt_auth.py:21-53`、`core/openai/auth/openai_auth.py:409-425,454-479` | **复用关系对应；初始生产者无法判同。** HAR 未证明设备 ID 也来自同种 warmup；不展开预检流程 |
| #5/#6；辅助证据 #0/#2 | body authSessionLoggingId；query auth_session_logging_id | #0 handoff / #2 login_with【辅助请求，仅用于登录上下文来源】与 #5 signin / #6 authorize 内容相同；首次生成未记录 | 会话创建时 UUID，或复用传入值/seed；正式 CSRF/signin 的 Referer 与 signin query 使用同一会话值，authorize 缺失才补。`core/openai/transport/session.py:147-153`、`core/openai/auth/chatgpt_auth.py:21-29,44-51,150-159` | **复用对应；生成来源无法判断** |
| Auth JSON；参考 #88 | 头 x-openai-document-navigation-id | #88/#133 同值，#237/#275 同值，两组不同 | 会话 UUID，文档导航时轮换；OTP 成功进入 profile 只更新状态，不额外导航/轮换。`core/openai/transport/session.py:155-156,599-603`、`core/openai/auth/openai_auth.py:345-380,584-606`、`core/openai/register/registration_flow.py:446-473` | **复用分组对应；HAR 内部轮换触发点/生成算法无法判断** |
| Auth JSON | 头 x-access-flow-invocation-id | 三次各异；生成过程未记录 | 每次 Auth JSON 构造时新 UUID。`core/openai/transport/session.py:590-599` | **每请求不同的现象对应；随机源无法判断** |
| Auth JSON | 头 traceparent/tracestate 与四项 x-datadog-* | 三次 trace ID 各异；逐条换算确认 traceparent 的 trace/parent 对应 x-datadog-trace-id 与 x-datadog-parent-id；tracestate=dd=s:1;o:rum | 每请求新建 64-bit trace/parent，转十六进制拼 traceparent；x-datadog-origin=rum、x-datadog-sampling-priority=1，tracestate 固定拼装。`core/openai/transport/session.py:572-599` | **字段与派生关系对应；浏览器内部生成器/随机源无法判断** |
| Auth 请求；#5/#279/#280 | 头 cookie ← 响应 Set-Cookie | 认证 Cookie 匹配：#6→#7/#88、#88→#133、#133→#137、#137→#138/#237、#237→#275；#5 设置 state/PKCE Cookie，#279 清理；#279 两片 session Cookie 均由 #280 携带。初始 Cookie 来源不全 | 同一 CookieJar 接收响应并按目标使用，Auth/ChatGPT 不混作同域；callback 后另取 session，不读首页 token。`core/openai/transport/session.py:201-205`、`core/infra/wreq_session.py:83-94,121-174`、`core/openai/account/account_export.py:192-279` | **响应→后续 Cookie 的机制可对应；本地 HTTPS 验收确认测试 Cookie 传递和主机隔离，但真实站点实际值/发送集合未抓包，不宣称逐项一致** |
| #128→#133；#236→#237；#270→#275 | 头 openai-sentinel-token 内 c/id/flow/p/t | 三组 c 分别匹配 challenge 响应 token；flow 为 username_password_create/email_otp_validate/oauth_create_account，id 匹配设备；不是按相邻序号配对 | 同一 SDK 先 requirements，接收当前 challenge 再 solve；校验 c=响应 token、id=设备、flow=业务，proof 由 SDK 产生。`core/openai/auth/openai_auth.py:409-494`、`core/openai/auth/sentinel_runner.py:165-227` | **c/id/flow 来源与绑定对应；p/t 内部采集/求解无法由 HAR 证明一致** |
| #237/#275；#133 无此头 | 头 openai-sentinel-so-token | 两次有 SO；响应 so.required/collector_dx/snapshot_dx 只是元数据，未找到直接提供同一 SO 值的响应字段 | 同一 SDK solve 返回 SO，校验 c/id/flow；要求 SO 而缺失则失败。`core/openai/auth/openai_auth.py:443-494`、`core/openai/auth/sentinel_runner.py:190-227` | **本地来源明确，HAR 内部来源无法判断。** 元数据不是 SO token |

### 请求头、body、query 的其余差异

**一致项合并：** Auth JSON 使用 JSON Accept/Content-Type、auth 根 Origin/Referer、sec-fetch-dest/mode/site 分别为 empty/cors/same-origin、Priority=u=1, i；Sentinel 使用 Accept: */*、text/plain;charset=UTF-8、Sentinel Origin/带 sv 的 frame Referer，Fetch Metadata 同前；导航使用 HTML Accept、sec-fetch-dest=document、sec-fetch-mode=navigate、Upgrade-Insecure-Requests: 1、Priority=u=0, i。本地由 builder 常量及 URL/调用方 Referer 构造，报文值可对应，不证明浏览器内部实现同源。导航例外见下表。`core/openai/transport/session.py:562-570,652-773`。

| 请求 | 位置/字段 | HAR 值或来源证据 | 本地值及构造来源 | 结论 |
| --- | --- | --- | --- | --- |
| #4 GET chatgpt.com/api/auth/csrf | Referer/Content-Type/cache-control/pragma；响应 csrfToken | 完整 Referer=/auth/login_with?...，与 #5 完全相同；无请求 Content-Type，cache-control/pragma=no-cache；csrfToken 匹配 #5 form | 以当前邮箱、已绑定 device_id、会话 logging ID 构造完整 Referer，并显式交给正式 CSRF 和 signin；GET 无 query/body、移除 Content-Type、增加两个 no-cache 头，读取本次响应 token。设备 ID 的辅助来源见前表。`core/openai/auth/chatgpt_auth.py:21-30,88-115`、`core/openai/register/registration_flow.py:295-307` | **源码头构造与 token 传递对应。** HAR 初始设备 Cookie 的生成过程未捕获；真实站点 wire 未实测 |
| #5 POST chatgpt.com/api/auth/signin/openai | form callbackUrl/csrfToken/json；query prompt/screen_hint/auth_session_logging_id/login_hint；Referer/缓存头 | callbackUrl=ChatGPT 根、json=true、token 来自 #4；邮箱/logging ID 延续入口；完整 Referer 与 #4 相同，两个禁缓存头存在 | form/query 字段与原值来源保留；token 来自正式 CSRF，邮箱来自注册输入/邮箱服务，logging ID 来自会话；显式共享 login_with Referer，未先 GET 页面；注册 POST 增加两个 no-cache 头，保留 form Content-Type/Origin，普通查活调用保持原策略。`core/openai/auth/chatgpt_auth.py:132-186`、`core/openai/register/registration_flow.py:295-307`、`core/openai/account/account_liveness.py:187-191` | **form/token 传递、头字段构造对应；Referer 取得经历不同。** HAR 的邮箱输入源及 prompt/screen_hint 生成代码无法判断，真实站点 wire 未实测 |
| #6 GET auth.openai.com/oauth/authorize | query：OAuth 参数、设备与登录上下文 | 完整 URL 匹配 #5 响应；含 client_id/scope/response_type/redirect_uri/audience/state/code_challenge/code_challenge_method、device_id/prompt/screen_hint/auth_session_logging_id/login_hint/ccaps | 以 signin 响应 URL 为底稿保留 OAuth 参数；强制 device_id=已绑定 Cookie，缺失才补 logging ID、邮箱、screen_hint、固定 ccaps，并删除旧上下文字段。`core/openai/auth/chatgpt_auth.py:32-53,178-185` | **服务端 URL 来路对应；本地存在改写/补值，不能称 URL 原样一致。** HAR 未证明前端也同样补写 |
| #7/#138 GET auth.openai.com/email-verification；#137 GET auth.openai.com/api/accounts/email-otp/send | Referer/sec-fetch-site/sec-fetch-user；URL | #7：ChatGPT 根/cross-site/无 user 头；#137/#138：auth 根/same-origin/?1。#137 URL 匹配 #133 continue_url，#138 匹配 #137 Location | 初次 authorize 关闭 user_initiated，根 Referer 可对应；send 使用服务端 continue_url，显式 auth 根 Referer、same-origin、user=?1；同一 Jar 自动跟随重定向，不增加页面请求或改变文档 ID 轮换时机。`core/openai/auth/openai_auth.py:345-380,584-606`、`core/openai/register/registration_flow.py:415-421`、`core/infra/wreq_session.py:150-174` | **URL 来路与导航头构造对应；本地 HTTPS 两跳接收端实测 Referer、same-origin、?1 及 Cookie 传递符合预期。** 未抓取真实站点 wire，不外推服务端接受差异 |
| #128/#236/#270 POST sentinel.openai.com/backend-api/sentinel/req | body p/id/flow；Referer 的 sv | text/plain 包装 JSON；id 对应设备，flow 对应上一表消费者；p 采集过程未直接记录，sv 指向捕获 SDK/frame 版本 | SDK requirements 产生 p，id 来自设备，flow 来自业务步骤；Referer 按配置 SENTINEL_SV 拼接，不先 GET frame。`core/openai/auth/openai_auth.py:409-443`、`core/openai/auth/sentinel.py:259-276`、`core/openai/transport/session.py:754-773`、`core/openai/auth/sentinel_runner.py:477-490` | **字段、设备/flow 关系对应；p 宿主环境不同，HAR 的 sv 选择来源无法判断** |
| #133 POST auth.openai.com/api/accounts/user/register | JSON username/password | 两字段；邮箱取得方式、密码生成器未记录 | 当前注册邮箱与 generate_registration_password() 结果；成功后保留密码。`core/openai/register/registration_flow.py:389-421`、`core/openai/auth/openai_auth.py:523-581` | **字段集合对应；输入生成来源无法与 HAR 判同** |
| #237 POST auth.openai.com/api/accounts/email-otp/validate | JSON code | 有验证码；读信/输入过程未记录 | 显式传入 otp_code，或 wait_for_otp() 从邮箱取当前验证码。`core/openai/register/registration_flow.py:152-176`、`core/openai/auth/openai_auth.py:627-690` | **字段集合对应；输入来源无法与 HAR 判同** |
| #275 POST auth.openai.com/api/accounts/create_account | JSON name/birthdate | 两字段；姓名/生日输入源未记录 | 调用方 name/birthday；CLI 姓名取配置、随机样本或输入，生日生成；协议入口没传生日也生成。`main.py:51-82`、`core/openai/register/registration_flow.py:252-300,463-473`、`core/openai/auth/openai_auth.py:693-743` | **字段集合对应；生成来源无法与 HAR 判同** |
| #279 GET chatgpt.com/api/auth/callback/openai；#280 GET chatgpt.com/ | query code/scope/state；Referer/sec-fetch-user；session Cookie/accessToken | #279 URL 匹配 #275 continue_url/payload.url，state 匹配 #5 返回 URL；两次 Referer=auth 根、无 Sec-Fetch-User；session Cookie 由 #279→#280，首页 HTML 有 accessToken | 新账号注册收尾使用服务端 continue_url，显式 auth 根 Referer、user_initiated=False；逐跳按目标重建导航头，auth 中间地址为 same-origin，ChatGPT callback/首页为 cross-site，保留同一 Jar 和 30 次跳转上限。已有账号及其他 callback 调用仍用原上下文/自动重定向。仍另 GET /api/auth/session【辅助请求】取 JSON accessToken/账号元数据，不解析首页 token。`core/openai/register/registration_flow.py:67-116,475-479`、`core/openai/account/account_export.py:192-279`、`core/openai/account/account_liveness.py:245` | **callback URL 来路及导航头对应；本地 HTTPS 验收覆盖直达 callback 和 auth→callback→首页，目标头及 Cookie 传递符合预期。** accessToken 取得位置仍不同；HAR 窗口无 session GET 或 Bearer 消费证据；真实站点未实测 |
| 以上请求 | accept-encoding/content-length/cookie；HTTP/2 :authority/:method/:path/:scheme | HAR 为实际报文，长度/Cookie/伪头随请求变化 | default_headers=False；未显式 Accept-Encoding，长度、Cookie 和协议字段由 wreq/Jar/序列化处理。`core/openai/transport/session.py:201-205`、`core/infra/wreq_session.py:83-94,121-174` | **真实站点最终 wire 未捕获，无法逐项判同。** 本地验收仅使用 wreq Chrome152、5100 端口 HTTPS 接收端和临时可信证书，覆盖正式 CSRF/signin 的 Referer/缓存/Content-Type、OTP 与 callback 重定向头及测试 Cookie、127.0.0.1→localhost 跨 origin/主机隔离；URL 仅做本地路由适配。未验证真实域名、HTTP/2 伪头或浏览器内部实现，未运行真实账号注册 |

### OSS：仅保留有用的字段构造参考

- 通用 Chromium builder 按非空画像值条件添加 full-version-list/platform-version/arch/bitness/model 五项高熵 CH，未显式 full-version；SDK 下载/challenge helper 独立构造头，不等于 HAR A 的低熵集合。`协议/auth_flow.py:1704-1787,1842-1879`、`协议/sentinel_quickjs.py:59-91,126-156`。
- dump、主动首页、重复 session 等辅助步骤不再展开流程审计，也不能用来证明 HAR 字段来源。callback/session 构造参考 `协议/auth_flow.py:3346-3433,3522-3625,3810-3945,4148-4198`。

## 分支 B：密码注册、OTP、profile、session 与注册后 TOTP（HAR B）

### 捕获边界与原记录的适用范围

HAR B 未捕获 providers、正式 CSRF、signin 或此前 authorize 导航，不能从 A/C 借用请求补齐。相关已知页面从 `#1 GET /create-account` 开始；本节注册/session 比较截止 `#471`，TOTP 子分支截止 `#733`。只展开本捕获中主 token 已按内容匹配到消费者的 challenge，未匹配者省略不代表不存在消费者。

本节保留既有 HAR B 记录中的字段差异与代码证据，不重新进行全流程审计。**B 节标为“原记录”的本地代码路径、行号来自此前模块布局（如 `core/openai_auth.py`、`core/session.py`、`core/account_export.py`）；它们是原证据索引，不是当前工作区可直接定位的源码行号，也不能自动外推为当前实现结论。** 当前布局下已核对的注册字段与源码索引见分支 A；不得因 A 的对齐修复覆盖 B 的捕获事实。OSS 路径仍按开头所述固定快照解释。

`#125` 的响应含 `passwordless_signup`，但本地原记录中 `/create-account` 作为已知 landing 会分类为 `new_password/password` 并跳过 probe；unknown landing 才根据 probe 分类。即使为 `new_passwordless`，原记录仍先尝试密码注册，明确拒绝后才回退登录 OTP，不能把 HAR payload mode 当作本地实际进入同名分支的证据。原记录：`core/openai_auth.py:937-976,1011-1035`、`main.py:446-464`。OSS 对已知 landing 也直接分类、未知才 fallback；其密码步骤可包含密码页 GET。`协议/auth_flow.py:1704-1786,2581-2790`。

### B1：注册、OTP、profile、callback 与 session 的字段差异

HAR B Auth JSON 共用头包括 JSON Accept/Content-Type、Origin、根路径 Referer、Priority、低熵 `sec-ch-ua` / `sec-ch-ua-mobile` / `sec-ch-ua-platform`、User-Agent、`Sec-Fetch-*`、W3C / Datadog trace、`x-access-flow-invocation-id`、`x-openai-document-navigation-id`；`#153/#257/#292` 未记录高熵 CH。共用头只在此列一次，不据此断言指纹值、随机源或本地最终 wire 一致。与 A 不同，本节原记录未建立浏览器画像选择、trace/导航 ID 生成算法及完整 Cookie 生产者的逐字段来源证据，均为**无法判断**；A 中的匹配关系不能移用于 B。

| 请求 | 位置/字段 | HAR 值或来源证据 | 本地值及构造来源 | 结论 |
| --- | --- | --- | --- | --- |
| #1 GET auth.openai.com/create-account | 导航与入口状态；Cookie 来源 | 200 页面；HTML 导航头、低熵 CH、Cookie 已存在；此前正式 CSRF/signin/authorize 未捕获 | 原记录通常跟随 signin 返回的 authorize URL，再分类 landing；unknown 才 probe，并非固定直访本 URL。`core/openai_auth.py:455-522,960-1035` | **捕获缺段，入口及初始 Cookie 来源不完整。** #1 是已捕获的相关页面，不补写此前请求 |
| #122 → #125 POST /api/accounts/authorize/continue | Sentinel req body p/id/flow → openai-sentinel-token | #122 flow=authorize_continue，响应主 token 按内容匹配 #125；challenge 的 so.required / SO 元数据不是 SO token | 原记录仅 unknown landing fallback 时按此 flow 获取 challenge。`core/openai_auth.py:525-572,1018-1035` | **消费关系仅属于 B；触发条件有差异。** 原记录对已知 /create-account 不发 probe；HAR 的 p 采集与随机源无法判断 |
| #125 POST auth.openai.com/api/accounts/authorize/continue | JSON username/screen_hint；Referer/Origin/SO；响应 mode | username 与 screen_hint=signup；根路径 Referer/Origin，未记录 SO；响应含 continue_url/method/page/oai-client-auth-session，page=email_otp_verification，mode=passwordless_signup | 原记录已知 landing 跳过；条件性 probe 使用 create-account Referer/Origin，consume_so=False 只附主 token。`core/openai_auth.py:937-976,1011-1035`、`core/session.py:816-832`、`main.py:446-464` | **Referer 与触发条件不同；mode 不能当作本地实际响应。** 邮箱输入及浏览器内部构造来源无法判断 |
| #150 → #153 POST /api/accounts/user/register | Sentinel req body p/id/flow → openai-sentinel-token | flow=username_password_create；#150 响应主 token 内容匹配 #153；#153 未记录 SO | 原记录密码分支按此 flow 获取 challenge，再交 register_user。`main.py:446-457`、`core/openai_auth.py:525-572,666-695` | **主 token 消费关系有内容证据，不能由相邻序号或 SO 元数据推导 SO 来源。** p/SDK 内部环境无法判同 |
| #153 POST auth.openai.com/api/accounts/user/register；HAR 未记录密码页 GET | JSON username/password；Referer；continue_url | 两字段；根路径 Referer；200 后 continue_url 指向 email-otp/send；未记录 GET /create-account/password | 原记录 new_password/new_passwordless 均先导航密码页，再 POST；POST 根路径 Referer；仅 new_passwordless 被明确拒绝才转登录 OTP。`main.py:429-464`、`core/openai_auth.py:641-695` | **body 字段及 POST Referer 对应；密码页 GET 是实现侧请求、非 HAR 已捕获节点。** 邮箱/密码生成来源未记录，缺项不证明该导航绝对未发生 |
| #157/#158 GET auth.openai.com/api/accounts/email-otp/send；#159 GET /email-verification | Referer/头集合；响应 Location | #157=307、#158=302，Location 链落到 #159 页面 200；三条均只记录 Referer、Upgrade-Insecure-Requests、User-Agent 及三项低熵 CH，Referer 为根路径；未记录 Cookie、Accept 或 Sentinel 头 | 原记录按服务端继续地址作文档导航，Referer 为 /create-account/password，再跟随跳转；不固定单独请求验证页面。`core/openai_auth.py:698-712` | **Referer 不同；HAR 只记录上述六项头。** 不把客户端补头当作捕获事实，也不由未记录推断 Cookie/状态不存在；重定向依赖同一会话 |
| #256 → #257 POST /api/accounts/email-otp/validate | Sentinel req body p/id/flow → openai-sentinel-token；SO 来源 | flow=email_otp_validate；#256 响应主 token 内容匹配 #257；#257 有 SO，但 challenge 响应未提供可直接关联的同一 SO token | 原记录 OTP 步骤获取该 flow 的主/SO 头并传入 validate。`main.py:209-225,510-512`、`core/openai_auth.py:525-572,733-795` | **主 token 来路明确；HAR 的 SO 内部来源无法判断。** 不把元数据或另一个 challenge 当作 SO 来源 |
| #257 POST auth.openai.com/api/accounts/email-otp/validate | JSON code；Referer；响应 page | code；根路径 Referer；主/SO 头；200 响应含 continue_url/method/page/oai-client-auth-session，page=about-you | 原记录 _validate_registration_otp() → validate_email_otp()，Referer 为根路径，flow=email_otp_validate；是否发送 SO 取决于该步获取结果。`main.py:208-225,510-512`、`core/openai_auth.py:733-795` | **字段与根路径 Referer 对应；读信/输入 code 的浏览器来源未记录。** 成功推进 profile 的事实不能外推到其他响应分支 |
| #285 → #292 POST /api/accounts/create_account | Sentinel req body p/id/flow → openai-sentinel-token；SO 来源 | flow=oauth_create_account；#285 响应主 token 内容匹配 #292；#292 有 SO，但无可直接匹配的响应 SO 值 | 原记录 profile 步骤按此 flow 获取 challenge 并传 create_account。`main.py:520-525`、`core/openai_auth.py:525-572,798-838` | **主 token 有捕获内消费证据；SO 内部来源、p 采集与求解无法判断** |
| #292 POST auth.openai.com/api/accounts/create_account | JSON name/birthdate；Referer；continue_url | 两字段、根路径 Referer；200 响应含 continue_url/method/page/oai-client-auth-session | 原记录 create_account() 使用 profile body 与 oauth_create_account challenge，Referer 为 auth 根路径。`core/openai_auth.py:798-838` | **字段集合和 Referer 对应；姓名/生日生成来源未记录。** 动态 continue/callback 状态不跨捕获复用 |
| #294 GET chatgpt.com/api/auth/callback/openai | query/Referer/Cookie 与回调状态 | callback GET 302，HAR Referer 为根路径；捕获导航头与 Cookie，但本节无完整 state/PKCE 生产者链 | 原记录 follow_oauth_callback() 在 profile 阶段传入 about-you Referer。`core/account_export.py:192-221` | **Referer 不同；state/PKCE 及初始认证 Cookie 来源未在 B 前段捕获。** 不从 A/C 补接或仅凭同名 query 判来源一致 |
| #343 prepare → #401 finalize【辅助请求，callback 后 ChatGPT bootstrap】 | body p → prepare_token/proofofwork/turnstile；Bearer/设备头 | #343 响应含 persona/prepare_token/proofofwork/so/turnstile，#401 body 含 prepare_token/proofofwork/turnstile，响应含 token/过期字段；头含 Bearer、JSON、Cookie、chatgpt-account-id、oai-device-id/oai-did/oai-language、Origin/根 Referer、originator、Codex 与浏览器头 | 原记录 authenticated_bootstrap() 支持 prepare 与按响应可用字段 finalize；setup_2fa() 只有收到 access_token 才调用，而注册入口 _setup_registration_totp() 不传该参数，故不触发。`core/chatgpt_bootstrap.py:97-138,185-225`、`core/account_export.py:669-746`、`main.py:270-302` | **这是正式认证后的辅助状态传递差异，不是 CSRF 前 warmup，也不是 Auth Sentinel /req。** 不以 #471 后取到的 token 倒填 #343/#401 Bearer 的来源；原记录未建立该内容匹配 |
| #471 GET chatgpt.com/api/auth/session | x-openai-target-path/route；session 响应字段及 accessToken | 200 JSON 含 WARNING_BANNER/accessToken/account/authProvider/expires/marketingEligibility/sessionToken/statsigContext/user；GET 含缓存头、Cookie、浏览器头及 target-path/route | 原记录 callback 后 fetch_session() 读取 session；get_nextauth_headers() 未显式构造 target-path/route。`core/account_export.py:224-254`、`core/session.py:785-797,909-931` | **target-path/route 静态构造有差异；session 仅属于 B。** 实际 token 不输出，静态头不等于最终 wire |

**SDK 来路辅助证据（非逐端点审计）：** HAR B `#95/#233` 为 SDK alias 200，`#104/#121/#242/#255` 为版本化 SDK 条件 GET 304（If-Modified-Since / If-None-Match），`#120/#250` 为 frame 200；这些只说明捕获中的资源来路，304 没有新 SDK body，不能证明 JS 内部环境。原记录本地 runner 使用仓库内 SDK，不显式下载这些资源（`core/openai/auth/sentinel_runner.py:60-112`）；OSS 仅 cache miss 下载版本化 SDK，不显式使用 alias/frame，也不显式添加上述缓存校验头（`协议/sentinel_quickjs.py:42-156,159-220`）。不将资源 GET 插入注册主链。

**OSS 字段构造补充：** unknown landing 的 authorize_continue fallback 使用相同 body、Referer=/create-account，不带 SO，同源 builder 不显式加 Origin（`协议/auth_flow.py:1704-1786,2476-2504,2581-2693`）。register_password() 先 GET 密码页并轮换文档导航 ID，再提交 username/password，POST Referer 为密码页而非 HAR 根路径（`协议/auth_flow.py:2695-2790`）。send_otp() 使用 API 头且可带已保存的主/SO token，不对应 HAR B 仅记录六项头的页面导航；OTP/profile 的 Referer 分别为 /email-verification、/about-you，而非 HAR 根路径（`协议/auth_flow.py:2792-2821,3034-3182,3347-3410,3522-3624`）。challenge 的 flow 分别在相应步骤刷新，B 的 token 映射不能当作 OSS wire 证据。callback 按重定向链维护 Referer，session 在 callback/continue 链读取、特定 callback 分支可能再次读取，2FA hook 复用可用 access token（`协议/auth_flow.py:3346-3624,3810-4199`）。

### B2：注册后 TOTP 同会话快路径

本子分支只使用 HAR B：从 `#592` 的 mfa_enabled=false 开始，`#609` enroll、`#618` activate，`#619` 复查，再有 `#621` factor_decision；`#701/#733` 是更后的状态查询，比较截止 #733。重复序号合并不表示连续调用。

本地原记录只有 ENABLE_2FA=true 才进入注册后 2FA，默认关闭；已启用且有 secret 时恢复本地状态，已启用无 secret 时失败收尾；未启用/未知时先复用注册 session/access token 的 setup_2fa_inline()，失败且有密码才新建 BrowserSession 慢速重认证。缺少密码或持久化异常分别失败收尾。HAR 仅证明初始关闭、同会话 enroll/activate 成功，不覆盖配置关闭、已启用或慢速 fallback。原记录：`config/twofa.py:5-15`、`main.py:270-305,535-580`、`core/account_export.py:651-753`。

| 请求 | 位置/字段 | HAR 值或来源证据 | 本地值及构造来源 | 结论 |
| --- | --- | --- | --- | --- |
| #592/#619/#701/#733 GET /backend-api/accounts/mfa_info | Bearer/设备/客户端头；Content-Type；响应 MFA 状态 | #592=false，后三次=true；响应含 factors。Accept=*/*，有 Cookie、chatgpt-account-id、oai-device-id/oai-did/oai-language、originator、Codex 头、根 Referer、Priority/Fetch/低熵 CH/UA 等；未记录 Origin/Content-Type | 原记录初查及 activate 后复查；get_mfa_enabled() 设置 Bearer/设备/language，ChatGPT builder 显式加 JSON Content-Type、oai-client-build-number/version、oai-session-id、Datadog、target-path/route，未显式设 oai-did 请求头或 Codex-only 头。`main.py:535-580`、`core/openai_auth.py:1244-1255`、`core/account_export.py:573-666`、`core/session.py:748-814,909-931` | **头字段集合不同；Cookie oai-did 不能代替同名请求头证据。** 四个状态查询不等于本地 helper 固定调用次数；Bearer/设备的浏览器内部来源未由本节建立完整证据 |
| #609 POST /backend-api/accounts/mfa/enroll | JSON factor_type/source；Origin/设备头；响应 secret/session_id | factor_type=totp、source=settings；200 提供 email/factor/secret/session_id；头同上并有 JSON/Origin | 原记录 _enroll_totp() body 仅 factor_type=totp；ChatGPT 头加 Bearer，未显式设 HAR 的 Origin/oai-did/Codex-only 头，并有上行所述客户端/session/trace/target 头。`core/account_export.py:573-596`、`core/session.py:748-814` | **少 source=settings，头集合不同；Accept 同为 */*。** 后续激活依赖本次 enrollment，不跨捕获复用 secret/session_id |
| #618 POST /backend-api/accounts/mfa/user/activate_enrollment | JSON code/factor_type/session_id/source；Origin/设备头 | body 四项，source=settings；头同 #609；200 响应 success | 原记录 _activate_totp() body 保留前三项但无 source；未显式构造 HAR 的 Origin/oai-did/Codex-only 头。`core/account_export.py:599-626`、`core/session.py:748-814` | **少 source，头集合不同。** session_id/code 必须绑定同次 enroll；HAR 没有记录浏览器生成/输入 TOTP code 的内部过程 |
| #621 POST /backend-api/accounts/mfa/factor_decision | JSON entrypoint；响应 recommended_factors | 200；body entrypoint，响应 recommended_factors；Bearer/JSON/Cookie/设备/Codex/Origin/导航上下文头 | 原记录注册 TOTP helper 没有对应实现。`main.py:535-580`、`core/account_export.py:651-666` | **没有对应实现，不是 HAR 未记录或条件性未触发；不能以 mfa_info 查询替代** |

**OSS TOTP 构造差异：** `_make_headers()` 使用 common Accept=application/json，而 HAR 为 */*；GET 不设 Content-Type。该 helper 不传 target_url，未显式设 HAR 的同源 Origin/oai-device-id，也不显式构造 oai-did/Codex-only 头；同名 Cookie 不作头证据。enroll/activate body 分别仅 factor_type 及 code/factor_type/session_id，均无 source=settings；激活消费同次 enroll 的 session_id。factor_decision 没有对应实现。`协议/webui/two_factor.py:74-199`、`协议/auth_flow.py:1704-1786`。静态构造差异不证明真实站点接受，未建立的随机源/指纹/最终 wire 均无法判断。

## 分支 C：passwordless 至 about-you（HAR C）

本分支与 A/B 独立。完整捕获 339 entries（#0–#338），本文主链起点为 `#63 GET chatgpt.com/api/auth/csrf`，其响应 csrfToken 被 `#64 POST /api/auth/signin/openai` 消费；不是依据“第二次 CSRF”命名。比较截止 `#305 POST auth.openai.com/api/accounts/email-otp/validate` 的 200 响应，page.type=about_you、continue_url 指向 /about-you。截止点是响应状态，后续未记录文档导航，不能补写独立 GET /about-you，更不能接入 A/B 的 profile/callback/session/2FA。`#338` 是捕获末条事件请求，不是本文主链截止点。

这里只补充独立捕获边界与正式 CSRF 消费证据，不展开新的全流程字段审计；未列出的正式 CSRF 后请求不因此被判为一致或未发生。前置 providers、入口页面、设备 warmup 统一排除；必要设备来路的实现说明仅见 A 的辅助证据，不外推到 C 的浏览器生产者。

| 请求 | 位置/字段 | HAR 值或来源证据 | 本地值及构造来源 | 结论 |
| --- | --- | --- | --- | --- |
| #63 CSRF → #64 signin | 响应 csrfToken → form csrfToken；callbackUrl/json | 两次均 200；#64 form 字段为 callbackUrl/csrfToken/json，token 内容匹配 #63 响应 | 当前正式 CSRF 读取响应 token，signin form 消费该 token。`core/openai/auth/chatgpt_auth.py:88-115,132-186` | **本捕获正式主链起点明确。** 仅证明 token 来路，不由字段同名推出所有头/query/设备来源一致 |
| #64 signin → #65 GET auth.openai.com/oauth/authorize | OAuth 导航边界 | 捕获 #65 authorize 302；动态 OAuth query 不复述 | 当前以 signin 响应 URL 为底稿，并存在设备/登录上下文改写与缺项补值。`core/openai/auth/chatgpt_auth.py:32-53,178-185` | **只记录已捕获节点；本节未建立 C 的逐字段 OAuth 内容匹配，不外推 A 的匹配结果** |
| #305 POST /api/accounts/email-otp/validate | 响应 page.type/continue_url | 200；page.type=about_you，continue_url 路径为 /about-you；不是页面 GET | 当前 OTP 验证后依响应更新 profile 状态。`core/openai/register/registration_flow.py:446-473` | **本文截止于 about-you 状态；不补写后续页面、profile 或会话完成链** |

## 分支层面的结论与证据限制

- **HAR A：** 从 #4 正式 CSRF → #5 signin 开始，到 about-you 的本地顺序按用户确认视为完整；字段、集合与来路见 A 的五列表。原记录的本地 HTTPS 接收端验收涵盖正式 CSRF/signin、OTP send 与 callback 的目标头和测试 Cookie；这不是本次文档编辑运行的验证，也不等于真实站点 wire、HTTP/2 伪头或服务端接受证明。已知差异仍有 Windows/Chrome 154 与本地 macOS/Chrome 152 画像、authorize URL 改写、Referer 所表示页面的取得经历及 accessToken 来源。
- **HAR B：** 正式 CSRF/signin 段未捕获，相关页面从 #1 create-account 开始；原记录的 landing/probe/passwordless 分类与同会话 TOTP 快路径差异保持独立。旧代码索引的时效边界见 B 节，不把其构造直接宣称为当前最终 wire。#343/#401 仅作 callback 后 bootstrap 状态传递辅助证据，不作为 CSRF 前 warmup 审计。
- **HAR C：** #63 CSRF → #64 signin 的 token 内容匹配已在本捕获中建立，截止 #305 about_you 响应；不把 A/B 的后续请求拼接过来。
- HAR 未记录、源码未显式构造、条件性请求未触发、没有对应实现与无法判断是不同结论。未展开不代表一致或未发生；值相同不证明生成来源一致。各分支未记录的画像选择、随机源、JS 内部环境与输入过程不补写。
- 固定 OSS 快照的构造细节见 [doc/registration-2fa-oss-comparison.md](registration-2fa-oss-comparison.md)（链接相对于本文目录，显示路径相对于仓库根）。本文不包含真实账号 smoke。

**敏感数据提示：** 本地注册日志可能记录密码候选与 TOTP secret；HAR 和日志均按凭据材料保护。
