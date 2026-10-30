# 协议注册流程差异分析（本地 vs OSS 快照）

> 对比对象：本地 `turb-gpt-free-register`（当前 `main`）vs
> `turb-gpt-free-register-oss-20260928`（2026-09-28 快照）。
> 只覆盖「代理预检 → 密码注册 → 设置 2FA」这一条纯协议注册链路，逐请求对比。
> 两端最终产物一致：带登录密码 + TOTP secret 的账号。

## 0. 对齐决策（层次 2，2026-10-30 定）

> 目标：本地协议注册链路在**机制**上对齐 OSS 快照（OSS 是稳定基线），
> **架构**保持本地原生化（`openai_auth.py` 直接编排，不引入 vendor 子进程）。
> 原则：机制对齐 OSS；字段采用现网证据与任务冻结画像，不照搬过期字段或固定语言样本。

```
层次1  架构级：连 vendored 子进程一起搬回来          <- 未选
层次2  机制级：架构本地化，机制按 OSS 重写           <- 已选
层次3  点状：  只改点名的点，其余保留本地            <- 未选
```

**已确认对齐的机制**：

| # | 机制 | 对齐目标 | 说明 |
|---|---|---|---|
| 1 | oai-did 来源 | 服务端种（warmup + 读 cookie） | 已实施：注册不再本地补种；独立 SDK SID 与非注册复用策略保持原语义 |
| 2 | sentinel | 真实 SDK 的 requirements + solve；实际宿主为 Node `node:vm` | 已切换为同 VM 双阶段、按需单 flow，SID 由 SDK 自产；本轮真实 challenge 与新号注册均已通过，见 4.5.5 |
| 3 | 2FA | 后续对齐注册同步快路径（直接 enroll） | 不在本轮；注册同步与独立补设仍保持既有重认证编排 |

**依赖顺序**：

```
地域硬门禁 -> csrf warmup(服务端 oai-did) -> SDK 单 flow -> 2FA 快路径（后续独立变更）
```

**边界决策与实施状态**（见第 8 节末尾 6/7 两条）：

- ① 注册流程请求序列：**providers 与 about-you 独立 GET 已移除**；两种 OTP 分流的受控观测保留，真实新号已通过密码／OTP／资料／最终会话与既有同步 2FA。Sentinel 按需单 flow，signup 仍为现有单路密码注册；本次成功不扩展为所有出口成功率结论。
- ② 请求头已按 4.7 实施：signin 四参数且无 Origin，authorize 使用 device_id 并清除多余字段；auth/backend 设备头、动态语言、trace、逐请求 invocation ID 与文档级 navigation ID 已分别观测，未全局删改独立消费者字段。

## 1. 结论摘要

这不是「流程细节差异」，而是**两次架构分叉**：

- **OSS 快照**：纯协议注册跑在 vendored 子进程引擎里 ——
  `register_protocol_engine.py` → 子进程 `vendor/gpt-register-tool/协议/_turb_bridge.py`
  → `auth_flow.py AuthFlow.run_register()`。
- **本地**：协议注册已原生化，由 `main.py run_registration()` 直接编排
  `core/openai_auth.py`（密码注册各步骤）+ `core/account_export.py setup_2fa()`（2FA）。

三段差异实质：

| 段 | OSS 快照 | 本地 |
|---|---|---|
| 代理预检 | `cloudflare trace` 观测出口 + `csrf` 种 oai-did | 已对齐两阶段及 cookie 门禁；原生会话，复用现有路由 |
| 密码注册 | 三分支（新号 / passwordless / 已有账号） | 纯新号密码注册（强杀 passwordless） |
| 设置 2FA | 复用 access_token 直接 enroll（零重认证） | 每次重认证邮箱 OTP 后再 enroll |

2FA 差异最大，详见第 5 节。

## 2. 对比基准与范围

- OSS 快照：`vendor/gpt-register-tool/协议/` 下的 `auth_flow.py`、`webui/two_factor.py`、
  `webui/registrar.py`、`_turb_bridge.py`。
- 本地：`core/openai_auth.py`、`core/account_export.py`、`core/twofa_service.py`、
  `core/chatgpt_auth.py`、`core/chatgpt_bootstrap.py`、`main.py`。
- 本地 `register_protocol_engine.py` / `vendor/` 目录**已不存在**（顶层无 `vendor/`）。
- 本轮涉及 `core/session.py`、`core/wreq_session.py`、`core/protocol_profiles.py` 与 `requirements.txt`：注册固定 `wreq==0.13.0/chrome152`，独立消费者仍按共享配置选择后端；实际运行环境 Python 3.12.15／Node v22.19.0，部署要求 Python 3.11+。

## 3. 架构分叉

```
OSS 快照 (2026-09-28)
main.py ──> register_protocol_engine.register()
                └─> 子进程 vendor/.../协议/_turb_bridge.py
                        └─> auth_flow.py AuthFlow.run_register()
                                ├─> register_password()   密码注册
                                └─> bind_totp_2fa_inline() 2FA(快路径)

本地 (当前 main)
main.py run_registration()  直接编排
        ├─> core/openai_auth.py      密码注册各步骤
        ├─> core/account_export.py   setup_2fa()(重认证式)
        ├─> core/twofa_service.py    后台补设队列(走 setup_2fa)
        └─> core/chatgpt_bootstrap.py 匿名/登录态预热
```

## 4. 代理预检逐请求对比

