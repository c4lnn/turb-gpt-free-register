# 登录密码与 TOTP SAZ：正式 CSRF 起的公共字段与请求差异

## 范围与展开规则

- 唯一输入：`saz/login-password-totp-403.saz`；本文捕获与源码路径均相对仓库根目录，`raw/` 指 SAZ 归档成员。主链从 #16 正式 CSRF → #17 signin 开始，截止 #93 MFA verify 的 403；保留 #16、#17、#18、#19、#34、#58、#84、#87、#93 共 9 条请求的字段明细，不推断成功回调、最终 session 或注册分支。概览见同目录 `login-password-totp-403-analysis.md`。
- #16 之前的入口、providers、trace / warmup 等不逐项展开，仅在解释正式请求字段来路时引用为辅助请求；省略不代表前置流程一致或请求未发生。辅助请求 #11 只证明 Referer 与入口 URL 的内容匹配，#8/#9/#10 只帮助定位早期 Cookie，#26/#28 只说明 SDK / frame 来源，#48（Cloudflare oneshot，动态路径省略）和 #89 只提供 Cookie 来源证据，不扩展为其他类别审计。
- Sentinel 仅展开有内容匹配消费者的 password_verify #34 → #58、verify_mfa #87 → #93。#89 / #98 未找到 token 内容匹配消费者，不展开其 challenge 或请求字段；#89 的 Set-Cookie 仅作为 #93 Cookie 的辅助来源，不据此推断 token 被消费或捕获外未消费。
- **完整字段＝公共组按所列顺序合并＋本条增补／覆盖**。同名字段以本条值为准；没有引用的组不适用，不从相似端点推断字段存在。`Cookie` 请求头由指定 Cookie 基线加本条值覆盖／新增组成。表格中未新增的基线项保持原值，不是删除。本文所有入选请求均无 Cookie 删除项。
- **相同字段名不代表相同值**。只把经核对相同的值放公共组；trace、invocation、proof、token 和变化的 Cookie 保留逐条占位符。名称集合相同与值相同分别判断。归类展示不保留原始头／Cookie 排列；字段、值和缺失状态不丢失，原序查 SAZ。query/form 解码后展示，不是可重放字节。
- `<类型-序号>` 为稳定脱敏标签，同值复用，不同值区分；编码前后仅在明确验证后称“解码后匹配”。凭据、邮箱、OTP、Cookie、CSRF、state/PKCE、动态标识不展示原文或摘要，不输出映射；opaque 证明整体占位。`""` 是空字符串，`null` 是 JSON null，“无”不是空值。Content-Length 是捕获原始字节数，不是脱敏文本长度。
- **来源边界**：响应字段／Set-Cookie 内容匹配不等于唯一生产者；同值关联不证明生成路径；本地赋值不证明浏览器同源或最终 wire headers。无证据即“无法判断”。Cookie 请求头不含 Domain/Path/HttpOnly 等存储属性，仅从实际匹配的响应引用。
- 全部 9 条主链请求未记录 `Priority`、`Authorization`、`oai-device-id`、`oai-language`、`openai-sentinel-so-token`、`x-csrf-token`。设备 ID 仍出现在 Cookie/query/body，CSRF 在表单。SAZ 未记录、源码未显式构造、条件分支未触发、没有对应实现与无法判断不能互换。
- HTTP 头画像、SDK JS 环境与 TLS/HTTP 传输后端指纹分别看待，不由字段同值断言等同浏览器。下文简称 `session.py`、`chatgpt_auth.py`、`openai_auth.py` 分别位于 `core/openai/transport/`、`core/openai/auth/`、`core/openai/auth/`，行号为本次离线所见版本。

## 公共请求头

各表中的字段均明确存在，字段名用小写归类；只有引用该组的请求继承它。

### B-通用

| 位置 | 字段／公共组 | 捕获值或引用 | 说明 |
|---|---|---|---|
| header | `connection` | `keep-alive` | 同值公共字段 |
| header | `sec-ch-ua` | `"Not-A.Brand";v="99", "Microsoft Edge";v="157", "Chromium";v="157"` | 同值公共字段 |
| header | `sec-ch-ua-mobile` | `?0` | 同值公共字段 |
| header | `sec-ch-ua-platform` | `"Windows"` | 同值公共字段 |
| header | `user-agent` | `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36 Edg/157.0.0.0` | 同值公共字段 |
| header | `accept-encoding` | `gzip, deflate, br, zstd` | 同值公共字段 |
| header | `accept-language` | `zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6` | 同值公共字段 |

**来源（按组）**：全部 9 条主链请求同值。UA／Client Hints 是 Windows x64 Edge/Chromium 157 HTTP 画像，画像选择、浏览器内部生成和网络栈协商无法判断；本地画像读取见 `core/openai/transport/session.py:529–555`。Connection 是捕获链路值，不能推断上游 HTTP/TLS 指纹。

### CH-高熵

| 位置 | 字段／公共组 | 捕获值或引用 | 说明 |
|---|---|---|---|
| header | `sec-ch-ua-full-version` | `"157.0.4341.46"` | 同值公共字段 |
| header | `sec-ch-ua-arch` | `"x86"` | 同值公共字段 |
| header | `sec-ch-ua-platform-version` | `"19.0.0"` | 同值公共字段 |
| header | `sec-ch-ua-model` | `""` | 同值公共字段 |
| header | `sec-ch-ua-bitness` | `"64"` | 同值公共字段 |
| header | `sec-ch-ua-full-version-list` | `"Not-A.Brand";v="99.0.0.0", "Microsoft Edge";v="157.0.4341.46", "Chromium";v="157.0.8103.39"` | 同值公共字段 |

**来源（按组）**：仅正式 CSRF / signin、OAuth 导航与密码／MFA 请求引用。Sentinel 不引用，不发送这六个字段；model 的 `""` 是已出现的空模型字符串。浏览器协商过程无法判断，本地 Auth 高熵开关见 `session.py:699–717`。

