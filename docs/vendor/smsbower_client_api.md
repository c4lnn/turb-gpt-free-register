# SMSBower 客户端 API 对接参考

## 文档来源

- 官方中文文档：https://smsbower.app/cn/api?page=client
- 官方 Postman 文档：https://documenter.getpostman.com/view/16514200/2sAYdkFTue
- 整理日期：2026-08-02

本文用于保存后续开发所需的 SMSBower 客户端 API 信息。内容依据上述官方页面整理，未使用真实 API Key，未调用取号、收码、充值等接口。

官方中文页面存在断行、翻译和 JSON 示例格式问题。本文修正明显的排版问题，但不会把文档未明确说明的行为当作稳定契约；标记为“待联调确认”的内容必须通过非生产账号验证。

## 基础信息

| 项目 | 值 |
| --- | --- |
| 客户端 API 基址 | `https://smsbower.page/stubs/handler_api.php` |
| 支持方法 | `GET` 或 `POST` |
| 认证参数 | `api_key` |
| 主要响应格式 | 文本状态或 JSON，取决于 `action` |
| Webhook 来源 IP | `167.235.198.205` |

注意：文档页面位于 `smsbower.app`，但官方列出的客户端 API 域名是 `smsbower.page`，实现时不要混用。

`api_key` 是机密信息。不得写入源码、示例文档、日志或异常消息，也不要记录包含完整查询字符串的请求 URL。使用 GET 时，Key 会出现在 URL 中；客户端应对日志做脱敏，条件允许时优先使用 POST 参数。

以下示例统一使用占位符：

```text
SMSBOWER_API_KEY=<secret>
SMSBOWER_API_BASE=https://smsbower.page/stubs/handler_api.php
```

## 核心激活流程

```text
getNumber / getNumberV2
  -> 获得 activationId 和 phoneNumber
  -> 将手机号提交给目标服务并触发短信
  -> 可选 setStatus(status=1)
  -> 周期性调用 getStatus
  -> STATUS_OK:<code>
  -> 将验证码提交给目标服务
  -> 成功：setStatus(status=6)
  -> 失败或放弃：setStatus(status=8)
```

当前项目联调规则确认 SMSBower 激活可在失败后立即通过 `setStatus=8` 取消，不需要等待 120 秒。项目不得在后台延迟取消；取消调用返回后才能获取替换号码。

## 查询余额

### `getBalance`

```http
GET /stubs/handler_api.php?api_key=<secret>&action=getBalance
```

参数：

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `api_key` | 是 | SMSBower API Key |
| `action` | 是 | 固定为 `getBalance` |

成功响应：

```text
ACCESS_BALANCE:<balance>
```

明确列出的错误：`BAD_KEY`。

## 请求号码

### `getNumber`

```http
GET /stubs/handler_api.php?api_key=<secret>&action=getNumber&service=<service>&country=<country>
```

完整参数：

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `api_key` | 是 | SMSBower API Key |
| `action` | 是 | 固定为 `getNumber` |
| `service` | 是 | 服务代码；应通过 `getServicesList` 查询，不要根据显示名称猜测 |
| `country` | 是 | 国家 ID；应通过 `getCountries` 查询 |
| `maxPrice` | 否 | 可接受的最高价格 |
| `minPrice` | 否 | 可接受的最低价格 |
| `providerIds` | 否 | 允许的供应商 ID，多个值用逗号分隔 |
| `exceptProviderIds` | 否 | 排除的供应商 ID，多个值用逗号分隔 |
| `phoneException` | 否 | 排除号码前缀，逗号分隔；格式为国家码加 3 至 6 位掩码，例如 `7918,7900111` |
| `ref` | 否 | 推荐 ID |
| `userID` | 否 | 经销商参数，具体语义需联系官方支持 |

成功响应：

```text
ACCESS_NUMBER:<activationId>:<phoneNumber>
```

客户端必须把 `activationId` 当作字符串保存，不应假定其数字范围；`phoneNumber` 也应作为字符串处理，避免丢失前导零。

官方明确列出的错误：

