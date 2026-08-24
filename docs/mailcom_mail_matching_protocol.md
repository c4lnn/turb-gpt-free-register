# mail.com 协议与别名注册邮箱参考

## 1. 文档目的

本文记录 mail.com Webmail 读信、地址 settings 和别名生命周期的协议链路，供主项目把已导入的 mail.com 母号作为 OpenAI 注册邮箱来源时参考。当前实现位于 `core/mailcom_client.py`、`core/mailcom_settings_client.py`、`core/mailcom_alias_service.py`、`core/mailcom_provider.py` 与 `core/mailcom_alias_cleanup.py`。

本文不是 mail.com 官方 API 文档。接口、字段和版本号来自用户提供的本地 HAR
<code>navigator-lxa.mail.com.har</code> 与 <code>navigator-lxa.mail.com1.har</code> 的脱敏分析，以及独立 demo <code>mailcom_latest_mail_demo.py</code> 的代码和实测结果。mail.com 前端可能随时变更接口、域名、版本号或风控策略，升级前应重新做小范围联调。

## 2. 能力边界与结论

已实测或已有脱敏契约测试覆盖的能力：

- 使用 mail.com 账号和密码完成 Web 登录。
- 将登录跳转得到的一次性导航会话换成 OAuth access token。
- 查询 <code>INBOX</code>，按 <code>INTERNALDATE DESC</code> 排序并分页。
- 按发件人地址识别 OpenAI/ChatGPT 邮件，获取完整邮件头和 HTML 正文，并转换为纯文本。
- 在共享母号收件箱中，以 <code>mailHeader.to</code> 的结构化、大小写不敏感精确地址匹配指定 alias；先读完整头，匹配后才读正文。
- 在 mail.com settings 临时登录会话中读取地址列表、校验候选地址、创建地址、URL 编码删除地址，并以地址列表回读确认结果。
- 以远端 <code>state=ACTIVE && deletable=true</code> 的地址数执行 9 个活动别名上限，并以全量历史快照执行 99 个生命周期累计上限；候选冲突有界重试，创建和删除通过母号级锁串行化。
- 以独立 <code>mailcom_aliases</code> 持久化 alias 到母号的映射、任务、注册、套餐和清理状态；记录不复制母号密码、mailbox AT、Cookie、<code>sid</code> 或邮件正文。
- 默认关闭地处理“套餐查询成功且明确无试用资格”后的单次别名删除。

本项目运行时不会请求 HAR 中的 <code>GET /domains</code>。HAR 提取出的 138 个唯一活动域名已经作为受版本控制的静态目录保存到 <code>config/mailcom_alias_domains.json</code>；每次创建随机选择其中一项。这样避免远端目录变化、权限变化或分页行为改变本次任务。该目录不是用户运行状态，不写入数据库。

当前没有验证的能力：

- mail.com 新账号注册、用户名可用性检查、注册表单提交。
- 注册验证码发送、验证码校验、手机验证、CAPTCHA 或二次验证处理。
- SMTP/IMAP 访问、应用专用密码和长期 refresh token。
- 邮件删除、标记已读、发送邮件等写操作。
- 在真实账户上自动化执行 mail.com 新账号注册、CAPTCHA、手机验证或二次验证。
- 将浏览器 Cookie、<code>sid</code>、settings 临时认证材料或邮箱正文持久化。
- 对 settings 创建/删除请求进行本次变更之外的真实写操作联调。本实现仅以脱敏 HAR 和 mock contract test 验证请求形状；实际部署前应由账户所有者在隔离账户中确认会话权限与风控语义。

本文支持“已有 mail.com 母号作为 OpenAI 注册邮箱 provider”的实现，不支持 mail.com 自身的新账号注册。若 mail.com 的 settings 身份验证机制发生变化，必须先在隔离账户复核，再调整 client 和 fixture。

## 3. 端到端时序

~~~text
mail.com 母号 / alias provider
  |
  | 领取母号（短租约）
  | 获取临时 settings 登录 session（不持久化 Cookie/sid）
  | GET /mailaccount/primary/emailAddresses
  | 读取活动/历史容量；活动 >=9 或生命周期 >=99 时终止当前任务
  | 本地 138 域名目录 + 随机名称生成候选 alias
  | POST /emailAddressValidations
  | POST /primary/emailAddresses
  | GET /primary/emailAddresses，确认精确 alias 为 ACTIVE/deletable
  | 持久化 alias -> parent，立即释放母号租约
  |
OpenAI 注册流程
  | 使用 alias_email 注册；开始前写 registration_started_at
  |
mailbox 读取 client
  | 使用母号持久化 mailbox AT；仅严格 401 + Bearer invalid_token 时登录刷新一次
  | POST maillist.mail.com/Mailbox/Mail?...INBOX...
  | 仅保留 OpenAI 候选及 internalDate >= registration_started_at
  | GET webmail-cats-live.../mailheader/<mail_id>
  | 仅 mailHeader.to 精确匹配当前 alias 时才继续
  | POST mailcom.mailbody-ui.de/Mail/<mail_id>/Body/html
  | 提取独立六位 OTP
  |
套餐后处理
  | 仅开关开启 + 查询成功 + trial_eligibility_known=true
  | 且 plus_trial_eligible 严格为 false 时删除 alias 一次
~~~

其中 <code>sid</code>、OAuth token、Cookie、邮件 ID 和邮件正文都属于运行时敏感数据。本文所有示例均使用占位符，不包含 HAR 中的真实值。

## 4. 公共请求约定

### 4.1 会话与请求库

- 推荐使用支持现代 TLS/浏览器指纹协商的 HTTP 客户端；当前 demo 使用 <code>curl_cffi.requests.Session(impersonate="chrome")</code>。
- 一个完整流程使用同一个 session，以保留登录 Cookie。
- 不要把 Cookie、<code>sid</code>、Bearer token 或邮件正文写入日志、URL 以外的持久化文件或异常堆栈。
- 所有请求设置合理超时；当前 demo 为 30 秒。
- <code>no_cache</code> 使用随机值，例如 <code>a-&lt;随机字符串&gt;</code>，用于避免 Webmail 前端缓存旧列表或正文。
- <code>X-Request-ID</code> 使用每次请求新的 UUID，便于服务端关联请求；日志中只记录脱敏后的请求 ID 或完全不记录。

