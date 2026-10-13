'use client';
import * as React from 'react';
import { AlertTriangle, Ban, Info } from 'lucide-react';
import { LoadingState } from '@/components/composites/loading-state';
import { KeyValueList } from '@/components/composites/key-value-list';
import { formatNumber } from '@/lib/format';
import { cn } from '@/lib/utils';
import type { UseQueryResult } from '@tanstack/react-query';
import type { RunEstimate } from './use-estimate';

const SEV = {
  blocking: { Icon: Ban, cls: 'border-danger-border bg-danger-soft text-danger-foreground', label: 'Blocking' },
  warning: { Icon: AlertTriangle, cls: 'border-warning-border bg-warning-soft text-warning-foreground', label: 'Warning' },
  info: { Icon: Info, cls: 'border-info-border bg-info-soft text-info-foreground', label: 'Note' },
} as const;

export function Notice({ tone, children }: { tone: keyof typeof SEV; children: React.ReactNode }) {
  const { Icon, cls, label } = SEV[tone];
  return (
    <li className={cn('flex items-start gap-2 rounded-md border p-2.5 text-xs', cls)}>
      <Icon className="mt-0.5 size-3.5 shrink-0" aria-hidden /><span><span className="sr-only">{label}: </span>{children}</span>
    </li>
  );
}

/** Honest expectations from POST /api/runs/estimate: businesses needed, history-based yield, shortages, warnings and restrictions. */
export function EstimateSummary({ q, showInfra }: { q: UseQueryResult<RunEstimate>; showInfra?: boolean }) {
  if (q.isPending) return <LoadingState label="Estimating…" rows={3} />;
  if (q.isError) return <p role="alert" className="text-sm text-destructive">The estimate could not be computed: {q.error.message}</p>;
  const e = q.data;
  if (!e.valid) {
    return (
      <div className="space-y-2" aria-live="polite">
        <p className="text-sm text-muted-foreground">An estimate needs a complete configuration. Remaining problems:</p>
        <ul className="list-disc space-y-0.5 pl-5 text-xs">{(e.errors ?? []).map((x, i) => <li key={i}><span className="font-mono">{x.field || 'config'}</span>: {x.message}</li>)}</ul>
      </div>
    );
  }
  const y = e.history as { emails_per_organisation?: number | null; published_emails?: number; organisations_with_domain?: number };
  const infra = e.infra as Record<string, number | undefined>;
  return (
    <div className="space-y-3" aria-live="polite">
      <KeyValueList columns={2} items={[
        { label: 'Businesses likely needed', value: <>{e.estimated_businesses_needed != null ? `~${formatNumber(e.estimated_businesses_needed)}` : 'Unknown'}<span className="block text-xs text-muted-foreground">{e.estimated_businesses_needed == null ? 'Not enough historical data to estimate the yield per business.' : y.emails_per_organisation ? `Based on ${y.emails_per_organisation} published emails per business seen so far.` : ''}</span></> },
        { label: 'Estimated page requests', value: e.estimated_requests != null ? `~${formatNumber(e.estimated_requests)}` : 'Unknown' },
        ...(showInfra ? [
          { label: 'Worker processes needed / online', value: `${infra.worker_processes_needed ?? '—'} / ${infra.worker_processes_online ?? '—'}` },
          { label: 'Concurrent fetches', value: formatNumber(infra.effective_http_concurrency) },
          { label: 'Estimated RAM', value: `${formatNumber(infra.estimated_ram_mb)} MB (available ${formatNumber(infra.available_ram_mb)} MB)` },
          { label: 'Estimated result storage', value: `${formatNumber(infra.estimated_disk_mb)} MB (free ${formatNumber(infra.free_disk_mb)} MB)` },
        ] : []),
      ]} />
      <ul className="space-y-1.5">
        {(e.shortages ?? []).map((s, i) => <Notice key={`s${i}`} tone="warning"><strong>Possible shortage ({s.resource.replaceAll('_', ' ')}):</strong> {s.message} Need {formatNumber(s.required)}, available {formatNumber(s.available)}.</Notice>)}
        {(e.restrictions ?? []).map((r) => <Notice key={r.code} tone={r.severity === 'blocking' ? 'blocking' : r.severity === 'warning' ? 'warning' : 'info'}>{r.message}</Notice>)}
        {(e.warnings ?? []).map((w, i) => <Notice key={`w${i}`} tone="info">{w}</Notice>)}
      </ul>
      {(e.assumptions ?? []).length > 0 && (
        <details className="text-xs text-muted-foreground">
          <summary className="cursor-pointer text-foreground">Assumptions behind the estimate</summary>
          <ul className="ml-4 mt-1 list-disc space-y-0.5">{(e.assumptions ?? []).map((a, i) => <li key={i}>{a}</li>)}</ul>
        </details>
      )}
    </div>
  );
}
