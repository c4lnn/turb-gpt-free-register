# Kakao Pay 提链 API 对接参考

## 文档来源

- 在线文档：<https://masi.cc.cd/kakao/extract/api-docs.html>
- 获取时间：2026-08-02（Asia/Shanghai）
- 原始快照：[`docs/vendor/kakaopay-extract-api-2026-08-02.html`](vendor/kakaopay-extract-api-2026-08-02.html)
- 快照 SHA-256：`6789AFAF0C4A25D2CA4196485C9F772C613ACCC4E9576EF6D3E6AC5021C10D2A`
- 响应 `Last-Modified`：`Sat, 01 Aug 2026 13:03:04 GMT`
- 响应 `ETag`：`"6a6dee88-1c04"`

访问说明：本机直连该域名时出现 TLS 主机名不匹配，并返回上海反诈拦截页；通过本机 Clash HTTP 代理 `127.0.0.1:7816` 后，TLS 校验正常并取得上述 API 文档。后续刷新文档时不得使用 `-k` 得到的页面覆盖当前快照，必须确认页面标题为“Kakao 纯提炼 API 文档”且证书校验成功。

本文依据供应方 HTML 文档整理。供应方没有提供 OpenAPI/JSON Schema，因此除示例中明确出现的字段外，不应自行假定响应结构。

## 能力边界

- 接口只提炼 Kakao/Nicepay 付款链接。
- 接口不创建扫码订单，也不向工人端推送任务。
- 每次请求提交一个 ChatGPT `access_token`（下文简称 AT）。
- API 不按邮箱域名限制提交。
- 是否成功由账号实时资格和上游返回决定。
- 只有成功返回付款链接才扣除一次 CDK；失败或取消不扣次数。

## 基础信息

| 项目 | 值 |
| --- | --- |
| Base URL | `https://masi.cc.cd` |
| 请求/响应格式 | `application/json` |
| 认证方式 | 请求头 `X-CDK` |
| 单次提交 | 一个 AT |
| 同一 CDK API 并发 | 最多 30 个任务 |
| 同一 CDK 网页并发 | 最多 10 个任务 |
| AT 去重 | 同一个 AT 在任务运行期间只创建一个任务 |

所有请求都应携带：

```http
X-CDK: KSCAN-XXXX-XXXX-XXXX
Content-Type: application/json
```

`X-CDK` 和 AT 都属于敏感凭据，不得写入日志、异常详情、URL 查询参数或前端列表接口。

## 创建任务

```http
POST /v1/kakao/jobs
```

请求：

```json
{
  "access_token": "YOUR_ACCESS_TOKEN"
}
```

成功响应示例：

```json
{
  "ok": true,
  "job": {
    "job_id": "JOB_ID",
    "status": "queued"
  }
}
```

集成要求：

- 从 `job.job_id` 读取任务 ID。
- 创建成功后进入轮询，不应把 `queued` 当作提链成功。
- 文档没有声明幂等键；本地应继续对同一账号做排队/运行中去重。
- 新接口固定为 Kakao 提链，请求体不包含现有旧协议的 `link_type` 或 `cdk` 字段。

## 查询任务

```http
GET /v1/kakao/jobs/{job_id}
```

成功完成示例：

```json
{
  "ok": true,
  "job": {
    "job_id": "JOB_ID",
    "status": "completed",
    "output": {
      "long_url": "https://..."
    }
  }
}
```

供应方示例每 `2.5` 秒查询一次。建议实现时使用可配置轮询间隔，并以现有任务总超时作为轮询截止时间。

状态处理：

| `job.status` | 含义 | 本地处理 | CDK |
| --- | --- | --- | --- |
| `queued` | 排队 | 保持 `running`/等待状态并继续轮询 | 预留一次 |
| `running` | 提炼中 | 更新进度并继续轮询 | 预留一次 |
| `completed` | 成功返回链接 | 保存 `job.output`，标记成功 | 扣除一次 |
| `failed` | 失败 | 保存 `job.error`（若存在），标记失败 | 释放预留 |
| `canceled` | 已取消 | 标记取消，不再轮询 | 释放预留 |

