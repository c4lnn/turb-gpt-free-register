import { useId, type ReactNode } from 'react'
import { cn } from 'cn'
import { Field, FieldGroup, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'

type TableToolbarProps = {
  search: string
  onSearchChange: (value: string) => void
  searchLabel: string
  filters?: ReactNode
  actions?: ReactNode
  inlineFilters?: boolean
}

export function TableToolbar({
  search,
  onSearchChange,
  searchLabel,
  filters,
  actions,
  inlineFilters = false,
}: TableToolbarProps) {
  const searchId = useId()
  return (
    <div className="flex flex-col gap-3">
      <FieldGroup className={inlineFilters ? 'flex-row flex-wrap items-end gap-2' : 'flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-start'}>
        <Field className={cn('w-full sm:max-w-64', inlineFilters && 'gap-1')}>
          <FieldLabel htmlFor={searchId} className={cn(inlineFilters && 'text-xs')}>{searchLabel}</FieldLabel>
          <Input
            id={searchId}
            type="search"
            className={cn(inlineFilters && 'h-7 rounded-[min(var(--radius-md),10px)]')}
            value={search}
            onChange={(event) => onSearchChange(event.currentTarget.value)}
            placeholder="输入关键词"
          />
        </Field>
        {inlineFilters ? filters : filters && <div className="flex flex-wrap items-center gap-2">{filters}</div>}
      </FieldGroup>
      {actions && (
        <div className="flex flex-wrap items-center gap-2" role="group" aria-label="列表操作">
          {actions}
        </div>
      )}
    </div>
  )
}
