import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { Copy, Download, Eye, EyeOff, MoreHorizontal, RefreshCw } from 'lucide-react'
import { ConfirmAction } from '@/components/app/confirm-action'
import { StatusBadge } from '@/components/app/status-badge'
import { TableToolbar } from '@/components/app/table-toolbar'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { DropdownMenu, DropdownMenuContent, DropdownMenuGroup, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { Checkbox } from '@/components/ui/checkbox'
import { Field, FieldGroup, FieldLabel } from '@/components/ui/field'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { toast } from '@/components/ui/toast'
import { api } from '@/lib/api'

type Capabilities = { can_start?: boolean; is_checking?: boolean; is_running?: boolean; can_retry?: boolean; can_stop?: boolean }
type Account = {
  id: number; email: string; user_name?: string; email_source?: string; note?: string; created_at?: string; password?: string
  has_access_token?: boolean; totp_enabled?: boolean; twofa_status?: string; twofa_stage?: string; twofa_error_code?: string
  totp_setup_status?: string; totp_setup_message?: string; totp_setup_error?: string
  codex_auth_status?: string; codex_operation_status?: string; codex_error?: string; codex_capabilities?: Capabilities
  live_check_status?: string; live_check_error?: string; live_checked_at?: string; live_check_fingerprint_text?: string; live_check_capabilities?: Capabilities
  plan_type?: string; current_plan_type?: string; plan_category_code?: string; plan_query_status?: string; plan_check_error?: string
  plan_checked_at?: string; plan_check_updated_at?: string; plan_last_success_at?: string; plan_capabilities?: Capabilities
  plan_expires_at?: string; expires_at?: string; plan_renews_at?: string; renews_at?: string; billing_period?: string; billing_currency?: string
  discount_type?: string; discount_amount?: number | string; discount_expires_at?: string; discount_promo_campaign_id?: string
  eligible_promo_campaigns?: Record<string, unknown>; token_expired?: boolean; token_expires_at?: string; needs_live_check?: boolean
}
type ListResult = { items: Account[]; total: number; revision?: string }
type Item = { id?: number; email?: string; reason?: string; error?: string; value?: string; fallback?: boolean }
type Result = {
  ok?: boolean; message?: string; error?: string; value?: string; fallback?: boolean; fallback_reason?: string; skip_reason?: string
  values?: Item[]; fallback_count?: number; skipped?: Item[]; started?: Item[]; updated?: Item[]; deleted?: Item[]
  started_count?: number; updated_count?: number; deleted_count?: number; stopped_count?: number; busy_count?: number; failed_count?: number
  download_url?: string; filename?: string; added_count?: number; error_count?: number
}
type Filters = { email_source: string; codex_auth_status: string; codex_operation_status: string; live_check_status: string; plan: string; promoType: string; promoDiscount: string; q: string; date_from: string; date_to: string }
type LogTarget = { kind: 'codex' | 'live' | 'totp'; email: string }
type Command = { title: string; description: string; url: string; body: unknown; log?: LogTarget; remove?: number[] }

const initialFilters: Filters = { email_source: '', codex_auth_status: '', codex_operation_status: '', live_check_status: '', plan: '', promoType: '', promoDiscount: '', q: '', date_from: '', date_to: '' }
const sources: Record<string, string> = { outlook: 'Outlook 邮箱池', generic_api: '通用 API 邮箱', cloudflare_domain: 'Cloudflare 域名邮箱', icloud: 'iCloud 隐私邮箱', cloudflare: 'Cloudflare 临时邮箱', unknown: '未知' }
const authLabels: Record<string, string> = { not_started: '未开始', success: '成功', failed: '失败', skipped: '已跳过', unknown: '未知' }
const operationLabels: Record<string, string> = { idle: '未操作', queued: '补跑排队中', running: '补跑中', success: '补跑成功', failed: '补跑失败', canceled: '已停止', unknown: '未知' }
const liveLabels: Record<string, string> = { pending: '待查活', queued: '排队中', running: '查活中', live: '正常', deactivated: '已废', failed: '失败', unknown: '未知' }
const queryLabels: Record<string, string> = { pending: '待查询', queued: '查询排队中', running: '查询中', success: '查询成功', failed: '查询失败', unknown: '未知' }
const categoryLabels: Record<string, string> = { free_trial_eligible: '有套餐优惠', free_no_trial: '无套餐优惠', paid: '付费套餐', unknown: '未知' }
const formats: Record<string, string> = { access_token: 'AT', email_access_token: '邮箱----AT', email_access_token_totp: '邮箱----AT----2FA' }
const skipLabels: Record<string, string> = { missing_access_token: '无 AT', missing_email: '无邮箱', empty: '值为空' }
const PASSWORD_DISPLAY_LENGTH = 14
const PASSWORD_MASK = '●'.repeat(PASSWORD_DISPLAY_LENGTH)

function passwordPreview(password: string) {
  return password.length <= PASSWORD_DISPLAY_LENGTH ? password : `${password.slice(0, 6)}...${password.slice(-5)}`
}

function post(body: unknown = {}) { return { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } }
function errorText(error: unknown) { return error instanceof Error ? error.message : '请求失败' }
function notify(title: string, type: 'success' | 'error' = 'success') { toast.add({ title, type }) }
function download(url: string, filename: string) {
  const anchor = document.createElement('a')
  anchor.href = url; anchor.download = filename; anchor.rel = 'noopener'
  document.body.appendChild(anchor); anchor.click(); anchor.remove()
}
async function clipboard(value: string) {
  if (!value) throw new Error('没有可复制内容')
  if (navigator.clipboard?.writeText && window.isSecureContext) { await navigator.clipboard.writeText(value); return }
  const input = document.createElement('textarea')
  input.value = value; input.style.position = 'fixed'; input.style.opacity = '0'; input.setAttribute('readonly', '')
  document.body.appendChild(input); input.select()
  try { if (!document.execCommand('copy')) throw new Error('浏览器拒绝复制，请使用 HTTPS 或 localhost') } finally { input.remove() }
}
function Choice({ label, value, options, onChange }: { label: string; value: string; options: Record<string, string>; onChange: (value: string) => void }) {
  const id = useId()
  const entries = Object.entries(options)
  const emptyOption = entries[0]?.[0] === ''
  const allValue = '__all__'
  const items = entries.map(([key, text]) => ({ value: key === '' ? allValue : key, label: text }))
  return (
    <Field className="w-fit min-w-0 gap-1">
      <FieldLabel htmlFor={id} className="text-xs">{label}</FieldLabel>
      <Select items={items} value={emptyOption && !value ? allValue : value} onValueChange={(next) => onChange(next === allValue ? '' : next ?? '')}>
        <SelectTrigger id={id} size="sm" className="max-w-full">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectGroup>{items.map((item) => <SelectItem key={item.value} value={item.value}>{item.label}</SelectItem>)}</SelectGroup>
        </SelectContent>
      </Select>
    </Field>
  )
}
function PlanCell({ account: a, onDetails }: { account: Account; onDetails: () => void }) {
  const status = a.plan_query_status || 'unknown'
  const plan = a.current_plan_type || a.plan_type || '未知'
  const failed = Boolean(a.plan_check_error) || status === 'failed'
  const expiry = a.plan_expires_at || a.expires_at || a.plan_renews_at || a.renews_at
  const details = [a.plan_check_error, a.plan_checked_at && `查询：${a.plan_checked_at}`, a.plan_last_success_at && `上次成功：${a.plan_last_success_at}`, a.billing_period, a.billing_currency, expiry && `到期：${expiry}`, a.plan_renews_at && `续费：${a.plan_renews_at}`, a.discount_amount != null && `折扣：${a.discount_amount}${a.discount_type === 'percentage' ? '%' : ''}`, a.discount_expires_at && `折扣结束：${a.discount_expires_at}`, a.discount_promo_campaign_id].filter(Boolean).join('\n')
  const label = ['queued', 'running'].includes(status) ? queryLabels[status] : failed ? `查询失败${a.plan_last_success_at ? `（上次：${plan}）` : ''}` : `${plan} · ${categoryLabels[a.plan_category_code || 'unknown'] || '未知'}`
  return <div className="flex flex-col items-start gap-1" title={details}><StatusBadge status={failed ? 'failed' : status} label={label} />{a.plan_check_updated_at && <span className="text-xs text-muted-foreground">{queryLabels[status] || status} · {a.plan_check_updated_at}</span>}{expiry && !failed && <span className="text-xs text-muted-foreground">到期 {expiry}</span>}{plan.toLowerCase() === 'free' && Object.keys(a.eligible_promo_campaigns || {}).length > 0 && <Button size="sm" variant="link" onClick={onDetails}>优惠详情 ({Object.keys(a.eligible_promo_campaigns || {}).length})</Button>}</div>
}

export default function AccountsPage() {
  const [filters, setFilters] = useState(initialFilters)
  const [advancedFiltersOpen, setAdvancedFiltersOpen] = useState(false)
  const advancedFiltersId = useId()
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [accounts, setAccounts] = useState<Account[]>([])
  const [total, setTotal] = useState(0)
  const [selected, setSelected] = useState<number[]>([])
  const [workersInput, setWorkersInput] = useState('3')
  const [format, setFormat] = useState('access_token')
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [command, setCommand] = useState<Command | null>(null)
  const [noteTarget, setNoteTarget] = useState<{ ids: number[]; single: boolean; email?: string } | null>(null)
  const [note, setNote] = useState('')
  const [logTarget, setLogTarget] = useState<LogTarget | null>(null)
  const [log, setLog] = useState('')
  const [logRunning, setLogRunning] = useState(false)
  const [promo, setPromo] = useState<Account | null>(null)
  const [totp, setTotp] = useState<{ id: number; email: string; code: string; expires: number } | null>(null)
  const [visiblePasswords, setVisiblePasswords] = useState<Set<number>>(() => new Set())
  const [now, setNow] = useState(Date.now())
  const request = useRef<AbortController | null>(null)
  const revision = useRef<string | undefined>(undefined)
  const selectedAccounts = useRef(new Map<number, Account>())
  const logContent = useRef<HTMLPreElement>(null)
  const pageCount = Math.max(1, Math.ceil(total / pageSize))
  const visibleIds = accounts.map((a) => a.id)
  const allSelected = visibleIds.length > 0 && visibleIds.every((id) => selected.includes(id))
  const workers = Math.max(1, Math.min(16, Number.parseInt(workersInput, 10) || 3))
  const activeAdvancedFilterCount = Number(Boolean(filters.codex_operation_status)) + Number(Boolean(filters.live_check_status)) + Number(Boolean(filters.plan)) + Number(Boolean(filters.date_from && filters.date_to))
  const params = new URLSearchParams({ paged: '1', page: String(page), page_size: String(pageSize) })
  for (const key of ['email_source', 'codex_auth_status', 'codex_operation_status', 'live_check_status', 'q'] as const) if (filters[key]) params.set(key, filters[key])
  if (filters.date_from && filters.date_to) { params.set('date_from', filters.date_from); params.set('date_to', filters.date_to) }
  const planFilter = filters.plan === 'promo' && (filters.promoType || filters.promoDiscount) ? `promo:${filters.promoType || '*'}:${filters.promoDiscount}` : filters.plan
  if (planFilter) params.set('plan', planFilter)
  const query = params.toString()

  const changeFilter = useCallback((key: keyof Filters, value: string) => {
    setFilters((current) => ({ ...current, [key]: value })); setPage(1); setSelected([]); selectedAccounts.current.clear(); revision.current = undefined
  }, [])
  useEffect(() => {
    const timer = window.setTimeout(() => { if (search.trim() !== filters.q) changeFilter('q', search.trim()) }, 250)
    return () => window.clearTimeout(timer)
  }, [search, filters.q, changeFilter])

  const loadAccounts = useCallback(async () => {
    request.current?.abort()
    const controller = new AbortController(); request.current = controller
    setLoading(true)
    setVisiblePasswords(new Set())
    try {
      const result = await api<ListResult>(`/api/accounts?${query}`, { signal: controller.signal })
      if (controller.signal.aborted) return
      const lastPage = Math.max(1, Math.ceil(result.total / pageSize))
      setTotal(result.total)
      if (page > lastPage) { setPage(lastPage); return }
      setAccounts(result.items || []); setError('')
      for (const account of result.items || []) if (selectedAccounts.current.has(account.id)) selectedAccounts.current.set(account.id, account)
    } catch (err) { if (!controller.signal.aborted) setError(errorText(err)) } finally { if (!controller.signal.aborted) setLoading(false) }
  }, [query, page, pageSize])
  useEffect(() => { void loadAccounts(); return () => request.current?.abort() }, [loadAccounts])
  useEffect(() => {
    let disposed = false
    let timer: number | undefined
    const controller = new AbortController()
    const poll = async () => {
      try {
        const snapshot = await api<ListResult>(`/api/accounts/plan-check-status?${query}`, { signal: controller.signal })
        if (disposed) return
        if (snapshot.revision !== revision.current) { revision.current = snapshot.revision; await loadAccounts() }
      } catch (err) { if (!disposed) setError(`状态刷新失败：${errorText(err)}`) }
      if (!disposed) timer = window.setTimeout(() => void poll(), 5000)
    }
    timer = window.setTimeout(() => void poll(), 5000)
    return () => { disposed = true; controller.abort(); window.clearTimeout(timer) }
  }, [query, loadAccounts])
  useEffect(() => {
    if (!logTarget) return
    let disposed = false
    let timer: number | undefined
    const controller = new AbortController()
    const target = logTarget
    setLog('加载中…'); setLogRunning(false)
    const poll = async () => {
      try {
        const path = target.kind === 'codex' ? '/api/codex/retry-log' : target.kind === 'live' ? '/api/accounts/live-check-log' : '/api/accounts/totp-setup-log'
        const result = await api<{ log: string; running: boolean }>(`${path}?email=${encodeURIComponent(target.email)}`, { signal: controller.signal })
        if (disposed) return
        const element = logContent.current
        const atBottom = !element || element.scrollTop + element.clientHeight >= element.scrollHeight - 30
        setLog(result.log || '暂无日志，等待任务写入…'); setLogRunning(result.running)
        if (atBottom) requestAnimationFrame(() => { if (logContent.current) logContent.current.scrollTop = logContent.current.scrollHeight })
        if (result.running) timer = window.setTimeout(() => void poll(), target.kind === 'totp' ? 5000 : 2000)
        else await loadAccounts()
      } catch (err) { if (!disposed) setLog(`读取日志失败：${errorText(err)}`) }
    }
    void poll()
    return () => { disposed = true; controller.abort(); window.clearTimeout(timer) }
  }, [logTarget, loadAccounts])
  useEffect(() => {
    if (!totp) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [totp])

  function toggle(account: Account, checked: boolean) {
    if (checked) selectedAccounts.current.set(account.id, account)
    else selectedAccounts.current.delete(account.id)
    setSelected((ids) => checked ? [...new Set([...ids, account.id])] : ids.filter((id) => id !== account.id))
  }
  async function run(url: string, body: unknown, options: { log?: LogTarget; remove?: number[] } = {}) {
    setBusy(true)
    try {
      const result = await api<Result>(url, post(body))
      if (result.ok === false) throw new Error(result.error || '操作失败')
      const details = [result.message, result.started_count != null && `已入队 ${result.started_count}`, result.updated_count != null && `已更新 ${result.updated_count}`, result.deleted_count != null && `已删除 ${result.deleted_count}`, result.stopped_count != null && `已停止 ${result.stopped_count}`, result.busy_count != null && `忙碌 ${result.busy_count}`, result.failed_count != null && `失败 ${result.failed_count}`, result.skipped?.length && `跳过 ${result.skipped.length}`].filter(Boolean).join('；') || '操作成功'
      setNotice(details + (result.skipped?.length ? `\n${result.skipped.map((item) => `${item.email || item.id || ''}：${skipLabels[item.reason || ''] || item.reason || item.error || '已跳过'}`).join('\n')}` : ''))
      notify(details)
      const removed = options.remove || (result.deleted || []).flatMap((item) => item.id == null ? [] : [item.id])
      if (removed.length) { setSelected((ids) => ids.filter((id) => !removed.includes(id))); removed.forEach((id) => selectedAccounts.current.delete(id)) }
      if (options.log) setLogTarget(options.log)
      else if (result.started?.[0]?.email && (url.includes('retry') || url.includes('check-live') || url.includes('totp-setup'))) setLogTarget({ kind: url.includes('retry') ? 'codex' : url.includes('check-live') ? 'live' : 'totp', email: result.started[0].email })
      await loadAccounts()
      return true
    } catch (err) { notify(errorText(err), 'error'); return false } finally { setBusy(false) }
  }
  function confirm(value: Command) { setCommand(value) }
  function bulk(action: 'live' | 'retry' | 'stop' | 'delete' | 'totp') {
    const ids = [...selected]
    if (!ids.length) return
    const descriptions = {
      live: '将重新登录邮箱 OTP，成功后标记正常并刷新最新 AT。',
      retry: '每个账号会消耗邮箱 OTP 和接码短信（约 $0.13）；正在运行或不可补跑的账号由服务端跳过。',
      stop: '发送停止信号，并将补跑状态标为已停止；不在补跑中的账号会跳过。',
      delete: '将从本地账号列表、注册成功邮箱和 token 文件中移除，无法撤销。',
      totp: '为有 AT 且尚未开启 2FA 的账号排队开启 TOTP，成功后密钥写回账号；其他账号跳过。',
    }
    const titles = { live: '查活刷新 AT', retry: '补跑 Codex', stop: '停止 Codex 补跑', delete: '删除账号', totp: '开启 2FA' }
    const url = { live: '/api/accounts/check-live-bulk', retry: '/api/codex/retry-bulk', stop: '/api/codex/stop-bulk', delete: '/api/accounts/delete-bulk', totp: '/api/accounts/totp-setup-bulk' }[action]
    const limit = ['live', 'retry', 'totp'].includes(action) ? 500 : 5000
    if (ids.length > limit) { notify(`单次最多选择 ${limit} 个账号`, 'error'); return }
    setWorkersInput(String(workers))
    confirm({ title: `${titles[action]}（${ids.length} 个）？`, description: `${descriptions[action]}${['live', 'retry'].includes(action) ? ` 并发线程数：${workers}。` : ''}`, url, body: { account_ids: ids, ...(['live', 'retry'].includes(action) ? { workers } : {}) } })
  }
  async function copyPlain(value: string, label = '已复制') { try { await clipboard(value); notify(label) } catch (err) { notify(errorText(err), 'error') } }
  async function copySecret(account: Account, field: string) {
    setBusy(true)
    try {
      const result = await api<Result>(`/api/accounts/${account.id}/secret?field=${encodeURIComponent(field)}`, { cache: 'no-store' })
      if (!result.value) throw new Error(skipLabels[result.skip_reason || ''] || result.skip_reason || '值为空')
      await clipboard(result.value); notify(`已复制${result.fallback ? '（无本地 2FA，已退化为邮箱----AT）' : ''}`)
    } catch (err) { notify(errorText(err), 'error') } finally { setBusy(false) }
  }
  async function exportAccounts(ids: number[], field: string, asFile = false) {
    if (!ids.length) return
    if (ids.length > 5000) { notify('单次最多导出 5000 个账号', 'error'); return }
    setBusy(true)
    try {
      const result = await api<Result>('/api/accounts/secret-bulk', post({ account_ids: ids, field }))
      const lines = (result.values || []).flatMap((item) => item.value ? [item.value] : [])
      const noAt = (result.skipped || []).filter((item) => item.reason === 'missing_access_token').length
      const fallback = result.fallback_count ?? (result.values || []).filter((item) => item.fallback).length
      const summary = `${lines.length ? asFile ? '已下载' : '已复制' : '未导出'} ${lines.length} 行（${formats[field] || '原始整行'}）${fallback ? `，2FA退化 ${fallback} 个` : ''}${noAt ? `，无AT跳过 ${noAt} 个` : ''}${(result.skipped?.length || 0) > noAt ? `，其他跳过 ${(result.skipped?.length || 0) - noAt} 个` : ''}`
      if (lines.length) {
        if (asFile) {
          const url = URL.createObjectURL(new Blob(['\ufeff' + lines.join('\n')], { type: 'text/plain;charset=utf-8' }))
          download(url, `accounts-selected-${new Date().toISOString().replace(/[:.]/g, '-')}.txt`)
          window.setTimeout(() => URL.revokeObjectURL(url), 1000)
        } else await clipboard(lines.join('\n'))
      }
      setNotice(summary); notify(summary)
    } catch (err) { notify(errorText(err), 'error') } finally { setBusy(false) }
  }
  async function getTotp(account: Account) {
    setBusy(true)
    try {
      const result = await api<{ code: string; remaining_seconds: number }>(`/api/accounts/${account.id}/totp-code`, { cache: 'no-store' })
      if (!/^\d{6}$/.test(result.code)) throw new Error('服务端返回的验证码无效')
      const timestamp = Date.now(); setNow(timestamp); setTotp({ id: account.id, email: account.email, code: result.code, expires: timestamp + result.remaining_seconds * 1000 })
    } catch (err) { notify(errorText(err), 'error') } finally { setBusy(false) }
  }
  async function downloadCpa() {
    if (selected.length > 1000) { notify('单次最多下载 1000 个账号', 'error'); return }
    setBusy(true)
    try {
      const result = await api<Result>('/api/accounts/download-cpa-bulk', post({ account_ids: selected, prepare: true }))
      if (!result.download_url) throw new Error('服务端未返回下载地址')
      download(result.download_url, result.filename || '')
      notify(`已生成 ZIP，开始下载${result.error_count ? `（${result.error_count} 个失败见 manifest）` : ''}`)
    } catch (err) { notify(errorText(err), 'error') } finally { setBusy(false) }
  }
  function editNote(ids: number[], account?: Account) { setNote(account?.note || ''); setNoteTarget({ ids, single: Boolean(account), email: account?.email }) }
  const none = selected.length === 0 || busy
  const selectedRows = [...selectedAccounts.current.values()].filter((account) => selected.includes(account.id))
  const canLive = selectedRows.some((account) => account.live_check_capabilities?.can_start === true)
  const canPlan = selectedRows.some((account) => account.plan_capabilities?.can_start === true)
  const canRetry = selectedRows.some((account) => account.codex_capabilities?.can_retry === true)
  const canStop = selectedRows.some((account) => account.codex_capabilities?.can_stop === true)

  return <div className="flex min-w-0 max-w-full flex-col gap-4">
    <div><h1 className="text-xl font-semibold">账号管理</h1><p className="text-sm text-muted-foreground">密码默认脱敏，点击眼睛按需查看；Token 和 TOTP 密钥仅在明确操作时读取，状态每 5 秒更新。</p></div>
    {error && <Alert variant="destructive"><AlertTitle>账号加载失败</AlertTitle><AlertDescription>{error}</AlertDescription></Alert>}
    {notice && <Alert><AlertTitle>操作结果</AlertTitle><AlertDescription className="max-h-40 overflow-auto whitespace-pre-wrap break-words">{notice}</AlertDescription></Alert>}
    <Card className="min-w-0"><CardHeader><CardTitle>已注册账号</CardTitle><CardDescription>共 {total} 个 · 已选 {selected.length} 个；翻页保留选择，改变筛选清空选择。</CardDescription></CardHeader><CardContent className="flex min-w-0 flex-col gap-4">
      <TableToolbar search={search} onSearchChange={setSearch} searchLabel="搜索邮箱、用户名、备注" filters={<>
        <Choice label="邮箱来源" value={filters.email_source} options={{ '': '全部邮箱来源', ...sources }} onChange={(v) => changeFilter('email_source', v)} />
        <Choice label="Codex 授权状态" value={filters.codex_auth_status} options={{ '': '全部 Codex 授权', ...authLabels }} onChange={(v) => changeFilter('codex_auth_status', v)} />
        <div className="basis-full">
          <Button size="sm" variant="outline" aria-expanded={advancedFiltersOpen} aria-controls={advancedFiltersId} onClick={() => setAdvancedFiltersOpen((open) => !open)}>
            高级筛选{activeAdvancedFilterCount > 0 && `（已选 ${activeAdvancedFilterCount} 项）`}
          </Button>
        </div>
        <div id={advancedFiltersId} className={advancedFiltersOpen ? 'basis-full flex flex-wrap items-end gap-2' : 'hidden'}>
          <Choice label="Codex 操作状态" value={filters.codex_operation_status} options={{ '': '全部 Codex 操作', ...operationLabels }} onChange={(v) => changeFilter('codex_operation_status', v)} />
          <Choice label="查活状态" value={filters.live_check_status} options={{ '': '全部查活状态', ...liveLabels }} onChange={(v) => changeFilter('live_check_status', v)} />
          <Choice label="套餐筛选" value={filters.plan} options={{ '': '全部套餐', plus: '已开通套餐', plus_trial: 'Free 可试用套餐', promo: '有套餐优惠', free_no_trial: 'Free 不可试用套餐' }} onChange={(v) => changeFilter('plan', v)} />
          {filters.plan === 'promo' && <><Choice label="优惠套餐类型" value={filters.promoType} options={{ '': '全部优惠类型', plus: 'Plus', pro: 'Pro', go: 'Go', team: 'Team', business: 'Business' }} onChange={(v) => changeFilter('promoType', v)} /><Choice label="优惠力度" value={filters.promoDiscount} options={{ '': '全部优惠力度', '100': '100%优惠', '50': '50%优惠', other: '其他优惠' }} onChange={(v) => changeFilter('promoDiscount', v)} /></>}
          <FieldGroup className="flex flex-row flex-wrap items-end gap-2">
            <Field className="w-fit">
              <FieldLabel htmlFor="account-date-from">起始日期</FieldLabel>
              <Input id="account-date-from" type="date" aria-label="账号注册起始日期" className="w-36" value={filters.date_from} onChange={(e) => changeFilter('date_from', e.currentTarget.value)} />
            </Field>
            <Field className="w-fit">
              <FieldLabel htmlFor="account-date-to">结束日期</FieldLabel>
              <Input id="account-date-to" type="date" aria-label="账号注册结束日期" className="w-36" value={filters.date_to} onChange={(e) => changeFilter('date_to', e.currentTarget.value)} />
            </Field>
          </FieldGroup>
        </div>
      </>} actions={<><Button size="sm" variant="outline" disabled={loading} onClick={() => void loadAccounts()}><RefreshCw data-icon="inline-start" />刷新</Button><Button size="sm" variant="ghost" onClick={() => { setFilters(initialFilters); setSearch(''); setPage(1); setSelected([]); selectedAccounts.current.clear(); revision.current = undefined }}>清空筛选</Button><Button size="sm" variant="ghost" disabled={!selected.length} onClick={() => { setSelected([]); selectedAccounts.current.clear() }}>清空选择</Button></>} />
      <div className="space-y-2">
        <p className="text-sm font-medium">批量操作</p>
        <div className="grid w-full grid-cols-[repeat(auto-fit,minmax(min(100%,9rem),1fr))] justify-items-start gap-3" role="group" aria-label="账号批量操作">
          <Field orientation="horizontal" className="w-fit items-center gap-2"><FieldLabel htmlFor="account-workers">并发线程</FieldLabel><Input id="account-workers" type="number" min={1} max={16} value={workersInput} onChange={(e) => setWorkersInput(e.currentTarget.value)} onBlur={() => setWorkersInput(String(workers))} className="w-20" /></Field>
          <Button size="sm" variant="outline" disabled={none || !canLive} onClick={() => bulk('live')}>查活刷新 AT</Button>
          <Button size="sm" variant="outline" disabled={none || !canPlan} onClick={() => { if (selected.length > 500) { notify('单次最多查询 500 个账号', 'error'); return } void run('/api/accounts/check-plan-bulk', { account_ids: selected, workers }) }}>查套餐</Button>
          <Button size="sm" variant="outline" disabled={none || !canRetry} onClick={() => bulk('retry')}>补跑 Codex</Button><Button size="sm" variant="outline" disabled={none || !canStop} onClick={() => bulk('stop')}>停止补跑</Button>
          <Button size="sm" variant="outline" disabled={none} onClick={() => bulk('totp')}>开启 2FA</Button><Button size="sm" variant="outline" disabled={none} onClick={() => editNote([...selected])}>统一备注</Button><Button size="sm" variant="destructive" disabled={none} onClick={() => bulk('delete')}>删除选中</Button>
        </div>
      </div>
      <div className="space-y-2">
        <p className="text-sm font-medium">复制与导出</p>
        <div className="grid w-full grid-cols-[repeat(auto-fit,minmax(min(100%,9rem),1fr))] justify-items-start gap-3" role="group" aria-label="账号复制与导出">
          <Choice label="AT 导出格式" value={format} options={formats} onChange={setFormat} />
          <Button size="sm" variant="outline" disabled={none} onClick={() => void copyPlain(selected.map((id) => selectedAccounts.current.get(id)?.email).filter(Boolean).join('\n'), '已复制选中邮箱')}><Copy data-icon="inline-start" />复制邮箱</Button>
          <Button size="sm" variant="outline" disabled={none} onClick={() => void exportAccounts(selected, format)}>复制选中整行</Button><Button size="sm" variant="outline" disabled={none} onClick={() => void exportAccounts(selected, 'access_token')}>复制选中 AT</Button>
          <Button size="sm" variant="outline" disabled={busy || !accounts.length} onClick={() => void exportAccounts(visibleIds, format)}>复制本页</Button><Button size="sm" variant="outline" disabled={none} onClick={() => void exportAccounts(selected, format, true)}><Download data-icon="inline-start" />下载 TXT</Button>
          <ConfirmAction title="从 CPA 下载选中账号？" description={`按 ${selected.length} 个账号邮箱匹配 CPA auth-files 并生成 ZIP；本地 Codex 不为 success 也会尝试匹配，失败项目写入 manifest。`} disabled={none} onConfirm={() => void downloadCpa()}>下载选中 CPA</ConfirmAction>
        </div>
      </div>
      <p className="text-sm text-muted-foreground sm:hidden">可左右滑动表格，查看全部账号列。</p>
      <div role="region" aria-label="账号表格，可横向滚动查看全部列" tabIndex={0} className="min-w-0 max-w-full overflow-x-auto rounded-md border focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"><Table className="w-max min-w-full table-fixed"><TableHeader><TableRow>
        <TableHead className="w-10"><Checkbox aria-label="选择当前页全部账号" disabled={!accounts.length} checked={allSelected} indeterminate={!allSelected && visibleIds.some((id) => selected.includes(id))} onCheckedChange={(checked) => { const shouldSelect = checked === true; accounts.forEach((a) => { if (shouldSelect) selectedAccounts.current.set(a.id, a); else selectedAccounts.current.delete(a.id) }); setSelected((ids) => shouldSelect ? [...new Set([...ids, ...visibleIds])] : ids.filter((id) => !visibleIds.includes(id))) }} /></TableHead>
        <TableHead>ID</TableHead><TableHead className="w-[22rem]">邮箱</TableHead><TableHead className="w-28">AT</TableHead><TableHead>密码</TableHead><TableHead>套餐</TableHead><TableHead>Codex</TableHead><TableHead>2FA</TableHead><TableHead>备注 / 时间</TableHead><TableHead>操作</TableHead>
      </TableRow></TableHeader><TableBody>{!accounts.length ? <TableRow><TableCell colSpan={10} className="h-24 text-center text-muted-foreground">{loading ? '正在加载…' : '没有符合条件的账号'}</TableCell></TableRow> : accounts.map((a) => {
        const codexStatus = ['queued', 'running', 'canceled'].includes(a.codex_operation_status || '') ? a.codex_operation_status || 'unknown' : a.codex_auth_status || 'unknown'
        const codexLabel = ['queued', 'running', 'canceled'].includes(a.codex_operation_status || '') ? operationLabels[codexStatus] : authLabels[codexStatus]
        const setupBusy = ['queued', 'running'].includes(a.totp_setup_status || '')
        return <TableRow key={a.id} data-state={selected.includes(a.id) ? 'selected' : undefined}>
          <TableCell><Checkbox aria-label={`选择 ${a.email}`} checked={selected.includes(a.id)} onCheckedChange={(checked) => toggle(a, checked === true)} /></TableCell><TableCell className="font-mono text-xs">{a.id}</TableCell>
          <TableCell className="w-[22rem] max-w-[22rem]"><div className="w-full max-w-[22rem] flex flex-col items-start gap-1"><Button variant="link" size="sm" className="h-7 w-full justify-start px-0" title={a.email} onClick={() => void copyPlain(a.email)}><span className="truncate">{a.email}</span></Button><span className="w-full truncate text-xs text-muted-foreground">{sources[a.email_source || 'unknown'] || a.email_source || '未知'}</span></div></TableCell>
          <TableCell className="w-28"><div className="flex flex-col items-start gap-1"><Button size="sm" variant="outline" disabled={busy || !a.has_access_token} onClick={() => void copySecret(a, 'access_token')}>{a.has_access_token ? '复制 AT' : '无 AT'}</Button><span title={[a.live_check_error, a.live_checked_at, a.live_check_fingerprint_text].filter(Boolean).join('\n')}><StatusBadge status={a.live_check_status} label={liveLabels[a.live_check_status || 'unknown']} /></span>{(a.token_expired || a.needs_live_check) && <span className="text-xs text-muted-foreground" title={a.token_expires_at}>需查活刷新</span>}</div></TableCell>
          <TableCell><div className="flex items-center gap-1">{a.password ? <>{visiblePasswords.has(a.id) ? <Button type="button" size="sm" variant="link" className="h-7 w-[14ch] shrink-0 justify-start px-0 font-mono text-xs" aria-label="复制密码" onClick={() => void copyPlain(a.password!, '已复制密码')}><span className="truncate">{passwordPreview(a.password)}</span></Button> : <span className="w-[14ch] shrink-0 whitespace-nowrap font-mono text-xs text-foreground">{PASSWORD_MASK}</span>}<Button type="button" size="icon-lg" variant="ghost" className="min-h-11 min-w-11" aria-label={visiblePasswords.has(a.id) ? '隐藏密码' : '显示密码'} onClick={() => setVisiblePasswords((current) => { const next = new Set(current); if (next.has(a.id)) next.delete(a.id); else next.add(a.id); return next })}>{visiblePasswords.has(a.id) ? <EyeOff aria-hidden="true" /> : <Eye aria-hidden="true" />}</Button></> : <span className="text-xs text-muted-foreground">未设置</span>}</div></TableCell>
          <TableCell><PlanCell account={a} onDetails={() => setPromo(a)} /></TableCell>
          <TableCell><span title={a.codex_error}><StatusBadge status={codexStatus} label={codexLabel || '未知'} /></span></TableCell>
          <TableCell><div className="flex flex-col items-start gap-1" title={[a.totp_setup_message, a.totp_setup_error, a.twofa_stage, a.twofa_error_code].filter(Boolean).join('\n')}>
            {a.totp_enabled ? <Button size="sm" variant="outline" disabled={busy} onClick={() => void getTotp(a)}>生成验证码</Button> : setupBusy ? <StatusBadge status={a.totp_setup_status} label={a.totp_setup_status === 'queued' ? '排队中' : '开启中'} /> : a.twofa_status === 'already_enabled' ? <span className="text-xs" title="远端已启用 TOTP，但本地未保存 Secret">远端已存在</span> : <Button size="sm" variant="outline" disabled={busy || !a.has_access_token} onClick={() => confirm({ title: `为 ${a.email} 开启 2FA？`, description: '成功后 TOTP 密钥会写回账号信息。', url: `/api/accounts/${a.id}/totp-setup`, body: {}, log: { kind: 'totp', email: a.email } })}>{a.has_access_token ? '开启 2FA' : '无 Token'}</Button>}
            {!a.totp_enabled && ['failed', 'failed_transient', 'failed_permanent'].includes(a.twofa_status || a.totp_setup_status || '') && <StatusBadge status="failed" label="设置失败" />}
          </div></TableCell>
          <TableCell><div className="flex max-w-48 flex-col gap-1"><span className="truncate text-xs" title={a.note}>{a.note || '—'}</span><span className="text-xs text-muted-foreground">{a.created_at || '—'}</span></div></TableCell>
          <TableCell><DropdownMenu><DropdownMenuTrigger render={<Button size="icon-sm" variant="ghost" aria-label={`${a.email} 的操作`} />}><MoreHorizontal /></DropdownMenuTrigger><DropdownMenuContent align="end"><DropdownMenuGroup>
            <DropdownMenuItem disabled={busy} onClick={() => void copySecret(a, format)}>复制整行（当前格式）</DropdownMenuItem><DropdownMenuItem disabled={busy} onClick={() => void copySecret(a, 'login_credentials')}>复制邮箱 / 密码 / 2FA</DropdownMenuItem><DropdownMenuItem disabled={busy} onClick={() => editNote([a.id], a)}>设置备注</DropdownMenuItem>
          </DropdownMenuGroup><DropdownMenuSeparator /><DropdownMenuGroup>
            <DropdownMenuItem disabled={busy || a.live_check_capabilities?.can_start !== true} onClick={() => confirm({ title: `查活 ${a.email}？`, description: '将重新登录邮箱 OTP 并刷新最新 AT。', url: '/api/accounts/check-live-bulk', body: { account_ids: [a.id], workers }, log: { kind: 'live', email: a.email } })}>查活刷新 AT</DropdownMenuItem><DropdownMenuItem onClick={() => setLogTarget({ kind: 'live', email: a.email })}>查活日志</DropdownMenuItem>
            <DropdownMenuItem disabled={busy || a.plan_capabilities?.can_start !== true} onClick={() => void run('/api/accounts/check-plan', { account_id: a.id })}>{a.plan_capabilities?.is_checking ? '套餐查询中…' : '查询套餐'}</DropdownMenuItem>
            {a.codex_capabilities?.can_retry === true && <DropdownMenuItem disabled={busy} onClick={() => confirm({ title: `补跑 ${a.email} 的 Codex 授权？`, description: '消耗 1 封邮箱 OTP 和 1 个接码短信（约 $0.13），后台完成后自动更新。', url: '/api/codex/retry', body: { email: a.email }, log: { kind: 'codex', email: a.email } })}>{a.codex_auth_status === 'success' ? '重新补跑 Codex' : '补跑 Codex'}</DropdownMenuItem>}
            {a.codex_capabilities?.can_stop === true && <><DropdownMenuItem disabled={busy} onClick={() => confirm({ title: `停止 ${a.email} 的补跑？`, description: '发送停止信号，并将状态标记为已停止。', url: '/api/codex/stop', body: { email: a.email } })}>停止 Codex 补跑</DropdownMenuItem><DropdownMenuItem disabled={busy} onClick={() => confirm({ title: `重置 ${a.email} 的补跑状态？`, description: '仅将状态重置为失败，不会向运行线程发送停止信号；之后可再次补跑。', url: '/api/codex/reset-retrying', body: { email: a.email, status: 'failed' } })}>重置补跑状态</DropdownMenuItem></>}
            <DropdownMenuItem onClick={() => setLogTarget({ kind: 'codex', email: a.email })}>Codex 补跑日志</DropdownMenuItem><DropdownMenuItem onClick={() => setLogTarget({ kind: 'totp', email: a.email })}>2FA 设置日志</DropdownMenuItem>
          </DropdownMenuGroup><DropdownMenuSeparator /><DropdownMenuGroup>
            <DropdownMenuItem variant="destructive" disabled={busy} onClick={() => confirm({ title: `删除 ${a.email}？`, description: '将从本地账号列表、注册成功邮箱和 token 文件中移除，无法撤销。', url: `/api/accounts/${a.id}/delete`, body: {}, remove: [a.id] })}>删除账号</DropdownMenuItem>
          </DropdownMenuGroup></DropdownMenuContent></DropdownMenu></TableCell>
        </TableRow>
      })}</TableBody></Table></div>
      <div className="flex flex-wrap items-center justify-between gap-2"><span className="text-sm text-muted-foreground">第 {total ? (page - 1) * pageSize + 1 : 0}–{Math.min(page * pageSize, total)} 条 / 共 {total} 条 · 第 {page} / {pageCount} 页</span><div className="flex flex-wrap items-center gap-2"><Choice label="每页账号数" value={String(pageSize)} options={{ '20': '20 条/页', '50': '50 条/页', '100': '100 条/页' }} onChange={(v) => { setPageSize(Number(v)); setPage(1) }} /><Button size="sm" variant="outline" disabled={page <= 1 || loading} onClick={() => setPage((p) => p - 1)}>上一页</Button><Button size="sm" variant="outline" disabled={page >= pageCount || loading} onClick={() => setPage((p) => p + 1)}>下一页</Button></div></div>
    </CardContent></Card>
    <Dialog open={Boolean(command)} onOpenChange={(open) => { if (!open) setCommand(null) }}><DialogContent><DialogHeader><DialogTitle>{command?.title || '确认操作'}</DialogTitle><DialogDescription>{command?.description}</DialogDescription></DialogHeader><DialogFooter><Button variant="outline" onClick={() => setCommand(null)}>取消</Button><Button variant="destructive" disabled={busy} onClick={() => { if (command) { const action = command; setCommand(null); void run(action.url, action.body, action) } }}>确认执行</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={Boolean(noteTarget)} onOpenChange={(open) => { if (!open) setNoteTarget(null) }}><DialogContent><DialogHeader><DialogTitle>{noteTarget?.single ? '设置账号备注' : `统一备注（${noteTarget?.ids.length || 0} 个账号）`}</DialogTitle><DialogDescription>{noteTarget?.email || '所选账号使用相同备注'}；留空清空备注，最多 2000 字符。</DialogDescription></DialogHeader><Input aria-label="账号备注" value={note} maxLength={2000} onChange={(e) => setNote(e.currentTarget.value)} /><DialogFooter><Button variant="outline" onClick={() => setNoteTarget(null)}>取消</Button><Button disabled={busy} onClick={() => { if (noteTarget) { const target = noteTarget; void run(target.single ? `/api/accounts/${target.ids[0]}/note` : '/api/accounts/note-bulk', target.single ? { note } : { account_ids: target.ids, note }).then((saved) => { if (saved) setNoteTarget(null) }) } }}>保存备注</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={Boolean(logTarget)} onOpenChange={(open) => { if (!open) setLogTarget(null) }}><DialogContent className="max-w-[calc(100vw-2rem)] sm:max-w-4xl"><DialogHeader><DialogTitle>{logTarget?.kind === 'codex' ? 'Codex 补跑日志' : logTarget?.kind === 'live' ? '查活日志' : '2FA 设置日志'}</DialogTitle><DialogDescription className="break-all">{logTarget?.email} · {logRunning ? '运行中，自动刷新' : '已结束 / 暂无运行任务'}</DialogDescription></DialogHeader><pre ref={logContent} className="max-h-[65vh] min-w-0 overflow-auto whitespace-pre-wrap break-all rounded-md bg-muted p-3 font-mono text-xs">{log}</pre><DialogFooter><Button variant="outline" onClick={() => { if (logTarget) setLogTarget({ ...logTarget }) }}>刷新日志</Button><Button variant="outline" onClick={() => void copyPlain(log)}>复制日志</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={Boolean(totp)} onOpenChange={(open) => { if (!open) setTotp(null) }}><DialogContent><DialogHeader><DialogTitle>当前 TOTP 验证码</DialogTitle><DialogDescription className="break-all">{totp?.email} · {totp && totp.expires > now ? `剩余 ${Math.ceil((totp.expires - now) / 1000)} 秒` : '验证码已过期，请重新生成'}</DialogDescription></DialogHeader><p className="font-mono text-3xl tracking-widest" aria-live="polite">{totp && totp.expires > now ? totp.code : '------'}</p><DialogFooter><Button variant="outline" disabled={busy} onClick={() => { if (totp) void getTotp({ id: totp.id, email: totp.email }) }}>重新生成</Button><Button disabled={!totp || totp.expires <= now} onClick={() => void copyPlain(totp?.code || '')}>复制验证码</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={Boolean(promo)} onOpenChange={(open) => { if (!open) setPromo(null) }}><DialogContent className="max-w-[calc(100vw-2rem)] sm:max-w-2xl"><DialogHeader><DialogTitle>套餐优惠详情</DialogTitle><DialogDescription className="break-all">{promo?.email} · eligible_promo_campaigns</DialogDescription></DialogHeader><pre className="max-h-[65vh] overflow-auto whitespace-pre-wrap break-all rounded-md bg-muted p-3 text-xs">{JSON.stringify(promo?.eligible_promo_campaigns || {}, null, 2)}</pre></DialogContent></Dialog>
  </div>
}
