import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react'
import {
  Activity,
  Boxes,
  CircleCheck,
  Gauge,
  KeyRound,
  LogOut,
  Mail,
  RefreshCw,
  Settings2,
  UsersRound,
} from 'lucide-react'
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarSeparator,
  SidebarTrigger,
} from '@/components/ui/sidebar'
import { TooltipProvider } from '@/components/ui/tooltip'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Separator } from '@/components/ui/separator'
import { Toaster, toast } from '@/components/ui/toast'
import { api } from '@/lib/api'
import { Skeleton } from '@/components/ui/skeleton'

const AccountsPage = lazy(() => import('@/pages/AccountsPage'))
const CodexPage = lazy(() => import('@/pages/CodexPage'))
const MailboxPage = lazy(() => import('@/pages/MailboxPage'))
const RegisterPage = lazy(() => import('@/pages/RegisterPage'))
const ConfigPage = lazy(() => import('@/pages/ConfigPage'))

type TabId = 'register' | 'accounts' | 'codex' | 'outlook' | 'config'
type Summary = {
  accounts: number
  outlook_total: number
  outlook_available: number
  outlook_used: number
  outlook_failed: number
}

const navigation: Array<{
  id: TabId
  label: string
  title: string
  description: string
  icon: typeof Gauge
}> = [
  {
    id: 'register',
    label: '注册任务',
    title: '注册任务',
    description: '创建批量注册任务并跟踪运行状态。',
    icon: Gauge,
  },
  {
    id: 'accounts',
    label: '账号管理',
    title: '账号管理',
    description: '查看账号状态、筛选记录并执行批量操作。',
    icon: UsersRound,
  },
  {
    id: 'codex',
    label: 'Codex 授权',
    title: 'Codex 授权',
    description: '管理 Codex 授权记录与导出状态。',
    icon: KeyRound,
  },
  {
    id: 'outlook',
    label: '邮箱池',
    title: '邮箱池',
    description: '管理邮箱来源、状态与导入记录。',
    icon: Mail,
  },
  {
    id: 'config',
    label: '配置',
    title: '配置',
    description: '查看并更新控制台与注册流程配置。',
    icon: Settings2,
  },
]

const summaryCards: Array<{
  label: string
  value: (summary: Summary) => number
  note: string
  icon: typeof Gauge
}> = [
  { label: '已管理账号', value: (summary) => summary.accounts, note: '本地账号记录', icon: UsersRound },
  { label: '邮箱总量', value: (summary) => summary.outlook_total, note: '当前启用的邮箱来源', icon: Mail },
  { label: '可用邮箱', value: (summary) => summary.outlook_available, note: '等待注册任务使用', icon: CircleCheck },
  { label: '已使用 / 失败', value: (summary) => summary.outlook_used + summary.outlook_failed, note: '邮箱池处理结果', icon: Activity },
]

function readInitialTab(): TabId {
  const saved = window.localStorage.getItem('gpt_console_active_tab')
  return navigation.some((item) => item.id === saved) ? (saved as TabId) : 'register'
}

function formatCount(value: number | undefined) {
  return new Intl.NumberFormat('zh-CN').format(Number.isFinite(value) ? Number(value) : 0)
}