当前文档只明确展示了 `output.long_url`。代码可以保留整个 `job.output` 作为原始结果，但展示层不得假设一定存在二维码、`copy_paste` 或过期时间等旧协议字段。

## 取消任务

```http
POST /v1/kakao/jobs/{job_id}/cancel
Content-Type: application/json

{}
```

- 只用于尚未结束的任务。
- 取消成功不扣 CDK。
- 文档未给出取消接口的响应结构，接入时只可依据 HTTP 状态和实际返回的 `ok`/`job` 字段做防御性解析。

## 查询 CDK 额度

```http
GET /v1/cdk/status
```

响应示例：

```json
{
  "ok": true,
  "cdk": {
    "total_uses": 10,
    "remaining_uses": 8,
    "pending_uses": 1,
    "available_uses": 7
  }
}
```

字段含义：

- `total_uses`：总次数。
- `remaining_uses`：尚未最终扣除的剩余次数。
- `pending_uses`：已被排队或运行中任务预留的次数。
- `available_uses`：当前还能创建任务的可用次数，通常应作为前端主要展示值。

## HTTP 错误

供应方明确列出的常见状态：

| HTTP 状态 | 含义 |
| --- | --- |
| `400` | 参数错误 |
| `401` | CDK 无效 |
| `429` | 同一 CDK 并发达到上限 |
| `502` | 上游暂不可用 |

文档未定义统一错误 JSON Schema。实现时应按以下顺序提取错误：`error.message`、`error`、`message`、`detail`，最后使用截断后的响应文本；任何路径都不得记录完整 AT 或 CDK。

## 推荐调用流程

```text
1. 校验本地账号存在且具有 access_token
2. 原子占用该账号的本地提链状态
3. POST /v1/kakao/jobs，X-CDK 放请求头，AT 放 JSON body
4. 保存 job_id
5. 每约 2.5 秒 GET /v1/kakao/jobs/{job_id}
6. queued/running：继续轮询
7. completed：保存 output.long_url 和原始 output，标记 success
8. failed/canceled：保存错误并结束
9. 本地超时或用户停止：尝试调用 cancel，然后结束本地任务
```

不得为了判断链接是否有效而访问、重放或轮询返回的付款 URL。`completed` 仅表示供应方成功返回了链接，不表示后续支付成功。

## 与当前仓库实现的差异

当前实现位于 `core/extract_link_service.py`，使用的是另一套旧协议：

| 当前实现 | 新文档协议 |
| --- | --- |
| `POST /api/extract` | `POST /v1/kakao/jobs` |
| JSON 包含 `token`、`cdk`、`link_type` | JSON 只包含 `access_token` |
| CDK 放 JSON 或 SSE 查询参数 | CDK 统一放 `X-CDK` 请求头 |
| `GET /api/jobs/{job_id}/events` SSE | `GET /v1/kakao/jobs/{job_id}` 轮询 |
| 终态为 SSE `result/error/done` | 终态为 `completed/failed/canceled` |
| 结果默认含链接、二维码、过期时间等 | 文档只保证示例中的 `output.long_url` |
| `GET /api/cdk?code=...` | `GET /v1/cdk/status` |

因此不能只替换 `EXTRACT_LINK_API_BASE` 完成对接，必须修改创建任务、认证、轮询、状态映射、额度查询和结果解析逻辑。

## 本项目接入约定

本项目把链类型、服务提供方和更新方式分开配置。Masi 当前只支持：

```text
link_type=kakao_pay
provider=masi
update_mode=poll
```

旧服务继续使用 `legacy + sse`，且仍从 `EXTRACT_LINK_CDK` 读取单个 CDK。Masi 不读取单值 `MASI_KAKAO_CDK`，而是从 WebUI 维护的 `提链CDK池.json` 选择凭据。该运行时文件已加入 `.gitignore`。

### 统一提链代理

`EXTRACT_LINK_PROXY` 是 provider 无关的可选显式代理，适用于 legacy 和 Masi 的全部外部请求。示例：

```dotenv
EXTRACT_LINK_PROXY=http://127.0.0.1:7816
```