- `BAD_KEY`
- `BAD_ACTION`
- `BAD_SERVICE`

号码库存不足、余额不足和国家无效等返回值在当前中文页面的本节没有完整列出，必须在联调时补充，不应仅根据第三方兼容接口的经验硬编码。

### `getNumberV2`

`getNumberV2` 与 `getNumber` 参数基本一致，但返回 JSON 和更多激活元数据。

```http
GET /stubs/handler_api.php?api_key=<secret>&action=getNumberV2&service=<service>&country=<country>
```

可选参数包括 `maxPrice`、`minPrice`、`providerIds`、`exceptProviderIds` 和 `userID`。官方示例没有列出 `phoneException`、`ref`，实现时不要默认 V2 接受这两个参数。

成功响应示例：

```json
{
  "activationId": "id",
  "phoneNumber": "number",
  "activationCost": 0.0,
  "countryCode": "countryCode",
  "canGetAnotherSms": false,
  "activationTime": "activationTime",
  "activationOperator": "activationOperator"
}
```

字段类型以联调响应为准。官方示例中的 `phoneNumber`、`countryCode`、时间字段没有给出稳定类型定义。

明确列出的错误：`BAD_KEY`、`BAD_ACTION`、`BAD_SERVICE`。

## 查询短信状态

### `getStatus`

```http
GET /stubs/handler_api.php?api_key=<secret>&action=getStatus&id=<activationId>
```

参数：

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `api_key` | 是 | SMSBower API Key |
| `action` | 是 | 固定为 `getStatus` |
| `id` | 是 | `getNumber` 返回的激活 ID |

响应状态：

| 响应 | 含义 | 建议处理 |
| --- | --- | --- |
| `STATUS_WAIT_CODE` | 等待第一条短信 | 按轮询间隔继续查询 |
| `STATUS_WAIT_RETRY:<lastCode>` | 等待下一条短信 | 保存最后一次代码并继续查询 |
| `STATUS_CANCEL` | 激活已取消 | 结束轮询并返回失败 |
| `STATUS_OK:<code>` | 已收到验证码 | 提取第一个冒号后的完整内容 |

明确列出的错误：`BAD_KEY`、`BAD_ACTION`、`NO_ACTIVATION`。

轮询实现应有总超时、固定或退避间隔、任务取消检查，并避免把完整 API Key 或短信正文写入普通日志。

## 修改激活状态

### `setStatus`

```http
GET /stubs/handler_api.php?api_key=<secret>&action=setStatus&status=<status>&id=<activationId>
```

状态值：

| `status` | 含义 | 典型时机 |
| --- | --- | --- |
| `1` | 通知号码已准备好接收短信 | 目标服务已触发短信后；官方标为可选 |
| `3` | 请求接收下一条短信 | 已收到一条短信且业务需要再次收码 |
| `6` | 完成激活 | 验证码已被目标服务接受 |
| `8` | 取消激活 | 号码不适用、超时或流程失败 |

成功响应：

| 响应 | 含义 |
| --- | --- |
| `ACCESS_READY` | 已进入等待短信状态 |
| `ACCESS_RETRY_GET` | 等待下一条短信 |
| `ACCESS_ACTIVATION` | 激活已成功完成 |
| `ACCESS_CANCEL` | 激活已取消 |

明确列出的错误：

- `NO_ACTIVATION`
- `BAD_STATUS`
- `BAD_KEY`
- `BAD_ACTION`
- `EARLY_CANCEL_DENIED`

状态机约束：

- `getNumber` 后可设为 `1` 或直接取消为 `8`。
- 状态 `1` 后可取消为 `8`。
- 收到验证码后可设为 `3` 请求下一条短信，或设为 `6` 完成。
- 状态 `3` 后应以 `6` 完成。

## 服务和国家元数据

### `getServicesList`

```http
GET /stubs/handler_api.php?api_key=<secret>&action=getServicesList
```

响应示例：

```json
{
  "status": "success",
  "services": [
    {
      "code": "kt",
      "name": "KakaoTalk"
    }
  ]
}
```

