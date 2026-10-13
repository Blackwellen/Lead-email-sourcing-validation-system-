import * as React from 'react';
import { cn } from '@/lib/utils';

export function confidenceBand(v: number) {
  return v >= 75 ? { label: 'High', tone: 'bg-success' } : v >= 45 ? { label: 'Medium', tone: 'bg-warning' } : { label: 'Low', tone: 'bg-danger' };
}

/** Confidence 0–100 as a 5-segment meter plus numeric value and a text band (never colour-only). */
export function ConfidenceMeter({ value, max = 100, showValue = true, showLabel = true, className }: {
  value: number | null | undefined; max?: number; showValue?: boolean; showLabel?: boolean; className?: string;
}) {
  if (value === null || value === undefined || Number.isNaN(value)) return <span className="text-muted-foreground">—</span>;
  const pct = Math.max(0, Math.min(100, (value / max) * 100));
  const band = confidenceBand(pct);
  const filled = Math.max(pct > 0 ? 1 : 0, Math.round(pct / 20));
  return (
    <span role="meter" aria-valuemin={0} aria-valuemax={max} aria-valuenow={value} aria-valuetext={`${band.label} confidence, ${Math.round(pct)} percent`} className={cn('inline-flex items-center gap-2', className)}>
      <span className="flex gap-0.5" aria-hidden>
        {[0, 1, 2, 3, 4].map((i) => <span key={i} className={cn('h-3 w-1.5 rounded-[2px]', i < filled ? band.tone : 'bg-muted')} />)}
      </span>
      {showValue && <span className="tabular text-xs font-medium">{Math.round(pct)}</span>}
      {showLabel && <span className="text-xs text-muted-foreground">{band.label}</span>}
    </span>
  );
}
