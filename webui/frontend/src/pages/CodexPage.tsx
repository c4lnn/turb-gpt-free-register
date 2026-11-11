import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Download,
  FileText,
  RefreshCw,
  RotateCcw,
  Square,
  Trash2,
} from 'lucide-react'
import { ConfirmAction } from '@/components/app/confirm-action'
import { StatusBadge } from '@/components/app/status-badge'
import { TableToolbar } from '@/components/app/table-toolbar'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Checkbox } from '@/components/ui/checkbox'
import { Field, FieldGroup, FieldLabel } from '@/components/ui/field'
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { api } from '@/lib/api'
import { toast } from '@/components/ui/toast'

type CodexCredential = {
  filename: string
  email?: string
  plan?: string
  status?: string
  account_id?: string | number
  mtime?: string
  expired?: string
  exported_count?: number
  exported_at?: string
}
type CodexResponse = {
  accounts?: CodexCredential[]
  total?: number
  summary?: { total?: number; exported?: number; pending?: number }
}
type AccountRow = {
  id: number
  email: string
  codex_status?: string
  codex_error?: string
  live_check_status?: string
  codex_operation_status?: string
}
type SelectedCredential = { filename: string; email: string }
type DownloadResult = { blob: Blob; filename: string }
type DownloadError = { error?: string; errors?: Array<{ filename?: string; error?: string }> }

const pageSizes = [10, 20, 30, 50, 100]
const pageSizeItems = pageSizes.map((size) => ({ value: String(size), label: `${size} 条/页` }))
const exportStatuses = [
  { value: 'all', label: '全部' },
  { value: 'pending', label: '未导出' },
  { value: 'exported', label: '已导出' },
]
const accountStatusLabels: Record<string, string> = {
  success: '成功',
  failed: '失败',
  skipped: '已跳过',
  not_started: '未开始',
  retrying: '补跑中',
  queued: '排队中',
  unknown: '未知',
}

function post(body: unknown): RequestInit {
  return {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }
}

function errorText(error: unknown) {
  return error instanceof Error ? error.message : '请求失败'
}

function filenameFromDisposition(value: string, fallback: string) {
  const extended = value.match(/filename\*=UTF-8''([^;]+)/i)?.[1]
  const plain = value.match(/filename="?([^";]+)"?/i)?.[1]
  const encoded = extended ?? plain
  if (!encoded) return fallback
  try {
    return decodeURIComponent(encoded)
  } catch {
    return encoded
  }
}

async function downloadResponse(url: string, fallback: string, init?: RequestInit): Promise<DownloadResult> {
  const response = await fetch(url, { ...init, credentials: 'same-origin' })
  if (!response.ok) {
    const payload = await response.json().catch(() => ({})) as DownloadError
    const details = payload.errors?.slice(0, 3).map((item) => `${item.filename ?? ''}${item.filename ? '：' : ''}${item.error ?? ''}`).filter(Boolean).join('；')
    throw new Error(`${payload.error || `HTTP ${response.status}`}${details ? `；${details}` : ''}`)
  }
  return {
    blob: await response.blob(),
    filename: filenameFromDisposition(response.headers.get('Content-Disposition') ?? '', fallback),
  }
}

function saveDownload({ blob, filename }: DownloadResult) {
  const objectUrl = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = objectUrl
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000)
}

function keyForCredential(filename: string) {
  return filename
}