### D-ChatGPT

| 位置 | 字段／公共组 | 捕获值或引用 | 说明 |
|---|---|---|---|
| header | `host` | `chatgpt.com` | 同值公共字段 |

**来源（按组）**：Host 与目标 URL authority 相同；Cookie 组成见 K-ChatGPT，不把 Cookie 域作用范围从 Host 反推。

### D-Auth

| 位置 | 字段／公共组 | 捕获值或引用 | 说明 |
|---|---|---|---|
| header | `host` | `auth.openai.com` | 同值公共字段 |

**来源（按组）**：Host 与目标 URL authority 相同；Cookie 组成见 K-Auth。

### D-Sentinel

| 位置 | 字段／公共组 | 捕获值或引用 | 说明 |
|---|---|---|---|
| header | `host` | `sentinel.openai.com` | 同值公共字段 |

**来源（按组）**：Host 与目标 URL authority 相同；Cookie 组成见 K-Sentinel。

### N-导航

| 位置 | 字段／公共组 | 捕获值或引用 | 说明 |
|---|---|---|---|
| header | `upgrade-insecure-requests` | `1` | 同值公共字段 |
| header | `accept` | `text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7` | 同值公共字段 |
| header | `sec-fetch-mode` | `navigate` | 同值公共字段 |
| header | `sec-fetch-dest` | `document` | 同值公共字段 |

**来源（按组）**：#18 / #19 的 HTML 导航共同值；Sec-Fetch-Site 和 Sec-Fetch-User 不在此组中。#28 iframe 只作为 Sentinel Referer / 版本的辅助来源，不继承本组为主链请求。浏览器上下文判定无法完整还原，本地模板见 `session.py:719–752`。

### F-fetch

| 位置 | 字段／公共组 | 捕获值或引用 | 说明 |
|---|---|---|---|
| header | `accept` | `*/*` | 同值公共字段 |
| header | `sec-fetch-mode` | `cors` | 同值公共字段 |
| header | `sec-fetch-dest` | `empty` | 同值公共字段 |

**来源（按组）**：CORS fetch 的共同值。Accept 可被认证组或本条覆盖，不能假定所有 API 都 application/json；本地见 `session.py:652–697`。

### NA-NextAuth

| 位置 | 字段／公共组 | 捕获值或引用 | 说明 |
|---|---|---|---|
| header | `pragma` | `no-cache` | 同值公共字段 |
| header | `cache-control` | `no-cache` | 同值公共字段 |
| header | `sec-fetch-site` | `same-origin` | 同值公共字段 |
| header | `referer` | `https://chatgpt.com/auth/login_with?callback_path=/&screen_hint=login_or_signup&login_hint=<email-1>&auth_session_logging_id=<auth-session-1>&ext-oai-did=<device-id-1>` | 同值公共字段 |

**来源（按组）**：#16/#17 的禁缓存、同源和完整登录页 Referer。辅助请求 #11 的 URL 与该 Referer 相同，仅作内容匹配证据，不审计其请求本身。浏览器缓存选项来源无法判断；本地注册显式上下文分支可构造此类头，默认登录分支需单独判断，见 `chatgpt_auth.py:100–106、159–166`。Referer.query 按 Q-登录上下文加 callback_path/ext-oai-did 展开。

### A-认证JSON

| 位置 | 字段／公共组 | 捕获值或引用 | 说明 |
|---|---|---|---|
| header | `accept` | `application/json` | 同值公共字段 |
| header | `content-type` | `application/json` | 同值公共字段 |
| header | `origin` | `https://auth.openai.com` | 同值公共字段 |
| header | `referer` | `https://auth.openai.com/` | 同值公共字段 |
| header | `sec-fetch-site` | `same-origin` | 同值公共字段 |
| header | `x-openai-document-navigation-id` | `<navigation-id-1>` | 同值公共字段 |
| header | `x-datadog-sampling-priority` | `1` | 同值公共字段 |
| header | `x-datadog-origin` | `rum` | 同值公共字段 |
| header | `tracestate` | `dd=s:1;o:rum` | 同值公共字段 |

**来源（按组）**：#58/#84/#93 认证 fetch 共同值，#84 单独将 Accept 覆盖为 */*。三个请求 navigation ID 相同，RUM 采样标记相同；生成算法无法判断。本地见 `session.py:572–598、683–697` 和 `openai_auth.py:987–1040`。Origin 为 auth 源，Referer 仅为根 URL，不擅加密码/MFA 路径。

### T-SentinelPOST

| 位置 | 字段／公共组 | 捕获值或引用 | 说明 |
|---|---|---|---|
| header | `content-type` | `text/plain;charset=UTF-8` | 同值公共字段 |
| header | `origin` | `https://sentinel.openai.com` | 同值公共字段 |
| header | `referer` | `https://sentinel.openai.com/backend-api/sentinel/frame.html?sv=20260810913b` | 同值公共字段 |
| header | `sec-fetch-site` | `same-origin` | 同值公共字段 |

**来源（按组）**：Sentinel frame 中的同源 fetch，Content-Type 是 text/plain;charset=UTF-8，body 虽可解析为 JSON 也不改写类型。Referer 与 #28 frame URL 相同（query 只有 sv）；本地见 `session.py:754–773`。

## 公共 query 与复合凭据

### Q-登录上下文

| 位置 | 字段／公共组 | 捕获值或引用 | 说明 |
|---|---|---|---|
| query | `screen_hint` | `login_or_signup` | #17、#18 同值 |
| query | `login_hint` | `<email-1>` | #17、#18 同值 |
| query | `auth_session_logging_id` | `<auth-session-1>` | #17、#18 同值 |

