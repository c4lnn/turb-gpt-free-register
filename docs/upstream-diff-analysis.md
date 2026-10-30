# Upstream 差异分析（长期对齐工程）

> 本文档是「本地 `main` 与上游 `upstream/main` 逐步对齐」的基准快照与路线图。
> 每完成一步，在第四节 checklist 勾选，并更新「当前进度」小节。

## 比较基准（快照）

| 项 | 值 |
|---|---|
| 本地 `main` | `74dab929d41dd4864f8101fa1b5cc71aca313380` |
| 上游 `upstream/main` | `e59bddf2bcf5787205ee6243a5f56bc560b4fb9a` ← 对齐起点基线 |
| merge-base | `d80cb8c7a21d6a327cd5d0e82645a8a84b2db9ab` |
| 上游 HEAD 提交 | `注册任务性能优化`（myfanhua，2026-10-24） |
| 比较日期 | 2026-10-28 |

> 约定：`git diff main upstream/main` 中
> - **A** = 远端有、本地无
> - **D** = 本地有、远端无
> - **M** = 两边都有但实现不同
>
> 已排除本地**主动删去**的能力（不计入「远端新增」）：
> GPTMail / MailNest / CloudMail 邮箱，cloakbrowser / browser_use / skyvern 驱动，Codex Agent Token。

---

## 分叉关系

```
                     merge-base d80cb8c
                           |
            +--------------+--------------+
            |                             |
        本地 main                     upstream/main
    (大量自研能力)                (巡检/新邮箱/新短信)
   - iCloud / SMSBower V1        + IMAP / MailCode / 西瓜接码
   - Checkout 检测               + 代理链 / wreq 协议驱动
   - SQLite 按来源分表            + SQLite 统一 email_pool
```

---

## 一、远端新增的能力（A，排除本地删去的）

| 能力 | 新增文件 | 说明 |
|---|---|---|
| 一键巡检 | `core/account_inspection_service.py`、`core/inspection_lease.py`、`webui/templates/account_inspections.html` | 仅对失效账号查活，跳过已废账号，失败详情落库（db 新增 `account_inspections` / `_snapshots` / `_failures` 三表） |
| IMAP 邮箱源 | `core/imap_mail_client.py` | 取代 iCloud 走通用 IMAP 取码 |
| 域名邮箱 MailCode | `core/mailcode_client.py` | 新增域名邮箱服务商 |
| 西瓜接码 | `core/xigua_client.py` | 新增短信服务商 + 全套 `XIGUA_*` 配置 |
| HTTP 诊断 | `core/http_diagnostics.py` | 403 重试 / 2FA 403 日志诊断 |
| 协议 profiles | `core/protocol_profiles.py` | 协议驱动 profile 管理 |
| 代理链 | `core/proxy_chain.py`、`core/proxy_utils.py` | 上游代理链（`PROXY_POOL_UPSTREAM_PROXY`、`PLAN_CHECK_UPSTREAM_PROXY`） |
| 2FA 独立服务 | `core/twofa_service.py` | 并发/队列/代理模式可配置（`TWOFA_WORKERS`/`TWOFA_QUEUE_LIMIT`/`TWOFA_PROXY_MODE`） |
| wreq 协议驱动 | `core/wreq_session.py` | 本地已声明并实测 `wreq==0.13.0`；注册专用 `chrome152`，独立任务仍按共享配置选择后端 |
| 优惠力度查询 | `webui/templates/promo_details.html` | `chatgpt_plan.py` 解析 `eligible_promo_campaigns` / `discount` |
| 注册性能基准 | `tools/benchmark_registration_jobs.py` | 注册任务性能优化 |

> **明确不需要**：邮箱改密/换绑（`core/email_change_service.py`）与改密（本地 `core/mail_password_change.py` 空壳，待删）。这两项不纳入对齐路线。

> 上游还**保留**了本地删掉的 gptmail/mailnest/cloudmail、cloakbrowser/browser_use/skyvern、codex_agent（属「本地删去」，不计入新增）。

---

## 二、本地独有、远端没有的能力（D）