function App() {
  const [activeTab, setActiveTab] = useState<TabId>(readInitialTab)
  const [summary, setSummary] = useState<Summary | null>(null)
  const [summaryError, setSummaryError] = useState('')
  const [refreshing, setRefreshing] = useState(false)
  const activePage = navigation.find((item) => item.id === activeTab) ?? navigation[0]
  const summaryRequest = useRef<Promise<void> | null>(null)
  const mounted = useRef(false)

  const loadSummary = useCallback((notify = false) => {
    if (summaryRequest.current) return summaryRequest.current
    setRefreshing(true)
    const request = (async () => {
      try {
        const next = await api<Summary>('/api/summary')
        if (!mounted.current) return
        setSummary(next)
        setSummaryError('')
        if (notify) toast.add({ title: '数据已刷新', type: 'success' })
      } catch (error) {
        if (!mounted.current) return
        const message = error instanceof Error ? error.message : '读取概览数据失败'
        setSummaryError(message)
        if (notify) toast.add({ title: message, type: 'error' })
      } finally {
        summaryRequest.current = null
        if (mounted.current) setRefreshing(false)
      }
    })()
    summaryRequest.current = request
    return request
  }, [])

  useEffect(() => {
    window.localStorage.setItem('gpt_console_active_tab', activeTab)
    document.title = `${activePage.title} · GPT Registrator`
  }, [activePage.title, activeTab])

  useEffect(() => {
    mounted.current = true
    void loadSummary()
    const timer = window.setInterval(() => void loadSummary(), 5000)
    return () => {
      mounted.current = false
      window.clearInterval(timer)
    }
  }, [loadSummary])

  return (
    <TooltipProvider>
      <SidebarProvider>
        <Sidebar collapsible="icon" variant="sidebar">
          <SidebarHeader className="gap-3 px-3 py-4">
            <div className="flex items-center gap-3 px-1">
              <div className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-primary text-primary-foreground">
                <Boxes data-icon="inline-start" aria-hidden="true" />
              </div>
              <div className="min-w-0 group-data-[collapsible=icon]:hidden">
                <p className="truncate text-sm font-semibold text-sidebar-foreground">GPT Registrator</p>
                <p className="text-xs text-muted-foreground">本地管理控制台</p>
              </div>
            </div>
          </SidebarHeader>
          <SidebarSeparator />
          <SidebarContent className="px-2 py-3">
            <SidebarGroup className="p-0">
              <SidebarGroupLabel className="px-2">工作空间</SidebarGroupLabel>
              <SidebarGroupContent>
                <SidebarMenu>
                  {navigation.map((item) => {
                    const Icon = item.icon
                    return (
                      <SidebarMenuItem key={item.id}>
                        <SidebarMenuButton
                          isActive={activeTab === item.id}
                          closeOnMobile
                          onClick={() => setActiveTab(item.id)}
                          aria-current={activeTab === item.id ? 'page' : undefined}
                          tooltip={item.label}
                        >
                          <Icon data-icon="inline-start" aria-hidden="true" />
                          <span>{item.label}</span>
                        </SidebarMenuButton>
                      </SidebarMenuItem>
                    )
                  })}
                </SidebarMenu>
              </SidebarGroupContent>
            </SidebarGroup>
          </SidebarContent>
          <SidebarFooter className="gap-2 p-2">
            <a
              className="flex min-h-9 items-center gap-2 rounded-md px-2 text-sm text-muted-foreground transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              href="https://github.com/c4lnn/turb-gpt-free-register"
              target="_blank"
              rel="noopener noreferrer"
            >
              <Activity data-icon="inline-start" aria-hidden="true" />
              <span className="group-data-[collapsible=icon]:hidden">GitHub 仓库</span>
            </a>
            <form action="/logout" method="post">
              <SidebarMenuButton render={<button type="submit" />}>
                <LogOut data-icon="inline-start" aria-hidden="true" />
                <span>退出登录</span>
              </SidebarMenuButton>
            </form>
          </SidebarFooter>
        </Sidebar>
        <SidebarInset className="min-h-svh min-w-0 bg-muted/30">
          <header className="sticky top-0 z-10 flex h-14 items-center gap-3 border-b bg-background/95 px-4 backdrop-blur md:px-6">
            <SidebarTrigger aria-label="切换导航" />
            <Separator orientation="vertical" className="h-5" />
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-semibold">{activePage.title}</p>
            </div>
            <div className="hidden items-center gap-2 text-xs text-muted-foreground sm:flex">
              <span className={`size-2 rounded-full ${summaryError ? 'bg-destructive' : 'bg-success'}`} aria-hidden="true" />
              <span>{summaryError ? '连接异常' : '本地服务已连接'}</span>
            </div>
            <Button
              variant="outline"
              size="sm"
              onClick={() => void loadSummary(true)}
              disabled={refreshing}
              aria-label="刷新概览数据"
            >
              <RefreshCw data-icon="inline-start" aria-hidden="true" />
              <span className="hidden sm:inline">刷新</span>
            </Button>
          </header>
          <main className="mx-auto flex w-full max-w-screen-2xl flex-col gap-6 p-4 md:p-6 lg:p-8">
            <section className="flex flex-wrap items-end justify-between gap-4">
              <div className="flex flex-col gap-1">
                <div className="flex items-center gap-2">
                  <h1 className="text-2xl font-semibold tracking-tight">{activePage.title}</h1>
                  <Badge variant="outline">控制台</Badge>
                </div>
                <p className="text-sm text-muted-foreground">{activePage.description}</p>
              </div>
              <div className="flex items-center gap-2 text-xs text-muted-foreground">
                <span>自动刷新</span>
                <span aria-hidden="true">·</span>
                <span>5 秒</span>
              </div>
            </section>

            {summaryError && (
              <Alert variant="destructive">
                <AlertTitle>暂时无法读取概览数据</AlertTitle>
                <AlertDescription>{summaryError}</AlertDescription>
              </Alert>
            )}

            <section aria-label="资源概览" className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
              {summaryCards.map((item) => {
                const Icon = item.icon
                return (
                  <Card key={item.label} size="sm" className="gap-0">
                    <CardHeader className="flex-row items-center justify-between gap-3 pb-2">
                      <CardDescription>{item.label}</CardDescription>
                      <Icon className="size-4 text-muted-foreground" aria-hidden="true" />
                    </CardHeader>
                    <CardContent className="flex items-end justify-between gap-2">
                      <CardTitle className="text-2xl font-semibold tabular-nums">
                        {summary ? formatCount(item.value(summary)) : '—'}
                      </CardTitle>
                      <span className="pb-0.5 text-xs text-muted-foreground">{item.note}</span>
                    </CardContent>
                  </Card>
                )
              })}
            </section>

            <Suspense fallback={<div className="flex flex-col gap-4" role="status" aria-busy="true" aria-label="正在加载模块"><span className="sr-only">正在加载模块</span><Skeleton className="h-8 w-48" /><Skeleton className="h-64 w-full" /></div>}>
              {activeTab === 'register' ? <RegisterPage />
                : activeTab === 'accounts' ? <AccountsPage />
                  : activeTab === 'codex' ? <CodexPage />
                    : activeTab === 'outlook' ? <MailboxPage />
                      : <ConfigPage />}
            </Suspense>
          </main>
        </SidebarInset>
      </SidebarProvider>
      <Toaster />
    </TooltipProvider>
  )
}

export default App
