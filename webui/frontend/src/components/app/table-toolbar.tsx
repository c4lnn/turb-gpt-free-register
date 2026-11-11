import { useId, type ReactNode } from 'react'
import { Field, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'

type TableToolbarProps = {
  search: string
  onSearchChange: (value: string) => void
  searchLabel: string
  filters?: ReactNode
  actions?: ReactNode
}

export function TableToolbar({
  search,
  onSearchChange,
  searchLabel,
  filters,
  actions,
}: TableToolbarProps) {
  const searchId = useId()
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-start">
        <Field className="w-full sm:max-w-64">
          <FieldLabel htmlFor={searchId}>{searchLabel}</FieldLabel>
          <Input
            id={searchId}
            type="search"
            value={search}
            onChange={(event) => onSearchChange(event.currentTarget.value)}
            placeholder="输入关键词"
          />
        </Field>
        {filters && <div className="flex flex-wrap items-center gap-2">{filters}</div>}
      </div>
      {actions && (
        <div className="flex flex-wrap items-center gap-2" role="group" aria-label="列表操作">
          {actions}
        </div>
      )}
    </div>
  )
}