| # | 本地 | OSS 快照 |
|---|---|---|
| 0 | `BrowserSession(server_device_id=True)` 建立注册专用 `wreq/chrome152` 与选定路由；HTTP／SDK 版本一致，平台保持 macOS | `_pick_clean_proxy()` 属 bridge 独立预筛，本地明确不移植 |
| 1 | 初始 GET trace 确认有效 IP／受支持国家，自动或手动选择完整冻结画像；warmup 内部重试不重复 trace | `check_proxy()` → GET `cloudflare.com/cdn-cgi/trace` |
| 2 | GET `chatgpt.com/api/auth/csrf`：navigation 转 XHR、priority u=0，无额外 Referer／Content-Type；按严格有效设备 cookie 通过，成功会话直接复用 | `warmup()` → 同一 CSRF，按 cookie 判定；本地不退化为只看键名 |
| 3 | `anonymous_bootstrap()`（可选）：GET `backend-anon/accounts/check` + `ces/v1/projects/oai/settings` + `backend-anon/me` + `backend-anon/checkout_pricing_config/configs/{cc}` | （无此阶段） |

**2026-10-30 实施边界**：

- 初始 trace 必须成功且提供有效 IP 与受支持国家；不由外部 GeoIP 源、缓存或手动国家补足未知结果，不默认日本／英语 UTC。HTTP／网络／解析／信息不足分别分类，不总括为代理损坏。注册不再访问 ipinfo／ipapi／ipwho 聚合取证，也不依赖它们的 IP 或 timezone；独立消费者保留原探测路径。
- 自动模式按实际国家映射完整语言／时区；显式手动模式允许另一已知画像国家覆盖，实际国家、画像国家和来源分开保留。多时区国家确定一次本国映射；AU／CA 使用 en-AU／en-CA 及本国时区。所选画像自身语言／时区不一致仍拒绝。
- HTTP 200 不是通过条件：必须存在对 `https://chatgpt.com/` 根路径有效、未过期、非空且唯一的服务端 `oai-did`。错误域、过期、歧义及只有 CF cookie 均不通过；请求抛错前已收到有效 cookie 可以通过。
- 注册入口显式传 `BrowserSession(server_device_id=True)`，设备值初始化为空；成功后才绑定 `session.device_id`。不按 seed 是否存在猜测注册模式。
- 本轮注册编排为 `anonymous_bootstrap（可选） → get_csrf_token → signin`，已移除 providers，不新增 account-picker。密码页导航与原 bootstrap 保留；signin/authorize 字段和 Sentinel 机制的本轮变更分别见 4.7、4.6。
- 只对齐初始化语义，不移植 OSS bridge 的独立探净出口架构，不为后续 `follow_authorize` 新增换会话/出口行为。独立 Codex 等默认会话仍沿用旧登录页预检及补种。

## 4.5 oai-did 来源与 id 一致性（已实施）

注册预检通过后，HTTP cookie、现有设备 query/header、Sentinel 请求 body 的 `id` 与最终保存的 `device_id` 均读取服务端设备值 X。不再向 ChatGPT/Auth/Sentinel 三域批量写本地设备或语言 cookie，导出 cookie 时也不合成缺失的设备 cookie。

| 消费者 | 本次注册值 | 是否改字段发送规则 |
|---|---|---|
| signin/authorize 中既有设备参数 | `session.device_id = X` | 4.7 所列字段修正已实施；设备仍来自成功的服务端初始化 |
| 既有设备标识头 | 同一个 X | 否 |
| `sentinel/req` body `id` | 同一个 X | 设备来源不变；单 flow 与 SDK 切换另见 4.6 |
| 账号持久化 `device_id` | 同一个 X | 否，不迁移历史账号 |

### 4.5.1 已确认：任务内复用会话，跨任务不要求连续

**会话边界按独立任务划分，而不是按单个功能划分。** 同一注册任务内，注册与同步设置 2FA 共享成功初始化的会话，沿用服务端设备值 X、cookie 和冻结画像；不因进入 2FA 功能而新建空白会话或改用另一设备标识。复用会话不取消服务端要求的重认证，本次不调整 2FA 快慢路径编排。

后续独立查活、补设 2FA、查套餐、Codex 授权等任务使用各自的会话生命周期，不要求恢复注册时的完整上下文或保持同一设备 ID；仍可使用已保存的账号凭据，并保持各任务内部的会话连续性。它们按各自既有显式值、持久状态或 seed 规则得到设备值 Y，X 与 Y 相同或不同均合法，不强制复用，也不强制更换设备。

这是已确认的预期边界，不是跨任务“同设备”连续性缺陷或待决策问题。本次不新增跨任务会话持久化/恢复机制，不迁移或覆盖历史设备数据，也不扩展其他组合任务（含已有 Codex 授权分支）的会话策略；不宣称所有任务使用全局统一设备身份。

初始化失败时仍按 4.5.2 重建干净传输会话；这属于身份建立前的有限重试，不授权成功后的注册与同步 2FA 随意更换会话。`sentinel_sid` / `sentinel_iframe_sid` 是独立 SDK 实例标识，不是另一份 `oai-did`；切换设备 cookie 来源不等于删除 SID，本次保持其原有生成和复用语义。

### 4.5.2 初始化重试与资源边界

| 配置 | 默认值 | 适用范围 |
|---|---|---|
| `OPENAI_REGISTRATION_WARMUP_ATTEMPTS` | 20 次（含首次） | 仅注册初始化，总上限不乘通用网络重试 |
| `OPENAI_REGISTRATION_WARMUP_TIMEOUT` | 40 秒 | 每次注册 csrf warmup |
| `OPENAI_PREFLIGHT_TIMEOUT` | 12 秒 | 保持非注册旧预检含义 |

