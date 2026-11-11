import { Badge } from '@/components/ui/badge'

type StatusBadgeProps = {
  status: string | null | undefined
  label?: string
}

const labels: Record<string, string> = {
  available: '可用',
  registering: '注册中',
  used: '已使用',
  failed: '失败',
  partial_success: '部分成功',
  disabled: '已停用',
  pending: '排队中',
  running: '运行中',
  stopping: '停止中',
  stopped: '已停止',
  success: '成功',
  cancelled: '已取消',
  canceled: '已取消',
  retrying: '重试中',
  active: '正常',
  inactive: '未启用',
  unknown: '未知',
}

const successStatuses: Record<string, true> = { available: true, success: true, active: true }
const destructiveStatuses: Record<string, true> = {
  failed: true,
  stopped: true,
  cancelled: true,
  canceled: true,
  disabled: true,
  inactive: true,
}
const pendingStatuses: Record<string, true> = {
  registering: true,
  pending: true,
  running: true,
  stopping: true,
  retrying: true,
}

export function StatusBadge({ status, label }: StatusBadgeProps) {
  const value = String(status ?? 'unknown').trim().toLowerCase()
  const variant = destructiveStatuses[value]
    ? 'destructive'
    : pendingStatuses[value]
      ? 'secondary'
      : 'outline'
  const semanticColor = successStatuses[value] ? 'text-success' : undefined

  return (
    <Badge variant={variant} className={semanticColor}>
      {label ?? labels[value] ?? (value || '未知')}
    </Badge>
  )
}
