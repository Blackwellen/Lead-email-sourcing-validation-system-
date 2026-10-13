import * as React from 'react';
import Link from 'next/link';
import { ArrowDownRight, ArrowUpRight, Minus } from 'lucide-react';
import { Card } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { Tooltip } from '@/components/ui/tooltip';
import { formatNumber } from '@/lib/format';
import { cn } from '@/lib/utils';

export interface StatDelta {
  /** Signed change, already formatted (e.g. "+12%" or "−3"). */
  label: string;
  direction: 'up' | 'down' | 'flat';
  /** Whether this direction is good news; drives colour (icon + sign still convey direction). */
  good?: boolean | null;
}

/**
 * KPI tile. `value` may be a number (formatted) or node. `approximate` prefixes "~" and exposes a tooltip
 * (use whenever the backend page/overview says `approximate: true`).
 */
export function StatCard({ label, value, hint, delta, sparkline, approximate, loading, href, icon, className }: {
  label: React.ReactNode;
  value: number | string | React.ReactNode | null | undefined;
  hint?: React.ReactNode;
  delta?: StatDelta;
  sparkline?: React.ReactNode;
  approximate?: boolean;
  loading?: boolean;
  href?: string;
  icon?: React.ReactNode;
  className?: string;
}) {
  const shown = typeof value === 'number' ? formatNumber(value) : value ?? '—';
  const DeltaIcon = delta?.direction === 'up' ? ArrowUpRight : delta?.direction === 'down' ? ArrowDownRight : Minus;
  const body = (
    <Card className={cn('p-4 transition-colors', href && 'hover:border-primary/40', className)}>
      <div className="flex items-start justify-between gap-2">
        <p className="text-xs font-medium text-muted-foreground">{label}</p>
        {icon && <span className="text-muted-foreground [&_svg]:size-4" aria-hidden>{icon}</span>}
      </div>
      {loading ? <Skeleton className="mt-2 h-8 w-24" /> : (
        <div className="mt-1.5 flex items-end justify-between gap-3">
          <div className="min-w-0">
            <p className="tabular flex items-baseline gap-1 text-2xl font-semibold tracking-tight">
              {approximate && (
                <Tooltip content="Approximate – estimated from a capped or sampled count">
                  <span className="cursor-help text-muted-foreground" aria-label="approximately">~</span>
                </Tooltip>
              )}
              <span>{shown}</span>
            </p>
            {delta && (
              <p className={cn('mt-1 inline-flex items-center gap-0.5 text-xs font-medium', delta.good === true && 'text-success', delta.good === false && 'text-danger', (delta.good === null || delta.good === undefined) && 'text-muted-foreground')}>
                <DeltaIcon className="size-3.5" aria-hidden />
                <span className="sr-only">{delta.direction === 'up' ? 'Up' : delta.direction === 'down' ? 'Down' : 'No change'} </span>
                {delta.label}
              </p>
            )}
          </div>
          {sparkline && <div className="shrink-0">{sparkline}</div>}
        </div>
      )}
      {hint && <p className="mt-2 text-xs text-muted-foreground">{hint}</p>}
    </Card>
  );
  return href ? <Link href={href} className="block rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">{body}</Link> : body;
}
