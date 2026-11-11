import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { ConfirmAction } from '@/components/app/confirm-action'
import { StatusBadge } from '@/components/app/status-badge'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Checkbox } from '@/components/ui/checkbox'
import { Field, FieldContent, FieldDescription, FieldError, FieldGroup, FieldLabel, FieldLegend, FieldSet } from '@/components/ui/field'
import { Switch } from '@/components/ui/switch'
import { Textarea } from '@/components/ui/textarea'
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Skeleton } from '@/components/ui/skeleton'
import { TableToolbar } from '@/components/app/table-toolbar'
import { api } from '@/lib/api'
import { toast } from '@/components/ui/toast'

type ConfigOption = {
  value: string
  label?: string
  disabled?: boolean
}

type ConfigField = {
  key: string
  group: string
  label: string
  help?: string
  type: 'bool' | 'int' | 'float' | 'list_str_multiline' | 'str' | string
  value: unknown
  secret?: boolean
  configured?: boolean
  control?: string
  options?: ConfigOption[]
  external_url?: string
  external_label?: string
}

type ConfigResponse = ConfigField[]
type ConfigValue = boolean | number | string | string[]
type Draft = { value: ConfigValue; version: number }
type ConfigSaveResponse = { reloaded?: boolean; note?: string; updated?: string[]; ignored?: string[] }
type MetadataOption = { value: string; label: string }
type MetadataResponse = { ok?: boolean; services?: MetadataOption[]; countries?: MetadataOption[]; error?: string }

type MetadataState = {
  services: MetadataOption[]
  countries: MetadataOption[]
  loaded: boolean
  loading: boolean
  error: string
  message: string
}

const INITIAL_METADATA: MetadataState = {
  services: [],
  countries: [],
  loaded: false,
  loading: false,
  error: '',
  message: '尚未获取服务与国家选项',
}



