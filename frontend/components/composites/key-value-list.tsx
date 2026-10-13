import * as React from 'react';
import { cn } from '@/lib/utils';
import { CopyButton } from './copy-button';

export interface KeyValueItem { label: React.ReactNode; value: React.ReactNode; hint?: React.ReactNode; copy?: string; mono?: boolean }

/** Definition list for detail panels. `columns` 1-3 on wide screens (always 1 on mobile). */
export function KeyValueList({ items, columns = 1, className }: { items: KeyValueItem[]; columns?: 1 | 2 | 3; className?: string }) {
  return (
    <dl className={cn('grid gap-x-6 gap-y-3', columns === 2 && 'sm:grid-cols-2', columns === 3 && 'sm:grid-cols-2 xl:grid-cols-3', className)}>
      {items.map((it, i) => (
        <div key={i} className="min-w-0">
          <dt className="text-xs font-medium text-muted-foreground">{it.label}</dt>
          <dd className={cn('mt-0.5 flex items-center gap-1 break-words text-sm', it.mono && 'font-mono text-xs')}>
            <span className="min-w-0">{it.value === null || it.value === undefined || it.value === '' ? <span className="text-muted-foreground">—</span> : it.value}</span>
            {it.copy && <CopyButton value={it.copy} label={`Copy ${typeof it.label === 'string' ? it.label : 'value'}`} />}
          </dd>
          {it.hint && <p className="mt-0.5 text-xs text-muted-foreground">{it.hint}</p>}
        </div>
      ))}
    </dl>
  );
}