| 能力 | 文件 | 说明 |
|---|---|---|
| Checkout Session 检测 | `core/chatgpt_checkout.py`、`core/checkout_session_service.py`、`config/checkout_session.py` | 本地自研，远端无 |
| iCloud 邮箱 | `core/icloud_client.py` | 隐私邮箱导入 + QQ IMAP 转发（远端用 imap 取代） |
| SMSBower 独立 provider | `core/smsbower_provider.py` | V1 handler API + 日志脱敏 |
| SQLite 存储封装 | `core/sqlite_store.py` | `SQLiteRuntimeStore` 独立封装 |
| Masi CDK 池 | `core/masi_cdk_pool.py`、`tools/poll_existing_masi_job.py` | 远端无 |
| OTP 操作锁 | `core/otp_operation_lock.py` | 远端无 |
| 邮箱池状态 / 账号状态契约 | `core/email_pool_status.py`、`core/account_status_contracts.py` | 本地统一状态分类契约 |
| 提链 providers | `core/extract_link_providers.py` | 远端把逻辑内联进 `extract_link_service.py` |
| 运维工具 | `tools/{migrate_runtime_storage,runtime_storage_admin,query_chatgpt_plan}.py` | 本地独有 |
| 凭证明文可追溯日志 | `core/account_export.py` / `core/openai_auth.py` | 本地有意保留的反向决策：密码/完整 TOTP secret/验证码明文写入 job log（上游「验证码已隐藏」） |

---

## 三、两边都有、但实现不同的核心差异（M）

### 存储层 `core/db.py`（差异最大，约 6900 行变动）

```
本地：SQLite 按来源分表                远端：SQLite 统一 email_pool
+---------------------------+         +---------------------------+
| outlook_emails            |         | email_pool (统一表)         |
| generic_api_emails        |         |   outlook/generic_api/imap/ |
| icloud_emails             |         |   domain 共用一张表          |
| accounts / jobs           |         | registration_jobs           |
+---------------------------+         | codex_agent_accounts        |
                                      | account_inspections*3       |
                                      | storage_meta                |
                                      +---------------------------+
```

本地分表承载 icloud；远端统一表承载 imap + 巡检三表。**schema 已彻底分叉**。

### 邮箱来源注册表 `core/email_provider.py`

| | 来源集合 |
|---|---|
| 本地 | outlook, generic_api, cloudflare_domain, icloud, cloudflare, remail |
| 远端 | outlook, generic_api, imap, cloudflare_domain, cloudflare, gptmail/mailnest/cloudmail, remail, xigua, mailcode |

### 短信 `sms_provider.py`
- 本地：SMSBower 走独立 `smsbower_provider.py`，V1 handler API，日志脱敏。
- 远端：SMSBower 内联进 `sms_provider.py`，GET handler_api 兼容 + `getNumberV2` 自动回退 + 余额/禁售细分异常。

### 注册协议指纹（`openai_auth.py` / `session.py` / `sentinel-runner.js` / `main.py`）
- 远端：协议注册指纹优化（`PROTOCOL_REUSE_FINGERPRINT_BY_EMAIL`）、协议密码注册、403 重试、2FA 并发修复。
- 本地：删了协议密码注册路径，保留 sentinel 常量 `SENTINEL_SDK_SHA256/URL/FRAME_URL`（远端已删）。

### 浏览器省流量（`config/browser.py`）
- 本地：已删除省流量能力（`BROWSER_DATA_SAVER_*` / `BROWSER_TRAFFIC_*` / `BROWSER_JS_COVERAGE_*` 配置，连同 `browser_data_saver` / `browser_traffic` 模块整体移除）。
- 远端：仍有省流量（`BROWSER_DATA_SAVER_DEEP_MODE`、`core/roxy_asset_cache.py`）。

---

## 四、逐步改造路线（候选合并顺序）

> 原则：**先对齐「核心链路、且远端持续更新」的共同功能**（因为本地自研功能可靠性未验证，核心链路回到上游已验证状态最优先），再按冲突难度排序。总体顺序：档 A（核心好合）→ 档 B（核心大冲突）→ 纯新增 → 本地保留项。
> 每个 `merge(upstream)` 提交 MUST 遵循仓库规范：
> ```text
> merge(upstream): <中文功能摘要>
>
> Upstream-Commit: <40 位完整 SHA>
> ```