注册参数通过现有 `.env` 覆盖机制读取。trace 单次超时 15 秒，仅在首次 warmup 前确认国家；设备重试不重复 trace。首轮 warmup 立即执行，第 i 次重试等待 `3 + 2*i` 秒（i 从 1 起），等待可被任务停止打断；次数、超时与退避未扩大。

失败尝试之间重建底层传输与空 CookieJar，保持冻结语言／时区、`wreq/chrome152` 及应用诊断 ID；SDK SID 由各次 SDK 交互自行管理。池内存在其它候选才换，显式代理保持原路由、不转直连。重建后 `exit_geo_observation_scope=previous_transport_trace`，无新 trace 不声称当前候选国家已实时确认；额外观测为已知国家仅诊断，明确未知仍拒绝。成功 warmup 的传输、cookie 和设备直接用于正式注册，无第二层探针、假邮箱 POST 或业务失败后重选。构造、重建失败及退出均释放已拥有资源。

**早先设备初始化 smoke（真实网络）**：2026-10-30 曾观察 `trace 200 → csrf 200` 及设备 cookie 绑定。该记录属于本轮地域硬门禁/SDK 切换前的实施阶段，不证明现在的现网 challenge 或完整注册通过；当前结果见 4.5.5。

### 4.5.3 集中行为验证

命令：`.venv/Scripts/python.exe -m unittest tests.test_session_navigation_headers tests.test_registration_preflight tests.test_openai_auth_registration_flow tests.test_protocol_registration_compatibility tests.test_chatgpt_auth_context tests.test_account_liveness tests.test_codex_retry_service`。

该阶段历史结果：**71 tests，OK**，覆盖服务端设备门禁、cookie/传输隔离、停止传播和独立复用边界。当前完整回归及严格地域门禁的证据见 4.5.5，不将旧回归或受控底层响应视为真实注册闭环。

### 4.5.4 服务端设备切换时的历史新号验收（2026-10-30）

使用当前邮箱服务领取一个新邮箱、现有代理池和原生协议链路执行，未启动或修改生产端口。2FA 按当前配置启用；敏感日志在临时验收进程内禁用，仅输出阶段、HTTP 状态及相等性结果，未记录邮箱、密码、OTP、token、设备值或代理凭据。

**端到端通过**：注册初始化前 8 轮 csrf 返回 403，第 9 轮返回 200 并获得有效服务端设备 cookie；每轮 trace 均为 200。成功绑定后继续原 bootstrap/providers/csrf/signin 链路，密码页、`user/register`、OTP send/validate、`create_account`、`/api/auth/session` 均返回 200。注册同步 2FA 仍按原重认证编排执行，`mfa/enroll` 与 `activate_enrollment` 均返回 200。

| 运行时检查 | 观测结果 |
|---|---|
| 初始设备未绑定、独立 SDK SID 保留 | true |
| 服务端设备 cookie = 注册 `session.device_id` | true |
| 初始化重试前后冻结画像不变 | true |
| 实际发送设备 query = X | 6 次，全部相等 |
| 实际发送设备 header = X | 21 次，全部相等 |
| 实际发送 Sentinel body `id` = X | 5 次，全部相等 |
| 保存入口设备值 = 当时 cookie = X | true |
| 实际落库设备值 = 保存的 `extra.device_id` | true |
| 最终账号已保存，access token / TOTP secret 存在 | 全部 true |
| 注册函数最终 `success` | true |

临时 runner 只观察真实请求和保存入口，没有替换网络响应、OTP、账号创建或 2FA。运行耗时约 266 秒；随后读取新落库账号核对设备字段及 token/TOTP 的存在性，未输出具体值。临时 runner 已删除；没有留下专用验收凭据文件。该记录仅证明本次样本，不承诺任意出口首轮通过，也不代表请求头/Sentinel/2FA 的后续机制对齐已实施。

### 4.5.5 本轮传输／国家画像／设备与完整注册验收

