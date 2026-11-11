import { useEffect, useMemo, useRef, useState } from 'react'
import { Clipboard, Copy, Download, RefreshCw, Trash2 } from 'lucide-react'
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
import { Field, FieldDescription, FieldGroup, FieldLabel } from '@/components/ui/field'
import { Textarea } from '@/components/ui/textarea'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { api } from '@/lib/api'
import { toast } from '@/components/ui/toast'

type PoolSource = 'outlook' | 'generic_api' | 'cloudflare_domain' | 'icloud'
type PoolRow = {
  source?: PoolSource
  email: string
  status?: string
  access_token?: string
  copy_line?: string
  account_copy_line?: string
  imported_at?: string
  created_at?: string
  used_at?: string
}
type PoolResponse = { items: PoolRow[]; total: number; page: number; page_size: number }
type SelectedPoolItem = { source: PoolSource; email: string }
type ImportResult = {
  as_registered?: boolean
  parsed?: number
  inserted?: number
  skipped?: number
  errors?: Array<{ line: number; reason: string }>
}

const sourceLabels: Record<string, string> = {
  all: '全部邮箱池',
  outlook: 'Outlook 邮箱池',
  generic_api: '通用 API 邮箱池',
  cloudflare_domain: '域名邮箱池',
  icloud: 'iCloud 隐私邮箱池',
}
const importSourceLabels: Record<string, string> = {
  outlook: 'Outlook 邮箱池',
  generic_api: '通用 API 取码邮箱',
  icloud: 'iCloud 隐私邮箱',
}
const statusLabels: Record<string, string> = {
  all: '全部状态',
  available: '可用',
  registering: '注册中',
  used: '已用',
  failed: '失败',
  disabled: '已停用',
}
const pageSizes = [10, 20, 30, 50, 100]
const sourceItems = Object.entries(sourceLabels).map(([value, label]) => ({ value, label }))
const importSourceItems = Object.entries(importSourceLabels).map(([value, label]) => ({ value, label }))
const statusItems = Object.entries(statusLabels).map(([value, label]) => ({ value, label }))
const pageSizeItems = pageSizes.map((size) => ({ value: String(size), label: `${size} 条/页` }))

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

function poolKey(item: SelectedPoolItem) {
  return `${item.source}|${item.email}`
}

