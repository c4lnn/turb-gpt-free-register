# `login-password-totp-403.saz` 链路对照

## 范围

- 唯一捕获输入为 `saz/login-password-totp-403.saz`，文中捕获、源码路径均相对仓库根目录；请求序号沿用 SAZ，不重编号。主链比较从 #16 正式 CSRF → #17 signin 开始，截止 #93 MFA verify 的 403，不推断后续回调或成功 session。
- #16 之前的入口导航、providers、handoff 等不逐项比较；仅在解释正式请求的 Referer、Cookie 或设备 / 会话字段来源时引用为辅助证据。省略不代表前置流程一致，也不代表请求未发生。详细字段见同目录 `login-password-totp-403-request-details.md`。
- 下文简称 `session.py`、`protocol_profiles.py` 位于 `core/openai/transport/`；`chatgpt_auth.py`、`openai_auth.py`、`sentinel.py` 位于 `core/openai/auth/`。本地列是源码构造证据，不代表本次运行或最终 wire headers。

## 结论

403 是账号被封后的预期结果，不作为链路故障分析。重点对照后，结论是：**密码验证、MFA challenge、TOTP verify 这几步的端点和 JSON 字段语义，与仓库实现基本一致；正式 CSRF 之后的请求头和 JSON 原始序列化并非逐项一致。** 本文主要对照本地查活的登录 / MFA 路径，不把该调用上下文的差异推广到注册入口。

## 主链顺序

```text
#16 CSRF --> #17 signin/openai --> #18 OAuth authorize --> #19 密码页
  --> #34 Sentinel(password_verify) --> #58 password/verify
  --> #84 mfa/issue_challenge --> #87 Sentinel(verify_mfa) --> #93 mfa/verify
```

- 辅助请求仅用于来源说明：#11 的 URL 与 #16 / #17 Referer 相同；#10 的 Set-Cookie 为登录请求 Cookie 的内容匹配证据。CES/AB 等不纳入主链审计。
- #16 返回的 CSRF 值与 #17 表单中的值相同；#17 响应中的 authorize URL 与 #18 请求 URL 相同。
- #34 的 Sentinel token 与 #58 请求头中的 token 内容匹配；#87 与 #93 也匹配。仅展开这两组 challenge → 消费者；其他无内容匹配消费者的 challenge 不展开，不推断其未发生或捕获外未消费。
- #58 返回的 MFA factor ID 与 #84、#93 请求体中的 ID 一致。

## 报文对照

| 请求 | 位置/字段 | SAZ 值或来源证据 | 本地构造来源 | 判断 |
|---|---|---|---|---|
| #17（辅助来源 #11） | 登录提示及会话参数 | signin query 携带 `screen_hint`、`login_hint`、`auth_session_logging_id`；#11 入口 URL 与正式 CSRF / signin 的完整 Referer 相同，包含 `callback_path`、`ext-oai-did` | `core/openai/auth/chatgpt_auth.py:21-29,150-157,159-166` 构造登录上下文；查活调用未显式共享注册的 login_referer 参数 | 字段语义相符；完整 Referer 是否采用入口上下文取决于本地调用分支，不审计 #11 本身 |
| #16 | Referer、GET 头 | Referer 是带 query 的 `/auth/login_with`；GET 未记录 `Content-Type`、`Priority` | 登录默认 `get_csrf_token()` 的 Referer 为 `/auth/login`；通用 NextAuth builder 构造 `Content-Type: application/json` 和 `Priority: u=1, i`（`core/openai/auth/chatgpt_auth.py:88-110`；`core/openai/transport/session.py:652-664`） | **不完全一致**：默认登录调用 Referer 不同，静态构造多出两个 SAZ 未记录的头；注册显式上下文分支不可混用 |
| #16 → #17 | CSRF | 响应中的 `csrfToken` 与 #17 form body 中的值相同 | `signin_openai` 把取到的 token 编码进表单（`chatgpt_auth.py:168-175`） | 传递一致 |
| #17 | signin query/body | body 字段是 `callbackUrl`、`csrfToken`、`json`；query 字段和值与本地相同 | `signin_openai` 使用 `urlencode`（`core/openai/auth/chatgpt_auth.py:150-175`）；本地 query 的字段顺序与 SAZ 不同 | 字段和值一致；query 顺序不完全一致 |
| #18 | OAuth authorize query | #17 返回的 URL 与 #18 请求完全相同；scope、client、redirect、audience、设备及登录上下文字段均在 URL 中 | 固定 OAuth 参数在 `config/openai_protocol.py:11-25`；`_ensure_authorize_context` 补设备和登录上下文并保留原参数（`chatgpt_auth.py:32-53`） | 链接传递一致；`state` / PKCE 是此次授权生成的动态值，本地代码保留它们，不自行生成 |
| #34、#87 | Sentinel body | JSON 字段为 `p`、`id`、`flow`；分别对应 `password_verify`、`verify_mfa` | `build_sentinel_request_body` 构造这三个字段；`request_sentinel_token` 传入当前 flow（`sentinel.py:259-276`；`openai_auth.py:409-427`） | **body 结构一致**；动态 `p` 内容与画像有关 |
| #58 | password body | 只有 `password` 字段 | `verify_login_password` 发送 `{"password": ...}`（`openai_auth.py:1025-1042`） | JSON 字段语义一致；SAZ 是紧凑 JSON，本地 `json.dumps` 默认会在分隔符后加空格，原始 body 字节不完全一致 |
| #84 | issue challenge body | `id`、`type: totp`、`force_fresh_challenge: false` | `_mfa_issue_challenge` 构造同样字段和值（`openai_auth.py:987-996`） | 字段和值一致；同样存在 JSON 空白差异 |
| #93 | verify body | `id`、`type: totp`、`code` | `_mfa_verify` 构造同样字段和值（`openai_auth.py:1004-1017`） | 字段语义一致；同样存在 JSON 空白差异 |