- 当前注册路径仅依据本会话 trace 的有效 IP 与受支持国家，不读旧 `_GEO_CACHE` 或注册多源聚合。自动／手动画像完整映射并冻结，实际国家与 `profile_country`、`locale_source` 分开保留；手动 JP 可覆盖实际 VN，未知实际国家即使手动 JP 也拒绝。同国轮换与后续另一已知国家仅更新观测，不重建画像；设备 warmup 门槛不变。
- 错误类别为 `proxy`、`geo_service`、`geo_parse`、`geo_incomplete`、`geo_mismatch`，保留阶段进入任务结果；最后一项仅用于画像自身真实冲突。独立查活、Codex、补设 2FA 不自动获得注册国家门禁。
- **国家策略修订阶段（历史）**：当时隔离运行完整入口 533 项通过；5100 曾验证未知国家＋手动 JP 拒绝，以及 200／403 无设备停止。本机 JP 路由当时 trace 200、CSRF 403 无设备，另一次候选预检在外部期限内未完成。这些历史结果不被新成功覆写，也不作为本轮通过依据。
- **新组合真实网络**：自动 VN 和实际 VN／手动 JP 两条独立会话均取得 trace 200、CSRF 200 与有效服务端设备，随后真实 SDK requirements、challenge HTTP 200、solve 与主 token 上下文检查通过；无意外认证请求。HTTP 和 SDK 均关闭，未借用其它会话设备。
- **跨层实际观测**：Node VM 的 UA、brands、uaFullVersion 与 fullVersionList 均为 152；平台仍为 MacIntel／macOS。自动 VN 使用 vi-VN／Asia/Ho_Chi_Minh，手动 JP 使用 ja-JP／Asia/Tokyo；冬夏日期偏移分别为 -420／-420 与 -540／-540。实际国家记录均保持 VN，不被手动画像回写。
- **完整真实新号**：使用当前自动邮箱服务与生产池模式执行一条原生密码注册，初始化、signin、密码提交、OTP 验证、资料创建、最终 `/api/auth/session` 均为 200，账号已保存且 access token 存在。启用中的同步 2FA 沿用原重认证编排，enroll 与 activate_enrollment 均为 200，TOTP secret 存在。三个实际 SDK 宿主及 HTTP 会话全部关闭；无 providers／account-picker／about-you 独立 GET。仅记录阶段和存在性，不记录邮箱、密码、OTP、设备值或 token。
- **重试与 cookie 边界**：受控无设备耗尽实际观察 trace 1／warmup 3／传输 3，全部关闭，观测范围为 previous_transport_trace；没有用受控响应冒充真实设备通过。真实 wreq loopback 回归覆盖重定向 host-only cookie 的来源域、根路径、有效期、发送及删除，修复原生 Jar 不暴露来源域造成的元数据丢失；严格域／路径／过期／歧义规则不放宽。
- **本轮 5100 实际表面**：浏览器实际点击四次注册并查看最终页面。受控依赖缺失在创建 HTTP 客户端前失败；未知国家＋手动 JP 为 failed／geo_incomplete／validate（trace 1、warmup 0）；受控 VN／手动 JP 的 200 无设备停止（trace 1、warmup 1）。第四项使用真实路由取得有效设备并推进到初始化后人工停止点；后三项传输全部关闭，四项均零认证 POST。前三项为受控边界，第四项不是完整注册成功；完整闭环由上一项单列证明，生产 5000 未改。
- **本轮完整隔离回归**：原 `unittest.defaultTestLoader.discover("tests")`／`TextTestRunner` 实际执行 533 项，全部通过、0 skipped；虽然与历史计数相同，使用的是本轮新运行结果。包含独立消费者、SDK／OTP 分流、停止／耗尽与资源边界；不以版本字段复制快照作为通过依据。
- **首轮实施证据（历史）**：
  - 实际 Node/SDK 的受控双阶段、JP/VN/AU/CA 语言和当前日期时区、不同交互 SID 隔离均通过；另以实际 VM 验证 AU/CA/DE/VN 冬夏偏移和逐日期长名。取消、超时、SDK 接口失配与协议异常的实际宿主均退出并关闭管道，错误未回显原始消息。
  - 受控出站运行实际密码注册与两种 OTP 后续分流：直接回调仅申请 `username_password_create`；资料分支再申请 `oauth_create_account`，无 providers/account-picker/about-you GET，保留密码页导航与资料 Referer。以上使用隔离上游响应，不是现网新号成功。
  - 旧池 50 项中选中项地域探测报 `proxy/detect`，其余 49 项到 `chatgpt.com:443` 的 SOCKS CONNECT 返回 code 2；本机代理当时因旧地域规则报 `geo_mismatch/validate`，不是当前策略的拒绝条件。
  - 更新池的首轮 100 项预筛中，71 项因跨来源 IP 或 VN／Asia/Bangkok 冲突被旧规则拒绝，29 项曾过旧地域门禁但 CSRF 403 challenge 且无有效设备。本轮已取消前一类注册强一致条件；后一类设备阻塞不因取消地域聚合而自动解决。该历史预筛不代表本次重新扫描全部池。

## 4.6 Sentinel 生成机制（已切换，真实双阶段与新号验收通过）

| 机制 | 本地当前 | OSS 基线 |
|---|---|---|
| requirements 与 solve | `core/sentinel_runner.py` 启动 `sentinel/sentinel-runner.js`，同一 `node:vm` 实际运行本地 SDK 的采集和求解 | `sentinel_quickjs.py` / `openai_sentinel_quickjs.js` 实际也使用 Node `node:vm`；QuickJS 是路径名称，不是已验证执行引擎 |
| HTTP challenge | Python 当前 `BrowserSession` 使用 SDK 的 request p 发起，响应送回同一宿主 | 按业务步骤申请对应 challenge |
| 身份与生命周期 | body `id` 来自服务端 oai-did；SDK 自己生成 SID，各单 flow 一个短生命周期宿主，无跨任务 token/storage 复用 | 服务端设备与 SDK SID 分开 |
| 结果检查 | 主 token 非空且绑定当前 challenge/设备/flow；PoW proof、SO 是否必需均读当前 challenge | 不把固定 flow 当作永久 SO 豁免 |

不再从注册入口调用手工 requirements、password bundle 或旧 runner 回退。仍用于匿名 bootstrap 的手工模块及独立业务能力保留，不扩大本轮切换边界。服务端明确无需 PoW 时 SDK 可以返回空 p，但主 token 与上下文绑定仍必须有效；不能把空 p 误判为空主 token。Date 长名按实际日期通过运行时 Intl 计算，不复用冻结当天的夏令时名称。受控双分流、真实服务端 challenge 与一条新号完整注册分别取证，见 4.5.5；成功样本不证明所有出口或未来 SDK 版本均可用。

## 4.7 2026-10-30 真机抓包定论（9 月 OSS 注释 vs 现网）

> 方法：Chrome 156 + CDP `Network` 域监听真实注册全程（signin → user/register →
> passwordless/send-otp → email-otp/validate → create_account → mfa/enroll → …），
> 只记 `method/url/headers`，不记 body，共 791 个请求。出口为数据中心 IP，但
> header 形态不受出口类型影响，字段结论有效。
>
> 对照基准：OSS 快照（2026-09-28）代码注释里的 9 月抓包记录
> （9-09 `chatgpt_auth.py` / 9-12 `roxy_registration.py` / 9-21 `auth_flow.py`）。