**来源**：#17 的上述 query 与辅助请求 #11 URL 中字段同值，#18 全部 query 由 #17 响应 JSON url 携带；不能从同值确定浏览器读取路径。login_hint 是邮箱提示，用户输入／保存／handoff 来源无法判断；auth_session_logging_id 最初生成无法判断。本地见 `core/openai/auth/chatgpt_auth.py:21–29、150–157`。

### E-58 / E-93：openai-sentinel-token 的 JSON 组成

只在 #58/#93 出现，头值整体标签仍在本条表中；五个子字段如下，未把 challenge 的 dx 或 SO 元数据当成 token。

| 位置 | 字段／公共组 | 捕获值或引用 | 说明 |
|---|---|---|---|
| header-json | `p` | `<sentinel-proof-2>` | E-58 |
| header-json | `p` | `<sentinel-proof-3>` | E-93 |
| header-json | `t` | `<turnstile-token-1>` | E-58 |
| header-json | `t` | `<turnstile-token-2>` | E-93 |
| header-json | `c` | `<sentinel-token-1>` | E-58 |
| header-json | `c` | `<sentinel-token-2>` | E-93 |
| header-json | `id` | `<device-id-1>` | E-58 |
| header-json | `id` | `<device-id-1>` | E-93 |
| header-json | `flow` | `password_verify` | E-58 |
| header-json | `flow` | `verify_mfa` | E-93 |

**来源（按字段机制）**：`c` 分别匹配 #34/#87 响应 token，确认 password_verify 的 #34→#58、verify_mfa 的 #87→#93；`id` 与对应请求 id 及 oai-did 同值；`flow` 与对应请求一致。两组 challenge 的 proofofwork.required、turnstile.required 都为 true，`p` 与 requirements p 不同，`t` 非空但不直接等于 dx。SDK 求解算法无法判断；本地当前 SDK 请求／solve 见 `core/openai/auth/openai_auth.py:409–451`，消费校验见 `459–475`。

## 公共 Cookie 基线

值仅比较本次捕获，名称集合相同不意味着值不变。未列出响应匹配的 Cookie，其初始写入来源无法判断；捕获前已存在不等于由相邻响应生成。Cookie 的 Domain/Path 等属性只在匹配证据中说明。

### K-ChatGPT（正式请求 #16 / #17 共用）

| 位置 | 字段／公共组 | 捕获值或引用 | 说明 |
|---|---|---|---|
| cookie | `_cfuvid` | `<cookie-value-1>` | 基线 #11 |
| cookie | `cf_clearance` | `<cookie-value-2>` | 基线 #11 |
| cookie | `__cf_bm` | `<cookie-value-3>` | 基线 #11 |
| cookie | `oai-did` | `<device-id-1>` | 基线 #11 |
| cookie | `oai-did-first-seen` | `<cookie-value-4>` | 基线 #11 |
| cookie | `oai-mweb-route` | `<cookie-value-5>` | 基线 #11 |
| cookie | `oai-mweb-origin` | `<cookie-value-5>` | 基线 #11 |
| cookie | `__cflb` | `<cookie-value-6>` | 基线 #11 |
| cookie | `g_state` | `<cookie-value-7>` | 基线 #11 |
| cookie | `__Host-next-auth.csrf-token` | `<cookie-value-8>` | 基线 #11 |
| cookie | `__Secure-next-auth.callback-url` | `<cookie-value-9>` | 基线 #11 |
| cookie | `__Secure-next-auth.state` | `<cookie-value-10>` | 基线 #11 |
| cookie | `__Secure-next-auth.pkce.code_verifier` | `<cookie-value-11>` | 基线 #11 |
| cookie | `oai-sc` | `<cookie-value-12>` | 基线 #11 |
| cookie | `oai-asli` | `<auth-session-1>` | 基线 #11 |
| cookie | `oai-mweb-auth-pending` | `<cookie-value-13>` | 基线 #11 |

**来源（按组）**：本表沿用辅助请求 #11 中与正式 #16 / #17 同值的 Cookie 基线，仅服务正式请求字段核对，不展开前置请求审计。大多数 Cookie 在辅助请求 #8/#9/#10 中已同名同值，初始来源未知；`oai-mweb-auth-pending` 匹配辅助请求 #10 Set-Cookie（Domain=.chatgpt.com、Path=/、Secure、HttpOnly、SameSite=Lax）。`oai-did` 与辅助请求 #11 ext-oai-did、#18 device_id 和 Sentinel body id 同值，最初算法未知。`oai-asli` 与 auth_session_logging_id 同值，不由名称推断生成者。

CSRF Cookie URL 解码后按竖线分为两部分：

| 位置 | 字段／公共组 | 捕获值或引用 | 说明 |
|---|---|---|---|
| cookie-decoded | `__Host-next-auth.csrf-token.token` | `<csrf-token-1>` | #16/#17 共用；此前已存在 |
| cookie-decoded | `__Host-next-auth.csrf-token.check` | `<csrf-check-1>` | #16/#17 共用；此前已存在 |

第一部分匹配 #16 JSON csrfToken 及 #17 form csrfToken；校验部分算法未知。NextAuth state/PKCE Cookie 是 opaque 包装值，不能直接等同明文 state/verifier。

### K-Auth

