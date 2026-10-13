'use client';
import * as React from 'react';
import { formatDateTime, formatRelative } from '@/lib/format';

/** "5 minutes ago" with the absolute timestamp in the title / <time dateTime>. Refreshes every minute. */
export function RelativeTime({ value, className, fallback = '—' }: { value: string | Date | null | undefined; className?: string; fallback?: string }) {
  const [, tick] = React.useReducer((n: number) => n + 1, 0);
  React.useEffect(() => { const t = setInterval(tick, 60_000); return () => clearInterval(t); }, []);
  if (!value) return <span className={className}>{fallback}</span>;
  const iso = typeof value === 'string' ? value : value.toISOString();
  return (
    <time dateTime={iso} title={formatDateTime(value)} className={className} suppressHydrationWarning>
      {formatRelative(value)}
    </time>
  );
}
