import * as React from 'react';
import { cn } from '@/lib/utils';
import { RelativeTime } from './relative-time';

export interface TimelineItem {
  id: string | number;
  title: React.ReactNode;
  description?: React.ReactNode;
  time?: string | Date | null;
  tone?: 'neutral' | 'success' | 'warning' | 'danger' | 'info';
  icon?: React.ReactNode;
}
const dot: Record<NonNullable<TimelineItem['tone']>, string> = {
  neutral: 'border-neutral-border bg-neutral-soft text-neutral-foreground',
  success: 'border-success-border bg-success-soft text-success-foreground',
  warning: 'border-warning-border bg-warning-soft text-warning-foreground',
  danger: 'border-danger-border bg-danger-soft text-danger-foreground',
  info: 'border-info-border bg-info-soft text-info-foreground',
};

/** Vertical event timeline (run events, audit trail, verification history). Newest first is the caller's order. */
export function Timeline({ items, className }: { items: TimelineItem[]; className?: string }) {
  return (
    <ol className={cn('relative space-y-4', className)}>
      {items.map((it, i) => (
        <li key={it.id} className="relative flex gap-3">
          {i < items.length - 1 && <span aria-hidden className="absolute left-[11px] top-6 h-[calc(100%-0.5rem)] w-px bg-border" />}
          <span className={cn('relative z-10 mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-full border [&_svg]:size-3', dot[it.tone ?? 'neutral'])}>
            {it.icon ?? <span className="size-1.5 rounded-full bg-current" />}
          </span>
          <div className="min-w-0 flex-1 pb-1">
            <div className="flex flex-wrap items-baseline justify-between gap-x-3">
              <p className="text-sm font-medium">{it.title}</p>
              {it.time && <RelativeTime value={it.time} className="text-xs text-muted-foreground" />}
            </div>
            {it.description && <p className="mt-0.5 text-xs text-muted-foreground">{it.description}</p>}
          </div>
        </li>
      ))}
    </ol>
  );
}