| 位置 | 字段／公共组 | 捕获值或引用 | 说明 |
|---|---|---|---|
| cookie | `oaicom-stable-id` | `<cookie-value-14>` | 基线 #18 |
| cookie | `__oailb` | `<cookie-value-15>` | 基线 #18 |
| cookie | `__cflb` | `<cookie-value-16>` | 基线 #18 |
| cookie | `oai-login-csrf_dev_3772291445` | `<cookie-value-17>` | 基线 #18 |
| cookie | `oai-did` | `<device-id-1>` | 基线 #18 |
| cookie | `rg_context` | `<cookie-value-18>` | 基线 #18 |
| cookie | `iss_context` | `<cookie-value-19>` | 基线 #18 |
| cookie | `auth-session-minimized` | `<cookie-value-20>` | 基线 #18 |
| cookie | `auth-session-minimized-client-checksum` | `<cookie-value-21>` | 基线 #18 |
| cookie | `_cfuvid` | `<cookie-value-22>` | 基线 #18 |
| cookie | `cf_clearance` | `<cookie-value-23>` | 基线 #18 |
| cookie | `oai-sc` | `<cookie-value-24>` | 基线 #18 |
| cookie | `__cf_bm` | `<cookie-value-25>` | 基线 #18 |

**来源（按组）**：#18 的初始认证 Cookie 生产者未捕获；#18 响应更新登录 CSRF、auth-session-minimized 及 checksum，#19 使用更新值。MFA 请求新增 _dd_s，不能从名字断言其具体 JS 写入点。后续匹配和更改按本条列出；state/context Cookie 不证明 browser session 生成算法。

### K-Sentinel

| 位置 | 字段／公共组 | 捕获值或引用 | 说明 |
|---|---|---|---|
| cookie | `oaicom-stable-id` | `<cookie-value-14>` | 基线 #26 |
| cookie | `oai-did` | `<device-id-1>` | 基线 #26 |
| cookie | `__cflb` | `<cookie-value-29>` | 基线 #26 |
| cookie | `__cf_bm` | `<cookie-value-30>` | 基线 #26 |
| cookie | `oai-sc` | `<cookie-value-24>` | 基线 #26 |

**来源（按组）**：沿用辅助请求 #26 的 Cookie 基线以展示后续 #34/#87 的差异，不逐项展开 SDK 请求；除 #19 Set-Cookie 与后续请求内容匹配的 oaicom-stable-id / oai-did 外，其余初始写入无法判断。Sentinel 响应可继续设置 __cf_bm、oai-sc；各条匹配证据如下，不能把五个相同名称误称五个恒定值。

## 请求明细

每条只列公共组引用、差异、query/body 和复合字段引用；将组展开即可恢复本次脱敏字段全貌。traceparent 与 Datadog trace 低 64 位／parent 的数值关系已核对，但不证明随机源；逐请求 invocation、trace、parent 值不同，本地取值见 `session.py:572–598`。

## 请求 #16 — GET chatgpt.com/api/auth/csrf

证据：`raw/16_c.txt` / `raw/16_s.txt`；响应 `HTTP/1.1 200 OK`。

| 位置 | 字段／公共组 | 捕获值或引用 | 说明 |
|---|---|---|---|
| header-group | `引用` | `B-通用 + D-ChatGPT + CH-高熵 + F-fetch + NA-NextAuth` | 按组顺序合并，再应用下列覆盖 |
| query | `—` | `无` | 请求 URL 无 query |
| body | `$` | `无` | 原始 0 字节；无请求体 |
| cookie-group | `引用` | `K-ChatGPT` | 等于基线加下列值覆盖／新增 |

### 字段来源

- 完整 Referer 与辅助请求 #11 URL 相同，头来源按 NA 组；#17 额外的 Origin/Content-Type/Content-Length 对应表单提交，不据此比较前置入口或 providers。
- Cookie `oai-mweb-auth-pending` 的值匹配此前 辅助请求 #10 Set-Cookie；响应属性 `domain=.chatgpt.com；path=/；httponly=True；secure=True；samesite=Lax`。这是作用域与内容证据，不保证唯一写入者。

## 请求 #17 — POST chatgpt.com/api/auth/signin/openai

证据：`raw/17_c.txt` / `raw/17_s.txt`；响应 `HTTP/1.1 200 OK`。

| 位置 | 字段／公共组 | 捕获值或引用 | 说明 |
|---|---|---|---|
| header-group | `引用` | `B-通用 + D-ChatGPT + CH-高熵 + F-fetch + NA-NextAuth` | 按组顺序合并，再应用下列覆盖 |
| header | `content-length` | `125` | 本条增补／覆盖 |
| header | `content-type` | `application/x-www-form-urlencoded` | 本条增补／覆盖 |
| header | `origin` | `https://chatgpt.com` | 本条增补／覆盖 |
| query-group | `引用` | `Q-登录上下文` | 本条其他 query 如下 |
| query | `prompt` | `login` | URL 解码后 |
| body | `$` | `callbackUrl=https://chatgpt.com/&csrfToken=<csrf-token-1>&json=true` | 原始 125 字节；application/x-www-form-urlencoded |
| cookie-group | `引用` | `K-ChatGPT` | 等于基线加下列值覆盖／新增 |

### 字段来源

- 完整 Referer 与辅助请求 #11 URL 相同，头来源按 NA 组；与 #16 相比，#17 额外的 Origin/Content-Type/Content-Length 对应表单提交。
- body 中 csrfToken 与 #16 JSON csrfToken 匹配；callbackUrl 为固定根 URL，json 为字符串 true，不是 JSON 布尔。本地表单编码与参数见 `core/openai/auth/chatgpt_auth.py:150–172`。prompt=login 与入口上下文同值传递分开判断。
- Content-Length 与本次原始 body 字节数相符；脱敏结构不是原始序列化，不按占位文本重算长度。
- Cookie `oai-mweb-auth-pending` 的值匹配此前 辅助请求 #10 Set-Cookie；响应属性 `domain=.chatgpt.com；path=/；httponly=True；secure=True；samesite=Lax`。这是作用域与内容证据，不保证唯一写入者。

## 请求 #18 — GET auth.openai.com/oauth/authorize

证据：`raw/18_c.txt` / `raw/18_s.txt`；响应 `HTTP/1.1 302 Found`。