### 档 A — 核心链路，本地几乎没动、远端大改（好合，先做）

> 本地改动量少，接近零冲突，先验证「合并 → 冒烟测试 → 打勾」完整流程。

#### A1 — 协议注册/认证流程升级 + 密码注册/2FA/密码保存闭环（一个整体，核心提交 `b56a9e9 换绑逻辑优化3` + `2161365`/`3867057`/`4bace93`~`f8709ab`）

> `session.py` / `openai_auth.py` / `chatgpt_auth.py` / `chatgpt_bootstrap.py` 被 `b56a9e9` 一个提交原子绑定，
> 且 `session.get_sentinel_frame_headers` 依赖 sentinel 层改动，拆开会破坏协议自洽，**必须作为整体合入**。
> 注：提交名含「换绑」，但 `b56a9e9` 不触及换绑功能本体（`email_change_service.py`），与换绑完全解耦。
>
> `b56a9e9` 完整文件范围（17 个，超出四文件）：
>
> ```
> 四文件            session.py(+69) openai_auth.py(+326) chatgpt_auth.py(+41) chatgpt_bootstrap.py(+141)
> 附带的协议层      sentinel/sentinel-runner.js(+310) core/sentinel_runner.py(+44) sentinel/sdk.js(+2)
> 附带的账号层      core/account_liveness.py(+162) core/account_export.py(+6)
> 附带的配置/测试    config/browser.py config/openai_protocol.py config/__init__.py + tests/*(4)
> ```
>
> **扩 A1（密码注册 + 2FA + 密码保存闭环）**，在 `b56a9e9` 之上追加三个提交（账号产出物是「带密码 + 2FA 的账号」，拆开不自洽）：
>
> ```
> 密码注册流程      main.py 接入 navigate_create_account_password → register_user → navigate_email_otp_send（3867057）
> 密码落库/展示      main.py extra.registration_password + db._extract_registration_password + webui 密码字段（2161365）
> 2FA 重认证        account_export.setup_2fa 完整版（send_email_otp + with_retry + 403/429 分类 + 重发轮）（2161365 + 4bace93~f8709ab）
> 独立补跑 2FA      twofa_service.py 后台队列 + db totp_setup 四函数 + webui totp-setup 接口 + 前端密码列/开启2FA（2161365）
> 排除              完整导出（6e9af56，留 A2）；roxy_2fa.py（阶段 4 已删）
> ```

- [x] A1-1 `core/http_diagnostics.py`（纯新增，无依赖）——`response_summary` 是 bootstrap 日志依赖
- [x] A1-2 sentinel 层 + `core/session.py`——协议指纹（`sentinel-runner.js`/`sentinel_runner.py`）+ 会话方法（`reset_circuit_breaker`/`get_sentinel_frame_headers`/`rotate_document_navigation_id`/`observe_chatgpt_document`）先就位
- [x] A1-3 `core/openai_auth.py` + `core/chatgpt_auth.py`——认证主链路（403 重试、密码页识别、协议密码注册）
- [x] A1-4 `core/chatgpt_bootstrap.py`——预热重写（对齐真实 Web 轨迹、资格诊断脱敏日志）
- [x] A1-5 附带的 `core/account_liveness.py`（查活 +162）与 `core/account_export.py` 的 `follow_oauth_callback` 403 暴露（`b56a9e9` 的 +6 部分）——随 `b56a9e9` 一并进入
- [x] A1-6 「密码注册 + 2FA + 密码保存」闭环（扩 A1，锚定 `2161365 密码注册，2FA` + `3867057 协议密码注册` + `4bace93`~`f8709ab`）：
  - 密码注册流程：`main.py` 接入 `navigate_create_account_password` → `register_user` → `navigate_email_otp_send`（`3867057`）
  - 密码落库/展示：`main.py` 写 `extra.registration_password` + `db._extract_registration_password` + `webui` 密码字段（`2161365`）
  - 2FA 重认证：`account_export.setup_2fa` 完整版（显式 `send_email_otp` + with_retry + 403/429 分类 + 重发轮）（`2161365` + `4bace93`~`f8709ab`）
  - 凭证明文可追溯：密码/完整 TOTP secret/重认证 OTP/激活 TOTP 验证码明文写入任务日志（本地反向决策，不采纳上游「验证码已隐藏」）
  - 独立补跑 2FA + 密码列：`core/twofa_service.py` 后台队列（`enqueue_account_totp_setup` / `queue_settings` / `is_running` / `log_path`）+ `db` 四个 totp_setup 状态函数 + `webui/app.py` 三个 totp-setup 接口 + `webui/templates/index.html`「密码」列 /「开启2FA」按钮 /「复制登录凭据」/「2FA日志」
  - 排除：账号页「完整导出」（`6e9af56`，留 A2）、`roxy_2fa.py`（阶段 4 已删）