- 支持 `http`、`https`、`socks5` 和 `socks5h` 代理 URL。
- 留空时不向 provider adapter 注入显式代理，保留 HTTP 客户端默认行为。
- 配置不关闭 TLS 校验；不得用 `verify=False` 或类似方式绕过证书问题。
- 任务入队时固化代理连接上下文，运行中修改只影响后续任务。
- 单个 CDK 刷新读取操作开始时的当前代理；批量刷新只读取一次并由本批次所有请求共用。
- 代理 URL 可能包含认证信息，因此作为 secret 存入 `.env`，配置读取、日志和错误响应不得返回完整值。

### CDK 双池和选择规则

Masi CDK 记录只属于 `selectable`（可选池）或 `exhausted`（已用完池）：

导入接口的 `refresh_quota` 默认为 `false`。默认导入只完成本地持久化和跨池去重，不逐条请求远端额度；前端“导入后立即查询额度”复选框也默认关闭。显式设为 `true` 时，才会对本批次新增和重复 CDK 执行受控并发刷新。

1. 选择器从可选池队首临时摘取一条 CDK，并在额度查询至创建 Job 响应期间持有进程内租约。
2. 查询失败时按 `MASI_CDK_QUERY_MAX_ATTEMPTS` 和 `MASI_CDK_QUERY_RETRY_DELAY` 重试；最终失败只更新脱敏 `last_error`，保留上次成功额度和原池归属。
3. 只有 `remaining_uses == 0` 才移入已用完池。
4. `available_uses > 0` 时创建 Job，并在本地任务中绑定 CDK 内部 ID 和指纹。
5. `available_uses == 0` 时放回可选池队尾，不能据此判定 CDK 已用完。
6. 已用完池中的记录刷新到 `remaining_uses > 0` 时移回可选池。

完整扫描后，如果存在正在分配、远端占用或暂时不可用的记录，选择器会在 `MASI_CDK_SELECTION_TIMEOUT` 内重扫；如果全部查询失败，则报告额度查询暂时失败；只有可选池确实为空时才报告额度已用完。

### Job 轮询规则

创建成功后，后端按 `EXTRACT_LINK_POLL_INTERVAL` 查询远端 Job。前端仍只轮询本地账号状态接口：

- `queued` / `running`：保持本地 `running` 并继续轮询。
- `completed`：要求 `job.output.long_url` 非空，保存完整 `job.output`，本地标记为 `success`。
- `failed`：保存供应商错误并标记为 `failed`。
- `canceled`：本地标记为 `canceled`。
- 未知状态或缺少必填字段：按协议错误结束。

网络错误、HTTP `429` 和 `5xx` 会重试；成功响应会清零连续错误计数。不可重试的 `4xx` 立即失败，连续暂时错误达到 `EXTRACT_LINK_POLL_MAX_ERRORS` 时失败。总等待超过 `EXTRACT_LINK_EVENT_TIMEOUT` 后，后端会尽力调用一次取消接口，再结束本地任务。

### 运维和安全边界

- `EXTRACT_LINK_WORKERS` 决定启动时创建的线程池大小，修改后必须重启 WebUI；其他轮询和重试参数可由后续任务读取最新配置。
- CDK 列表 API 只返回内部 ID、短指纹、掩码和额度数据，不返回完整 CDK。
- 账号列表和本地状态接口不返回 AT 或完整 CDK。
- 日志不记录完整 CDK、AT、带凭据查询参数或完整付款链接。
- 对 `long_url` 只执行保存和按现有字段展示，不主动访问、重放、轮询或根据重定向验证。

## 文档未说明的事项

开发或联调前仍需确认：

- 各接口完整错误响应结构。
- 重复提交同一个运行中 AT 时的 HTTP 状态和返回体。
- `failed` 状态下 `job.error` 的类型和字段。
- `cancel` 的成功及重复取消响应。
- `job.output` 除 `long_url` 外是否有稳定字段。
- 付款链接是否有明确过期时间。
- 是否返回 `Retry-After` 或其他限流响应头。
- 服务是否提供任务历史或服务重启后的任务恢复能力。