### 4.2 常见浏览器头

以下头在业务请求中出现，值应按当前会话设置：

~~~http
Accept-Language: zh-CN,zh;q=0.9,en;q=0.8
User-Agent: <与实际 HTTP 客户端一致的浏览器 UA>
Origin: https://webmailer.mail.com
Referer: https://webmailer.mail.com/
~~~

<code>sec-fetch-*</code>、<code>sec-ch-ua*</code>、<code>priority</code> 等是浏览器导航/Fetch 头。纯协议客户端不一定能完整复现；它们不是本文已验证业务字段，除非服务端实际拒绝请求，否则不应硬编码一组过期浏览器值。

## 5. 接口详解

### 5.1 获取登录页

**请求**

~~~http
GET https://www.mail.com/premiumlogin
Accept: text/html,application/xhtml+xml
Accept-Language: <会话语言>
User-Agent: <浏览器 UA>
~~~

**用途**

获取当前登录表单，而不是猜测固定表单字段。页面可能包含多个 <code>&lt;form&gt;</code>；客户端应选择 action 解析后主机为 <code>login.mail.com</code>、路径为 <code>/login</code> 的表单。

**响应**

- HTTP <code>200</code>。
- HTML 表单通常包含登录上下文隐藏字段，以及 <code>username</code>、<code>password</code> 输入项。
- demo 保留服务端返回的隐藏字段，再覆盖 <code>username</code> 和 <code>password</code>；不要只发送两个字段。

### 5.2 提交账号密码

**请求**

~~~http
POST https://login.mail.com/login
Content-Type: application/x-www-form-urlencoded
Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8
Origin: https://www.mail.com
Referer: https://www.mail.com/premiumlogin

service=<form-value>&statistics=<form-value>&username=<account>&password=<password>&
edition=<form-value>&lang=<form-value>&usertype=<form-value>&
uasServiceID=<form-value>&successURL=<form-value>&loginFailedURL=<form-value>&
loginErrorURL=<form-value>&goto=<form-value>&gotoparams=<form-value>&ibaInfo=<form-value>
~~~

HAR 中观察到的字段名为：

~~~text
edition, goto, gotoparams, ibaInfo, lang, loginErrorURL,
loginFailedURL, password, service, statistics, successURL,
uasServiceID, username, usertype
~~~

不要把上面示例中的 <code>&lt;form-value&gt;</code> 当成固定常量；它们应来自本次登录页。

**成功响应**

- HTTP <code>302</code> 或 <code>303</code>。
- <code>Location</code> 指向 <code>https://navigator-lxa.mail.com/login?... </code>，包含一次性登录参数（例如 <code>ott</code> 等）。
- 关闭自动跟随重定向，先校验主机和路径，再继续后续步骤。

**失败与风控**

- <code>401</code>、<code>403</code>、非预期 HTML 或没有 <code>Location</code> 都应视为登录失败。
- 出现验证码、二次验证或风控页面时，不应循环重试同一账号；保存脱敏错误状态并交给上层处理。

### 5.3 导航登录中转与 sid

#### 5.3.1 访问 /login

~~~http
GET https://navigator-lxa.mail.com/login?<login-redirect-query>
Referer: https://www.mail.com/premiumlogin
Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8
~~~

该请求确认或建立导航侧登录状态。响应可能是 <code>200</code> 或继续 <code>302/303</code>。

#### 5.3.2 访问 /halogin

将上一步 <code>Location</code> 的查询参数复制到 <code>/halogin</code>，并补充：

~~~text
auth_time=1
tz=<本地时区小时数>
~~~

示例（占位符）：

~~~http
GET https://navigator-lxa.mail.com/halogin?
edition=<value>&usertype=<value>&goto=<value>&gotoparams=<value>&
ibaInfo=<value>&auth_time=1&uasServiceID=<value>&lang=<value>&
ott=<one-time-value>&tz=8
Referer: https://navigator-lxa.mail.com/login
~~~

<code>tz</code> 的具体语义在样本中是本地时区小时数；代码按当前机器 <code>utcoffset()</code> 计算，不应固定为 <code>8</code>。查询参数必须 URL 编码，不能拼接未编码的 <code>gotoparams</code> 或一次性值。

允许跟随此请求的重定向。最终 URL、历史 URL 或历史响应 <code>Location</code> 中只要出现 <code>navigator-lxa.mail.com</code> 且查询参数包含 <code>sid</code>，即可提取本次导航会话 ID。

<code>sid</code> 是短期、一次性/会话级凭据，不能复用到其他账号、长期保存或写入 URL 日志。

### 5.4 OAuth token 交换

**请求**