### 4.7.1 字段演进对照（9 月 OSS 注释 → 10-30 现网）

| 字段 | 9 月 OSS 注释 | 10-30 现网 | 变化 |
|---|---|---|---|
| signin `ext-passkey-client-capabilities` | 不带（9-21）／带 `11111`（9-12） | 不带 | 9-12 过时结论 |
| signin `ext-oai-did` | 带（9-21 逐字对照） | **不带** | ⚠️ 10-30 删掉 |
| signin `screen_hint` | `login_or_signup`（9-21）／`signup`（9-12） | `login_or_signup` | 9-12 过时结论 |
| signin `origin` 头 | 不带（9-21） | 不带 | ✅ 一致 |
| authorize `device_id` | 补 `ext-oai-did`（9-21） | **`device_id=`** | ⚠️ 字段名换 |
| authorize `ccaps` | 带 `login_methods chatgpt_login_finalizer_v1`（9-09/9-21） | 带 | ✅ 一致 |
| authorize `auth_return_target_category` | 带 `chatgpt_home`（9-09/9-21） | **不带** | ⚠️ 10-30 删掉 |
| authorize PKCE | state + 可选 challenge（9-21） | state + code_challenge | ✅ 一致 |
| auth `x-openai-document-navigation-id` | 带，3 个 POST（9-21） | 带，4 个 POST | ✅ 一致 |
| auth `oai-device-id` | auth API 从不带（9-21） | 从不带 | ✅ 一致 |
| auth `x-access-flow-invocation-id` | 每次新 uuid4（9-21） | 每次新 uuid4 | ✅ 一致 |

**核心**：9-21 与 10-30 高度一致，仅 3 处 10-30 又变 —— signin 删 `ext-oai-did`、
authorize 用 `device_id=` 取代 `ext-oai-did=`、authorize 删 `auth_return_target_category`。
9-12（`roxy_registration.py`）的 `ext-passkey=11111` + `screen_hint=signup` 是过时结论。

`x-openai-document-navigation-id` 轮换语义（现网实证）：

- 每个**整页文档导航**铸一个新 uuid4，同一 document 内多个 POST 共用。
- `passwordless/send-otp` + `user/register` = 同值（`167312ca-…`，注册页 document）
- `email-otp/validate` + `create_account` = 同值（`2918bc27-…`，OTP 验证页 document）

### 4.7.2 本地字段修正（已实施，出站观测、真实新号及共享消费者回归通过）

- signin 只构造 `prompt/screen_hint/auth_session_logging_id/login_hint`，不主动发送 Origin。
- authorize 使用当前设备的 `device_id`，清除本地多余的 `ext-oai-did/ui_locales/auth_return_target_category`，保留 state、PKCE、redirect_uri 与 fragment。
- 现有 auth 头不含 backend 专用 `oai-device-id`；同一 document 复用 navigation ID，每次请求更新 invocation ID 与 Datadog trace。
- 2026-10-30 一次性受控出站观测运行实际 `signin_openai` 与现有 `BrowserSession` 头部构造器，上述断言全部通过，仅输出字段名称与布尔结果。HTTP 返回由观测器提供，不是一次真实注册，也不替代后续共享消费者及端到端验收。
- 后续真实新号与共享消费者回归亦已通过，见 4.5.5；保留上项受控字段观测的证据层级，不将其改称浏览器 body 抓包。

### 4.7.3 现网请求头全形态（供对齐参考）

**chatgpt.com/backend-api**（登录后 mfa/enroll、conversation/init、sentinel prepare/finalize）：

- `oai-did` + `OAI-Device-Id`（同值，两个都发）
- `OAI-Language`（zh-CN/en）
- `Authorization: Bearer …`、`ChatGPT-Account-Id`
- `x-openai-codex-window-type` / `x-openai-rate-limit-join-workspace` / `x-openai-web-frontend` / `originator`
- **无 `oai-session-id`**、**无 `origin`**

**auth.openai.com**（user/register、email-otp/validate、create_account、passwordless/send-otp）：

- `traceparent` + `tracestate` + `x-datadog-trace-id` + `x-datadog-parent-id` + `x-datadog-origin` + `x-datadog-sampling-priority`
- `x-access-flow-invocation-id`（每次新 uuid4）
- `x-openai-document-navigation-id`
- `openai-sentinel-token` + `openai-sentinel-so-token`
- `Referer: https://auth.openai.com/`、`accept` + `content-type: application/json`
- **无 `origin`**（同源）、**无 `oai-device-id`**

## 5. 密码注册逐请求对比