function formatImport(source: string, asRegistered: boolean) {
  if (source === 'generic_api') {
    return asRegistered
      ? { hint: '通用 API 已注册账号：邮箱----取码地址，可在后面附加 AT', placeholder: 'email----取码地址----AT' }
      : { hint: '通用 API 邮箱素材：邮箱----取码地址', placeholder: 'email----取码地址' }
  }
  if (source === 'icloud') {
    return asRegistered
      ? { hint: 'iCloud 已注册账号：邮箱----AT；后续 OTP 自动使用配置的 QQ IMAP', placeholder: 'email----accessToken' }
      : { hint: 'iCloud 邮箱素材：每行一个已有隐私邮箱地址', placeholder: 'email@privaterelay.appleid.com' }
  }
  return asRegistered
    ? { hint: 'Outlook 已注册账号：邮箱----密码----clientId----refreshToken，可在后面附加 AT', placeholder: 'email----password----clientId----refreshToken----AT' }
    : { hint: 'Outlook 邮箱素材：邮箱----密码----clientId----refreshToken', placeholder: 'email----password----clientId----refreshToken' }
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
        <Button variant="outline" size="sm" disabled={page <= 1} onClick={() => onPageChange(page - 1)}>
          上一页
        </Button>
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
        <Button variant="outline" size="sm" disabled={page >= totalPages} onClick={() => onPageChange(page + 1)}>
          下一页
        </Button>
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

export default function MailboxPage() {
  const [rows, setRows] = useState<PoolRow[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [source, setSource] = useState('all')
  const [status, setStatus] = useState('all')
  const [search, setSearch] = useState('')
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState<Record<string, SelectedPoolItem>>({})
  const [loading, setLoading] = useState(true)
  const [pageError, setPageError] = useState('')
  const [busy, setBusy] = useState(false)
  const [refreshVersion, setRefreshVersion] = useState(0)
  const [importOpen, setImportOpen] = useState(false)
  const [importSource, setImportSource] = useState('outlook')
  const [asRegistered, setAsRegistered] = useState(false)
  const [reactivateExisting, setReactivateExisting] = useState(false)
  const [importText, setImportText] = useState('')
  const [importing, setImporting] = useState(false)
  const [restoreTarget, setRestoreTarget] = useState<{ row?: PoolRow } | null>(null)
  const [restoreReason, setRestoreReason] = useState('人工确认后恢复邮箱池条目')
  const [importResult, setImportResult] = useState<{ text: string; error: boolean } | null>(null)
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
    const params = new URLSearchParams({
      paged: '1',
      page: String(page),
      page_size: String(pageSize),
      source,
      status: status === 'all' ? '' : status,
      q: query,
    })
    void api<PoolResponse>(`/api/outlook?${params.toString()}`, { signal: controller.signal })
      .then((result) => {
        if (currentRequest !== requestId.current) return
        const nextTotal = Number(result.total ?? 0)
        const maxPage = Math.max(1, Math.ceil(nextTotal / pageSize))
        setTotal(nextTotal)
        if (page > maxPage) {
          setPage(maxPage)
          return
        }
        setRows(result.items ?? [])
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return
        if (currentRequest === requestId.current) setPageError(errorText(error))
      })
      .finally(() => {
        if (currentRequest === requestId.current) setLoading(false)
      })
    return () => controller.abort()
  }, [page, pageSize, query, refreshVersion, source, status])

  const selectedItems = useMemo(() => Object.values(selected), [selected])
  const pageKeys = rows.map((row) => poolKey({ source: row.source ?? 'outlook', email: row.email }))
  const checkedOnPage = pageKeys.filter((key) => Boolean(selected[key])).length
  const allChecked = pageKeys.length > 0 && checkedOnPage === pageKeys.length
  const someChecked = checkedOnPage > 0 && !allChecked
  const importFormat = formatImport(importSource, asRegistered)

  function chooseSource(value: string) {
    setSource(value)
    setPage(1)
    setSelected({})
    if (value === 'outlook' || value === 'generic_api' || value === 'icloud') setImportSource(value)
  }

  function chooseStatus(value: string) {
    setStatus(value)
    setPage(1)
    setSelected({})
  }

  function togglePageSelection(checked: boolean) {
    setSelected((current) => {
      const next = { ...current }
      rows.forEach((row) => {
        const item = { source: row.source ?? 'outlook', email: row.email }
        const key = poolKey(item)
        if (checked) next[key] = item
        else delete next[key]
      })
      return next
    })
  }

  function toggleRowSelection(row: PoolRow, checked: boolean) {
    const item = { source: row.source ?? 'outlook', email: row.email }
    const key = poolKey(item)
    setSelected((current) => {
      const next = { ...current }
      if (checked) next[key] = item
      else delete next[key]
      return next
    })
  }

  async function copyValue(value: string | undefined, label: string) {
    if (!value) {
      toast.add({ title: `${label}为空`, type: 'warning' })
      return
    }
    try {
      await navigator.clipboard.writeText(value)
      toast.add({ title: `${label}已复制`, type: 'success' })
    } catch (error) {
      toast.add({ title: `复制失败：${errorText(error)}`, type: 'error' })
    }
  }

  async function updateOneStatus(row: PoolRow, nextStatus: string, restoreReason?: string) {
    const sourceName = row.source ?? 'outlook'
    let reason = restoreReason?.trim() || (nextStatus === 'failed' ? '手动标记失败' : nextStatus === 'disabled' ? '手动停用' : '')
    if (nextStatus === 'available' && !reason) return
    setBusy(true)
    try {
      await api('/api/outlook/status', post({ email: row.email, status: nextStatus, source: sourceName, reason }))
      toast.add({ title: nextStatus === 'available' ? '已恢复为可用' : nextStatus === 'failed' ? '已标失败' : '已停用', type: 'success' })
      setRefreshVersion((value) => value + 1)
    } catch (error) {
      toast.add({ title: `状态更新失败：${errorText(error)}`, type: 'error' })
    } finally {
      setBusy(false)
    }
  }

  async function deleteOne(row: PoolRow) {
    setBusy(true)
    try {
      await api('/api/outlook/delete', post({ email: row.email, source: row.source ?? 'outlook' }))
      setSelected((current) => {
        const next = { ...current }
        delete next[poolKey({ source: row.source ?? 'outlook', email: row.email })]
        return next
      })
      toast.add({ title: '邮箱素材已删除', type: 'success' })
      setRefreshVersion((value) => value + 1)
    } catch (error) {
      toast.add({ title: `删除失败：${errorText(error)}`, type: 'error' })
    } finally {
      setBusy(false)
    }
  }

  function openRestore(row?: PoolRow) {
    setRestoreReason('人工确认后恢复邮箱池条目')
    setRestoreTarget(row ? { row } : {})
  }

  async function confirmRestore() {
    const reason = restoreReason.trim()
    if (!restoreTarget || !reason) return
    const target = restoreTarget
    setRestoreTarget(null)
    if (target.row) await updateOneStatus(target.row, 'available', reason)
    else await updateSelectedStatus('available', reason)
  }
  async function updateSelectedStatus(nextStatus: 'disabled' | 'failed' | 'available', restoreReason?: string) {
    if (!selectedItems.length) return
    const labels = { disabled: '停用', failed: '标记为失败', available: '恢复为可用' }
    let note = `批量标记${labels[nextStatus]}`
    if (nextStatus === 'available') {
      if (!restoreReason?.trim()) return
      note = restoreReason.trim()
    }
    setBusy(true)
    try {
      const result = await api<{ updated_count?: number; skipped?: unknown[] }>('/api/outlook/status-bulk', post({
        items: selectedItems,
        status: nextStatus,
        source,
        note,
      }))
      const skipped = result.skipped?.length ?? 0
      toast.add({
        title: skipped ? `已更新 ${result.updated_count ?? 0} 个，跳过 ${skipped} 个` : `已更新 ${result.updated_count ?? 0} 个`,
        type: skipped ? 'warning' : 'success',
      })
      setSelected({})
      setRefreshVersion((value) => value + 1)
    } catch (error) {
      toast.add({ title: `批量状态更新失败：${errorText(error)}`, type: 'error' })
    } finally {
      setBusy(false)
    }
  }

  async function deleteSelected() {
    if (!selectedItems.length) return
    setBusy(true)
    try {
      const result = await api<{ deleted_count?: number; deleted?: SelectedPoolItem[]; skipped?: unknown[] }>('/api/outlook/delete-bulk', post({
        items: selectedItems,
        source,
      }))
      const deletedKeys = new Set((result.deleted ?? []).map((item) => poolKey(item)))
      setSelected((current) => Object.fromEntries(Object.entries(current).filter(([key]) => !deletedKeys.has(key))))
      const skipped = result.skipped?.length ?? 0
      toast.add({
        title: skipped ? `已删除 ${result.deleted_count ?? 0} 个，跳过 ${skipped} 个` : `已删除 ${result.deleted_count ?? 0} 个`,
        type: skipped ? 'warning' : 'success',
      })
      setRefreshVersion((value) => value + 1)
    } catch (error) {
      toast.add({ title: `批量删除失败：${errorText(error)}`, type: 'error' })
    } finally {
      setBusy(false)
    }
  }

  function openImport() {
    setImportResult(null)
    setImportText('')
    setAsRegistered(false)
    setReactivateExisting(false)
    setImportSource(source === 'outlook' || source === 'generic_api' || source === 'icloud' ? source : 'outlook')
    setImportOpen(true)
  }

  async function submitImport(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!importText.trim()) {
      setImportResult({ text: '请粘贴邮箱素材。', error: true })
      return
    }
    setImporting(true)
    setImportResult(null)
    try {
      const result = await api<ImportResult>('/api/outlook/import', post({
        text: importText,
        source: importSource,
        as_registered: asRegistered,
        reactivate_existing: reactivateExisting,
      }))
      const mode = result.as_registered ? '已注册账号' : '邮箱池素材'
      const errors = (result.errors ?? []).map((item) => `第 ${item.line} 行：${item.reason}`).join('；')
      const message = `按 ${mode} 导入：解析 ${result.parsed ?? 0} 行，新增 ${result.inserted ?? 0}，跳过 ${result.skipped ?? 0}${result.as_registered ? '；可到“账号”页选择后批量补跑 Codex' : ''}${errors ? `；${errors}` : ''}`
      setImportResult({ text: message, error: false })
      setImportText('')
      setReactivateExisting(false)
      setSource(importSource)
      setPage(1)
      setSelected({})
      setRefreshVersion((value) => value + 1)
      toast.add({ title: `导入完成：新增 ${result.inserted ?? 0}，跳过 ${result.skipped ?? 0}`, type: 'success' })
    } catch (error) {
      setImportResult({ text: errorText(error), error: true })
      toast.add({ title: `导入失败：${errorText(error)}`, type: 'error' })
    } finally {
      setImporting(false)
    }
  }

  return (
    <div className="flex min-w-0 flex-col gap-4">
      {pageError && (
        <Alert variant="destructive">
          <AlertTitle>邮箱池加载失败</AlertTitle>
          <AlertDescription>{pageError}</AlertDescription>
        </Alert>
      )}
      <Card className="min-w-0">
        <CardHeader className="gap-3">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <CardTitle>邮箱池</CardTitle>
              <CardDescription className="mt-1">筛选、导入和维护注册邮箱素材。</CardDescription>
            </div>
            <Button variant="outline" size="sm" onClick={() => setRefreshVersion((value) => value + 1)} disabled={loading}>
              <RefreshCw data-icon="inline-start" aria-hidden="true" />刷新
            </Button>
          </div>
          <TableToolbar
            search={search}
            onSearchChange={(value) => { setSearch(value); setPage(1); setSelected({}) }}
            searchLabel="搜索邮箱"
            filters={(
              <>
                <Field className="w-fit min-w-0 gap-1">
                  <FieldLabel htmlFor="mailbox-source-filter" className="text-xs">邮箱来源</FieldLabel>
                  <Select items={sourceItems} value={source} onValueChange={(value) => { if (value !== null) chooseSource(value) }}>
                    <SelectTrigger id="mailbox-source-filter" className="max-w-full">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectGroup>
                        {sourceItems.map((item) => <SelectItem key={item.value} value={item.value}>{item.label}</SelectItem>)}
                      </SelectGroup>
                    </SelectContent>
                  </Select>
                </Field>
                <Field className="w-fit min-w-0 gap-1">
                  <FieldLabel htmlFor="mailbox-status-filter" className="text-xs">邮箱状态</FieldLabel>
                  <Select items={statusItems} value={status} onValueChange={(value) => { if (value !== null) chooseStatus(value) }}>
                    <SelectTrigger id="mailbox-status-filter" className="max-w-full">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectGroup>
                        {statusItems.map((item) => <SelectItem key={item.value} value={item.value}>{item.label}</SelectItem>)}
                      </SelectGroup>
                    </SelectContent>
                  </Select>
                </Field>
              </>
            )}
            actions={(
              <>
                <ConfirmAction
                  title="批量停用邮箱"
                  description={`确定停用选中的 ${selectedItems.length} 个邮箱吗？它们将不会被注册任务领取。`}
                  disabled={!selectedItems.length || busy}
                  onConfirm={() => void updateSelectedStatus('disabled')}
                >停用选中</ConfirmAction>
                <ConfirmAction
                  title="批量标记失败"
                  description={`确定把选中的 ${selectedItems.length} 个邮箱标记为失败吗？`}
                  disabled={!selectedItems.length || busy}
                  onConfirm={() => void updateSelectedStatus('failed')}
                >标失败</ConfirmAction>
                <Button variant="outline" disabled={!selectedItems.length || busy} onClick={() => openRestore()}>
                  恢复选中
                </Button>
                <ConfirmAction
                  title="永久删除邮箱素材"
                  description={`确定永久删除选中的 ${selectedItems.length} 个邮箱素材吗？此操作不可撤销。`}
                  triggerVariant="destructive"
                  disabled={!selectedItems.length || busy}
                  onConfirm={() => void deleteSelected()}
                >删除选中</ConfirmAction>
                <Button variant="outline" onClick={() => void copyValue(rows.map((row) => row.copy_line || row.email).filter(Boolean).join('\n'), '当前页邮箱素材')} disabled={!rows.length}>
                  <Copy data-icon="inline-start" aria-hidden="true" />复制当前页素材
                </Button>
                <Button onClick={openImport}>导入</Button>
                {selectedItems.length > 0 && <span className="text-sm text-muted-foreground">已选 {selectedItems.length}</span>}
              </>
            )}
          />
        </CardHeader>
        <CardContent className="min-w-0 p-0">
          <p className="px-4 text-sm text-muted-foreground sm:hidden">可左右滑动表格，查看全部邮箱池列。</p>
          <div role="region" aria-label="邮箱池表格，可横向滚动查看全部列" tabIndex={0} className="max-w-full overflow-x-auto focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring">
            <Table className="min-w-[900px]">
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
                  <TableHead>邮箱</TableHead>
                  <TableHead>来源</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead>AT</TableHead>
                  <TableHead>导入时间</TableHead>
                  <TableHead>已用时间</TableHead>
                  <TableHead className="text-right">操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {loading && !rows.length ? (
                  <TableRow><TableCell colSpan={8} className="h-24 text-center text-muted-foreground">正在加载邮箱池…</TableCell></TableRow>
                ) : rows.length ? rows.map((row) => {
                  const sourceName = row.source ?? 'outlook'
                  const selectedKey = poolKey({ source: sourceName, email: row.email })
                  const copyLine = row.copy_line || row.account_copy_line || row.email
                  return (
                    <TableRow key={selectedKey}>
                      <TableCell>
                        <Checkbox
                          aria-label={`选择 ${row.email}`}
                          checked={Boolean(selected[selectedKey])}
                          onCheckedChange={(checked) => toggleRowSelection(row, checked === true)}
                        />
                      </TableCell>
                      <TableCell className="max-w-64">
                        <div className="truncate font-medium" title={row.email}>{row.email || '—'}</div>
                        <div className="text-xs text-muted-foreground">{row.account_copy_line ? '含注册账号信息' : '邮箱素材'}</div>
                      </TableCell>
                      <TableCell className="whitespace-nowrap">{sourceLabels[sourceName] ?? sourceName}</TableCell>
                      <TableCell><StatusBadge status={row.status} label={statusLabels[row.status ?? ''] ?? row.status ?? '未知'} /></TableCell>
                      <TableCell>
                        {row.access_token ? (
                          <Button variant="ghost" size="sm" onClick={() => void copyValue(row.access_token, 'AT')} aria-label={`复制 ${row.email} 的 AT`}>
                            <Clipboard data-icon="inline-start" aria-hidden="true" />复制
                          </Button>
                        ) : <span className="text-sm text-muted-foreground">无 AT</span>}
                      </TableCell>
                      <TableCell className="whitespace-nowrap text-sm text-muted-foreground">{row.imported_at || row.created_at || '—'}</TableCell>
                      <TableCell className="whitespace-nowrap text-sm text-muted-foreground">{row.used_at || '—'}</TableCell>
                      <TableCell>
                        <div className="flex flex-wrap justify-end gap-1">
                          <Button variant="outline" size="sm" onClick={() => void copyValue(copyLine, '邮箱素材')} aria-label={`复制 ${row.email} 的邮箱素材`}>
                            <Copy data-icon="inline-start" aria-hidden="true" />复制整行
                          </Button>
                          {row.status !== 'disabled' && (
                            <ConfirmAction title="停用邮箱" description={`停用 ${row.email} 后，注册任务不会领取该邮箱。`} disabled={busy} onConfirm={() => void updateOneStatus(row, 'disabled')}>
                              停用
                            </ConfirmAction>
                          )}
                          {row.status !== 'failed' && (
                            <ConfirmAction title="标记邮箱失败" description={`确定把 ${row.email} 标记为失败吗？`} disabled={busy} onConfirm={() => void updateOneStatus(row, 'failed')}>
                              标失败
                            </ConfirmAction>
                          )}
                          {['failed', 'disabled', 'used'].includes(row.status ?? '') && (
                            <ConfirmAction title="恢复邮箱" description={`恢复 ${row.email} 为可用状态后，它可能再次被注册任务领取。`} disabled={busy} onConfirm={() => openRestore(row)}>
                              恢复
                            </ConfirmAction>
                          )}
                          <ConfirmAction title="永久删除邮箱素材" description={`确定永久删除 ${row.email} 吗？此操作不可撤销。`} triggerVariant="destructive" disabled={busy} onConfirm={() => void deleteOne(row)}>
                            <Trash2 data-icon="inline-start" aria-hidden="true" />删除
                          </ConfirmAction>
                        </div>
                      </TableCell>
                    </TableRow>
                  )
                }) : (
                  <TableRow><TableCell colSpan={8} className="h-24 text-center text-muted-foreground">邮箱池为空</TableCell></TableRow>
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

      <Dialog open={Boolean(restoreTarget)} onOpenChange={(open) => { if (!open) setRestoreTarget(null) }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>恢复邮箱</DialogTitle>
            <DialogDescription>
              {restoreTarget?.row ? `请输入恢复 ${restoreTarget.row.email} 的原因：` : '请输入恢复原因（会写入审计）：'}
            </DialogDescription>
          </DialogHeader>
          <Field>
            <FieldLabel htmlFor="mailbox-restore-reason">恢复原因</FieldLabel>
            <Input id="mailbox-restore-reason" autoFocus value={restoreReason} onChange={(event) => setRestoreReason(event.currentTarget.value)} />
          </Field>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setRestoreTarget(null)}>取消</Button>
            <Button type="button" disabled={!restoreReason.trim() || busy} onClick={() => void confirmRestore()}>确认恢复</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <Dialog open={importOpen} onOpenChange={setImportOpen}>
        <DialogContent className="max-h-[90svh] overflow-y-auto sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>导入邮箱素材</DialogTitle>
            <DialogDescription>支持 Outlook、通用 API 与 iCloud。每行一条记录，分隔符可用 ---- 或 ====。</DialogDescription>
          </DialogHeader>
          <form className="flex flex-col gap-4" onSubmit={submitImport}>
            {importResult && (
              <Alert variant={importResult.error ? 'destructive' : 'default'}>
                <AlertTitle>{importResult.error ? '导入未完成' : '导入结果'}</AlertTitle>
                <AlertDescription className="break-words">{importResult.text}</AlertDescription>
              </Alert>
            )}
            <FieldGroup className="gap-4">
              <Field>
                <FieldLabel htmlFor="mailbox-import-source">导入类型</FieldLabel>
                <Select items={importSourceItems} value={importSource} onValueChange={(value) => { if (value !== null) setImportSource(value) }}>
                  <SelectTrigger id="mailbox-import-source"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectGroup>
                      {importSourceItems.map((item) => <SelectItem key={item.value} value={item.value}>{item.label}</SelectItem>)}
                    </SelectGroup>
                  </SelectContent>
                </Select>
                <FieldDescription>{importFormat.hint}</FieldDescription>
              </Field>
              <Field orientation="horizontal" className="items-start">
                <Checkbox id="mailbox-as-registered" checked={asRegistered} onCheckedChange={(checked) => setAsRegistered(checked === true)} />
                <FieldLabel htmlFor="mailbox-as-registered" className="font-normal">导入后默认视为「注册成功账号」</FieldLabel>
              </Field>
              <Field orientation="horizontal" className="items-start">
                <Checkbox id="mailbox-reactivate-existing" checked={reactivateExisting} onCheckedChange={(checked) => setReactivateExisting(checked === true)} />
                <FieldLabel htmlFor="mailbox-reactivate-existing" className="font-normal">显式重新导入并恢复已有失败、停用或已用邮箱（会写入恢复审计）</FieldLabel>
              </Field>
            {importSource === 'icloud' && asRegistered && (
              <Alert>
                <AlertTitle>iCloud 账号说明</AlertTitle>
                <AlertDescription>后续 OTP 将自动使用已配置的 QQ IMAP；已注册账号行需包含邮箱和 AT。</AlertDescription>
              </Alert>
            )}
              <Field>
                <FieldLabel htmlFor="mailbox-import-text">邮箱素材</FieldLabel>
                <Textarea
                  id="mailbox-import-text"
                  value={importText}
                  onChange={(event) => setImportText(event.currentTarget.value)}
                  placeholder={importFormat.placeholder}
                  rows={8}
                  className="min-h-36 resize-y"
                />
              </Field>
            </FieldGroup>
            <DialogFooter className="flex-col-reverse sm:flex-row">
              <Button type="button" variant="outline" onClick={() => setImportOpen(false)}>取消</Button>
              <Button type="submit" disabled={importing}>
                {importing ? '正在导入…' : '导入'}
                {importing && <Download data-icon="inline-start" aria-hidden="true" />}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  )
}