- [x] A1-连带 session.py 对齐引入的运行时依赖（阶段 3 提前就位）：`core/protocol_profiles.py`、`core/proxy_chain.py`、`core/proxy_utils.py`、`core/wreq_session.py` + `config/browser.py` 三属性（`PROTOCOL_HTTP_CLIENT`/`CURL_CFFI_IMPERSONATE`/`WREQ_EMULATION`）与 `core/sentinel.py` 的 `SENTINEL_SDK_URL`→`SENTINEL_SV` 内联改写
- [x] A1-验收（第一部分）：跑通协议注册主链路（匿名预热 → 密码注册 → 邮箱 OTP → 拿到 access token）——验证：账号 `3m7fs52r5rr@vgjkjb-mail.yuanxuai.xyz` 落库 id=1515，`access_token_present=True`（length=2001）；Codex 按 `ENABLE_CODEX_AUTO=False` 跳过
- [x] A1-验收（第二部分，扩 A1）：密码 + 2FA 完整闭环冒烟——账号 id=1516 落库含 `access_token` + `registration_password` + `totp_secret`，账号详情/复制可读回密码，任务日志明文可追溯密码/完整 secret/OTP/TOTP 验证码（对齐 tasks 8.7）

#### A2 — 其余核心好合项

- [ ] `core/generic_api_mail_client.py`：通用取码兼容（1 vs 4）
- [ ] `core/account_export.py`：批量复制凭据、全量导出（远端 9）

### 档 B — 核心链路，两边都大改（难合，逐个 diff，语义可能冲突）

- [x] `core/chatgpt_plan.py`：本地「查询失败原因」vs 远端「优惠力度查询」（6 vs 5）——已随 change `align-chatgpt-plan-check-upstream` 整文件对齐上游：删除三态资格校验（`trial_eligibility_known`）与失败原因分类（`plan_check_error_kind`）、`env=`→`session=`、删 `pool` 代理、新增 `PLAN_CHECK_UPSTREAM_PROXY` 代理链
- [ ] `core/live_check_service.py`：本地「查活后查套餐」vs 远端「查活协议优化 ×4」（3 vs 6）。**A1 已知遗留**：`account_liveness` 对齐后内部已查套餐（`result["plan_result"]`），本地 `live_check_service` 仍再查一次（`result["plan_check"]`），形成重复查套餐；且未传递 `fingerprint_state`/`email_source`。档 B 对齐时需移除重复逻辑 + 传递指纹状态 + 改 403 代理轮转。
- [ ] `core/email_provider.py`：来源集合互斥，决策 icloud vs imap（4 vs 4）
- [ ] `core/db.py`：本地分表 vs 远端统一 `email_pool` + 巡检三表。**最高风险**，单独迁移方案 + 数据兼容测试（15 vs 19）
- [ ] `webui/app.py`：路由层大面积分歧（18 vs 17）
- [ ] `webui/config_editor.py`：配置编辑器（8 vs 19）

### 阶段 3 — 纯新增文件（本地无此文件，零冲突，可与档 A 穿插）

