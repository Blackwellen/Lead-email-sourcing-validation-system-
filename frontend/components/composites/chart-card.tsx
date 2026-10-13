'use client';
import * as React from 'react';
import { ResponsiveContainer } from 'recharts';
import { BarChart3, Table2 } from 'lucide-react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { cn } from '@/lib/utils';

/** Categorical palette (CSS-variable backed, themed for light and dark). Use in order; never rely on colour alone – add legends/labels. */
export const CHART_COLORS = [1, 2, 3, 4, 5, 6, 7, 8].map((n) => `hsl(var(--chart-${n}))`);
export const chartColor = (i: number) => CHART_COLORS[i % CHART_COLORS.length];
/** Props to spread on recharts axes / grids for consistent theming. */
export const chartAxisProps = { stroke: 'hsl(var(--muted-foreground))', fontSize: 11, tickLine: false, axisLine: { stroke: 'hsl(var(--border))' } } as const;
export const chartGridProps = { stroke: 'hsl(var(--chart-grid))', strokeDasharray: '3 3', vertical: false } as const;
export const chartTooltipStyle = {
  contentStyle: { background: 'hsl(var(--popover))', border: '1px solid hsl(var(--border))', borderRadius: 8, fontSize: 12, color: 'hsl(var(--popover-foreground))', boxShadow: 'var(--tw-shadow, none)' },
  labelStyle: { color: 'hsl(var(--muted-foreground))', fontWeight: 600 },
  itemStyle: { color: 'hsl(var(--popover-foreground))' },
} as const;

export interface ChartTable { columns: string[]; rows: (string | number | null)[][] }

/**
 * Card wrapper for a Recharts chart. Provides an accessible name + description, a "view as table" toggle
 * (data table fallback for screen readers and anyone who prefers numbers), loading/empty/error slots.
 * `children` must be a single Recharts chart element (it is wrapped in ResponsiveContainer).
 */
export function ChartCard({ title, description, children, table, height = 260, loading, empty, actions, footer, className }: {
  title: string;
  description?: string;
  children: React.ReactElement;
  /** Tabular equivalent of the chart data. */
  table?: ChartTable;
  height?: number;
  loading?: boolean;
  /** Render this instead of the chart when there is no data (e.g. <EmptyState compact …/>). */
  empty?: React.ReactNode;
  actions?: React.ReactNode;
  footer?: React.ReactNode;
  className?: string;
}) {
  const [asTable, setAsTable] = React.useState(false);
  const id = React.useId();
  return (
    <Card className={className} role="figure" aria-labelledby={`${id}-t`} aria-describedby={description ? `${id}-d` : undefined}>
      <CardHeader className="flex-row items-start justify-between gap-3 space-y-0">
        <div className="space-y-1">
          <CardTitle id={`${id}-t`}>{title}</CardTitle>
          {description && <CardDescription id={`${id}-d`}>{description}</CardDescription>}
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {actions}
          {table && !empty && (
            <Button variant="ghost" size="icon-sm" aria-pressed={asTable} aria-label={asTable ? 'Show chart' : 'Show data table'} onClick={() => setAsTable((v) => !v)}>
              {asTable ? <BarChart3 /> : <Table2 />}
            </Button>
          )}
        </div>
      </CardHeader>
      <CardContent>
        {loading ? <Skeleton style={{ height }} className="w-full" /> : empty ? empty : asTable && table ? (
          <div className="overflow-auto" style={{ maxHeight: height + 20 }}>
            <table className="w-full text-left text-sm">
              <caption className="sr-only">{title}</caption>
              <thead><tr>{table.columns.map((c) => <th key={c} scope="col" className="sticky top-0 border-b bg-card px-2 py-1.5 text-xs font-medium text-muted-foreground">{c}</th>)}</tr></thead>
              <tbody>{table.rows.map((r, i) => <tr key={i} className="border-b last:border-0">{r.map((c, j) => <td key={j} className={cn('px-2 py-1.5', j > 0 && 'tabular')}>{c ?? '—'}</td>)}</tr>)}</tbody>
            </table>
          </div>
        ) : (
          <div style={{ height }} aria-hidden={table ? true : undefined}>
            <ResponsiveContainer width="100%" height="100%">{children}</ResponsiveContainer>
          </div>
        )}
        {!asTable && table && !loading && !empty && (
          <table className="sr-only">
            <caption>{title}</caption>
            <thead><tr>{table.columns.map((c) => <th key={c} scope="col">{c}</th>)}</tr></thead>
            <tbody>{table.rows.map((r, i) => <tr key={i}>{r.map((c, j) => <td key={j}>{c ?? ''}</td>)}</tr>)}</tbody>
          </table>
        )}
        {footer && <div className="mt-3 text-xs text-muted-foreground">{footer}</div>}
      </CardContent>
    </Card>
  );
}