~~~http
POST https://oauthbridge.navigator-lxa.mail.com/navigator/oauth2/token?sid=<sid>
Content-Type: application/x-www-form-urlencoded
Accept: */*
Origin: https://webmailer.mail.com
Referer: https://webmailer.mail.com/
X-UI-App: mailcom.webmailer.mail-list/6.6.3
Authorization: Basic <base64(client_id:public_client_secret)>

grant_type=urn%3Amam%3Aoauth%3Agrant-type%3Aspa&scope=mail_mailbox_r
~~~

当前 demo 使用的公开客户端标识为：

~~~text
client_id = mailcom_webmailermaillist_passport_live
scope     = mail_mailbox_r
~~~

<code>Authorization: Basic ...</code> 是 OAuthBridge 客户端认证的一部分。HAR 导出没有保留该头，demo 中的客户端标识/公开字段来自 Webmailer 前端配置恢复；正式接入时应从当前前端版本重新确认，不能把用户密码放到 Basic 认证中。

**响应**

HTTP <code>200</code> JSON，已观察到的顶层字段：

~~~json
{
  "access_token": "<bearer-token>",
  "scope": "mail_mailbox_r",
  "token_type": "<token-type>",
  "expires_in": 3600
}
~~~

实际过期时间和 scope 以服务端响应为准。缺少 <code>access_token</code> 时流程立即失败。

**scope 说明**

HAR 还观察到 Webmailer 其他组件申请 <code>mail_account_r</code>、<code>mail_mailbox_w</code>、<code>mail_attachment_r</code>、<code>mail_confix_*</code> 等 scope。匹配邮件只需要已验证的 <code>mail_mailbox_r</code>；不要为了“完整权限”申请写权限或收集权限。

### 5.5 查询收件箱邮件列表

**请求 URL**

~~~http
POST https://maillist.mail.com/Mailbox/Mail?
folderTypeOrId=INBOX&offset=0&amount=50&
orderBy=INTERNALDATE%20DESC&no_cache=<cache-buster>
~~~

分页时将 <code>offset</code> 增加 <code>amount</code>，例如 <code>50</code>、<code>100</code>。当前 demo 将 <code>amount</code> 固定为 50，并限制最多扫描 10000 条，避免异常响应导致无限循环。

**请求头**

~~~http
Accept: application/vnd.1and1.mms.unified-maillist-v1+json; charset=utf-8
Authorization: Bearer <access-token>
Content-Type: application/vnd.1and1.mms.inboxadrequest-v1+json; charset=utf-8
Origin: https://webmailer.mail.com
Referer: https://webmailer.mail.com/
X-Request-ID: <uuid>
X-UI-App: mailcom.webmailer.mail-list/6.6.3
~~~

**请求体**

~~~json
{
  "aditionContext": {
    "brand": "mailcom",
    "category": "mail",
    "section": "3c/folder",
    "tagid": "inline",
    "layoutclass": "b"
  },
  "deviceContext": {
    "app": {"name": "browser"},
    "deviceclass": "b"
  },
  "adBlocker": false,
  "mailboxContext": {
    "currentPage": 1,
    "visibleMessages": 8
  }
}
~~~

<code>aditionContext</code> 是服务端协议中的拼写，必须保持原样。<code>currentPage</code> 应与 <code>offset / amount + 1</code> 对应；<code>visibleMessages</code> 是前端布局上下文，不是实际返回条数。

**响应**

响应媒体类型为 <code>application/vnd.1and1.mms.unified-maillist-v1+json</code>。已观察到的关键字段：

~~~json
{
  "mailListElements": [
    {
      "type": "mail",
      "rawData": {
        "attribute": {
          "mailIdentifier": "<mail-id>",
          "internalDate": 0,
          "folderType": "INBOX",
          "read": false
        },
        "mailHeader": {
          "from": "Display Name <sender@example.com>",
          "subject": "<subject>",
          "date": 0
        }
      }
    }
  ],
  "mailsURI": "<uri>",
  "totalCount": 0,
  "unreadCount": 0
}
~~~

列表也可能包含广告或其他非邮件元素，例如 <code>type=ad</code>；只处理 <code>type=mail</code>。邮件 ID 通常位于 <code>rawData.attribute.mailIdentifier</code>，代码会校验字符集后再放入路径。

**按发件人取最新邮件**

1. 请求参数显式使用 <code>orderBy=INTERNALDATE DESC</code>。
2. 对每个 <code>type=mail</code> 读取 <code>rawData.mailHeader.from</code>。
3. 用邮箱地址解析器比较地址部分，忽略显示名并进行大小写不敏感匹配。
4. 由于服务端已按时间倒序，遇到的第一个匹配项就是最新邮件。
5. 当前页没有匹配且 <code>offset + amount &lt; totalCount</code> 时继续下一页。

不要仅用字符串 <code>contains</code> 判断发件人，否则可能把 <code>not-sender@example.com</code> 错当成目标地址。

### 5.6 获取邮件完整邮件头

**请求**

~~~http
GET https://webmail-cats-live.mail.com/mailbox/primary/mailheader/<mail-id>?
absoluteURI=false&no_cache=<cache-buster>
Accept: application/vnd.ui.trinity.message+json; charset=utf-8; client-meta=mail-drop;
Authorization: Bearer <access-token>
Origin: https://webmailer.mail.com
Referer: https://webmailer.mail.com/
X-Request-ID: <uuid>
X-UI-App: mailcom.webmailer.mail-detail/7.40.1
~~~

路径中的 <code>&lt;mail-id&gt;</code> 必须 URL 编码。<code>absoluteURI=false</code> 保持与 Webmail 请求一致。

**响应关键字段**

~~~json
{
  "attribute": {
    "mailIdentifier": "<mail-id>",
    "size": 0,
    "hasHtmlDisplayPart": true,
    "folderType": "INBOX",
    "internalDate": 0,
    "read": false
  },
  "mailHeader": {
    "from": "Display Name <sender@example.com>",
    "replyTo": "<optional>",
    "to": ["<recipient>"],
    "cc": [],
    "subject": "<subject>",
    "date": 0,
    "messageId": "<message-id>"
  },
  "mailBodyURI": "<uri>",
  "attachments": {"attachment": []},
  "security": {
    "senderAuthenticated": false,
    "senderTrusted": false,
    "contentTrusted": false
  }
}
~~~

邮件头接口用于补齐或确认 <code>subject</code>、<code>from</code>、<code>date</code> 等字段；它不是正文接口，也不应假设返回正文内容。

### 5.7 获取 HTML 邮件正文

**请求**

~~~http
POST https://mailcom.mailbody-ui.de/Mail/<mail-id>/Body/html?
target_origin=https%3A%2F%2Fwebmailer.mail.com&no_cache=<cache-buster>
Content-Type: application/x-www-form-urlencoded
Accept: text/html,application/xhtml+xml
Origin: https://webmailer.mail.com
Referer: https://webmailer.mail.com/

access_token=<access-token>
~~~

HAR 中该请求以跨站 iframe 导航形态出现，可能带有 <code>Sec-Fetch-Dest: iframe</code>、<code>Sec-Fetch-Storage-Access: active</code> 等浏览器头；协议客户端可先发送上面业务必需头，遇到服务端策略变化再按当前浏览器重新核对。

**响应**

- HTTP <code>200</code>。
- <code>Content-Type: text/html;charset=UTF-8</code>。
- 正文可能包含 HTML、内联样式、脚本、图片和邮件原始格式内容。
- 当前 demo 使用 <code>HTMLParser</code> 删除 <code>script</code>、<code>style</code>、<code>noscript</code>，按块级标签换行，并输出纯文本；不会把原始 HTML 写盘。

不要把邮件 HTML 直接插入主项目管理后台的同源页面。若必须渲染，应使用隔离 iframe、严格 CSP 和明确的 HTML 清洗策略；邮件正文应视为不可信输入。

### 5.8 管理 mail.com 别名（已接入 provider 的 settings 协议）

本节来自 <code>navigator-lxa.mail.com1.har</code> 中已经登录的浏览器操作。核心服务为
<code>https://settings-cats.mail.com</code>，页面来源为
<code>https://mailset-root.mail.com</code>。HAR 导出中的 settings 请求没有保留显式 <code>Authorization</code> 或 <code>Cookie</code> 值；这不代表接口可匿名调用。与 HAR 同批保存的 settings 前端 bundle 明确显示：页面会用
<code>mailcom_mailset_root_live</code> 换取包含 <code>webmailer_setting_r</code>/<code>webmailer_setting_w</code> 的短时 OAuth token，并以 <code>Authorization: Bearer &lt;settings-token&gt;</code> 调用 settings API。项目按规范化母号邮箱维护仅进程内的 settings token 缓存，缓存项只有 Bearer token 与 <code>expires_at</code>；不保存 Cookie、<code>sid</code>、HTTP session、账密或 mailbox AT，也不会写入数据库、JSON 回退、配置文件、API 响应或日志。

每个 <code>MailComSettingsClient</code> 仍持有自己的临时 HTTP session。新任务命中缓存且 token 距过期仍大于 60 秒时，直接使用该 token 发起地址列表/别名请求；不会再次提交账密、执行 bootstrap 或复用旧 session。剩余 60 秒或更少、进程重启，或 settings API 返回 HTTP <code>401</code> 时，才在母号专属 settings 锁内重新执行登录、bootstrap 与 token 交换。该锁独立于 mailbox AT 刷新锁和母号别名创建/删除锁。

缓存未命中或需刷新时，客户端必须完成以下临时 bootstrap。<code>sid</code>、<code>iac_token</code>、mailset-root 启动 URL 和 settings token 只能留在当前进程的内存中，异常与日志中均不得输出其值：

~~~text
账密登录 -> navigator sid
  -> GET /mail_settings?sid=<sid>
  -> 从 HTML 中严格提取同 sid 的 https://mailset-root.mail.com/?navsid=...&iac_appname=...&iac_token=...
  -> GET 该 mailset-root 启动 URL
  -> POST oauth2/token（scope: mail_mailbox_w webmailer_setting_r webmailer_setting_w mail_confix_w）
  -> Authorization: Bearer <settings-token> 调用 settings-cats
~~~

HTTP <code>401</code> 的恢复规则必须区分幂等读与别名写操作：<code>GET /emailAddresses</code> 刷新 token 后最多重试一次；候选校验是无持久化副作用的校验请求，可刷新后重试一次；创建或删除别名的 POST 收到 <code>401</code> 后不得重放原请求。创建改为刷新后回读地址列表，只有目标地址已为 <code>ACTIVE/deletable</code> 才确认成功；删除改为刷新后回读，只有目标地址已不存在才确认成功，否则返回确认失败。

日志按脱敏阶段记录 <code>settings_navigation</code>、<code>settings_root</code>、<code>settings_oauth_token</code>、<code>email_addresses</code>、<code>email_address_validation</code>、<code>email_address_create</code> 和 <code>email_address_delete</code>，仅含阶段、动作、HTTP 状态和错误类别。不得记录 token、<code>sid</code>、<code>iac_token</code>、完整启动 URL、Cookie、密码、邮箱明文或请求体。

当前 settings 请求形状、缓存失效与 401 分流已由脱敏 HAR fixture 和 mock contract test 覆盖；自动化测试不向真实 mail.com 账户发送创建或删除请求。因此，认证权限、风控与会话失效的真实行为仍应在隔离账户中确认。

以下业务请求均观察到相同的页面上下文头：

~~~http
Origin: https://mailset-root.mail.com
Referer: https://mailset-root.mail.com/
X-Request-ID: <uuid>
X-UI-App: mailcom.mailset-compose/1.0.6
~~~

<code>X-Request-ID</code> 必须每次新建；不要记录其真实值。浏览器还会发送
<code>sec-fetch-*</code>、<code>sec-ch-ua*</code> 等导航头，它们不是本节已验证的业务字段。

#### 5.8.1 获取当前活动地址并用于结果确认

**请求**

~~~http
GET https://settings-cats.mail.com/mailaccount/primary/emailAddresses?
absoluteURI=false&q.state.in=ACTIVE&q.type.in=MANAGED,DOMAIN_HOSTING
Accept: application/vnd.ui.trinity.mailaddress.list-v5+json
Content-Type: application/vnd.ui.trinity.mailaddress.list-v5+json
Origin: https://mailset-root.mail.com
Referer: https://mailset-root.mail.com/
X-Request-ID: <uuid>
X-UI-App: mailcom.mailset-compose/1.0.6
~~~

**响应**

HTTP <code>200</code>，响应媒体类型为
<code>application/vnd.ui.trinity.mailaddress.list-v5+json</code>。已观察到的结构为：

~~~json
{
  "mailaddresslist": [
    {
      "type": "MANAGED",
      "entryDate": 0,
      "address": "<redacted-address>",
      "displayName": "<optional>",
      "deletable": true,
      "pgpEnabled": false,
      "defaultSenderAddress": false,
      "defaultReceiverAddress": false,
      "state": "ACTIVE",
      "_links": {}
    }
  ],
  "_links": {"self": {}}
}
~~~

HAR 中三次列表快照的地址数量依次为 <code>4 -&gt; 5 -&gt; 4</code>：创建成功后多出一项，删除后恢复。
这证明应以地址列表回读作为写操作的最终确认，不能仅依赖创建响应正文。地址和显示名均属于个人数据，
日志中只能记录数量、脱敏地址或不可逆摘要。

#### 5.8.1.1 获取生命周期历史容量（只读）

创建前或用户手动刷新容量时，使用同一 settings 会话调用下列 URL：

~~~http
GET https://settings-cats.mail.com/mailaccount/primary/emailAddresses?
absoluteURI=false&q.type.in=MANAGED,DOMAIN_HOSTING
~~~

该请求刻意不带 <code>q.state.in=ACTIVE</code>，因此会同时返回母号和历史
<code>INACTIVE</code> 别名。只在内存中解析后保存聚合字段，不保存地址、
<code>displayName</code>、<code>_links</code>、响应正文或认证材料：

- <code>deletable=true</code> 计入生命周期累计别名，母号的
  <code>deletable=false</code> 行不计入；
- <code>state=ACTIVE && deletable=true</code> 同时计入活动别名；
- 可删除但状态未知的行仍占用生命周期计数，并增加
  <code>remote_history_unknown_count</code>；缺字段或明确分页未完成时状态为
  <code>capacity_unknown</code>，不得推断存在剩余容量。

生命周期上限是母号之外累计 <strong>99</strong> 个别名，活动上限仍是
<strong>9</strong> 个。生命周期剩余为 <code>99 - lifetime_count</code>（下限为
零），创建预算为 <code>min(active_gap, lifetime_remaining)</code>。

历史快照默认缓存 12 小时（<code>MAILCOM_LIFETIME_SNAPSHOT_TTL_SECONDS=43200</code>），
生命周期剩余不超过 9 个时创建前强制刷新。普通 <code>GET /api/mailcom</code>、
页面轮询和候选地址校验只读本地快照；创建批次成功后最多校准一次。首个创建
<code>409</code> 会停止当前批次并强制刷新，按结果分类为
<code>lifetime_capacity_full</code>、<code>active_capacity_full</code>、
<code>remote_create_conflict</code> 或 <code>capacity_unknown</code>。

#### 5.8.2 HAR 中的候选域名目录与本地静态目录

**请求**

~~~http
GET https://settings-cats.mail.com/domains?
absoluteURI=false&q.state.eq=ACTIVE&q.owner.eq=gmx_MAILCOM
Accept: application/json
Content-Type: application/json
Origin: https://mailset-root.mail.com
Referer: https://mailset-root.mail.com/
X-Request-ID: <uuid>
X-UI-App: mailcom.mailset-compose/1.0.6
~~~

**响应**

HTTP <code>200</code>，顶层只有 <code>domains</code> 数组。单项的已观察字段为：

~~~json
{
  "domain": "<domain>",
  "categories": ["<category>"],
  "domainGroups": ["mailcom.Freemail", "mailcom.Premium"],
  "owner": "gmx_MAILCOM",
  "state": "ACTIVE",
  "type": "PRIMARY"
}
~~~

本 HAR 的响应含 <code>138</code> 个互不重复的域名；顶层没有
<code>page</code>、<code>offset</code>、<code>next</code>、<code>cursor</code>、
<code>limit</code> 或 <code>total</code> 等续页字段，且全部为
<code>ACTIVE / gmx_MAILCOM / PRIMARY</code>，每项同时包含
<code>mailcom.Freemail</code> 与 <code>mailcom.Premium</code> 分组。

该响应是当前静态目录的取证来源，而不是运行时依赖。项目将其中 138 个唯一、规范化小写的活动域名保存为
<code>config/mailcom_alias_domains.json</code>，由 <code>core/mailcom_alias_domains.py</code> 在创建前严格校验数量、重复项和域名格式，再随机选择一个。运行时禁止请求 <code>/domains</code>，也禁止从数据库或母号域名隐式回退。

目录只能提供候选域名，不等于“当前账号一定可创建”。远端别名数、local-part 冲突、套餐、风控和服务端策略仍以第 5.8.1、5.8.3 至 5.8.5 节的结果为准。目录更新必须作为受版本控制的数据维护动作，并重新固定 138 项测试。

#### 5.8.3 校验候选别名

**请求**

~~~http
POST https://settings-cats.mail.com/mailaccount/emailAddressValidations?absoluteURI=false
Accept: application/vnd.ui.trinity.email-address-validation-response+json
Content-Type: application/vnd.ui.trinity.email-address-validation-request+json
Origin: https://mailset-root.mail.com
Referer: https://mailset-root.mail.com/
X-Request-ID: <uuid>
X-UI-App: mailcom.mailset-compose/1.0.6

["<candidate-alias@example.test>"]
~~~

请求体是只含一个地址字符串的 JSON 数组，而不是 JSON 对象或单个 JSON 字符串。HAR 中两次校验均返回
HTTP <code>200</code>、媒体类型
<code>application/vnd.ui.trinity.email-address-validation-response+json</code> 和空对象
<code>{}</code>。

空校验结果不能当作创建成功保证：同一 HAR 中一次校验返回 <code>200</code> 后，随后的创建仍得到
<code>412</code>。校验只能作为前置检查，创建和地址列表回读才决定实际结果。

#### 5.8.4 创建别名

**请求**

~~~http
POST https://settings-cats.mail.com/mailaccount/primary/emailAddresses?absoluteURI=false
Accept: application/vnd.ui.trinity.minimalmailaddress-v3+json
Content-Type: application/vnd.ui.trinity.minimalmailaddress-v3+json
Origin: https://mailset-root.mail.com
Referer: https://mailset-root.mail.com/
X-Request-ID: <uuid>
X-UI-App: mailcom.mailset-compose/1.0.6

{
  "address": "<candidate-alias@example.test>",
  "deletable": true,
  "pgpEnabled": false,
  "defaultSenderAddress": false,
  "defaultReceiverAddress": false,
  "state": "ACTIVE"
}
~~~

**成功与失败响应**

- 成功样本为 HTTP <code>201</code>。
- 失败样本为 HTTP <code>412</code>。
- 两个样本的响应媒体类型都是 <code>application/json</code>，且都被 CATS 代理包装为下列形态；
  <code>status</code> 随 HTTP 状态变化：

~~~json
{
  "type": "urn:problem:mam:cats:target-response-masked",
  "detail": "The target system's response has been masked by CATS.",
  "status": 201
}
~~~

因此不可从该响应正文推导“地址已创建”或失败原因。处理逻辑应以 HTTP 状态为第一信号：仅在
<code>201</code> 后刷新第 5.8.1 节的地址列表确认候选地址存在；<code>412</code> 应作为业务拒绝返回给
上层，不要盲目重试或改写地址。

#### 5.8.5 删除别名

**请求**

~~~http
POST https://settings-cats.mail.com/mailaccount/primary/emailAddressesRemovals/<urlencoded-alias>/removals?absoluteURI=false
Accept: text/plain;charset=UTF-8
Content-Type: text/plain;charset=UTF-8
Origin: https://mailset-root.mail.com
Referer: https://mailset-root.mail.com/
X-Request-ID: <uuid>
X-UI-App: mailcom.mailset-compose/1.0.6
~~~

该请求没有业务请求体。<code>&lt;urlencoded-alias&gt;</code> 是单一路径段，后续实现应使用
<code>encodeURIComponent</code> 或等价的路径段编码函数构造，不能直接拼接未校验地址。

HAR 中删除成功返回 HTTP <code>204</code> 和空响应。收到 <code>204</code> 后仍应刷新第 5.8.1 节的
地址列表，确认该地址不再存在。实现还将 HTTP <code>404</code> 作为“地址已不存在”的幂等结果，但这是一项保守的本地语义：仍须回读确认地址确实不存在；HAR 没有独立验证重复删除或非可删除地址的服务端含义。

#### 5.8.6 实现状态机、容量与并发边界

~~~text
本地 138 域名目录 + 随机 local-part
  -> GET /primary/emailAddresses
  -> 读取缓存/必要时读取全量历史，预算=min(活动缺口, 生命周期剩余)
  -> 活动 >=9 或生命周期 >=99 时终止任务
  -> POST /emailAddressValidations
  -> POST /primary/emailAddresses
  -> GET /primary/emailAddresses（确认创建）
  -> 原子持久化 alias -> parent

POST /emailAddressesRemovals/<alias>/removals
  -> GET /primary/emailAddresses（确认删除）
~~~

创建和删除共用 <code>mother_alias_lock(parent_email)</code>，历史刷新使用独立的母号去重锁，与 mailbox AT 刷新锁分离。锁覆盖“远端容量读取至地址列表确认”的短临界区，不覆盖 OpenAI 注册和 OTP 轮询，因此一个母号可在确认 alias 后重新领取给下一个任务。当前锁保证单个项目进程内串行；多进程部署必须通过单进程 WebUI/worker 或额外的跨进程协调边界保证同一母号不并发创建。

创建遇到 <code>412</code> 或服务端校验明确拒绝时最多换候选重试；首个创建 <code>409</code> 立即停止本批次并只做一次历史校准，分类为 <code>lifetime_capacity_full</code>、<code>active_capacity_full</code>、<code>remote_create_conflict</code> 或 <code>capacity_unknown</code>。达到重试上限、容量满、认证/风控失败、字段缺失或回读未确认都会终止当前任务，绝不回退使用母号直接注册或隐式换用其他来源。删除接受 <code>204</code> 成功与 <code>404</code> 已不存在；不因删除立即读取历史，生命周期累计数不减少。其他删除错误保留别名并交给 <code>cleanup_pending</code> 审计状态。

## 6. 状态码与错误处理

当前 demo 对以下成功状态做了显式校验：

| 阶段 | 允许状态 |
| --- | --- |
| 登录页 | <code>200</code> |
| 登录提交 | <code>302</code>、<code>303</code> |
| 登录中转 | <code>200</code>、<code>302</code>、<code>303</code> |
| 登录完成 | <code>200</code> |
| OAuth token | <code>200</code> |
| 邮件列表 | <code>200</code> |
| 邮件头 | <code>200</code> |
| 邮件正文 | <code>200</code> |
| 当前地址列表（别名管理） | <code>200</code> |
| 别名候选域名目录（HAR 取证，运行时不请求） | <code>200</code> |
| 别名校验 | <code>200</code>（仍需创建确认） |
| 别名创建 | <code>201</code> 后回读列表确认 |
| 别名删除 | <code>204</code> 或本地幂等的 <code>404</code> 后回读列表确认 |

建议的上层分类：

- settings <code>401/403</code>：分别分类为 <code>unauthorized</code> 与 <code>forbidden_or_risk</code>；停止当前 settings 会话，不要立即无限重试。
- mailbox <code>401</code>：仅 <code>WWW-Authenticate: Bearer error="invalid_token"</code> 可触发一次 AT 登录恢复；其余 401、403、429、5xx、超时和风控页不会自动重登。
- <code>429</code>：限流；尊重 <code>Retry-After</code>，结束当前账号尝试或按全局退避策略排队。
- <code>5xx</code>、连接超时、TLS 错误：可在幂等读取请求上有限重试，但必须避免重复登录提交。
- <code>200</code> 但 JSON 结构缺失：视为协议变更或异常响应，记录接口名称和字段路径，不记录响应原文。
- 找不到匹配邮件：这是业务结果，不应伪装成网络错误；可由上层决定是否等待后再次查询。

同一账号或会话遇到 <code>403</code> 或 <code>429</code> 后，推荐立即停止当前会话。此前真实测试曾在重复登录后触发 mail.com <code>403</code> 防护，因此不要把“失败就重登”作为默认重试策略。

## 7. 主项目 provider、别名路由与 AT 持久化

主项目中的 `mailcom` 是独立邮箱来源。导入的 mail.com 账号是母号：它保存账密和 mailbox AT，提供共享收件箱；每次 OpenAI 注册前由 provider 创建一个新的 alias，并把 alias 作为注册邮箱。它不包含 mail.com 新账号注册、CAPTCHA、手机验证、二次验证、Cookie/sid 持久化或改密流程。

### 7.1 母号与 alias 的持久化边界

母号记录保存以下运行时字段：

```text
email, password, status, used_at,
mail_access_token, mail_access_token_expires_at,
mail_access_token_updated_at, mail_auth_error,
remote_active_alias_count, remote_lifetime_alias_count,
remote_lifetime_alias_limit, remote_history_synced_at,
remote_capacity_status, remote_history_unknown_count,
remote_history_error
```

alias 记录位于独立的 `mailcom_aliases` 集合，至少保存：

```text
alias_email, parent_email, local_part, domain, status,
registration_job_id, registration_started_at, registered_account_id,
created_at, deleted_at, plan_check_status, cleanup_status, last_error
```

邮箱池 `status` 统一使用五种值：`available`（可领取）、`registering`（注册中）、
`used`（已消耗）、`failed`（注册失败且永久不可领取）和 `disabled`（停用/远端删除且不可领取）。
其中只有 `available` 可以被注册任务领取；`failed` 与 `disabled` 不会因导入、同步或重试恢复。

- SQLite 模式分别使用 `runtime_records(kind=mailcom_emails)` 与 `runtime_records(kind=mailcom_aliases)`；JSON 回退模式使用独立、已忽略的 `mailcom_emails.json` 与 `mailcom_aliases.json`。
- alias 地址和母号地址以小写键保存，`alias_email` 全局唯一。重复创建同一地址只返回原有同母号记录；归属不同母号时显式失败。
- alias 记录不复制密码、mailbox AT、Cookie、`sid`、settings 认证材料或邮件正文。WebUI 的 `GET /api/mailcom/aliases` 只返回 alias、母号掩码、生命周期、任务/账号关联、套餐/清理摘要和脱敏错误。
- 母号只在创建 alias 的短临界区被领取。alias 远端确认后立即释放母号租约；成功账号消耗 alias 槽位，不会把母号标记为 `used`。注册成功的 alias 标记为 `used`；没有本地账号的终态失败标记为 `failed`，不会自动复用 alias。

### 7.2 alias 到母号的 OTP 路由

`resolve_email_source()` 优先查 alias 映射。OTP 读取收到 alias 时必须先解析到活动 alias 和可用母号；未知 alias、非活动 alias、母号不存在或母号已禁用都会显式失败，不会把 alias 当作独立 mailbox，也不会退回到“只按 OpenAI 发件人”取最新邮件。

alias 模式候选邮件必须同时满足以下条件：

1. 通过现有 OpenAI/ChatGPT 发件人和主题识别；
2. 列表 `internalDate` 不早于 `registration_started_at`；
3. 读取完整头后，`mailHeader.to` 可结构化解析且包含当前 alias 的大小写不敏感精确地址；
4. 正文包含独立六位 OTP。

任何一项不满足均 fail-closed：缺失或无法解析的 `to`、相似 local-part、其他 alias、旧邮件或无效 OTP 都不得返回验证码。每次 `fetch_latest_otp()` 都维护自己的候选与 settle 状态，多个 alias 共享母号时不会通过全局“最新邮件”缓存串码。

### 7.3 AT-only 冷启动

新进程读取一条未过期记录时，创建全新的 HTTP session，只设置：

```http
Authorization: Bearer <mailbox-at>
```

然后直接请求邮件列表、邮件头和正文接口。不会先访问登录页，不会执行 OAuth
交换，也不会加载任何 Cookie/sid。该行为已由无 Cookie/sid 的 mock 冷启动探针覆盖；
它证明 client 的持久化边界正确，不代表真实服务端未来永远接受同一个 AT。

### 7.4 到期与失效恢复

正常路径：

```text
存在 AT 且 now < mail_access_token_expires_at
  -> 直接读信
否则
  -> 账密登录 -> OAuth mail_mailbox_r -> 原子写回 AT/expires_at -> 读信
```

OAuth token 响应必须同时包含非空 `access_token` 和正数 `expires_in`。到期时间按
`now + expires_in` 计算；字段缺失时快速失败，不猜测默认时长。

服务端提前失效时，只有同时满足下列三项才允许恢复：

1. 邮件读取接口的 HTTP 状态为 `401`；
2. `WWW-Authenticate` 响应头中存在可解析的 `Bearer` challenge；
3. challenge 的 `error` 参数（大小写不敏感）等于 `invalid_token`。

已验证的伪造 AT 响应为：

```http
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer error="invalid_token", error_description="Provided token isn't active"
```

该响应 body 为空，不是 JSON。实现不能通过正文字符串匹配判断失效。

### 7.5 单次互斥恢复语义

```text
读信
  -> 401 + Bearer error=invalid_token
  -> 获取邮箱级锁
  -> 再读数据库：已有其他任务写入的新未过期 AT 时直接复用
  -> 否则按账密登录并交换新 AT，条件原子写回
  -> 只重试原读请求一次
  -> 仍失败即返回脱敏错误，不再递归登录
```

条件写入要求旧 AT 仍与预期值一致，避免慢任务把较新的 AT 覆盖掉。401 缺少
`invalid_token`、403、429、5xx、超时、风控 HTML 和协议结构错误都不会自动登录；
仅记录脱敏错误类型，并保留当前 AT。

### 7.6 套餐查询驱动的 alias 清理

配置项 `MAILCOM_DELETE_ALIAS_IF_NO_TRIAL` 默认是 `false`，可通过 WebUI “邮箱 / OTP” 中的“明确无试用资格后删除 mail.com 别名”开关设置。`POST /api/config` 只接受 JSON 布尔值，避免字符串或数值被静默解释为删除授权。

套餐解析结果必须同时满足以下条件才允许调用 settings 删除接口：

1. 对应 OpenAI 账号已关联一个状态为 `used` 的 alias；
2. 开关已启用；
3. `ok=true`；
4. `trial_eligibility_known=true`，即套餐和促销资格字段已完整解析；
5. `plus_trial_eligible` 严格为 JSON 布尔值 `false`。

当套餐或促销字段缺失、类型错误，或 `plus` campaign 的结构不完整时，解析结果为
`trial_eligibility_known=false` 且 `plus_trial_eligible=null`。该三态边界会被保存到账号状态并在
WebUI 显示为“Plus 试用资格未明确”；它绝不能被折叠为 `false`，也不会触发删除请求。

```text
查询失败 / 超时             -> plan_check_status=failed,      cleanup_status=not_requested
字段缺失或不可判定           -> plan_check_status=incomplete,  cleanup_status=not_requested
明确有试用资格               -> plan_check_status=success,     cleanup_status=not_eligible
开关关闭                     -> plan_check_status=success,     cleanup_status=not_requested
满足条件并删除确认           -> status=disabled, cleanup_status=deleted
删除请求失败或回读未确认     -> status=used, cleanup_status=cleanup_pending
```

清理权由 `claim_mailcom_alias_cleanup()` 原子领取。`cleanup_pending`、`cleanup_running` 和 `deleted` 不会因同一套餐结果再次自动发送删除请求；人工重试/批量修复不属于本变更范围。

## 8. 凭据、日志和数据保留

- <code>mail_test.txt</code> 仅用于本地实验，已加入 <code>.gitignore</code>；主项目应使用安全凭据存储，不应仿照明文三行文件进入生产。
- 母号密码和 mailbox AT 是本地运行时敏感数据，只保存在私有 `mailcom_emails` 存储；其余 alias 记录绝不复制它们。mailbox AT 与 OpenAI 账号 `access_token` 完全隔离。
- <code>sid</code>、Cookie、settings 临时认证材料、浏览器 profile、登录跳转 URL、邮件 ID、邮件正文和附件内容不持久化。
- 禁止记录完整 <code>Authorization</code>、Cookie、<code>Location</code> 查询串、<code>sid</code>、<code>ott</code>、密码、mailbox AT 或邮件正文。
- 错误日志只记录阶段、HTTP 状态、脱敏请求 ID 和可操作的错误类别。
- 邮件正文可能包含个人数据、验证码和恶意链接；应设置最小保留时间，完成取码后立即丢弃。
- HAR 文件含真实会话、请求参数和邮件数据，不能提交仓库、上传到第三方或作为测试 fixture；测试应使用人工构造的脱敏响应。

## 9. 当前主项目接入结构

当前实现按以下职责拆分，避免把 settings 写操作混入 mailbox AT 读信逻辑：

~~~text
mailcom_alias_domains
  -> 本地 138 域名加载、格式校验、随机选择、local-part 规范化
mailcom_settings_client
  -> 临时 settings 登录、地址列表、校验、创建、URL 编码删除、协议脱敏
mailcom_alias_service
  -> 母号锁、远端容量、候选重试、创建/删除回读确认、alias 持久化
mailcom_provider
  -> 母号短租约、alias 领取、alias -> mother、mailbox AT 恢复、定向 OTP
mailcom_alias_cleanup
  -> 套餐结果的安全判定与单次删除状态机
~~~

注册驱动只看到 alias 邮箱。`core/email_provider.py` 在 mail.com alias 创建失败时停止来源回退，避免任务在没有 alias 的情况下改用母号或静默切到后续来源。`core/registration_service.py` 和 `main.run_registration()` 都会在进入注册阶段时写入一次 `registration_started_at`；该值不会被后续 OTP 重试覆盖。

## 10. mail.com 自身注册能力的后续取证清单

本文不支持 mail.com 新账号注册开发。若要继续，需要在隔离测试账号上重新录制并记录至少以下步骤：

- 注册入口、地区/语言参数和可用性检查。
- 邮箱/用户名、密码、生日等字段提交 URL 与请求体。
- 验证码发送和校验接口，以及验证码失败/过期响应。
- CAPTCHA、设备校验、二次验证和风控跳转分支。
- 注册成功后的 session、欢迎邮件和账号初始化请求。
- 账号创建失败时是否消耗邮箱、是否允许重试、是否存在明确 <code>Retry-After</code>。

每次取证都应保存“字段结构和状态码”，而不是保存真实密码、Cookie、token 或完整邮件内容。注册接口若需要绕过 CAPTCHA、批量规避风控或违反 mail.com 服务条款，不应纳入主项目自动化范围。

## 11. 现有实现与验证

读取 demo：<code>mailcom_latest_mail_demo.py</code>。主项目 alias provider 的实现和 mock 验证覆盖如下：

~~~powershell
python -m pytest -q tests\test_mailcom_alias_domains.py tests\test_mailcom_settings_client.py tests\test_mailcom_alias_lifecycle.py tests\test_mailcom_client.py tests\test_mailcom_provider.py tests\test_email_provider_mailcom.py tests\test_mailcom_plan_cleanup.py tests\test_mailcom_storage.py tests\test_sqlite_store.py tests\test_webui_mailcom.py
python -m py_compile core\mailcom_alias_domains.py core\mailcom_settings_client.py core\mailcom_alias_service.py core\mailcom_alias_cleanup.py core\mailcom_provider.py core\mailcom_client.py
~~~

此前用更新后的 <code>mail_test.txt</code> 做过一次真实端到端验证，登录、OAuth、列表筛选和正文读取成功。该结果只证明当时账号、网络和服务端状态下的读取链路可用，不代表接口长期稳定。别名 settings 创建/删除在本次变更中没有发起真实写请求；其实现状态仅为 HAR 形状与 mock contract test 已验证。