| # | 本地 `run_registration` | OSS `AuthFlow.run_register`（新号分支） |
|---|---|---|
| 1 | 已移除注册 providers GET；不新增 account-picker | （无此步） |
| 2 | GET `chatgpt.com/api/auth/csrf` | GET `chatgpt.com/api/auth/csrf`（`get_csrf_token`） |
| 3 | POST `chatgpt.com/api/auth/signin/openai`：prompt/screen_hint/auth_session_logging_id/login_hint，未主动添加 Origin | 同路径；快照参数不是现网字段基线 |
| 4 | GET `auth.openai.com/.../authorize?...`（`follow_authorize`，403 重试） | 同左（`auth_oauth_init`，**记录落地 URL**） |
| 5 | —（当前无 signup 三态判定；本阶段保持现有单路密码注册，后续单独对齐分流） | `signup()` 按落地 URL 判定 新号/已有/passwordless（**不发 POST**） |
| 6 | GET `auth.openai.com/create-account/password` | 同左（`register_password` 内，建页面状态） |
| 7 | 密码提交当场申请 `username_password_create`；真实 SDK 双阶段及本轮新号密码提交已通过 | 按业务步骤单 flow 现算；既有部分 HAR 不能证明完整密码时序 |
| 8 | POST `auth.openai.com/api/accounts/user/register` | 同左（**成功后立即 `_on_password` 落盘密码**） |
| 9 | GET `auth.openai.com/api/accounts/email-otp/send` | 同左（`send_otp`） |
| 10 | [可选] POST `sentinel/req`（email_otp_validate，按开关） | 无独立调用（合并进 verify 内现算） |
| 11 | POST `auth.openai.com/api/accounts/email-otp/validate` | 同左（`verify_otp`，**强制 email_otp_validate 的 SO token**） |
| 12 | 已移除独立 GET about-you；资料分支直接申请下一 flow | （无独立 GET，直接 create_account） |
| 13 | POST `sentinel/req`（oauth_create_account） | 同左（`create_account` 内） |
| 14 | POST `auth.openai.com/api/accounts/create_account` | 同左 |
| 15 | GET continue_url → OAuth callback → GET `chatgpt.com/api/auth/session` | `follow_redirect_chain` → GET `chatgpt.com/api/auth/callback/openai` → GET `chatgpt.com/api/auth/session` |

**注册段差异**：

- 本地已删除注册 `get_providers` 与 `about-you` 独立 GET；匿名/登录态 bootstrap 仍保留。资料 POST 与 OTP 后直接回调/补全资料判断也保留，两个分流的受控出站观测已通过。
- OSS **多了** `signup()` 三分支判定（新号/passwordless/已有账号）、passwordless 发码
  （`passwordless/send-otp`）、已有账号登录（`password/verify`、`mfa-challenge`、`mfa/verify`）、
  workspace/choose-account 选择、`client_auth_session_dump`、OAuth refresh_token 交换、
  Codex consent——本地注册链**全砍掉了这些**，只保留纯新号密码注册。
- 密码落盘时机：OSS 在 `user/register` 200 后**立即**回调落盘；本地在主流程末尾
  `save_account_data` 才落库。

**providers 决策（2026-10-30，已实施）**：

- 注册已删除 `GET chatgpt.com/api/auth/providers`，不新增 account-picker 替代；与现有浏览器抓包和 OSS 默认注册链路的该步骤边界对齐。
- OSS `vendor/gpt-register-tool/协议/auth_flow.py:3828-3831` 直接执行 `get_csrf_token → get_auth_url → auth_oauth_init`，没有 providers 请求；第 1940 行的 providers 只是 warmup 注释中的候选接口，不是实际请求步骤，默认 warmup 使用 csrf。
- `main.run_registration` 已不再调用 providers；共享 `get_providers` 仍由查活使用，未盲删。第 4.5.4 节包含 providers 的记录仅是早先设备初始化切换时的历史成功样本。
- 此结论仅针对注册链路不再调用该接口，不意味着 providers 接口已被服务端移除；不据此改变 bootstrap 编排。signup 分流与 about-you 导航的独立决策见下。

**注册分流阶段决策（2026-10-30，已确认）**：

- 本阶段保持当前单路密码注册：跟随 authorize 得到落地 URL，已在密码页则直接继续，否则使用当前会话主动进入 `/create-account/password`；不在本阶段引入 OSS 的 signup 三态识别、兜底探测及已有账号登录链路。
- 普通新号、passwordless 新号、已有账号的状态识别与分流留待后续单独对齐；这是一项阶段范围决策，不表示已支持全部状态，也不改变当前导航或注册失败的处理。

**about-you 导航决策（2026-10-30，已实施）**：

- 已删除 OTP 后、创建账号前额外的 `GET /about-you` 与无引用导航 helper；保留 `oauth_create_account` 申请及提交姓名生日的 `POST /api/accounts/create_account`。资料分支顺序为 `email-otp/validate → oauth_create_account Sentinel → create_account`。
- 没有真实整页导航时不因阶段名称改为 about-you 而轮换 document ID；受控观测确认 OTP 验证与资料提交复用该 ID。密码页与发码后的验证页导航仍按实际文档边界更新。
- `create_account()` 自行设置 `Referer: https://auth.openai.com/about-you`，不依赖独立 GET 的返回值；OSS `vendor/gpt-register-tool/协议/auth_flow.py:3122-3162` 同样使用该 Referer，先获取 Sentinel 再直接提交资料，没有独立资料页 GET。
- 保留 OTP 响应的后续状态判断：可直接 OAuth 回调时继续跳过资料提交，需要补全资料时才进入创建账号路径；删除独立 GET 不等于删除这些判断，也不影响前面的密码页导航。
- 删除后的两种分流已通过行为回归与受控出站 smoke；本轮完整真实新号走资料分支并已成功。部分 HAR 不覆盖完整创建账号阶段，不能将本次程序运行证据改称为旧 HAR 已抓到完整时序。

**sentinel flow 决策与证据边界（2026-10-30）**：