后续接入 OpenAI/Codex 前，应先调用此接口确认当前有效的服务代码，不要直接沿用其他接码平台的服务代码。

项目 WebUI 通过需认证的本地 `/api/smsbower/metadata` 接口同时读取服务与国家元数据。该接口只调用 `getServicesList` 和 `getCountries`，不会调用可能计费的 `getNumber`；返回给前端的数据不包含 API Key。

### `getCountries`

```http
GET /stubs/handler_api.php?api_key=<secret>&action=getCountries
```

官方中文页的 JSON 示例语法不合法，只能确认单个国家至少可能包含：

```json
{
  "id": 1003,
  "rus": "...",
  "eng": "Bermuda",
  "chn": "百慕大"
}
```

顶层究竟是数组还是对象、字段是否始终存在，需要联调确认。

### `getTopCountriesByService`

```http
GET /stubs/handler_api.php?api_key=<secret>&action=getTopCountriesByService&service=<service>
```

用途：返回指定服务优先级最高的 10 个国家，并按销售量返回 Gold 等级合作方。

响应示例：

```json
{
  "usa": {
    "3170": {
      "price": 0.12,
      "count": 542
    }
  }
}
```

明确列出的错误：`BAD_KEY`、`BAD_ACTION`、`BAD_SERVICE`。

## 价格查询

### `getPrices`

```http
GET /stubs/handler_api.php?api_key=<secret>&action=getPrices&service=<service>&country=<country>
```

`service` 和 `country` 都是可选筛选项；不传时返回完整范围。响应为 JSON，大致结构为：

```json
{
  "country": {
    "service": {
      "cost": 0.0,
      "count": 0
    }
  }
}
```

### `getPricesV2`

```http
GET /stubs/handler_api.php?api_key=<secret>&action=getPricesV2&service=<service>&country=<country>
```

响应按国家、服务和价格档位组织，价格作为键、库存数量作为值：

```json
{
  "country": {
    "service": {
      "0.10": 12,
      "0.12": 30
    }
  }
}
```

官方示例不完整，上述结构只表达层级含义，键和值的实际 JSON 类型待联调确认。

### `getPricesV3`

```http
GET /stubs/handler_api.php?api_key=<secret>&action=getPricesV3&service=<service>&country=<country>
```

响应增加 provider 维度：

```json
{
  "country": {
    "service": {
      "provider-id": {
        "count": 10,
        "price": 0.12,
        "provider_id": 3170
      }
    }
  }
}
```

明确列出的错误：`BAD_KEY`、`BAD_ACTION`、`BAD_SERVICE`、`BAD_COUNTRY`。官方中文页把 `BAD_COUNTRY` 的说明误写成了服务名称错误，应按“国家参数错误”理解并在联调时确认。

## Webhook

在 SMSBower 个人资料中配置 Webhook URL 后，收到短信时平台会向该 URL 发送 POST 请求，从而替代持续调用 `getStatus`。

来源 IP：

```text
167.235.198.205
```

请求体示例：

```json
{
  "activationId": 123456,
  "service": "go",
  "text": "Sms text",
  "code": "12345",
  "country": 2,
  "receivedAt": "2023-01-01 12:00:00"
}
```

接收端必须返回 HTTP 200。若没有成功响应，SMSBower 会在约 1 分钟和 5 分钟后各重试一次，总计最多 3 次尝试。

安全注意事项：

- 来源 IP 白名单只能作为辅助校验，不能替代请求签名；官方页面未说明 Webhook 签名机制。
- 接收端应使用 HTTPS、限制请求体大小、校验字段类型并做幂等处理。
- 建议以 `activationId` 加短信标识构造幂等键。
- 不要在普通日志中记录完整短信正文或验证码。
- Webhook 返回 200 前应确保消息已可靠入队或持久化。

## 静态充值钱包

该接口与短信激活主流程无关，记录在此仅为完整性。

```http
GET https://smsbower.page/api/payment/getActualWalletAddress?api_key=<secret>&coin=<coin>&network=<network>
```

