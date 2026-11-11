import { useCallback, useEffect, useRef, useState } from 'react'
import { Activity, FileText, ListX, RefreshCw, RotateCcw, Square, Trash2 } from 'lucide-react'
import { ConfirmAction } from '@/components/app/confirm-action'
import { StatusBadge } from '@/components/app/status-badge'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Checkbox } from '@/components/ui/checkbox'
import { Field, FieldGroup, FieldLabel } from '@/components/ui/field'
import { InputGroup, InputGroupAddon, InputGroupButton, InputGroupInput } from '@/components/ui/input-group'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { api } from '@/lib/api'
import { toast } from '@/components/ui/toast'

type Job = {
  id: number
  status: string
  email?: string
  started_at?: string
  completed_at?: string
  display_status?: string
  retryable?: boolean
  retry_action?: string
  retry_label?: string
  manual_otp_required?: boolean
  error_message?: string
  error_stage?: string
  network_traffic?: { available?: boolean; bytes_sent?: number; bytes_received?: number }
}

type JobsResponse = {
  items: Job[]
  total: number
  page: number
  page_size: number
  status_counts: Record<string, number>
}

const PAGE_SIZE = 25

function post(body: unknown = {}) {
  return { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
}

function errorText(error: unknown) {
  return error instanceof Error ? error.message : '请求失败'
}

export default function RegisterPage() {
  const [jobs, setJobs] = useState<Job[]>([])
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const [statusCounts, setStatusCounts] = useState<Record<string, number>>({})
  const [selected, setSelected] = useState<number[]>([])
  const [count, setCount] = useState('1')
  const [workers, setWorkers] = useState('3')
  const [loading, setLoading] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [pageError, setPageError] = useState('')
  const [notice, setNotice] = useState('')
  const [logJob, setLogJob] = useState<Job | null>(null)
  const [log, setLog] = useState('')
  const [manualCodes, setManualCodes] = useState<Record<number, string>>({})
  const [otpPending, setOtpPending] = useState<number | null>(null)
  const jobsRequestInFlight = useRef(false)

  const refreshJobs = useCallback(async (targetPage = page) => {
    if (jobsRequestInFlight.current) return
    jobsRequestInFlight.current = true
    setLoading(true)
    try {
      const result = await api<JobsResponse>(`/api/jobs?paged=1&page=${targetPage}&page_size=${PAGE_SIZE}`)
      setJobs(result.items ?? [])
      setTotal(result.total ?? 0)
      setStatusCounts(result.status_counts ?? {})
      setSelected((current) => current.filter((id) => result.items?.some((job) => job.id === id)))
      setPageError('')
    } catch (error) {
      setPageError(errorText(error))
    } finally {
      jobsRequestInFlight.current = false
      setLoading(false)
    }
  }, [page])

  useEffect(() => {
    void refreshJobs(page)
    const timer = window.setInterval(() => void refreshJobs(page), 3000)
    return () => window.clearInterval(timer)
  }, [page, refreshJobs])

  useEffect(() => {
    if (!logJob) return
    let active = true
    let requestInFlight = false
    const refreshLog = async () => {
      if (!active || requestInFlight) return
      requestInFlight = true
      try {
        const result = await api<{ log?: string }>(`/api/jobs/${logJob.id}/log`)
        if (active) setLog(result.log || '(暂无日志)')
      } catch (error) {
        if (active) setLog(`读取日志失败：${errorText(error)}`)
      } finally {
        requestInFlight = false
      }
    }
    void refreshLog()
    const timer = window.setInterval(() => void refreshLog(), 2000)
    return () => {
      active = false
      window.clearInterval(timer)
    }
  }, [logJob])

  async function createJobs(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setSubmitting(true)
    setNotice('')
    try {
      const result = await api<{ submitted: number; warning?: string; workers?: number }>('/api/jobs', post({ count: Number(count), workers: Number(workers) }))
      setNotice(result.warning || `已提交 ${result.submitted} 个注册任务，并发 ${result.workers ?? workers}`)
      toast.add({ title: result.warning || `已提交 ${result.submitted} 个注册任务`, type: result.warning ? 'warning' : 'success' })
      setPage(1)
      await refreshJobs(1)
    } catch (error) {
      toast.add({ title: errorText(error), type: 'error' })
    } finally {
      setSubmitting(false)
    }
  }

  async function submitOtp(job: Job) {
    const code = manualCodes[job.id]?.trim()
    if (!code) return
    setOtpPending(job.id)
    try {
      await api('/api/manual-otp', post({ job_id: job.id, code }))
      setManualCodes((current) => ({ ...current, [job.id]: '' }))
      toast.add({ title: '验证码已提交', type: 'success' })
      await refreshJobs()
    } catch (error) {
      toast.add({ title: errorText(error), type: 'error' })
    } finally {
      setOtpPending(null)
    }
  }

  async function stopJob(job: Job) {
    try {
      const result = await api<{ message?: string }>(`/api/jobs/${job.id}/stop`, post())
      toast.add({ title: result.message || '已发送停止信号', type: 'success' })
      await refreshJobs()
    } catch (error) {
      toast.add({ title: errorText(error), type: 'error' })
    }
  }

  async function deleteJob(job: Job) {
    try {
      await api(`/api/jobs/${job.id}/delete`, post())
      setSelected((current) => current.filter((id) => id !== job.id))
      toast.add({ title: '任务记录已删除', type: 'success' })
      await refreshJobs()
    } catch (error) {
      toast.add({ title: errorText(error), type: 'error' })
    }
  }

  async function retryJob(job: Job) {
    try {
      const result = await api<{ message?: string; action?: string }>(`/api/jobs/${job.id}/retry`, post({ workers: Number(workers) }))
      toast.add({ title: result.message || (job.retry_action === 'codex' ? '已提交 Codex 补跑' : '已提交完整注册重试'), type: 'success' })
      await refreshJobs()
    } catch (error) {
      toast.add({ title: errorText(error), type: 'error' })
    }
  }

  async function bulkAction(action: 'retry' | 'delete') {
    try {
      const result = await api<{ skipped?: Array<{ reason?: string }>; deleted_count?: number; retried_count?: number }>(
        action === 'retry' ? '/api/jobs/retry-bulk' : '/api/jobs/delete-bulk',
        post({ job_ids: selected, workers: Number(workers) }),
      )
      const skipped = result.skipped?.length ?? 0
      toast.add({
        title: action === 'retry'
          ? `已提交重试${skipped ? `，跳过 ${skipped} 项` : ''}`
          : `已删除 ${result.deleted_count ?? 0} 项${skipped ? `，跳过 ${skipped} 项` : ''}`,
        type: skipped ? 'warning' : 'success',
      })
      setSelected([])
      await refreshJobs()
    } catch (error) {
      toast.add({ title: errorText(error), type: 'error' })
    }
  }

  async function cancelPending() {
    try {
      const result = await api<{ cancelled: number }>('/api/jobs/cancel-pending', post())
      toast.add({ title: `已取消 ${result.cancelled} 个排队任务`, type: 'success' })
      await refreshJobs()
    } catch (error) {
      toast.add({ title: errorText(error), type: 'error' })
    }
  }

  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const visibleIds = jobs.map((job) => job.id)
  const allVisibleSelected = visibleIds.length > 0 && visibleIds.every((id) => selected.includes(id))

  return (
    <div className="flex flex-col gap-4">
      {pageError && <Alert variant="destructive"><AlertTitle>任务数据暂不可用</AlertTitle><AlertDescription>{pageError}</AlertDescription></Alert>}
      {notice && <Alert><AlertTitle>任务提交结果</AlertTitle><AlertDescription>{notice}</AlertDescription></Alert>}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base"><Activity className="size-4" aria-hidden="true" />创建注册任务</CardTitle>
          <CardDescription>批量创建任务；邮箱来源和验证码模式由当前配置决定。</CardDescription>
        </CardHeader>
        <CardContent>
          <form className="flex flex-col gap-3" onSubmit={createJobs}>
          <FieldGroup className="sm:flex-row sm:items-end">
            <Field className="w-fit">
              <FieldLabel htmlFor="register-job-count">任务数量</FieldLabel>
              <Input id="register-job-count" type="number" min={1} max={200} value={count} onChange={(event) => setCount(event.currentTarget.value)} required className="w-32" />
            </Field>
            <Field className="w-fit">
              <FieldLabel htmlFor="register-workers">并发线程</FieldLabel>
              <Input id="register-workers" type="number" min={1} max={16} value={workers} onChange={(event) => setWorkers(event.currentTarget.value)} required className="w-32" />
            </Field>
          </FieldGroup>
            <div className="flex flex-wrap items-center gap-2">
              <Button type="submit" disabled={submitting}>{submitting ? '正在提交…' : '开始注册'}</Button>
              <ConfirmAction title="取消所有排队任务？" description="仅取消尚未运行的任务，运行中的任务不会停止。" confirmLabel="取消排队任务" onConfirm={cancelPending}>
                <ListX data-icon="inline-start" aria-hidden="true" />取消排队任务
              </ConfirmAction>
            </div>
          </form>
        </CardContent>
      </Card>
      <section aria-label="任务状态统计" className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {['pending', 'running', 'success', 'failed'].map((status) => (
          <Card key={status} size="sm"><CardContent className="flex items-center justify-between gap-2 pt-4"><StatusBadge status={status} /><span className="font-semibold tabular-nums">{statusCounts[status] ?? 0}</span></CardContent></Card>
        ))}
      </section>
      <Card>
        <CardHeader>
          <CardTitle className="text-base">注册任务</CardTitle>
          <CardDescription>共 {total} 项；每 3 秒刷新任务状态。</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <div className="flex flex-wrap items-center gap-2">
            <Button variant="outline" size="sm" onClick={() => void refreshJobs()} disabled={loading}><RefreshCw data-icon="inline-start" aria-hidden="true" />刷新</Button>
            <ConfirmAction title="重试所选任务？" description="每项由服务端判断重跑完整注册或仅补跑 Codex；不支持的任务会跳过。" confirmLabel="重试所选" onConfirm={() => void bulkAction('retry')} disabled={selected.length === 0}><RotateCcw data-icon="inline-start" aria-hidden="true" />重试所选 ({selected.length})</ConfirmAction>
            <ConfirmAction title="删除所选任务记录？" description="任务记录和对应日志将被删除；运行中的任务会被跳过。" confirmLabel="删除所选" onConfirm={() => void bulkAction('delete')} disabled={selected.length === 0}><Trash2 data-icon="inline-start" aria-hidden="true" />删除所选 ({selected.length})</ConfirmAction>
          </div>
          <p className="text-sm text-muted-foreground sm:hidden">可左右滑动表格，查看全部任务列。</p>
          <div role="region" aria-label="注册任务表格，可横向滚动查看全部列" tabIndex={0} className="overflow-x-auto rounded-md border focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring">
            <Table>
              <TableHeader><TableRow>
                <TableHead className="w-10"><Checkbox aria-label="选择当前页全部任务" checked={allVisibleSelected} indeterminate={!allVisibleSelected && visibleIds.some((id) => selected.includes(id))} onCheckedChange={(checked) => setSelected(checked === true ? [...new Set([...selected, ...visibleIds])] : selected.filter((id) => !visibleIds.includes(id)))} /></TableHead>
                <TableHead>ID</TableHead><TableHead>邮箱</TableHead><TableHead>状态</TableHead><TableHead>开始时间</TableHead><TableHead>错误摘要</TableHead><TableHead className="text-right">操作</TableHead>
              </TableRow></TableHeader>
              <TableBody>
                {jobs.length === 0 ? <TableRow><TableCell colSpan={7} className="h-24 text-center text-muted-foreground">{loading ? '正在加载…' : '暂无任务记录'}</TableCell></TableRow> : jobs.map((job) => (
                  <TableRow key={job.id}>
                    <TableCell><Checkbox aria-label={`选择任务 ${job.id}`} checked={selected.includes(job.id)} onCheckedChange={(checked) => setSelected((current) => checked === true ? [...current, job.id] : current.filter((id) => id !== job.id))} /></TableCell>
                    <TableCell className="font-mono text-xs">{job.id}</TableCell>
                    <TableCell className="max-w-48 truncate">{job.email || '等待分配'}</TableCell>
                    <TableCell><StatusBadge status={job.status} label={job.display_status} /></TableCell>
                    <TableCell className="whitespace-nowrap text-xs text-muted-foreground">{job.started_at || '—'}</TableCell>
                    <TableCell className="max-w-64 truncate text-xs text-muted-foreground" title={job.error_message}>{job.error_message || job.error_stage || '—'}</TableCell>
                    <TableCell><div className="flex justify-end gap-1">
                      {job.status === 'running' && job.email && job.manual_otp_required && <Field className="min-w-0"><FieldLabel htmlFor={`manual-otp-${job.id}`} className="sr-only">任务 {job.id} 的邮箱验证码</FieldLabel><InputGroup><InputGroupInput id={`manual-otp-${job.id}`} value={manualCodes[job.id] ?? ''} onChange={(event) => { const value = event.currentTarget.value; setManualCodes((current) => ({ ...current, [job.id]: value })) }} placeholder="OTP" maxLength={8} /><InputGroupAddon align="inline-end"><InputGroupButton variant="outline" disabled={!manualCodes[job.id]?.trim() || otpPending === job.id} onClick={() => void submitOtp(job)}>提交 OTP</InputGroupButton></InputGroupAddon></InputGroup></Field>}
                      <Button size="icon-sm" variant="ghost" aria-label={`查看任务 ${job.id} 日志`} onClick={() => { setLogJob(job); setLog('') }}><FileText aria-hidden="true" /></Button>
                      {['pending', 'running', 'stopping'].includes(job.status) && <ConfirmAction title={`停止任务 ${job.id}？`} description={`当前状态：${job.status}；${job.email || '邮箱尚未分配'}。`} confirmLabel="停止任务" onConfirm={() => void stopJob(job)} triggerSize="icon-sm" triggerVariant="ghost"><Square aria-hidden="true" /></ConfirmAction>}
                      {job.retryable && <ConfirmAction title="重试此任务？" description={job.retry_action === 'codex' ? '将仅补跑 Codex 授权。' : '将创建一个新的完整注册任务。'} confirmLabel={job.retry_label || '重试'} onConfirm={() => void retryJob(job)} triggerSize="icon-sm" triggerVariant="ghost"><RotateCcw aria-hidden="true" /></ConfirmAction>}
                      {!['running', 'stopping'].includes(job.status) && <ConfirmAction title={`删除任务 ${job.id}？`} description="任务记录与运行日志将被删除。" confirmLabel="删除任务" onConfirm={() => void deleteJob(job)} triggerSize="icon-sm" triggerVariant="ghost"><Trash2 aria-hidden="true" /></ConfirmAction>}
                    </div></TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-sm text-muted-foreground">第 {page} / {pageCount} 页 · 每页 {PAGE_SIZE} 项</span>
            <div className="flex gap-2"><Button size="sm" variant="outline" disabled={page <= 1} onClick={() => setPage((value) => value - 1)}>上一页</Button><Button size="sm" variant="outline" disabled={page >= pageCount} onClick={() => setPage((value) => value + 1)}>下一页</Button></div>
          </div>
        </CardContent>
      </Card>
      <Dialog open={Boolean(logJob)} onOpenChange={(open) => { if (!open) setLogJob(null) }}>
        <DialogContent className="max-h-[85vh] sm:max-w-3xl">
          <DialogHeader><DialogTitle>任务日志 · #{logJob?.id}</DialogTitle><DialogDescription>{logJob?.email || '等待分配邮箱'}</DialogDescription></DialogHeader>
          <pre className="max-h-[60vh] overflow-auto rounded-md bg-muted p-3 text-xs leading-relaxed whitespace-pre-wrap">{log || '正在读取日志…'}</pre>
        </DialogContent>
      </Dialog>
    </div>
  )
}