function Pager({
  page,
  pageSize,
  total,
  onPageChange,
  onPageSizeChange,
}: {
  page: number
  pageSize: number
  total: number
  onPageChange: (page: number) => void
  onPageSizeChange: (size: number) => void
}) {
  const totalPages = Math.max(1, Math.ceil(total / pageSize))
  const from = total ? (page - 1) * pageSize + 1 : 0
  const to = Math.min(page * pageSize, total)
  const first = Math.max(1, Math.min(page - 2, totalPages - 4))
  const pages = Array.from({ length: Math.min(5, totalPages) }, (_, index) => first + index)

  return (
    <div className="flex flex-col gap-3 border-t px-4 py-3 sm:flex-row sm:flex-wrap sm:items-center sm:justify-between">
      <p className="text-sm text-muted-foreground" aria-live="polite">
        {total ? `第 ${from}–${to} 条 / 共 ${total} 条（第 ${page} / ${totalPages} 页）` : '无数据'}
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="outline" size="sm" disabled={page <= 1} onClick={() => onPageChange(page - 1)}>上一页</Button>
        {pages.map((value) => (
          <Button
            key={value}
            variant={page === value ? 'secondary' : 'outline'}
            size="icon-sm"
            aria-current={page === value ? 'page' : undefined}
            onClick={() => onPageChange(value)}
          >
            {value}
          </Button>
        ))}
        <Button variant="outline" size="sm" disabled={page >= totalPages} onClick={() => onPageChange(page + 1)}>下一页</Button>
        <Select items={pageSizeItems} value={String(pageSize)} onValueChange={(value) => onPageSizeChange(Number(value))}>
          <SelectTrigger size="sm" className="w-28">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectGroup>
              {pageSizeItems.map((item) => <SelectItem key={item.value} value={item.value}>{item.label}</SelectItem>)}
            </SelectGroup>
          </SelectContent>
        </Select>
      </div>
    </div>
  )
}