- 本轮按 OSS 的按需单 flow 机制实施：密码提交时才申请 `username_password_create`，资料分支才申请 `oauth_create_account`；不预取 password bundle，OTP 继续尊重现有开关，不新增 authorize_continue 注册步骤。
- 已有 `captures/register-20261030.har` 共 874 条请求；本次脱敏核验确认两条 `sentinel/req` 的 body 分别含字符串 flow：`authorize_continue`、`email_otp_validate`。未覆盖 `user/register`、`create_account` 或 `email-otp/validate` 业务提交，不能据此推断完整密码注册时序。
- “每个 body 的 flow 是字符串”也不能单独证明没有多次预取：旧 password bundle 同样由多个单 flow 请求组成。移除预取是本轮已选策略与 OSS 机制对齐，不冒充完整浏览器 body 抓包结论。

| flow | 使用阶段 | 页面上下文 |
|---|---|---|
| `username_password_create` | 密码提交时当场申请 | `create-account/password` |
| `email_otp_validate` | 现有 OTP 开关启用时申请 | `email-verification` |
| `authorize_continue` | 现有其它消费者保留，本轮不新增注册调用 | `email-verification` |
| `oauth_create_account` | 仅 OTP 后需要补全资料时申请 | `about-you` Referer；不新增独立 GET |

## 6. 设置 2FA 对比（差异最大）

### 6.1 请求序列

| # | 本地 `setup_2fa`（重认证式） | OSS `bind_totp_2fa_inline`（快路径，默认） |
|---|---|---|
| 1 | GET `chatgpt.com/api/auth/csrf` | GET `chatgpt.com/backend-api/accounts/mfa_info`（查已绑） |
| 2 | POST `signin/openai`（callbackUrl=`?action=enable&factor=totp`） | POST `.../accounts/mfa/enroll` |
| 3 | GET `auth.openai.com` authorize | POST `.../accounts/mfa/user/activate_enrollment` |
| 4 | GET `auth.openai.com/api/accounts/email-otp/send`（显式触发） | GET `.../accounts/mfa_info`（复核） |
| 5 | POST `.../email-otp/validate`（**再收一次邮箱 OTP 重认证**） | |
| 6 | GET `.../authorize/continue`（换新 token） | |
| 7 | POST `chatgpt.com/backend-api/accounts/mfa/enroll` | （第 2 行） |
| 8 | POST `chatgpt.com/backend-api/accounts/mfa/user/activate_enrollment` | （第 3 行） |

两端 **enroll/activate 端点完全相同**（`chatgpt.com/backend-api/accounts/mfa/*`）。

### 6.2 recent_auth_required 论证

OSS `webui/two_factor.py:18-20` 用实测推翻了「注册会话直接 enroll 会 401 recent_auth_required」的推测：

> 桌面《2FA绑定实现方案.txt》【二】说"注册会话直接 enroll 会 401
> recent_auth_required、必须重走正式链"—— 这条【实测不成立】，见上面探针结果。
> 原文是推测不是实测。慢路径保留作兜底，但不再是唯一路。

快路径能走通的依据（`two_factor.py:214`）：

> 注册链本身几十秒前刚做完 OTP 验证 + create_account，服务端眼里这就是
> "最近认证过"，login_challenge 那套要求它已经满足了。

本地重认证的目的（`account_export.py:544`，`_exchange_new_token` docstring）：

> 此时 token 内嵌的 pwd_auth_time 是新鲜的，2FA enroll 才会接受。

本地用 OIDC `max_age=0` 强制重新认证（`account_export.py:418-419`）：

```
query = {
    "connection": "password",
    "login_hint": email,
    "reauth": "password",
    "max_age": "0",          # 强制重新认证，忽略现有会话
    "ext-oai-did": session.device_id,
}
```

`pwd_auth_time` 新鲜度 = 服务端 `recent_auth_required` 判定的依据。两端是同一约束的两种表述。

### 6.3 两种策略取舍

```
                    OSS (先快后慢)                    本地 (只走重认证)
                    ===============                   ====================
注册同步   快路径：复用 access_token 直接 enroll       也走重认证（冗余）
           6.2s / 零 PoW / 零邮件                     +1 封邮件 + ~数秒
后台补设   慢路径：新起 AuthFlow 重走 login 链        重认证（必须）
           40s + 1 次 PoW + 1 封邮件

         时间窗口敏感（注册后多久内"快"仍有效是隐性的）
         快路径失败自动回落慢路径
```

**关键结论**：本地 `setup_2fa` 的重认证之所以「必须」，是因为它同时服务**后台补设**场景
（`twofa_service._run_twofa` 里新建 `BrowserSession` + 传数据库旧 `access_token`），
那个场景不重认证 enroll 必 401 recent_auth_required；注册同步场景其实可以像 OSS 快路径
那样省掉重认证，但本地没做这个分流，统一走了重认证。

**另一个落地障碍**：本地 `setup_2fa` 的 `access_token` 参数只做「可选预热」
（`authenticated_bootstrap`），预热完**照样走 `_trigger_reauth`**，不跳过重认证。
而 `main.py` 阶段 7 调用 `setup_2fa(session, email)` 时明明已经拿到了新鲜 `access_token`
却未传入。所以「对齐 OSS」不是改一个参数，是要新增一条快路径。

### 6.4 对齐落点（供后续变更参考）

```
                    +----------------------------------------+
                    |  main.py 注册同步(access_token 新鲜)      |
                    +----------------------------------------+
                      快路径: _enroll_totp(session, at) + _activate_totp(...)
                      (account_export 里这两个函数本就独立可用)
                                |
                          401 recent_auth / 任何失败
                                v
                      慢路径: setup_2fa(session, email)  ← 现有重认证，当兜底

                    +----------------------------------------+
                    |  twofa_service 后台补设(旧 at + 新 session)|
                    +----------------------------------------+
                      setup_2fa(session, email, access_token=旧)  ← 保持不变
```

这与 OSS registrar 的「先快后慢」同构：