## 请求头差异

| 请求 | 位置/字段 | SAZ 值或来源证据 | 本地构造来源 | 判断 |
|---|---|---|---|---|
| #16–19、#34、#58、#84、#87、#93 | UA / Client Hints | SAZ 是 Windows Edge/Chromium 157；认证 API 请求带 `sec-ch-ua-full-version`、`full-version-list`、platform version、arch、bitness、model | 当前协议会话固定 `wreq/chrome152`，画像对齐为 macOS Chrome（`core/openai/transport/session.py:201-205`；`core/openai/transport/protocol_profiles.py:23-46`）；认证调用默认不发高熵 hints（`session.py:683-717`） | **明显不一致**：浏览器/平台/版本画像不同；高熵头在 SAZ 出现、本地默认构造不出现 |
| #34、#87 | Sentinel 请求头 | SAZ 有低熵 Client Hints、`Accept-Language`、`Origin`、`Referer`、Fetch Metadata；未记录 `Priority` | `get_sentinel_headers` 构造这些头并加 `Priority: u=1, i`（`session.py:754-773`） | 主字段形态相符；`Priority` 是构造层差异 |
| #16–17、#58、#84、#93 | `Priority` | SAZ 未记录该头 | NextAuth / Auth JSON 头构造器会设置 `Priority: u=1, i`（`core/openai/transport/session.py:652-697`） | **不一致**，未实测最终 wire headers |
| #16 | `Content-Type` | SAZ 的 GET 未记录该头 | 登录默认 `get_nextauth_headers` 会设置 `application/json`（`core/openai/transport/session.py:652-664`）；正式注册分支显式移除（`core/openai/auth/chatgpt_auth.py:104-106`） | 默认登录构造存在差异，不能推广为注册仍携带该头 |
| #18 | 导航头 | SAZ 有导航 Fetch Metadata、Referer 和 `upgrade-insecure-requests`；未记录 `Priority`、`Sec-Fetch-User` | `follow_authorize` 调用 `get_auth_navigate_headers`；默认设置 `Priority: u=0, i` 和 `Sec-Fetch-User: ?1`（`openai_auth.py:345-362`；`session.py:719-735`） | 主要导航头相符；实际是否带 `Sec-Fetch-User` 还取决于调用的 `user_initiated` 参数，注册显式传 false，不等同默认登录路径 |
| #58、#84、#93 | Auth trace / navigation IDs | 同一 document-navigation ID；每次请求的 invocation ID 不同，trace/RUM 字段存在 | `_attach_auth_rum_headers` 复用 navigation ID、每次生成 invocation ID 并附加 trace（`session.py:590-599`） | 字段和跨请求模式相符；动态 ID 不作字节值比较 |
| #16–19、#34、#58、#84、#87、#93 | `Accept-Language` | SAZ 有该头，值为中文优先的语言列表 | 本地从 `browser_profile.accept_language` 取值（`session.py:529-555`） | 字段来源类型一致；没有本次运行的画像，不能确认本地实际值相同 |

另一个 body 内部差异：#34 的 `p` 解码后含 Windows Edge 157 UA、screen sum `3000`、`hardwareConcurrency=20`；而本地协议画像会对齐 Chrome 152/macOS，当前画像池的 CPU 值为 6/8/10/12。`p` 外层字段结构一致，但其中的指纹值**并不等同于当前代码默认/画像池样本**。该值差异不改变本次流程的字段传递关系。

## 流程层差异

从正式 CSRF 起，本地查活还会在 signin 前再次读取 `/api/auth/session`，这份 SAZ 未记录该请求（`core/openai/account/account_liveness.py:126-152,187-191`）。这属于范围内的请求差异，不据此反推此前入口或 providers 的实现。当前注册链正式 CSRF 后直接 signin（`core/openai/register/registration_flow.py:300-306`），不能与查活登录分支混为一谈。

以上差异是捕获与当前代码构造规则的对照，不据此判断它们导致了 #93 的账号状态响应。SAZ 中的 Cookie、凭据和动态 ID 均未复述。