| 位置 | 字段／公共组 | 捕获值或引用 | 说明 |
|---|---|---|---|
| header-group | `引用` | `B-通用 + D-Auth + CH-高熵 + N-导航` | 按组顺序合并，再应用下列覆盖 |
| header | `sec-fetch-site` | `cross-site` | 本条增补／覆盖 |
| header | `referer` | `https://chatgpt.com/` | 本条增补／覆盖 |
| query-group | `引用` | `Q-登录上下文` | 本条其他 query 如下 |
| query | `client_id` | `app_X8zY6vW2pQ9tR3dE7nK1jL5gH` | URL 解码后 |
| query | `scope` | `openid email profile offline_access model.request model.read organization.read organization.write` | URL 解码后 |
| query | `response_type` | `code` | URL 解码后 |
| query | `redirect_uri` | `https://chatgpt.com/api/auth/callback/openai` | URL 解码后 |
| query | `audience` | `https://api.openai.com/v1` | URL 解码后 |
| query | `device_id` | `<device-id-1>` | URL 解码后 |
| query | `prompt` | `login` | URL 解码后 |
| query | `ccaps` | `login_methods chatgpt_login_finalizer_v1` | URL 解码后 |
| query | `state` | `<oauth-state-1>` | URL 解码后 |
| query | `code_challenge` | `<pkce-challenge-1>` | URL 解码后 |
| query | `code_challenge_method` | `S256` | URL 解码后 |
| body | `$` | `无` | 原始 0 字节；无请求体 |
| cookie-group | `引用` | `K-Auth` | 等于基线加下列值覆盖／新增 |

### 字段来源

- 本条所有 query（包括 state、S256 code_challenge、client_id、scope、redirect_uri 等）完整包含于 #17 JSON url，且该 url 与 #18 请求 URL 完全相同。#17 同时设置 state/pkce.code_verifier Cookie，不代表 opaque Cookie 就是明文 verifier，浏览器／服务端内部生成算法无法判断。#18 Location 解析后与 #19 请求 URL 相同。
- Sec-Fetch-Site=cross-site、mode=navigate、dest=document，Referer 仍为 chatgpt.com 根 URL；未记录 Sec-Fetch-User。#19 不能仅因目标域名未变而改写为 same-origin。本地导航模板见 `core/openai/transport/session.py:719–735`，不代替捕获证据。
- Cookie 初始来源按对应 K 组；本条未找到此前域／路径和值匹配的 Set-Cookie，不能由相邻请求推定来源。

## 请求 #19 — GET auth.openai.com/log-in/password

证据：`raw/19_c.txt` / `raw/19_s.txt`；响应 `HTTP/1.1 200 OK`。

| 位置 | 字段／公共组 | 捕获值或引用 | 说明 |
|---|---|---|---|
| header-group | `引用` | `B-通用 + D-Auth + CH-高熵 + N-导航` | 按组顺序合并，再应用下列覆盖 |
| header | `sec-fetch-site` | `cross-site` | 本条增补／覆盖 |
| header | `referer` | `https://chatgpt.com/` | 本条增补／覆盖 |
| query | `—` | `无` | 请求 URL 无 query |
| body | `$` | `无` | 原始 0 字节；无请求体 |
| cookie-group | `引用` | `K-Auth` | 等于基线加下列值覆盖／新增 |
| cookie | `oai-login-csrf_dev_3772291445` | `<cookie-value-26>` | 覆盖基线值 |
| cookie | `auth-session-minimized` | `<cookie-value-27>` | 覆盖基线值 |
| cookie | `auth-session-minimized-client-checksum` | `<cookie-value-28>` | 覆盖基线值 |

### 字段来源

- Sec-Fetch-Site=cross-site、mode=navigate、dest=document，Referer 仍为 chatgpt.com 根 URL；未记录 Sec-Fetch-User。#19 不能仅因目标域名未变而改写为 same-origin。本地导航模板见 `core/openai/transport/session.py:719–735`，不代替捕获证据。
- Cookie `oai-did` 的值匹配此前 #18 Set-Cookie；响应属性 `domain=.openai.com；path=/；samesite=lax`。这是作用域与内容证据，不保证唯一写入者。
- Cookie `rg_context`、`iss_context` 的值匹配此前 #18 Set-Cookie；响应属性 `domain=auth.openai.com；path=/；samesite=lax`。这是作用域与内容证据，不保证唯一写入者。
- Cookie `oai-login-csrf_dev_3772291445` 的值匹配此前 #18 Set-Cookie；响应属性 `httponly=True；path=/；samesite=Lax；secure=True`。这是作用域与内容证据，不保证唯一写入者。
- Cookie `auth-session-minimized` 的值匹配此前 #18 Set-Cookie；响应属性 `path=/；domain=auth.openai.com；samesite=lax；secure=True；httponly=True`。这是作用域与内容证据，不保证唯一写入者。
- Cookie `auth-session-minimized-client-checksum` 的值匹配此前 #18 Set-Cookie；响应属性 `path=/；domain=auth.openai.com；samesite=lax；secure=True`。这是作用域与内容证据，不保证唯一写入者。

## 辅助来源：Sentinel SDK 与 frame

- 辅助请求 #26（`raw/26_s.txt`）响应含本次版本化 SDK 路径，#28（`raw/28_c.txt`、`raw/28_s.txt`）的 frame query `sv=20260810913b` 与版本路径对应；#34 / #87 的 Referer 为带该版本的 frame URL。这里只解释请求字段来源，不展开 SDK 下载、缓存或 iframe 导航的逐项头部对比；版本选择算法及完整调用栈无法判断。
- K-Sentinel 仅保存后续请求需要的 Cookie 基线；oaicom-stable-id / oai-did 与 #19 Set-Cookie 的内容及作用域匹配，详见后续主链请求。字段相同不代表 JS 环境与传输指纹等同浏览器。

## 请求 #34 — POST sentinel.openai.com/backend-api/sentinel/req

