import * as React from 'react';
import { cn } from '@/lib/utils';

/** Tiny dependency-free sparkline for StatCard's `sparkline` slot. Decorative unless `label` is set. */
export function Sparkline({ data, width = 96, height = 28, className, label }: { data: number[]; width?: number; height?: number; className?: string; label?: string }) {
  if (data.length < 2) return null;
  const min = Math.min(...data);
  const max = Math.max(...data);
  const span = max - min || 1;
  const pts = data.map((d, i) => [(i / (data.length - 1)) * (width - 2) + 1, height - 2 - ((d - min) / span) * (height - 4)] as const);
  const line = pts.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
  const area = `${line} L${width - 1},${height} L1,${height} Z`;
  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} className={cn('text-chart-1', className)} role={label ? 'img' : undefined} aria-label={label} aria-hidden={label ? undefined : true}>
      <path d={area} fill="currentColor" opacity={0.1} />
      <path d={line} fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}