- [x] 代理链：`core/proxy_chain.py`、`core/proxy_utils.py`（代码已随 A1 连带就位；`config/proxy.py` 的 `PROXY_POOL_UPSTREAM_PROXY`/`PLAN_CHECK_UPSTREAM_PROXY` 可选开关未加，默认直连）
- [x] wreq 协议驱动：`core/wreq_session.py`（代码已随 A1 就位；本轮已补齐 `wreq==0.13.0`，要求 Python 3.11+；注册固定 `wreq/chrome152`，HTTP／SDK 版本一致、平台不变，独立任务保留原后端配置；缺能力明确失败，无自动安装或静默回退。实际 trace／CSRF／SDK challenge 已通过，完整注册另列验收，见 `protocol-registration-flow-diff.md` 4.5.5）
- [x] 协议 profiles：`core/protocol_profiles.py`（已随 A1 连带就位）
- [x] HTTP 诊断：`core/http_diagnostics.py`（已随 A1 连带就位）
- [ ] IMAP 邮箱：`core/imap_mail_client.py`
- [ ] MailCode 邮箱：`core/mailcode_client.py`
- [ ] 西瓜接码：`core/xigua_client.py` + `config/email.py`（`XIGUA_*`）
- [x] 优惠力度查询：`webui/templates/promo_details.html` + `chatgpt_plan.py` 解析（已随 `align-chatgpt-plan-check-upstream` 照抄上游模板并接入 `data-promo-details` 详情按钮）
- [ ] 一键巡检：`core/account_inspection_service.py` + `inspection_lease.py` + 模板（依赖档 B 的 db 表）
- [ ] 注册性能基准：`tools/benchmark_registration_jobs.py`

### 阶段 4 — 需决策取舍的小冲突（短信）

- [ ] 短信 provider：本地 V1（`smsbower_provider.py`）vs 远端 GET handler_api。决策：采用远端 GET 兼容 / 保留 V1 / 两者共存。
- [x] 2FA 驱动：Roxy 驱动 `roxy_2fa.py` 已删除（协议注册同步 `setup_2fa` 与后台补设队列 `twofa_service.py` 已随 A1-6 纳入，`TWOFA_WORKERS`/`TWOFA_QUEUE_LIMIT`/`TWOFA_PROXY_MODE` 已引入；Roxy 真实浏览器驱动整体移除后无需 Roxy 2FA）。
- [x] 浏览器省流量：已删除省流量能力（`browser_data_saver` / `browser_traffic` 及 `BROWSER_DATA_SAVER_*` / `BROWSER_TRAFFIC_*` / `BROWSER_JS_COVERAGE_*` 配置整体移除），不引入 `BROWSER_DATA_SAVER_DEEP_MODE`。

### 阶段 5 — 本地大改、远端小改（远端无可合内容，保留本地，仅记录）

- [ ] `core/registration_service.py`（本地 6 vs 远端 1）：确认无上游修复可捡，保留本地。
- [ ] `core/plan_check_service.py`（本地 3 vs 远端 1）：确认无上游修复可捡，保留本地。

---

## 当前进度

- [x] 建立差异基准快照（本文档）
- [x] 档 A1（协议注册/认证流程升级 + 密码注册/2FA/密码保存闭环：已完成——代码对齐 + 测试套件通过 + 完整闭环冒烟（密码 + 2FA + `totp_secret` 落库 + 明文可追溯日志）+ 独立补跑 2FA 后台队列 + 账号页密码列/「开启2FA」按钮）
- [ ] 档 A2（其余核心好合项）
- [ ] 档 B（核心链路大冲突项）——首项 `core/chatgpt_plan.py` 已随 change `align-chatgpt-plan-check-upstream` 完成（废弃 in-progress change `display-plan-check-error-summary`：失败原因分类已删除；`require-full-plan-promo-discount` 语义被取代）
- [ ] 阶段 3（纯新增；`proxy_chain`/`proxy_utils`/`protocol_profiles`/`wreq_session`/`http_diagnostics` 已随 A1 连带就位，剩余 IMAP/MailCode/西瓜/巡检等；「Roxy 资源缓存」因省流量已删不再需要）
- [ ] 阶段 4（仅剩短信 provider 决策；2FA 驱动与浏览器省流量已决策删除）
- [ ] 阶段 5（本地保留项确认）