```
tinfo = bind_totp_2fa_inline(flow, at)      # 快
if not (tinfo and tinfo.get("secret")):
    tinfo = bind_totp_2fa(...)               # 慢兜底
```

## 7. 关键差异总结

| 事项 | 分类 | 本地当前实现／理由 | 状态、证据与限制 |
|---|---|---|---|
| 初始化主干 | 与 OSS 对齐 | trace → CSRF → 服务端设备，保持原生编排 | 已实施，4.5.5 两条真实初始化及新号闭环通过 |
| HTTP 后端／版本 | 与 OSS 成功组合对齐 | 注册专用 wreq/chrome152；HTTP／SDK 版本统一，平台不升级 | 已实施并实际安装／运行；独立消费者保留共享配置，不据小样本归因或承诺成功率 |
| CSRF warmup 头 | 与 OSS 对齐 | navigation 转 XHR、priority u=0，无额外 Referer／Content-Type | 已实施，真实出站观测通过；仅限设备 warmup，不做单字段 403 归因 |
| trace 时机 | 与 OSS 主干对齐 | 初始一次，设备重试不重复 | 已实施，受控耗尽观测通过；轮换后无新 trace 不声称国家已实时确认 |
| 正式注册前筛出口 | 本地原生保留／改进 | 用 warmup 筛选并直接复用成功会话，不移植 bridge 独立筛选 | 已实施，真实新号复用通过；减少重复初始化是状态差异，不是已测性能收益，不保证未来 Auth／OTP 可达 |
| cookie 接受条件 | 本地保留更严格校验 | 适用域／根路径、非空、未过期、无歧义；恢复真实 host-only 来源 | 已实施，真实 Jar 生命周期及拒绝行为通过；防假阳性，不声称提高服务器放行率 |
| 国家画像 | OSS 映射机制＋本地策略差异 | 允许已知国家手动覆盖，明确未知仍拒绝，不用 UTC／英语兜底 | 已实施，真实自动 VN／手动 JP 与受控未知拒绝通过；观测国家与画像国家分开 |
| SDK／单 flow | 与 OSS 对齐＋本地状态衔接改进 | 同一短生命周期 VM 承接 requirements/solve，设备与 SDK SID 分离 | 已实施，真实 challenge／新号通过；不将旧 HAR 的 body 缺失写成完整时序证据 |
| 认证字段／导航 | 现网规则与已选最小序列 | 保留必要 OAuth 上下文，删除 providers／独立 about-you GET | 已实施，受控双分流及真实资料分支通过；signup 三态、已有账号登录未扩展 |
| 自动安装／隐式回退、台账、假邮箱探针 | 明确未移植 | 显式部署依赖、失败可见，避免额外认证与第二层筛选 | 不保留注册旧实现兜底；独立 Codex／查活／补设 2FA 保持原配置及业务 |
| 注册分支／2FA 快路径 | 本轮未移植 | 保留单路新号密码注册和同步 2FA 原重认证 | 现有同步 2FA 真实通过；零重认证快路径仍属后续独立变更 |

## 8. 待追问理清的问题

1. **recent_auth 窗口边界** — 注册拿 at → enroll 之间的「最近认证」窗口到底多宽？
   OSS 只实测过 6.2s 内可行，无上限数据。是否需要先跑一次探针确认窗口？
2. **`authenticated_bootstrap` 与 2FA 的顺序** — 本地阶段 5/6 拿 at 后先
   `authenticated_bootstrap` 预热、再 `setup_2fa`，预热会拉长到 enroll 的间隔。
   快路径是否要插到 bootstrap **之前**？
3. **`mfa_info` 幂等检查** — OSS 快路径先 GET `mfa_info` 查已绑（secret 一次性下发，
   重复 enroll 会丢 secret）；本地 `_enroll_totp` 直接 enroll 没查。是否对齐补这一步？
4. **401 回落判定条件** — `recent_auth_required` 与其它业务 401 是否要区分 error code，
   决定「回落重认证」还是「直接判失败」？
5. **测试边界** — 现有 `tests/test_account_export_twofa.py` 只覆盖重认证路径，
   快路径 + 401 回落两条用例要补。
6. **对齐边界 ①：注册流程请求序列** — 当前阶段范围已确认：providers 与 about-you 独立 GET 移除，sentinel bundle 改为单 flow；signup 保持现有单路密码注册，三态识别与分流后续单独对齐。
   **providers：已移除注册调用**，不新增 account-picker；共享查活调用保留，见第 5 节。
   **about-you：已移除独立 GET**，保留资料提交与 OTP 后两种分流；受控双分流和本轮真实新号资料分支均通过，见 4.5.5。
   **sentinel bundle 已定：按需单 flow**，以 OSS 机制和本轮决策为依据；既有部分 HAR 不覆盖密码与资料提交，不能把它写成完整 flow 时序已抓包证实。实施与真实运行验收分别记录，不扩展已有账号登录或其它认证分支。
7. **对齐边界 ②：请求头** — oai-device-id 只发 chatgpt backend-api/anon、Origin
   同源不发、datadog trace 头，是否纳入本次层次 2 一起对齐？→ **已定：纳入**。
   2026-10-30 真机抓包定论见 4.7（9 月 OSS 注释 vs 现网）：`ext-passkey-client-capabilities`
   现网已移除、`x-openai-document-navigation-id` 9 月注释已带、现网确认（本地均已对齐）；
   signin 四参数与无 Origin、authorize device_id 及多余字段清理已实施，受控出站字段观测通过；真实新号及共享消费者验收另记，见 4.7.2。