证据：`raw/34_c.txt` / `raw/34_s.txt`；响应 `HTTP/1.1 200 OK`。

| 位置 | 字段／公共组 | 捕获值或引用 | 说明 |
|---|---|---|---|
| header-group | `引用` | `B-通用 + D-Sentinel + F-fetch + T-SentinelPOST` | 按组顺序合并，再应用下列覆盖 |
| header | `content-length` | `714` | 本条增补／覆盖 |
| query | `—` | `无` | 请求 URL 无 query |
| body | `$` | `{"p":"<sentinel-proof-1>","id":"<device-id-1>","flow":"password_verify"}` | 原始 714 字节；text/plain;charset=UTF-8 |
| cookie-group | `引用` | `K-Sentinel` | 等于基线加下列值覆盖／新增 |

### 字段来源

- body 的 p 是当前 flow requirements 编码证明，整体脱敏，其生成算法无法判断；id 与设备 query/Cookie 同值；flow 指定认证步骤。本地输入和紧凑 JSON 构造见 `core/openai/auth/openai_auth.py:414–428`、`core/openai/auth/sentinel.py:259–276`。Content-Type 保留 text/plain，不改为 application/json。
- Content-Length 与本次原始 body 字节数相符；脱敏结构不是原始序列化，不按占位文本重算长度。
- Cookie `oaicom-stable-id` 的值匹配此前 #19 Set-Cookie；响应属性 `domain=openai.com；path=/；secure=True；samesite=Lax`。这是作用域与内容证据，不保证唯一写入者。
- Cookie `oai-did` 的值匹配此前 #19 Set-Cookie；响应属性 `domain=openai.com；path=/；samesite=Lax`。这是作用域与内容证据，不保证唯一写入者。

## 请求 #58 — POST auth.openai.com/api/accounts/password/verify

证据：`raw/58_c.txt` / `raw/58_s.txt`；响应 `HTTP/1.1 200 OK`。

| 位置 | 字段／公共组 | 捕获值或引用 | 说明 |
|---|---|---|---|
| header-group | `引用` | `B-通用 + D-Auth + CH-高熵 + F-fetch + A-认证JSON` | 按组顺序合并，再应用下列覆盖 |
| header | `content-length` | `29` | 本条增补／覆盖 |
| header | `x-datadog-parent-id` | `<span-id-1>` | 本条增补／覆盖 |
| header | `openai-sentinel-token` | `<sentinel-header-1>` | 本条增补／覆盖 |
| header | `x-access-flow-invocation-id` | `<invocation-id-1>` | 本条增补／覆盖 |
| header | `x-datadog-trace-id` | `<trace-id-1>` | 本条增补／覆盖 |
| header | `traceparent` | `<trace-context-1>` | 本条增补／覆盖 |
| header-json | `openai-sentinel-token` | `E-58` | 五个子字段见公共复合凭据表 |
| query | `—` | `无` | 请求 URL 无 query |
| body | `$` | `{"password":"<password-1>"}` | 原始 29 字节；application/json |
| cookie-group | `引用` | `K-Auth` | 等于基线加下列值覆盖／新增 |
| cookie | `oai-login-csrf_dev_3772291445` | `<cookie-value-26>` | 覆盖基线值 |
| cookie | `auth-session-minimized` | `<cookie-value-27>` | 覆盖基线值 |
| cookie | `auth-session-minimized-client-checksum` | `<cookie-value-28>` | 覆盖基线值 |
| cookie | `oai-sc` | `<cookie-value-31>` | 覆盖基线值 |
| cookie | `cf_clearance` | `<cookie-value-32>` | 覆盖基线值 |
| cookie | `__cf_bm` | `<cookie-value-33>` | 覆盖基线值 |
| cookie | `_dd_s` | `<cookie-value-34>` | 本条新增 |

### 字段来源

- body.password 是密码输入，无法判断人工／密码管理器／自动填入；本地 password 入参见 `core/openai/auth/openai_auth.py:1025–1040`。token 组成和 #34→#58 消费关系见 E-58。
- Content-Length 与本次原始 body 字节数相符；脱敏结构不是原始序列化，不按占位文本重算长度。
- Cookie `oaicom-stable-id` 的值匹配此前 #19 Set-Cookie；响应属性 `domain=openai.com；path=/；secure=True；samesite=Lax`。这是作用域与内容证据，不保证唯一写入者。
- Cookie `oai-did` 的值匹配此前 #19 Set-Cookie；响应属性 `domain=openai.com；path=/；samesite=Lax`。这是作用域与内容证据，不保证唯一写入者。
- Cookie `rg_context`、`iss_context` 的值匹配此前 #18 Set-Cookie；响应属性 `domain=auth.openai.com；path=/；samesite=lax`。这是作用域与内容证据，不保证唯一写入者。
- Cookie `oai-login-csrf_dev_3772291445` 的值匹配此前 #18 Set-Cookie；响应属性 `httponly=True；path=/；samesite=Lax；secure=True`。这是作用域与内容证据，不保证唯一写入者。
- Cookie `auth-session-minimized` 的值匹配此前 #18 Set-Cookie；响应属性 `path=/；domain=auth.openai.com；samesite=lax；secure=True；httponly=True`。这是作用域与内容证据，不保证唯一写入者。
- Cookie `auth-session-minimized-client-checksum` 的值匹配此前 #18 Set-Cookie；响应属性 `path=/；domain=auth.openai.com；samesite=lax；secure=True`。这是作用域与内容证据，不保证唯一写入者。
- Cookie `oai-sc` 的值匹配此前 #34 Set-Cookie；响应属性 `domain=.openai.com；path=/；samesite=none；secure=True`。这是作用域与内容证据，不保证唯一写入者。
- Cookie `cf_clearance`、`__cf_bm` 的值匹配此前 辅助请求 #48 Set-Cookie；响应属性 `httponly=True；samesite=None；secure=True；path=/；domain=auth.openai.com`。这是作用域与内容证据，不保证唯一写入者。