export default function CodexPage() {
  const [rows, setRows] = useState<CodexCredential[]>([])
  const [total, setTotal] = useState(0)
  const [summary, setSummary] = useState({ total: 0, exported: 0, pending: 0 })
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [search, setSearch] = useState('')
  const [query, setQuery] = useState('')
  const [exportStatus, setExportStatus] = useState('all')
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [selected, setSelected] = useState<Record<string, SelectedCredential>>({})
  const [accounts, setAccounts] = useState<Record<string, AccountRow>>({})
  const [loading, setLoading] = useState(true)
  const [accountLoadingError, setAccountLoadingError] = useState('')
  const [pageError, setPageError] = useState('')
  const [busy, setBusy] = useState(false)
  const [retryWorkers, setRetryWorkers] = useState('3')
  const [refreshVersion, setRefreshVersion] = useState(0)
  const [logEmail, setLogEmail] = useState<string | null>(null)
  const [logText, setLogText] = useState('')
  const [logError, setLogError] = useState('')
  const [logRunning, setLogRunning] = useState(false)
  const logContentRef = useRef<HTMLPreElement>(null)
  const requestId = useRef(0)

  useEffect(() => {
    const timer = window.setTimeout(() => setQuery(search.trim()), 250)
    return () => window.clearTimeout(timer)
  }, [search])

  useEffect(() => {
    const controller = new AbortController()
    const currentRequest = ++requestId.current
    setLoading(true)
    setPageError('')
    setAccountLoadingError('')
    const params = new URLSearchParams({
      paged: '1',
      page: String(page),
      page_size: String(pageSize),
      q: query,
      export_status: exportStatus,
      date_from: dateFrom,
      date_to: dateTo,
    })
    void api<CodexResponse>(`/api/codex?${params.toString()}`, { signal: controller.signal })
      .then((result) => {
        if (currentRequest !== requestId.current) return
        const nextTotal = Number(result.total ?? 0)
        const maxPage = Math.max(1, Math.ceil(nextTotal / pageSize))
        setTotal(nextTotal)
        setSummary({
          total: Number(result.summary?.total ?? 0),
          exported: Number(result.summary?.exported ?? 0),
          pending: Number(result.summary?.pending ?? 0),
        })
        if (page > maxPage) {
          setPage(maxPage)
          return
        }
        setRows(result.accounts ?? [])
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted && currentRequest === requestId.current) setPageError(errorText(error))
      })
      .finally(() => {
        if (currentRequest === requestId.current) setLoading(false)
      })

    void api<AccountRow[]>('/api/accounts?limit=5000', { signal: controller.signal })
      .then((result) => {
        if (currentRequest !== requestId.current) return
        const next: Record<string, AccountRow> = {}
        result.forEach((account) => { next[account.email.toLowerCase()] = account })
        setAccounts(next)
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted && currentRequest === requestId.current) {
          setAccounts({})
          setAccountLoadingError(errorText(error))
        }
      })
    return () => controller.abort()
  }, [dateFrom, dateTo, exportStatus, page, pageSize, query, refreshVersion])

  useEffect(() => {
    if (!logEmail) return
    let active = true
    let requestInFlight = false
    const refreshLog = async () => {
      if (!active || requestInFlight) return
      requestInFlight = true
      try {
        const result = await api<{ log?: string; running?: boolean }>(`/api/codex/retry-log?email=${encodeURIComponent(logEmail)}`)
        if (!active) return
        const content = logContentRef.current
        const wasAtBottom = !content || content.scrollTop + content.clientHeight >= content.scrollHeight - 30
        setLogText(result.log || '(暂无日志，等待任务写入…)')
        setLogRunning(Boolean(result.running))
        setLogError('')
        if (wasAtBottom) window.requestAnimationFrame(() => {
          if (logContentRef.current) logContentRef.current.scrollTop = logContentRef.current.scrollHeight
        })
      } catch (error) {
        if (active) setLogError(errorText(error))
      } finally {
        requestInFlight = false
      }
    }
    setLogText('加载中…')
    setLogError('')
    void refreshLog()
    const timer = window.setInterval(() => void refreshLog(), 2000)
    return () => {
      active = false
      window.clearInterval(timer)
    }
  }, [logEmail])

  const selectedItems = useMemo(() => Object.values(selected), [selected])
  const pageKeys = rows.map((row) => keyForCredential(row.filename))
  const checkedOnPage = pageKeys.filter((filename) => Boolean(selected[filename])).length
  const allChecked = pageKeys.length > 0 && checkedOnPage === pageKeys.length
  const someChecked = checkedOnPage > 0 && !allChecked
  const selectedAccounts = selectedItems
    .map((item) => accounts[item.email.toLowerCase()])
    .filter((account): account is AccountRow => Boolean(account))
  const uniqueSelectedAccounts = Array.from(new Map(selectedAccounts.map((account) => [account.id, account])).values())
  const retryableSelected = uniqueSelectedAccounts.filter((account) => !['queued', 'running', 'retrying'].includes(account.codex_operation_status ?? ''))
  const stoppableSelected = uniqueSelectedAccounts.filter((account) => ['queued', 'running', 'retrying'].includes(account.codex_operation_status ?? ''))

  function refresh() {
    setRefreshVersion((value) => value + 1)
  }

  function togglePageSelection(checked: boolean) {
    setSelected((current) => {
      const next = { ...current }
      rows.forEach((row) => {
        const key = keyForCredential(row.filename)
        if (checked) next[key] = { filename: row.filename, email: row.email ?? '' }
        else delete next[key]
      })
      return next
    })
  }

  function toggleRowSelection(row: CodexCredential, checked: boolean) {
    const key = keyForCredential(row.filename)
    setSelected((current) => {
      const next = { ...current }
      if (checked) next[key] = { filename: row.filename, email: row.email ?? '' }
      else delete next[key]
      return next
    })
  }

  async function downloadOne(filename: string, fromCpa: boolean) {
    setBusy(true)
    try {
      const route = fromCpa ? 'download-from-cpa' : 'download'
      const fallback = fromCpa ? filename.replace(/-cpa-callback\.json$/i, '-free.json') : filename
      const result = await downloadResponse(`/api/codex/${route}/${encodeURIComponent(filename)}`, fallback)
      saveDownload(result)
      toast.add({ title: fromCpa ? '已从 CPA 下载' : '本地凭证下载成功', type: 'success' })
      refresh()
    } catch (error) {
      toast.add({ title: `${fromCpa ? '从 CPA 下载' : '本地下载'}失败：${errorText(error)}`, type: 'error' })
    } finally {
      setBusy(false)
    }
  }

  async function downloadSelectedLocal() {
    if (!selectedItems.length) return
    setBusy(true)
    try {
      const result = await downloadResponse('/api/codex/download-bulk', `codex-bulk-${Date.now()}.json`, post({ filenames: selectedItems.map((item) => item.filename) }))
      let message = `已打包下载 ${selectedItems.length} 个凭证`
      let warning = false
      try {
        const bundle = JSON.parse(await result.blob.text()) as { count?: number; errors?: Array<{ filename?: string; error?: string }> }
        if (bundle.errors?.length) {
          warning = true
          message = `已打包 ${bundle.count ?? 0} 个，${bundle.errors.length} 个失败：${bundle.errors.slice(0, 3).map((item) => `${item.filename ?? ''}${item.filename ? '：' : ''}${item.error ?? ''}`).join('；')}`
        } else if (typeof bundle.count === 'number') {
          message = `已打包下载 ${bundle.count} 个凭证`
        }
      } catch {
        // Preserve the server's attachment even if its JSON metadata cannot be parsed locally.
      }
      saveDownload(result)
      toast.add({ title: message, type: warning ? 'warning' : 'success' })
      setSelected({})
      refresh()
    } catch (error) {
      toast.add({ title: `批量下载失败：${errorText(error)}`, type: 'error' })
    } finally {
      setBusy(false)
    }
  }

  async function downloadSelectedCpa() {
    if (!selectedItems.length) return
    setBusy(true)
    try {
      const result = await downloadResponse('/api/codex/download-bulk-from-cpa', `codex-cpa-bulk-${Date.now()}.zip`, post({ filenames: selectedItems.map((item) => item.filename) }))
      saveDownload(result)
      toast.add({ title: `已从 CPA 打包下载 ${selectedItems.length} 个凭证（详情见 manifest.json）`, type: 'success' })
      setSelected({})
      refresh()
    } catch (error) {
      toast.add({ title: `从 CPA 批量下载失败：${errorText(error)}`, type: 'error' })
    } finally {
      setBusy(false)
    }
  }


  async function deleteOne(row: CodexCredential) {
    setBusy(true)
    try {
      const result = await api<{ ok?: boolean }>('/api/codex/delete', post({ filename: row.filename }))
      if (result.ok === false) throw new Error('删除操作未成功')
      setSelected((current) => {
        const next = { ...current }
        delete next[row.filename]
        return next
      })
      toast.add({ title: 'Codex 凭证已删除', type: 'success' })
      refresh()
    } catch (error) {
      toast.add({ title: `删除失败：${errorText(error)}`, type: 'error' })
    } finally {
      setBusy(false)
    }
  }

  async function deleteSelected() {
    if (!selectedItems.length) return
    setBusy(true)
    try {
      const result = await api<{ deleted?: string[]; deleted_count?: number; skipped?: unknown[] }>('/api/codex/delete-bulk', post({
        filenames: selectedItems.map((item) => item.filename),
      }))
      const deleted = new Set(result.deleted ?? [])
      setSelected((current) => Object.fromEntries(Object.entries(current).filter(([filename]) => !deleted.has(filename))))
      const skipped = result.skipped?.length ?? 0
      toast.add({
        title: skipped ? `已删除 ${result.deleted_count ?? 0} 个，跳过 ${skipped} 个` : `已删除 ${result.deleted_count ?? 0} 个`,
        type: skipped ? 'warning' : 'success',
      })
      refresh()
    } catch (error) {
      toast.add({ title: `批量删除失败：${errorText(error)}`, type: 'error' })
    } finally {
      setBusy(false)
    }
  }

  async function resetExport(filename: string) {
    setBusy(true)
    try {
      const result = await api<{ ok?: boolean }>('/api/codex/reset-export', post({ filename }))
      if (result.ok === false) throw new Error('导出计数重置未成功')
      toast.add({ title: '导出计数已重置', type: 'success' })
      refresh()
    } catch (error) {
      toast.add({ title: `导出计数重置失败：${errorText(error)}`, type: 'error' })
    } finally {
      setBusy(false)
    }
  }

  async function actOnAccount(email: string, action: 'retry' | 'stop' | 'reset-retrying') {
    const account = accounts[email.toLowerCase()]
    if (!account) {
      toast.add({ title: '未找到关联账号，请刷新后重试', type: 'warning' })
      return
    }
    const actionLabel = action === 'retry' ? '补跑' : action === 'stop' ? '停止' : '重置'
    setBusy(true)
    try {
      const result = await api<{ ok?: boolean; message?: string; error?: string; status?: string }>(`/api/codex/${action}`, post({
        email,
        ...(action === 'reset-retrying' ? { status: 'failed' } : {}),
      }))
      if (result.ok === false) throw new Error(result.error || `${actionLabel}操作未成功`)
      toast.add({ title: result.message || `Codex ${actionLabel}请求已完成`, type: 'success' })
      refresh()
      if (action === 'retry') setLogEmail(email)
    } catch (error) {
      toast.add({ title: `Codex ${actionLabel}失败：${errorText(error)}`, type: 'error' })
    } finally {
      setBusy(false)
    }
  }

  async function retrySelected() {
    const ids = retryableSelected.map((account) => account.id)
    if (!ids.length) return
    const workers = Math.max(1, Math.min(16, Number.parseInt(retryWorkers, 10) || 3))
    setRetryWorkers(String(workers))
    setBusy(true)
    try {
      const result = await api<{ started_count?: number; skipped?: Array<{ email?: string; reason?: string }>; message?: string }>('/api/codex/retry-bulk', post({
        account_ids: ids,
        workers,
      }))
      const skipped = result.skipped ?? []
      const details = skipped.slice(0, 3).map((item) => `${item.email ?? ''}${item.email ? '：' : ''}${item.reason ?? ''}`).join('；')
      toast.add({
        title: `${result.message || `已开始补跑 ${result.started_count ?? 0} 个账号`}${details ? `；跳过 ${skipped.length} 个：${details}` : ''}`,
        type: skipped.length ? 'warning' : 'success',
      })
      refresh()
    } catch (error) {
      toast.add({ title: `批量补跑失败：${errorText(error)}`, type: 'error' })
    } finally {
      setBusy(false)
    }
  }

  async function stopSelected() {
    const ids = stoppableSelected.map((account) => account.id)
    if (!ids.length) return
    setBusy(true)
    try {
      const result = await api<{ stopped_count?: number; skipped?: Array<{ email?: string; reason?: string }> }>('/api/codex/stop-bulk', post({ account_ids: ids }))
      const skipped = result.skipped ?? []
      const details = skipped.slice(0, 3).map((item) => `${item.email ?? ''}${item.email ? '：' : ''}${item.reason ?? ''}`).join('；')
      toast.add({
        title: `已发送停止信号 ${result.stopped_count ?? 0} 个${skipped.length ? `，跳过 ${skipped.length} 个${details ? `：${details}` : ''}` : ''}`,
        type: skipped.length ? 'warning' : 'success',
      })
      refresh()
    } catch (error) {
      toast.add({ title: `批量停止失败：${errorText(error)}`, type: 'error' })
    } finally {
      setBusy(false)
    }
  }

  function chooseExportStatus(value: string) {
    setExportStatus(value)
    setPage(1)
    setSelected({})
  }


  return (
    <div className="flex min-w-0 flex-col gap-4">
      {pageError && (
        <Alert variant="destructive">
          <AlertTitle>Codex 凭证加载失败</AlertTitle>
          <AlertDescription>{pageError}</AlertDescription>
        </Alert>
      )}
      {accountLoadingError && (
        <Alert variant="destructive">
          <AlertTitle>账号补跑状态暂不可用</AlertTitle>
          <AlertDescription>{accountLoadingError}。凭证的下载和删除仍可使用；重试与停止操作需要成功读取账号状态。</AlertDescription>
        </Alert>
      )}
      <section aria-label="Codex 导出统计" className="grid gap-3 sm:grid-cols-3">
        <Card size="sm" className="gap-0">
          <CardHeader className="pb-2"><CardDescription>凭证总数</CardDescription></CardHeader>
          <CardContent><CardTitle className="text-2xl tabular-nums">{summary.total}</CardTitle></CardContent>
        </Card>
        <Card size="sm" className="gap-0">
          <CardHeader className="pb-2"><CardDescription>已导出</CardDescription></CardHeader>
          <CardContent><CardTitle className="text-2xl tabular-nums">{summary.exported}</CardTitle></CardContent>
        </Card>
        <Card size="sm" className="gap-0">
          <CardHeader className="pb-2"><CardDescription>未导出</CardDescription></CardHeader>
          <CardContent><CardTitle className="text-2xl tabular-nums">{summary.pending}</CardTitle></CardContent>
        </Card>
      </section>
      <Card className="min-w-0">
        <CardHeader className="gap-3">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <CardTitle>Codex 授权</CardTitle>
              <CardDescription className="mt-1">管理本地 Codex 凭证、导出记录和账号补跑。</CardDescription>
            </div>
            <Button variant="outline" size="sm" onClick={refresh} disabled={loading || busy}>
              <RefreshCw data-icon="inline-start" aria-hidden="true" />刷新
            </Button>
          </div>
          <TableToolbar
            search={search}
            onSearchChange={(value) => { setSearch(value); setPage(1); setSelected({}) }}
            searchLabel="搜索邮箱、plan、account_id"
            filters={(
              <>
                <ToggleGroup aria-label="导出状态筛选" value={[exportStatus]} onValueChange={(values) => { if (values[0]) chooseExportStatus(values[0]) }} variant="outline" size="sm" className="flex-wrap">
                  {exportStatuses.map((option) => <ToggleGroupItem key={option.value} value={option.value}>{option.label}</ToggleGroupItem>)}
                </ToggleGroup>
                <FieldGroup className="flex flex-row flex-wrap items-end gap-2 sm:min-w-max sm:flex-nowrap">
                  <Field className="w-fit">
                    <FieldLabel htmlFor="codex-date-from">修改起始</FieldLabel>
                    <Input id="codex-date-from" type="date" className="w-36" value={dateFrom} onChange={(event) => { setDateFrom(event.currentTarget.value); setPage(1) }} />
                  </Field>
                  <Field className="w-fit">
                    <FieldLabel htmlFor="codex-date-to">修改结束</FieldLabel>
                    <Input id="codex-date-to" type="date" className="w-36" value={dateTo} onChange={(event) => { setDateTo(event.currentTarget.value); setPage(1) }} />
                  </Field>
                </FieldGroup>
              </>
            )}
            actions={(
              <>
                <ConfirmAction
                  title="批量下载本地凭证"
                  description={`选中 ${selectedItems.length} 个凭证，将打包为一个 JSON 文件下载。该聚合格式用于备份/迁移，不能直接作为 CPA auth-dir 文件使用。继续？`}
                  confirmLabel="继续"
                  disabled={!selectedItems.length || busy}
                  onConfirm={() => void downloadSelectedLocal()}
                >
                  <Download data-icon="inline-start" aria-hidden="true" />下载本地
                </ConfirmAction>
                <ConfirmAction
                  title="从 CPA 批量下载"
                  description={`选中 ${selectedItems.length} 个凭证，将按邮箱从 CPA auth-files 下载真实 Codex JSON 并打包为 ZIP。详情（包含未匹配项目）见 ZIP 内 manifest.json。继续？`}
                  confirmLabel="继续"
                  disabled={!selectedItems.length || busy}
                  onConfirm={() => void downloadSelectedCpa()}
                >
                  <Download data-icon="inline-start" aria-hidden="true" />从 CPA 下载
                </ConfirmAction>
                <ConfirmAction
                  title="永久删除 Codex 凭证"
                  description={`确定删除选中的 ${selectedItems.length} 个本地 Codex JSON 凭证吗？关联导出标记也会清理，此操作不可撤销。`}
                  triggerVariant="destructive"
                  disabled={!selectedItems.length || busy}
                  onConfirm={() => void deleteSelected()}
                >
                  <Trash2 data-icon="inline-start" aria-hidden="true" />删除选中
                </ConfirmAction>
                <Field orientation="horizontal" className="w-fit items-center gap-2">
                  <FieldLabel htmlFor="codex-retry-workers">补跑并发</FieldLabel>
                  <Input id="codex-retry-workers" type="number" min={1} max={16} value={retryWorkers} onChange={(event) => setRetryWorkers(event.currentTarget.value)} className="w-20" aria-label="补跑并发数" />
                </Field>
                <ConfirmAction
                  title="批量补跑 Codex"
                  description={`为选中凭证关联的 ${retryableSelected.length} 个账号启动 Codex 补跑。操作会消耗邮箱验证码与接码资源；其余当前不可补跑的账号将由服务端跳过。`}
                  confirmLabel="开始补跑"
                  disabled={!retryableSelected.length || busy}
                  onConfirm={() => void retrySelected()}
                >补跑选中</ConfirmAction>
                <ConfirmAction
                  title="停止 Codex 补跑"
                  description={`向选中凭证关联的 ${stoppableSelected.length} 个正在补跑账号发送停止信号吗？`}
                  confirmLabel="停止补跑"
                  disabled={!stoppableSelected.length || busy}
                  onConfirm={() => void stopSelected()}
                >停止选中</ConfirmAction>
                {selectedItems.length > 0 && <span className="text-sm text-muted-foreground">已选 {selectedItems.length}</span>}
              </>
            )}
          />
        </CardHeader>
        <CardContent className="min-w-0 p-0">
          <p className="px-4 text-sm text-muted-foreground sm:hidden">可左右滑动表格，查看全部 Codex 列。</p>
          <div role="region" aria-label="Codex 凭证表格，可横向滚动查看全部列" tabIndex={0} className="max-w-full overflow-x-auto focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring">
            <Table className="min-w-[1120px]">
              <TableHeader>
                <TableRow>
                  <TableHead className="w-10">
                    <Checkbox
                      aria-label="全选或取消全选当前页"
                      checked={allChecked}
                      indeterminate={someChecked}
                      disabled={!rows.length || loading}
                      onCheckedChange={(checked) => togglePageSelection(checked === true)}
                    />
                  </TableHead>
                  <TableHead>邮箱 / 文件</TableHead>
                  <TableHead>Plan</TableHead>
                  <TableHead>状态 / 导出</TableHead>
                  <TableHead>Account ID</TableHead>
                  <TableHead>更新时间</TableHead>
                  <TableHead>过期时间</TableHead>
                  <TableHead className="text-right">操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {loading && !rows.length ? (
                  <TableRow><TableCell colSpan={8} className="h-24 text-center text-muted-foreground">正在加载 Codex 凭证…</TableCell></TableRow>
                ) : rows.length ? rows.map((row) => {
                  const account = accounts[(row.email ?? '').toLowerCase()]
                  const exported = Number(row.exported_count ?? 0) > 0
                  const status = account?.codex_status ?? row.status ?? 'unknown'
                  const operation = account?.codex_operation_status ?? ''
                  const canRetry = Boolean(account) && !['queued', 'running', 'retrying'].includes(operation)
                  const canStop = ['queued', 'running', 'retrying'].includes(operation)
                  return (
                    <TableRow key={row.filename}>
                      <TableCell>
                        <Checkbox
                          aria-label={`选择 ${row.filename}`}
                          checked={Boolean(selected[row.filename])}
                          onCheckedChange={(checked) => toggleRowSelection(row, checked === true)}
                        />
                      </TableCell>
                      <TableCell className="max-w-72">
                        <div className="truncate font-medium" title={row.email || ''}>{row.email || '—'}</div>
                        <div className="break-all text-xs text-muted-foreground" title={row.filename}>{row.filename}</div>
                      </TableCell>
                      <TableCell>{row.plan || '—'}</TableCell>
                      <TableCell>
                        <div className="flex flex-col items-start gap-1">
                          <StatusBadge status={status} label={accountStatusLabels[status] ?? status} />
                          <StatusBadge
                            status={exported ? 'used' : 'available'}
                            label={exported ? `已导出 ${row.exported_count} 次` : '未导出'}
                          />
                          {exported && row.exported_at && <span className="text-xs text-muted-foreground">最近 {row.exported_at}</span>}
                          {account?.codex_error && <span className="max-w-56 truncate text-xs text-destructive" title={account.codex_error}>{account.codex_error}</span>}
                        </div>
                      </TableCell>
                      <TableCell className="max-w-40 break-all">{row.account_id || '—'}</TableCell>
                      <TableCell className="whitespace-nowrap text-sm text-muted-foreground">{row.mtime || '—'}</TableCell>
                      <TableCell className="whitespace-nowrap text-sm text-muted-foreground">{row.expired || '—'}</TableCell>
                      <TableCell>
                        <div className="flex flex-wrap justify-end gap-1">
                          <Button variant="outline" size="sm" disabled={busy} onClick={() => void downloadOne(row.filename, false)}>
                            <Download data-icon="inline-start" aria-hidden="true" />本地下载
                          </Button>
                          <Button variant="outline" size="sm" disabled={busy} onClick={() => void downloadOne(row.filename, true)}>
                            <Download data-icon="inline-start" aria-hidden="true" />从 CPA
                          </Button>
                          {canRetry && (
                            <ConfirmAction
                              title="补跑 Codex"
                              description={`为 ${row.email} 启动 Codex 补跑；该操作会消耗邮箱验证码与接码资源。`}
                              confirmLabel="开始补跑"
                              disabled={busy}
                              onConfirm={() => void actOnAccount(row.email ?? '', 'retry')}
                            >补跑</ConfirmAction>
                          )}
                          {canStop && (
                            <>
                              <ConfirmAction title="停止 Codex 补跑" description={`向 ${row.email} 的 Codex 补跑任务发送停止信号吗？`} confirmLabel="停止补跑" disabled={busy} onConfirm={() => void actOnAccount(row.email ?? '', 'stop')}>
                                <Square data-icon="inline-start" aria-hidden="true" />停止
                              </ConfirmAction>
                              <ConfirmAction title="重置补跑状态" description={`把 ${row.email} 的 Codex 补跑中状态重置为失败；此操作不会向运行线程发送停止信号。`} confirmLabel="重置状态" disabled={busy} onConfirm={() => void actOnAccount(row.email ?? '', 'reset-retrying')}>
                                <RotateCcw data-icon="inline-start" aria-hidden="true" />重置状态
                              </ConfirmAction>
                            </>
                          )}
                          {!account && <Button variant="ghost" size="sm" disabled title="没有找到关联账号">无账号</Button>}
                          <Button variant="ghost" size="sm" disabled={!row.email} onClick={() => setLogEmail(row.email ?? '')}>
                            <FileText data-icon="inline-start" aria-hidden="true" />补跑日志
                          </Button>
                          {exported && (
                            <ConfirmAction title="重置导出计数" description={`将 ${row.filename} 的导出计数清零并标记为未导出吗？`} disabled={busy} onConfirm={() => void resetExport(row.filename)}>
                              <RotateCcw data-icon="inline-start" aria-hidden="true" />重置导出
                            </ConfirmAction>
                          )}
                          <ConfirmAction title="永久删除 Codex 凭证" description={`删除本地凭证文件 ${row.filename} 并清理导出标记吗？此操作不可撤销。`} triggerVariant="destructive" disabled={busy} onConfirm={() => void deleteOne(row)}>
                            <Trash2 data-icon="inline-start" aria-hidden="true" />删除
                          </ConfirmAction>
                        </div>
                      </TableCell>
                    </TableRow>
                  )
                }) : (
                  <TableRow><TableCell colSpan={8} className="h-24 text-center text-muted-foreground">还没有 Codex 凭证</TableCell></TableRow>
                )}
              </TableBody>
            </Table>
          </div>
          <Pager
            page={page}
            pageSize={pageSize}
            total={total}
            onPageChange={setPage}
            onPageSizeChange={(size) => { setPageSize(size); setPage(1) }}
          />
        </CardContent>
      </Card>

      <Dialog open={Boolean(logEmail)} onOpenChange={(open) => { if (!open) setLogEmail(null) }}>
        <DialogContent className="sm:max-w-3xl">
          <DialogHeader>
            <DialogTitle>Codex 补跑日志</DialogTitle>
            <DialogDescription className="break-all">{logEmail}</DialogDescription>
          </DialogHeader>
          {logError && (
            <Alert variant="destructive">
              <AlertTitle>读取日志失败</AlertTitle>
              <AlertDescription>{logError}</AlertDescription>
            </Alert>
          )}
          <pre ref={logContentRef} className="max-h-[60svh] min-h-48 max-w-full overflow-auto whitespace-pre-wrap break-words rounded-md border bg-muted/30 p-3 text-xs leading-relaxed">
            {logText || '(暂无日志，等待任务写入…)'}
          </pre>
          <DialogFooter className="flex-col-reverse sm:flex-row sm:items-center sm:justify-between">
            <span className="text-xs text-muted-foreground">{logRunning ? '补跑进行中 · 每 2 秒刷新' : '已停止 · 每 2 秒刷新'}</span>
            <Button variant="outline" onClick={() => setLogEmail(null)}>关闭</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