function post(body: unknown) {
  return {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : '请求失败'
}


function groupIntro(name: string) {
  const intros: Record<string, string> = {
    'WebUI 授权': '授权码与 Session 签名密钥保存在 .env 中。机密值不会从 API 返回。',
    '功能开关': '控制注册完成后的自动流程。',
    '邮箱 / OTP': '邮箱来源、OTP 等待与各邮箱服务商参数。',
    '接码平台': '接码通道、国家代码与本地取号服务参数。',
    '人工节奏': '注册流程中的随机停顿节奏。',
    '浏览器画像': '浏览器语言、时区与出口 IP 画像。',
    '代理池': '代理列表与套餐网络模式。',
    Codex: 'Codex 授权地址、CPA 与 sub2api 配置。',
  }
  return intros[name] ?? ''
}

function subsectionFor(field: ConfigField): [string, string] {
  const key = field.key
  if (field.group === '邮箱 / OTP') {
    if (['USE_EMAIL_SERVICE', 'REGISTER_EMAIL', 'REGISTER_NAME', 'OTP_MAX_WAIT', 'OTP_POLL_INTERVAL', 'EMAIL_SOURCE'].includes(key)) {
      return ['通用邮箱 / OTP', '邮箱来源选择、OTP 等待参数、手动邮箱等通用设置']
    }
    if (key.startsWith('REMAIL_')) return ['Remail', 'Remail 开放 API、项目下单和取件配置']
    if (key.startsWith('OUTLOOK_')) return ['Outlook 邮箱池', 'Outlook 邮箱池和取件模式配置']
    if (key.startsWith('CLOUDFLARE_')) return ['Cloudflare 临时邮箱', 'Cloudflare Worker 临时邮箱 API、鉴权与路径配置']
    if (['EMAIL_DOMAIN', 'QQ_EMAIL', 'QQ_IMAP_PASSWORD'].includes(key)) {
      return ['Cloudflare 域名邮箱', 'Cloudflare 转发到 QQ 邮箱后的 IMAP 收信配置']
    }
    return ['其他邮箱配置', '']
  }
  if (field.group === '接码平台') {
    if (['SMS_PROVIDER', 'SMS_COUNTRY', 'SMS_SERVICE', 'SMS_MAX_RETRIES', 'SMS_CODE_WAIT'].includes(key)) {
      return ['通用接码', '接码通道选择、国家/服务代码、等待和重试参数；H 通道可复用 SMS_SERVICE/SMS_COUNTRY 作为 projectId/country']
    }
    if (key === 'SMS_API_KEY') return ['GrizzlySMS', 'GrizzlySMS 平台 API Key 配置']
    if (key.startsWith('SMSBOWER_')) return ['SMSBower', 'SMSBower 独立 V1 provider、服务/国家与价格范围配置']
    if (key.startsWith('H_')) return ['H 接码', 'H_API.md 本地 H 取号服务配置；项目ID/国家留空时复用通用字段']
    if (key.startsWith('L_')) return ['L 接码', 'L_API.md 本地 L 取号服务配置']
    return ['其他接码配置', '']
  }
  if (field.group === 'Codex') {
    if (['CODEX_OAUTH_DRIVER', 'CODEX_AUTH_URL_SOURCE'].includes(key)) return ['基础配置', 'Codex 授权驱动和授权地址来源配置']
    if (key.startsWith('CPA_')) return ['CPA配置', 'CPA 授权链接生成、回调上传和管理接口配置']
    if (['SUB2API_API_BASE', 'SUB2API_API_KEY', 'SUB2API_API_TIMEOUT'].includes(key)) {
      return ['sub2api通用', 'Codex OAuth 使用的 sub2api API 基址、鉴权和超时配置']
    }
    return ['基础配置', '']
  }
  return ['', '']
}

function preferredSubsections(group: string) {
  if (group === '邮箱 / OTP') return ['通用邮箱 / OTP', 'Remail', 'Outlook 邮箱池', 'Cloudflare 临时邮箱', 'Cloudflare 域名邮箱', '其他邮箱配置']
  if (group === '接码平台') return ['通用接码', 'GrizzlySMS', 'SMSBower', 'H 接码', 'L 接码', '其他接码配置']
  if (group === 'Codex') return ['基础配置', 'CPA配置', 'sub2api通用']
  return []
}

function getSections(group: string, fields: ConfigField[]) {
  const names = [...new Set(fields.map((field) => subsectionFor(field)[0]).filter(Boolean))]
  const preferred = preferredSubsections(group)
  return [...preferred.filter((name) => names.includes(name)), ...names.filter((name) => !preferred.includes(name))]
}

function parseEmailSource(value: unknown) {
  const raw = Array.isArray(value)
    ? value
    : String(value ?? '').replace(/[;|]/g, ',').split(',')
  const values: string[] = []
  let hasEmptyItem = false
  raw.forEach((item) => {
    const source = String(item ?? '').trim().replace(/^(?:"|')|(?:"|')$/g, '')
    if (!source) {
      if (raw.length > 1 || String(value ?? '').trim()) hasEmptyItem = true
      return
    }
    values.push(source)
  })
  return { values, hasEmptyItem }
}

function emailSourceError(field: ConfigField, value: unknown) {
  const parsed = parseEmailSource(value)
  if (!parsed.values.length) return '至少需要选择一个邮箱来源'
  if (parsed.hasEmptyItem) return '配置中包含空邮箱来源，请重新选择'
  const options = new Set((field.options ?? []).map((option) => String(option.value).trim()).filter(Boolean))
  const unknown = [...new Set(parsed.values.filter((source) => !options.has(source)))]
  if (unknown.length) return `包含未知来源：${unknown.join('、')}，请移除或修正后保存`
  const seen = new Set<string>()
  const duplicates = [...new Set(parsed.values.filter((source) => seen.has(source) || !seen.add(source)))]
  if (duplicates.length) return `包含重复来源：${duplicates.join('、')}，请移除重复项后保存`
  return ''
}

function toConfigValue(field: ConfigField, value: ConfigValue): ConfigValue {
  if (field.type === 'bool') return Boolean(value)
  if (field.type === 'list_str_multiline') {
    if (Array.isArray(value)) return value.map((item) => String(item).trim()).filter(Boolean)
    return String(value ?? '').split('\n').map((item) => item.trim()).filter(Boolean)
  }
  if (field.type === 'int' || field.type === 'float') {
    const text = String(value ?? '').trim()
    if (!text) return 0
    const number = field.type === 'int' ? Number.parseInt(text, 10) : Number.parseFloat(text)
    return number
  }
  const text = String(value ?? '').trim()
  return text
}

function valuesEqual(field: ConfigField, left: unknown, right: unknown) {
  if (field.key === 'EMAIL_SOURCE') {
    const a = parseEmailSource(left).values
    const b = parseEmailSource(right).values
    return a.length === b.length && a.every((item, index) => item === b[index])
  }
  if (field.type === 'bool') return Boolean(left) === Boolean(right)
  if (field.type === 'int' || field.type === 'float') {
    const a = String(left ?? '').trim() === '' ? 0 : Number(left)
    const b = String(right ?? '').trim() === '' ? 0 : Number(right)
    return Number.isFinite(a) && Number.isFinite(b) && a === b
  }
  if (field.type === 'list_str_multiline') {
    const toLines = (value: unknown) => (Array.isArray(value) ? value : String(value ?? '').split('\n')).map((item) => String(item).trim()).filter(Boolean)
    const a = toLines(left)
    const b = toLines(right)
    return a.length === b.length && a.every((item, index) => item === b[index])
  }
  const a = String(left ?? '').trim()
  const b = String(right ?? '').trim()
  return field.secret && !a ? true : a === b
}

function valueForInput(field: ConfigField, value: unknown): string {
  if (field.secret && !value) return ''
  if (field.type === 'list_str_multiline') return Array.isArray(value) ? value.join('\n') : String(value ?? '')
  if (field.key === 'EMAIL_SOURCE' && Array.isArray(value)) return value.join(',')
  return String(value ?? '')
}

function configFieldMatches(field: ConfigField, query: string) {
  if (!query) return true
  return `${field.label} ${field.key} ${field.help ?? ''}`.toLocaleLowerCase().includes(query)
}

function getFieldChoices(field: ConfigField, currentValue: unknown, metadata: MetadataState): ConfigOption[] {
  let choices: ConfigOption[] = []
  if (field.key === 'PLAN_CHECK_PROXY_MODE') {
    choices = [
      { value: 'auto', label: '自动（专用代理优先）' },
      { value: 'proxy', label: '强制代理（专用代理优先）' },
      { value: 'direct', label: '强制直连' },
    ]
  } else if (field.key === 'SMS_PROVIDER') {
    choices = [
      { value: 'grizzly', label: 'GrizzlySMS' },
      { value: 'smsbower', label: 'SMSBower' },
      { value: 'l', label: '本地 L' },
      { value: 'h', label: '本地 H' },
    ]
  } else if (field.key === 'SMSBOWER_SERVICE' || field.key === 'SMSBOWER_COUNTRY') {
    choices = field.key === 'SMSBOWER_SERVICE' ? metadata.services : metadata.countries
  } else if (Array.isArray(field.options)) {
    choices = field.options.map((option) => ({ ...option, value: String(option.value) }))
  }

  const current = String(currentValue ?? '').trim()
  if (current && !choices.some((option) => String(option.value) === current)) {
    choices = [{ value: current, label: `当前值 (${current})` }, ...choices]
  }
  return choices
}

function normalizeMetadataOptions(items: unknown): MetadataOption[] {
  if (!Array.isArray(items)) return []
  return items.flatMap((item) => {
    if (!item || typeof item !== 'object') return []
    const record = item as Record<string, unknown>
    const value = String(record.value ?? record.id ?? '').trim()
    if (!value) return []
    return [{ value, label: String(record.label ?? record.name ?? value) }]
  })
}

function slug(name: string) {
  return `config-${name.toLowerCase().replace(/[^\w\u4e00-\u9fff]+/g, '-').replace(/^-+|-+$/g, '')}`
}

function ConfigInput({
  field,
  value,
  metadata,
  metadataSearch,
  onChange,
  onMetadataSearch,
  error,
}: {
  field: ConfigField
  value: ConfigValue
  metadata: MetadataState
  metadataSearch: string
  onChange: (value: ConfigValue) => void
  onMetadataSearch: (value: string) => void
  error?: string
}) {
  const id = `setting-${field.key.toLowerCase()}`
  const choices = getFieldChoices(field, value, metadata)
  const selected = String(value ?? '')
  const isMetadataField = field.key === 'SMSBOWER_SERVICE' || field.key === 'SMSBOWER_COUNTRY'
  const selectQuery = isMetadataField ? metadataSearch : ''
  const filteredChoices = isMetadataField && selectQuery
    ? choices.filter((choice) => `${choice.label ?? choice.value} ${choice.value}`.toLocaleLowerCase().includes(selectQuery.toLocaleLowerCase()) || choice.value === selected)
    : choices
  const metadataSearchId = `${id}-search`
  const selectItems = filteredChoices
    .filter((choice) => choice.value !== '')
    .map((choice) => ({ value: choice.value, label: choice.label || choice.value, disabled: choice.disabled }))

  if (field.type === 'bool') {
    return (
      <Field orientation="horizontal" data-invalid={Boolean(error) || undefined} className="rounded-lg border p-3">
        <FieldContent>
          <FieldLabel htmlFor={id}>{field.label}</FieldLabel>
          {field.help && <FieldDescription>{field.help}</FieldDescription>}
          {error && <FieldError>{error}</FieldError>}
        </FieldContent>
        <Switch
          id={id}
          checked={Boolean(value)}
          onCheckedChange={(checked) => onChange(checked)}
          aria-invalid={Boolean(error) || undefined}
        />
      </Field>
    )
  }

  if (field.control === 'ordered_multi_select' && field.key === 'EMAIL_SOURCE') {
    return <EmailSourceField field={field} value={value} onChange={onChange} error={error} />
  }

  const help = field.secret
    ? field.configured ? `${field.help ?? ''}${field.help ? ' ' : ''}已配置；输入新值可覆盖，留空保留现有值。`
      : `${field.help ?? ''}${field.help ? ' ' : ''}保存在 .env，可留空。`
    : field.help

  let control: ReactNode
  if (field.key === 'PLAN_CHECK_PROXY_MODE' || field.key === 'SMS_PROVIDER') {
    control = (
      <ToggleGroup
        aria-label={field.label}
        value={choices.some((choice) => choice.value === selected) ? [selected] : []}
        onValueChange={(values) => { if (values[0]) onChange(values[0]) }}
        variant="outline"
        size="sm"
        className="flex-wrap"
      >
        {choices.filter((choice) => choice.value !== '').map((choice) => (
          <ToggleGroupItem key={`${field.key}-${choice.value}`} value={choice.value} disabled={choice.disabled}>
            {choice.label || choice.value}
          </ToggleGroupItem>
        ))}
      </ToggleGroup>
    )
  } else if (choices.length || field.key === 'SMSBOWER_SERVICE' || field.key === 'SMSBOWER_COUNTRY') {
    control = (
      <>
        {isMetadataField && (
          <Field className="mb-2">
            <FieldLabel htmlFor={metadataSearchId} className="text-xs">搜索{field.label}</FieldLabel>
            <Input
              id={metadataSearchId}
              type="search"
              value={selectQuery}
              onChange={(event) => onMetadataSearch(event.currentTarget.value)}
              placeholder="输入名称、代码或 ID"
            />
          </Field>
        )}
        <Select
          items={selectItems}
          value={selected || null}
          onValueChange={(next) => { if (next !== null) onChange(next) }}
          disabled={!choices.length}
        >
          <SelectTrigger id={id} aria-invalid={Boolean(error) || undefined} className="w-full min-w-0">
            <SelectValue placeholder={isMetadataField && !metadata.loaded ? '请先刷新服务与国家' : `选择${field.label}`} />
          </SelectTrigger>
          <SelectContent>
            <SelectGroup>
              {selectItems.map((choice) => (
                <SelectItem key={`${field.key}-${choice.value}`} value={choice.value} disabled={choice.disabled}>
                  {choice.label}
                </SelectItem>
              ))}
            </SelectGroup>
          </SelectContent>
        </Select>
      </>
    )
  } else if (field.type === 'int' || field.type === 'float') {
    control = (
      <Input
        id={id}
        type="number"
        step={field.type === 'float' ? 'any' : '1'}
        value={valueForInput(field, value)}
        onChange={(event) => onChange(event.currentTarget.value)}
        aria-invalid={Boolean(error) || undefined}
      />
    )
  } else if (field.type === 'list_str_multiline') {
    control = (
      <Textarea
        id={id}
        value={valueForInput(field, value)}
        onChange={(event) => onChange(event.currentTarget.value)}
        aria-invalid={Boolean(error) || undefined}
        placeholder="每行一条，可留空"
        className="min-h-28"
      />
    )
  } else {
    control = (
      <Input
        id={id}
        type={field.secret ? 'password' : 'text'}
        value={valueForInput(field, value)}
        onChange={(event) => onChange(event.currentTarget.value)}
        aria-invalid={Boolean(error) || undefined}
        placeholder={field.secret ? (field.configured ? '已配置；输入新值可覆盖' : '保存在 .env，可留空') : '可留空'}
        autoComplete="off"
        spellCheck={false}
      />
    )
  }

    return (
      <Field data-invalid={Boolean(error) || undefined}>
        <FieldLabel htmlFor={id}>{field.label}</FieldLabel>
        {help && <FieldDescription>{help}</FieldDescription>}
        {control}
        {field.external_url && (
          <a className="w-fit text-sm text-primary underline underline-offset-4" href={field.external_url} target="_blank" rel="noopener noreferrer">
            {field.external_label || '打开外部服务'}
          </a>
        )}
        {error && <FieldError>{error}</FieldError>}
      </Field>
    )
}


function EmailSourceField({
  field,
  value,
  onChange,
  error,
}: {
  field: ConfigField
  value: ConfigValue
  onChange: (value: ConfigValue) => void
  error?: string
}) {
  const state = parseEmailSource(value)
  const sources = state.values
  const optionByValue = new Map((field.options ?? []).map((option) => [String(option.value), option]))
  const summary = error
    ? `配置需修正（${sources.length} 项）`
    : sources.length === 0
      ? '请选择邮箱来源'
      : sources.length === 1
        ? `已选择：${optionByValue.get(sources[0])?.label || sources[0]}`
        : `已选择 ${sources.length} 个：${optionByValue.get(sources[0])?.label || sources[0]} 为主来源`

  const update = (next: string[]) => onChange(next.join(','))
  const toggleOption = (source: string) => {
    const next = sources.includes(source) ? sources.filter((item) => item !== source) : [...sources, source]
    if (!next.some((item) => optionByValue.has(item))) return
    update(next)
  }
  const move = (index: number, delta: number) => {
    const target = index + delta
    if (target < 0 || target >= sources.length) return
    const next = [...sources]
    ;[next[index], next[target]] = [next[target], next[index]]
    update(next)
  }
  const remove = (index: number) => {
    const next = sources.filter((_, current) => current !== index)
    if (!next.some((item) => optionByValue.has(item))) return
    update(next)
  }

  return (
    <FieldSet data-invalid={Boolean(error) || undefined} className="min-w-0 rounded-lg border p-3">
      <FieldLegend variant="label">{field.label}</FieldLegend>
      {field.help && <FieldDescription>{field.help}</FieldDescription>}
      <FieldGroup className="gap-2 sm:grid sm:grid-cols-2">
        {(field.options ?? []).map((option) => {
          const id = `email-source-${option.value}`
          return (
            <Field key={option.value} orientation="horizontal" className="min-w-0">
              <Checkbox
                id={id}
                checked={sources.includes(String(option.value))}
                onCheckedChange={() => toggleOption(String(option.value))}
                aria-label={option.label || option.value}
              />
              <FieldContent className="min-w-0">
                <FieldLabel htmlFor={id} className="font-normal">{option.label || option.value}</FieldLabel>
                <FieldDescription className="break-all">{option.value}</FieldDescription>
              </FieldContent>
            </Field>
          )
        })}
      </FieldGroup>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-sm text-muted-foreground">{summary}</span>
        <span className="text-xs text-muted-foreground">按顺序作为主来源与兜底来源</span>
      </div>
      {sources.length ? (
        <ol className="flex min-w-0 flex-col gap-2" aria-label="邮箱来源优先顺序">
          {sources.map((source, index) => {
            const option = optionByValue.get(source)
            const label = option?.label || `未知来源：${source}`
            return (
              <li key={`${source}-${index}`} className="flex min-w-0 items-center gap-2 rounded-lg border p-2">
                <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-muted text-xs font-semibold">{index + 1}</span>
                <span className="min-w-0 flex-1 break-words text-sm">{label}<span className="ml-1 text-xs text-muted-foreground">{index === 0 ? '主来源' : `第 ${index + 1} 顺位兜底`}</span></span>
                <div className="flex shrink-0 gap-1">
                  <Button type="button" variant="outline" size="icon-sm" aria-label={`上移 ${label}`} title="上移" disabled={index === 0} onClick={() => move(index, -1)}>↑</Button>
                  <Button type="button" variant="outline" size="icon-sm" aria-label={`下移 ${label}`} title="下移" disabled={index === sources.length - 1} onClick={() => move(index, 1)}>↓</Button>
                  <Button type="button" variant="ghost" size="icon-sm" aria-label={`移除 ${label}`} title="移除" onClick={() => remove(index)}>×</Button>
                </div>
              </li>
            )
          })}
        </ol>
      ) : (
        <p className="rounded-lg border border-dashed p-3 text-sm text-muted-foreground">请至少选择一个邮箱来源。</p>
      )}
      {error && <FieldError>{error}</FieldError>}
    </FieldSet>
  )
}

export default function ConfigPage() {
  const [config, setConfig] = useState<ConfigField[]>([])
  const [drafts, setDrafts] = useState<Record<string, Draft>>({})
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})
  const [search, setSearch] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [activeGroup, setActiveGroup] = useState('')
  const [activeSubsections, setActiveSubsections] = useState<Record<string, string>>({})
  const [metadata, setMetadata] = useState<MetadataState>(INITIAL_METADATA)
  const [metadataSearch, setMetadataSearch] = useState<Record<string, string>>({})
  const version = useRef(0)
  const initialGroupChosen = useRef(false)

  const groups = useMemo(() => {
    const grouped = new Map<string, ConfigField[]>()
    config.forEach((field) => {
      const name = field.group || '其他'
      const list = grouped.get(name) ?? []
      list.push(field)
      grouped.set(name, list)
    })
    return [...grouped.entries()]
  }, [config])

  const query = search.trim().toLocaleLowerCase()
  const visibleGroups = useMemo(() => groups.flatMap(([name, fields]) => {
    const matchingFields = fields.filter((field) => configFieldMatches(field, query))
    return matchingFields.length ? [{ name, fields: matchingFields }] : []
  }), [groups, query])

  const fieldsByKey = useMemo(() => new Map(config.map((field) => [field.key, field])), [config])
  const dirtyFields = useMemo(() => config.filter((field) => {
    const draft = drafts[field.key]
    return draft !== undefined && !valuesEqual(field, draft.value, field.value)
  }), [config, drafts])
  const dirtyCount = dirtyFields.length
  const currentFieldValue = useCallback((field: ConfigField): ConfigValue => {
    const draft = drafts[field.key]
    return draft ? draft.value : (field.value as ConfigValue)
  }, [drafts])
  const currentValueByKey = useCallback((key: string) => {
    const field = fieldsByKey.get(key)
    return field ? currentFieldValue(field) : ''
  }, [currentFieldValue, fieldsByKey])

  const updateField = useCallback((key: string, value: ConfigValue) => {
    version.current += 1
    setDrafts((current) => ({ ...current, [key]: { value, version: version.current } }))
    setFieldErrors((current) => {
      if (!current[key]) return current
      const next = { ...current }
      delete next[key]
      return next
    })
    setError('')
  }, [])

  const loadConfig = useCallback(async () => {
    setLoading(true)
    try {
      const result = await api<ConfigResponse>('/api/config')
      const valid = Array.isArray(result) ? result : []
      setConfig(valid)
      setDrafts({})
      setFieldErrors({})
      setError('')
      setMetadata(INITIAL_METADATA)
      if (!initialGroupChosen.current && valid[0]?.group) {
        setActiveGroup(valid[0].group)
        initialGroupChosen.current = true
      }
      toast.add({ title: '配置已重新加载', type: 'success' })
    } catch (requestError) {
      setError(`加载配置失败：${errorMessage(requestError)}`)
      toast.add({ title: errorMessage(requestError), type: 'error' })
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadConfig()
  }, [loadConfig])

  const saveConfig = useCallback(async () => {
    if (saving) return
    const pending = config.filter((field) => {
      const draft = drafts[field.key]
      return draft !== undefined && !valuesEqual(field, draft.value, field.value)
    })
    if (!pending.length) return

    const updates: Record<string, ConfigValue> = {}
    const versions: Record<string, number> = {}
    const validationErrors: Record<string, string> = {}
    pending.forEach((field) => {
      const draft = drafts[field.key]
      if (!draft) return
      if (field.key === 'EMAIL_SOURCE') {
        const message = emailSourceError(field, draft.value)
        if (message) {
          validationErrors[field.key] = message
          return
        }
      }
      if (field.type === 'int' || field.type === 'float') {
        const text = String(draft.value ?? '').trim()
        if (text && !Number.isFinite(Number(text))) {
          validationErrors[field.key] = '请输入有效数字。'
          return
        }
        if (field.type === 'int' && text && !Number.isInteger(Number(text))) {
          validationErrors[field.key] = '请输入整数。'
          return
        }
      }
      const next = toConfigValue(field, draft.value)
      if (field.secret && !String(next ?? '').trim()) return
      updates[field.key] = next
      versions[field.key] = draft.version
    })

    if (Object.keys(validationErrors).length) {
      setFieldErrors((current) => ({ ...current, ...validationErrors }))
      const firstInvalid = config.find((field) => validationErrors[field.key])
      setError(firstInvalid ? `${firstInvalid.label}：${validationErrors[firstInvalid.key]}` : '请修正配置字段后再保存。')
      return
    }
    if (!Object.keys(updates).length) {
      setDrafts((current) => {
        const next = { ...current }
        pending.forEach((field) => {
          const draft = current[field.key]
          if (draft && field.secret && !String(draft.value ?? '').trim()) delete next[field.key]
        })
        return next
      })
      return
    }

    setSaving(true)
    setError('')
    try {
      const result = await api<ConfigSaveResponse>('/api/config', post({ updates }))
      setConfig((current) => current.map((field) => {
        if (!(field.key in updates)) return field
        const saved = updates[field.key]
        if (field.secret) return { ...field, value: '', configured: Boolean(String(saved ?? '').trim()) || Boolean(field.configured) }
        return { ...field, value: saved }
      }))
      setDrafts((current) => {
        const next = { ...current }
        Object.entries(versions).forEach(([key, savedVersion]) => {
          if (current[key]?.version === savedVersion) delete next[key]
        })
        return next
      })
      setFieldErrors((current) => {
        const next = { ...current }
        Object.keys(updates).forEach((key) => delete next[key])
        return next
      })
      const ignored = result.ignored?.filter((key) => key in updates) ?? []
      if (ignored.length) {
        setError(`服务端未接受字段：${ignored.join('、')}。未接受的输入仍保留在编辑区。`)
        toast.add({ title: '部分配置未被服务端接受', type: 'warning' })
      } else {
        toast.add({ title: result.note || (result.reloaded ? '配置已保存并生效' : '配置已保存'), type: 'success' })
      }
    } catch (requestError) {
      const message = errorMessage(requestError)
      const matched = config.find((field) => message.includes(field.key))
      if (matched) setFieldErrors((current) => ({ ...current, [matched.key]: message }))
      setError(`保存失败：${message}`)
      toast.add({ title: `保存失败：${message}`, type: 'error' })
    } finally {
      setSaving(false)
    }
  }, [config, drafts, saving])

  const loadMetadata = useCallback(async () => {
    if (metadata.loading) return
    setMetadata((current) => ({ ...current, loading: true, error: '', message: '正在获取服务与国家…' }))
    try {
      const apiBase = String(currentValueByKey('SMSBOWER_API_BASE') ?? '').trim()
      const apiKeyValue = String(currentValueByKey('SMSBOWER_API_KEY') ?? '').trim()
      const requestBody: Record<string, string> = {}
      if (apiBase) requestBody.api_base = apiBase
      if (apiKeyValue) requestBody.api_key = apiKeyValue
      const result = await api<MetadataResponse>('/api/smsbower/metadata', post(requestBody))
      if (result.ok === false) throw new Error(result.error || 'SMSBower 元数据读取失败')
      const services = normalizeMetadataOptions(result.services)
      const countries = normalizeMetadataOptions(result.countries)
      setMetadata({
        services,
        countries,
        loaded: true,
        loading: false,
        error: '',
        message: `已加载 ${services.length} 个服务、${countries.length} 个国家`,
      })
      toast.add({ title: `SMSBower 选项已加载（${services.length} 个服务、${countries.length} 个国家）`, type: 'success' })
    } catch (requestError) {
      const message = errorMessage(requestError)
      setMetadata((current) => ({ ...current, loading: false, error: message, message: `获取失败：${message}` }))
      toast.add({ title: `SMSBower 元数据获取失败：${message}`, type: 'error' })
    }
  }, [currentValueByKey, metadata.loading])

  const saveEnabled = dirtyCount > 0 && !saving && !loading

  const setSubsection = (group: string, section: string) => {
    setActiveSubsections((current) => ({ ...current, [group]: section }))
  }

  const groupFieldsForRender = (group: string, visibleFields: ConfigField[]) => {
    const sections = getSections(group, visibleFields)
    if (!sections.length) return { sections, activeSection: '', fields: visibleFields }
    const saved = activeSubsections[group]
    const active = saved && sections.includes(saved) ? saved : sections[0]
    const matchSection = sections.find((section) => visibleFields.some((field) => subsectionFor(field)[0] === section))
    const activeSection = query && matchSection ? matchSection : active
    return {
      sections,
      activeSection,
      fields: visibleFields.filter((field) => subsectionFor(field)[0] === activeSection),
    }
  }

  return (
    <main className="mx-auto w-full min-w-0 max-w-screen-2xl overflow-x-hidden px-3 py-4 sm:px-5 lg:px-6">
      <header className="mb-4 flex min-w-0 flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <h1 className="text-xl font-semibold tracking-tight">运行配置</h1>
          <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
            编辑运行时配置。机密字段不会回显，密钥只在本次编辑中暂存，不写入浏览器存储。
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge
            status={error ? 'failed' : loading ? 'pending' : dirtyCount ? 'pending' : 'active'}
            label={error ? '需要处理' : loading ? '读取中' : dirtyCount ? `未保存 ${dirtyCount} 项` : '已同步'}
          />
          {dirtyCount ? (
            <ConfirmAction
              title="丢弃未保存的修改？"
              description="重新加载会丢弃当前页面所有未保存的配置修改。"
              confirmLabel="丢弃并重新加载"
              triggerVariant="outline"
              triggerSize="sm"
              disabled={loading || saving}
              onConfirm={() => { void loadConfig() }}
            >
              重新加载
            </ConfirmAction>
          ) : (
            <Button type="button" variant="outline" size="sm" onClick={() => { void loadConfig() }} disabled={loading || saving}>
              重新加载
            </Button>
          )}
          <Button type="button" size="sm" onClick={() => { void saveConfig() }} disabled={!saveEnabled}>
            {saving ? '保存中…' : `保存${dirtyCount ? `（${dirtyCount}）` : ''}`}
          </Button>
        </div>
      </header>

      {error && (
        <Alert variant="destructive" className="mb-4">
          <AlertTitle>配置操作失败</AlertTitle>
          <AlertDescription className="break-words">{error}</AlertDescription>
        </Alert>
      )}

      <div className="mb-4">
        <TableToolbar
          search={search}
          onSearchChange={setSearch}
          searchLabel="搜索配置名称、键名或说明"
          actions={dirtyCount > 0 ? (
            <span className="text-sm text-muted-foreground">修改只保留在当前编辑会话中，点击保存提交。</span>
          ) : null}
        />
      </div>

      {loading && !config.length ? (
        <div className="flex flex-col gap-3">
          <Skeleton className="h-16 w-full" />
          <Skeleton className="h-56 w-full" />
        </div>
      ) : config.length === 0 ? (
        <Card>
          <CardHeader>
            <CardTitle>未加载到配置项</CardTitle>
            <CardDescription>请重新加载，或检查配置 API 是否可用。</CardDescription>
          </CardHeader>
          <CardFooter>
            <Button type="button" variant="outline" onClick={() => { void loadConfig() }} disabled={loading}>重新加载</Button>
          </CardFooter>
        </Card>
      ) : (
        <div className="grid min-w-0 gap-4 lg:grid-cols-[13rem_minmax(0,1fr)]">
          <nav aria-label="配置分组" className="flex min-w-0 gap-2 overflow-x-auto pb-1 lg:sticky lg:top-4 lg:h-fit lg:flex-col lg:overflow-visible lg:pb-0">
            {visibleGroups.length ? visibleGroups.map(({ name, fields }) => (
              <button
                key={name}
                type="button"
                onClick={() => {
                  setActiveGroup(name)
                  document.getElementById(slug(name))?.scrollIntoView({ behavior: 'smooth', block: 'start' })
                }}
                aria-current={activeGroup === name ? 'location' : undefined}
                className={`flex shrink-0 items-center justify-between gap-2 rounded-lg border px-3 py-2 text-left text-sm transition-colors lg:w-full ${activeGroup === name ? 'border-primary bg-accent text-accent-foreground' : 'bg-card hover:bg-muted'}`}
              >
                <span className="min-w-0 truncate">{name}</span>
                <span className="text-xs text-muted-foreground">{fields.length}</span>
              </button>
            )) : (
              <p className="px-2 py-3 text-sm text-muted-foreground">没有匹配的配置项。</p>
            )}
          </nav>

          <div className="flex min-w-0 flex-col gap-4">
            {visibleGroups.map(({ name, fields: visibleFields }) => {
              const { sections, activeSection, fields: displayedFields } = groupFieldsForRender(name, visibleFields)
              const hasSubsections = sections.length > 0
              const sectionIntro = hasSubsections
                ? subsectionFor(displayedFields[0] ?? visibleFields[0])[1]
                : groupIntro(name)
              const fieldsToRender = name === '功能开关'
                ? [...displayedFields].sort((a, b) => {
                  const priority = ['ENABLE_CODEX_AUTO', 'ENABLE_2FA', 'ENABLE_FLOW_TRIGGER', 'AUTO_PLAN_CHECK_AFTER_REGISTER']
                  const ai = priority.indexOf(a.key)
                  const bi = priority.indexOf(b.key)
                  return (ai < 0 ? priority.length : ai) - (bi < 0 ? priority.length : bi)
                })
                : displayedFields
              const displayedDirtyCount = dirtyFields.filter((field) => visibleFields.includes(field)).length
              const groupId = slug(name)
              const groupContent = (
                <>
                  {name === '接码平台' && activeSection === 'SMSBower' && (
                    <div className="mb-5 rounded-lg border bg-muted/30 p-3">
                      <div className="flex min-w-0 flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
                        <div className="min-w-0">
                          <p className="font-medium">SMSBower 服务与国家</p>
                          <p className="text-sm text-muted-foreground">使用当前填写或已保存的 API 地址和 API Key 获取可选项。仅点击刷新时向 API 请求。</p>
                        </div>
                        <Button type="button" variant="outline" size="sm" onClick={() => { void loadMetadata() }} disabled={metadata.loading}>
                          {metadata.loading ? '获取中…' : '刷新选项'}
                        </Button>
                      </div>
                      <p className={`mt-2 break-words text-sm ${metadata.error ? 'text-destructive' : 'text-muted-foreground'}`} role={metadata.error ? 'alert' : 'status'}>
                        {metadata.message}
                      </p>
                      {metadata.error && <p className="mt-1 text-sm text-destructive">{metadata.error}</p>}
                    </div>
                  )}
                  {sectionIntro && hasSubsections && <p className="mb-4 text-sm text-muted-foreground">{sectionIntro}</p>}
                  <FieldGroup className="sm:grid sm:grid-cols-2 sm:gap-x-5">
                    {fieldsToRender.map((field) => {
                      const value = currentFieldValue(field)
                      const fieldError = fieldErrors[field.key] || (field.key === 'EMAIL_SOURCE' ? emailSourceError(field, value) : '')
                      return (
                        <div key={field.key} className={field.type === 'bool' || field.control === 'ordered_multi_select' || field.type === 'list_str_multiline' ? 'sm:col-span-2' : ''}>
                          <ConfigInput
                            field={field}
                            value={value}
                            metadata={metadata}
                            metadataSearch={metadataSearch[field.key] ?? ''}
                            onChange={(next) => updateField(field.key, next)}
                            onMetadataSearch={(next) => setMetadataSearch((current) => ({ ...current, [field.key]: next }))}
                            error={fieldError || undefined}
                          />
                        </div>
                      )
                    })}
                  </FieldGroup>
                </>
              )
              return (
                <Card key={name} id={groupId} className="min-w-0 scroll-mt-4 overflow-hidden">
                  <CardHeader className="min-w-0">
                    <CardTitle>{name}</CardTitle>
                    <CardDescription>{groupIntro(name) || `${visibleFields.length} 个可编辑字段`}</CardDescription>
                    {name === '功能开关' && !query && <span className="sr-only">功能开关列表</span>}
                  </CardHeader>
                  <CardContent className="min-w-0">
                    {sections.length > 0 ? (
                      <Tabs value={activeSection} onValueChange={(value) => setSubsection(name, value)} className="min-w-0">
                        <TabsList aria-label={`${name}配置分组`} className="max-w-full flex-wrap">
                          {sections.map((section) => <TabsTrigger key={section} value={section}>{section}</TabsTrigger>)}
                        </TabsList>
                        <TabsContent value={activeSection} className="min-w-0 pt-4">{groupContent}</TabsContent>
                      </Tabs>
                    ) : groupContent}
                  </CardContent>
                  <CardFooter className="flex flex-col items-stretch gap-2 border-t bg-muted/20 sm:flex-row sm:items-center sm:justify-between">
                    <span className="text-xs text-muted-foreground">
                      {displayedDirtyCount ? `本分组有 ${displayedDirtyCount} 项未保存` : '本分组没有未保存修改'}
                    </span>
                    <Button type="button" size="sm" onClick={() => { void saveConfig() }} disabled={!saveEnabled}>
                      {saving ? '保存中…' : '保存修改'}
                    </Button>
                  </CardFooter>
                </Card>
              )
            })}
            {visibleGroups.length === 0 && (
              <Card>
                <CardContent className="py-8 text-center text-sm text-muted-foreground">没有找到匹配的配置项。</CardContent>
              </Card>
            )}
          </div>
        </div>
      )}
    </main>
  )
}