参数：

- `coin`：官方示例列出 `usdt`、`trx`。
- `network`：官方示例列出 `tron`。

响应示例：

```json
{
  "wallet_address": "..."
}
```

涉及充值时必须由人工核对当前官方文档、币种、网络和地址，不应把旧文档中的钱包地址缓存为付款目标。

## 错误处理汇总

| 错误码 | 含义 | 建议分类 |
| --- | --- | --- |
| `BAD_KEY` | API Key 错误 | 配置错误，不重试 |
| `BAD_ACTION` | `action` 不支持 | 客户端实现错误，不重试 |
| `BAD_SERVICE` | 服务代码错误 | 配置或元数据过期，刷新服务列表 |
| `BAD_COUNTRY` | 国家参数错误 | 配置或元数据过期，刷新国家列表 |
| `BAD_STATUS` | 激活状态值或状态转换错误 | 客户端状态机错误，不盲目重试 |
| `NO_ACTIVATION` | 激活 ID 不存在 | 终止当前激活流程 |
| `EARLY_CANCEL_DENIED` | 兼容识别的异常取消响应 | 不预等待 120 秒；按普通取消失败短间隔有限重试 |

以下返回值被项目现有兼容客户端识别，但没有在本次官方中文页面对应接口中完整列出，接入 SMSBower 时需要实测确认：

- `NO_BALANCE`
- `NO_NUMBERS`
- `SERVICE_UNAVAILABLE_REGION`
- `STATUS_WAIT_RESEND`

未知文本响应不能一律当作“等待中”。应记录脱敏后的状态摘要并返回显式错误，防止无限轮询或错误扣费。

## 与当前项目的适配关系

当前 [core/sms_provider.py](../../core/sms_provider.py) 的 `grizzly` 分支已经采用兼容的 handler API 协议：

| 项目动作 | SMSBower API |
| --- | --- |
| `acquire_number()` | `action=getNumber` |
| `wait_for_sms_code()` | `action=getStatus` |
| 短信已触发 | `action=setStatus&status=1` |
| `complete()` | `action=setStatus&status=6` |
| `cancel()` | `action=setStatus&status=8` |

因此后续实现优先考虑新增显式 provider 名 `smsbower`，复用 handler 协议解析，而不是把 SMSBower 隐藏在 `grizzly` 名称下。建议配置项：

```env
SMS_PROVIDER="smsbower"
SMSBOWER_API_BASE="https://smsbower.page/stubs/handler_api.php"
SMSBOWER_API_KEY=""
SMS_SERVICE="<通过 getServicesList 确认>"
SMS_COUNTRY="<通过 getCountries 确认>"
```

实现要求：

1. `SMSBOWER_API_KEY` 必须加入机密配置列表，WebUI 只显示掩码。
2. 不得在日志中输出完整请求 URL，因为 Key 位于请求参数。
3. 服务代码和国家 ID 必须从 SMSBower 元数据确认，不能默认复用 GrizzlySMS 的值。
4. 取号属于可能计费的操作；配置检查、余额检查和价格/库存查询应先于取号。
5. 号码失败后必须立即同步进入取消流程；取消调用返回后才能获取替换号码。
6. 单次激活必须设置总超时，取消失败需要保留可追踪但脱敏的告警。
7. 单元测试应覆盖所有文本状态、未知响应、JSON 解析失败、超时、提前取消和密钥脱敏。

## 待联调确认

- OpenAI/Codex 在 SMSBower 中对应的准确 `service` 代码。
- 计划使用国家的准确 `country` ID，以及号码是否包含国际区号。
- `getNumber` 在余额不足、无库存、价格范围不匹配时的实际返回文本。
- `getCountries` 的真实顶层 JSON 结构。
- `getNumberV2` 各字段的稳定类型和错误响应格式。
- POST 请求时参数应放在表单、查询字符串还是两者均可。
- Webhook 是否提供签名、共享密钥或可配置的自定义 Header。
- 激活有效期、轮询频率限制、API 整体限流与并发限制。