## 请求 #84 — POST auth.openai.com/api/accounts/mfa/issue_challenge

证据：`raw/84_c.txt` / `raw/84_s.txt`；响应 `HTTP/1.1 200 OK`。

| 位置 | 字段／公共组 | 捕获值或引用 | 说明 |
|---|---|---|---|
| header-group | `引用` | `B-通用 + D-Auth + CH-高熵 + F-fetch + A-认证JSON` | 按组顺序合并，再应用下列覆盖 |
| header | `content-length` | `85` | 本条增补／覆盖 |
| header | `x-datadog-parent-id` | `<span-id-2>` | 本条增补／覆盖 |
| header | `x-access-flow-invocation-id` | `<invocation-id-2>` | 本条增补／覆盖 |
| header | `x-datadog-trace-id` | `<trace-id-2>` | 本条增补／覆盖 |
| header | `traceparent` | `<trace-context-2>` | 本条增补／覆盖 |
| header | `accept` | `*/*` | 本条增补／覆盖 |
| query | `—` | `无` | 请求 URL 无 query |
| body | `$` | `{"id":"<factor-id-1>","type":"totp","force_fresh_challenge":false}` | 原始 85 字节；application/json |
| cookie-group | `引用` | `K-Auth` | 等于基线加下列值覆盖／新增 |
| cookie | `oai-login-csrf_dev_3772291445` | `<cookie-value-26>` | 覆盖基线值 |
| cookie | `oai-sc` | `<cookie-value-31>` | 覆盖基线值 |
| cookie | `cf_clearance` | `<cookie-value-32>` | 覆盖基线值 |
| cookie | `__cf_bm` | `<cookie-value-33>` | 覆盖基线值 |
| cookie | `auth-session-minimized` | `<cookie-value-35>` | 覆盖基线值 |
| cookie | `auth-session-minimized-client-checksum` | `<cookie-value-36>` | 覆盖基线值 |
| cookie | `_dd_s` | `<cookie-value-37>` | 本条新增 |

### 字段来源

- body.id 与 #58 JSON page.payload.factor_id、page.payload.factors[0].id 及 oai-client-auth-session 的 MFA factor 同值；#93 还匹配 #84 的 MFA factor。多个位置同值不能证明浏览器读取哪一个。type=totp 与响应 factor_type 一致；本地见 `core/openai/auth/openai_auth.py:987–1016`。
- force_fresh_challenge 是 JSON false；Accept 显式覆盖为 */*；本请求没有 openai-sentinel-token，不能套用 #58/#93 的凭据模板。
- Content-Length 与本次原始 body 字节数相符；脱敏结构不是原始序列化，不按占位文本重算长度。
- Cookie `oaicom-stable-id` 的值匹配此前 #19 Set-Cookie；响应属性 `domain=openai.com；path=/；secure=True；samesite=Lax`。这是作用域与内容证据，不保证唯一写入者。
- Cookie `oai-did` 的值匹配此前 #19 Set-Cookie；响应属性 `domain=openai.com；path=/；samesite=Lax`。这是作用域与内容证据，不保证唯一写入者。
- Cookie `rg_context`、`iss_context` 的值匹配此前 #18 Set-Cookie；响应属性 `domain=auth.openai.com；path=/；samesite=lax`。这是作用域与内容证据，不保证唯一写入者。
- Cookie `oai-login-csrf_dev_3772291445` 的值匹配此前 #18 Set-Cookie；响应属性 `httponly=True；path=/；samesite=Lax；secure=True`。这是作用域与内容证据，不保证唯一写入者。
- Cookie `oai-sc` 的值匹配此前 #34 Set-Cookie；响应属性 `domain=.openai.com；path=/；samesite=none；secure=True`。这是作用域与内容证据，不保证唯一写入者。
- Cookie `cf_clearance`、`__cf_bm` 的值匹配此前 辅助请求 #48 Set-Cookie；响应属性 `httponly=True；samesite=None；secure=True；path=/；domain=auth.openai.com`。这是作用域与内容证据，不保证唯一写入者。
- Cookie `auth-session-minimized` 的值匹配此前 #58 Set-Cookie；响应属性 `path=/；domain=auth.openai.com；samesite=lax；secure=True；httponly=True`。这是作用域与内容证据，不保证唯一写入者。
- Cookie `auth-session-minimized-client-checksum` 的值匹配此前 #58 Set-Cookie；响应属性 `path=/；domain=auth.openai.com；samesite=lax；secure=True`。这是作用域与内容证据，不保证唯一写入者。

## 请求 #87 — POST sentinel.openai.com/backend-api/sentinel/req

证据：`raw/87_c.txt` / `raw/87_s.txt`；响应 `HTTP/1.1 200 OK`。

| 位置 | 字段／公共组 | 捕获值或引用 | 说明 |
|---|---|---|---|
| header-group | `引用` | `B-通用 + D-Sentinel + F-fetch + T-SentinelPOST` | 按组顺序合并，再应用下列覆盖 |
| header | `content-length` | `709` | 本条增补／覆盖 |
| query | `—` | `无` | 请求 URL 无 query |
| body | `$` | `{"p":"<sentinel-proof-1>","id":"<device-id-1>","flow":"verify_mfa"}` | 原始 709 字节；text/plain;charset=UTF-8 |
| cookie-group | `引用` | `K-Sentinel` | 等于基线加下列值覆盖／新增 |
| cookie | `oai-sc` | `<cookie-value-31>` | 覆盖基线值 |

### 字段来源

- body 的 p 是当前 flow requirements 编码证明，整体脱敏，其生成算法无法判断；id 与设备 query/Cookie 同值；flow 指定认证步骤。本地输入和紧凑 JSON 构造见 `core/openai/auth/openai_auth.py:414–428`、`core/openai/auth/sentinel.py:259–276`。Content-Type 保留 text/plain，不改为 application/json。
- Content-Length 与本次原始 body 字节数相符；脱敏结构不是原始序列化，不按占位文本重算长度。
- Cookie `oaicom-stable-id` 的值匹配此前 #19 Set-Cookie；响应属性 `domain=openai.com；path=/；secure=True；samesite=Lax`。这是作用域与内容证据，不保证唯一写入者。
- Cookie `oai-did` 的值匹配此前 #19 Set-Cookie；响应属性 `domain=openai.com；path=/；samesite=Lax`。这是作用域与内容证据，不保证唯一写入者。
- Cookie `oai-sc` 的值匹配此前 #34 Set-Cookie；响应属性 `domain=.openai.com；path=/；samesite=none；secure=True`。这是作用域与内容证据，不保证唯一写入者。

## 请求 #93 — POST auth.openai.com/api/accounts/mfa/verify

证据：`raw/93_c.txt` / `raw/93_s.txt`；响应 `HTTP/1.1 403 Forbidden`。

| 位置 | 字段／公共组 | 捕获值或引用 | 说明 |
|---|---|---|---|
| header-group | `引用` | `B-通用 + D-Auth + CH-高熵 + F-fetch + A-认证JSON` | 按组顺序合并，再应用下列覆盖 |
| header | `content-length` | `71` | 本条增补／覆盖 |
| header | `x-datadog-parent-id` | `<span-id-3>` | 本条增补／覆盖 |
| header | `openai-sentinel-token` | `<sentinel-header-2>` | 本条增补／覆盖 |
| header | `x-access-flow-invocation-id` | `<invocation-id-3>` | 本条增补／覆盖 |
| header | `x-datadog-trace-id` | `<trace-id-3>` | 本条增补／覆盖 |
| header | `traceparent` | `<trace-context-3>` | 本条增补／覆盖 |
| header-json | `openai-sentinel-token` | `E-93` | 五个子字段见公共复合凭据表 |
| query | `—` | `无` | 请求 URL 无 query |
| body | `$` | `{"id":"<factor-id-1>","type":"totp","code":"<otp-1>"}` | 原始 71 字节；application/json |
| cookie-group | `引用` | `K-Auth` | 等于基线加下列值覆盖／新增 |
| cookie | `oai-login-csrf_dev_3772291445` | `<cookie-value-26>` | 覆盖基线值 |
| cookie | `cf_clearance` | `<cookie-value-32>` | 覆盖基线值 |
| cookie | `__cf_bm` | `<cookie-value-33>` | 覆盖基线值 |
| cookie | `auth-session-minimized` | `<cookie-value-38>` | 覆盖基线值 |
| cookie | `auth-session-minimized-client-checksum` | `<cookie-value-39>` | 覆盖基线值 |
| cookie | `oai-sc` | `<cookie-value-40>` | 覆盖基线值 |
| cookie | `_dd_s` | `<cookie-value-41>` | 本条新增 |

### 字段来源

- body.id 与 #58 JSON page.payload.factor_id、page.payload.factors[0].id 及 oai-client-auth-session 的 MFA factor 同值；#93 还匹配 #84 的 MFA factor。多个位置同值不能证明浏览器读取哪一个。type=totp 与响应 factor_type 一致；本地见 `core/openai/auth/openai_auth.py:987–1016`。
- body.code 为验证码输入，具体生成密钥、时钟或手工／自动来源无法判断；本地 code 参数与 TOTP 调用见 `core/openai/auth/openai_auth.py:1004–1016、1132–1146`。token 组成和 #87→#93 消费关系见 E-93。
- Content-Length 与本次原始 body 字节数相符；脱敏结构不是原始序列化，不按占位文本重算长度。
- Cookie `oaicom-stable-id` 的值匹配此前 #19 Set-Cookie；响应属性 `domain=openai.com；path=/；secure=True；samesite=Lax`。这是作用域与内容证据，不保证唯一写入者。
- Cookie `oai-did` 的值匹配此前 #19 Set-Cookie；响应属性 `domain=openai.com；path=/；samesite=Lax`。这是作用域与内容证据，不保证唯一写入者。
- Cookie `rg_context`、`iss_context` 的值匹配此前 #18 Set-Cookie；响应属性 `domain=auth.openai.com；path=/；samesite=lax`。这是作用域与内容证据，不保证唯一写入者。
- Cookie `oai-login-csrf_dev_3772291445` 的值匹配此前 #18 Set-Cookie；响应属性 `httponly=True；path=/；samesite=Lax；secure=True`。这是作用域与内容证据，不保证唯一写入者。
- Cookie `cf_clearance`、`__cf_bm` 的值匹配此前 辅助请求 #48 Set-Cookie；响应属性 `httponly=True；samesite=None；secure=True；path=/；domain=auth.openai.com`。这是作用域与内容证据，不保证唯一写入者。
- Cookie `auth-session-minimized` 的值匹配此前 #84 Set-Cookie；响应属性 `path=/；domain=auth.openai.com；samesite=lax；secure=True；httponly=True`。这是作用域与内容证据，不保证唯一写入者。
- Cookie `auth-session-minimized-client-checksum` 的值匹配此前 #84 Set-Cookie；响应属性 `path=/；domain=auth.openai.com；samesite=lax；secure=True`。这是作用域与内容证据，不保证唯一写入者。
- Cookie `oai-sc` 的值匹配辅助请求 #89 Set-Cookie（`raw/89_s.txt`）；响应属性 `domain=.openai.com；path=/；samesite=none；secure=True`。仅保留这项 Cookie 来源证据，不展开无内容匹配 token 消费者的 challenge，也不保证唯一写入者。

