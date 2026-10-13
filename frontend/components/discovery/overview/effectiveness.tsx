'use client';
import * as React from 'react';
import { useQuery } from '@tanstack/react-query';
import { Gauge } from 'lucide-react';
import { api, type ApiError, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { formatNumber } from '@/lib/format';
import { EmptyState } from '@/components/composites/empty-state';
import { ErrorState } from '@/components/composites/error-state';
import { LoadingState } from '@/components/composites/loading-state';
import { Badge } from '@/components/ui/badge';
import { SimpleSelect } from '@/components/ui/select';
import { Tooltip } from '@/components/ui/tooltip';
import { ScrollRegion, tdClass, thClass } from '@/components/system/scroll-region';
import { Measured, SectionTitle } from '../shared';

type Eff = Schema<'EffectivenessOut'>;
type Metrics = Schema<'MetricsOut'>;

/** Columns in the order the product spec lists them. Each value is a measured ratio or null (never estimated). */
const COLS: { key: string; label: string; help: string; suffix?: string; digits?: number }[] = [
  { key: 'unique_businesses_per_1000_records', label: 'Unique businesses / 1,000 records', help: 'New distinct businesses per 1,000 records read' },
  { key: 'confirmed_domains_per_1000_businesses', label: 'Confirmed domains / 1,000 businesses', help: 'Businesses whose official website was confirmed' },
  { key: 'published_emails_per_1000_businesses', label: 'Published e-mails / 1,000 businesses', help: 'Published or corroborated addresses found' },
  { key: 'eligible_contacts_per_1000_businesses', label: 'Eligible contacts / 1,000 businesses', help: 'Contacts eligible under the marketing policy' },
  { key: 'http_requests_per_new_contact', label: 'HTTP requests per new contact', help: 'Fetch cost of each eligible contact' },
  { key: 'cpu_seconds_per_job', label: 'CPU s / job', help: 'Average CPU seconds per job' },
  { key: 'peak_rss_mb', label: 'Peak memory (MB)', help: 'Highest resident memory observed' },
  { key: 'duplicate_pct', label: 'Duplicate %', help: 'Records that matched an existing business', suffix: '%' },
  { key: 'failure_pct', label: 'Failure %', help: 'Jobs that failed', suffix: '%' },
  { key: 'new_contacts_per_processing_hour', label: 'New contacts / hour', help: 'Eligible contacts per hour of processing' },
];

export function EffectivenessSection() {
  const [run, setRun] = React.useState('all');
  const q = useQuery<Eff, ApiError>({
    queryKey: qk.custom('discovery', 'effectiveness', { run }),
    queryFn: ({ signal }) => api.get<Eff>('/api/discovery/effectiveness', { query: { run_id: run === 'all' ? undefined : run, runs: 10 }, signal }),
  });
  if (q.isPending) return <LoadingState rows={6} label="Loading source effectiveness" />;
  if (q.isError) return <ErrorState error={q.error} onRetry={() => q.refetch()} />;
  const rows: Metrics[] = run === 'all' ? q.data.all_time : q.data.run ?? [];
  return (
    <section aria-labelledby="eff-h" className="space-y-3">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <SectionTitle id="eff-h" description={<>Measured per source from stored counters. A ratio needs at least {q.data.min_samples} samples in its denominator; below that it shows “— not enough data” instead of a guess.</>}>Source effectiveness</SectionTitle>
        <SimpleSelect aria-label="Measurement scope" className="w-auto min-w-48" value={run} onValueChange={setRun}
          options={[{ value: 'all', label: 'All time' }, ...q.data.recent_runs.map((r) => ({ value: String(r.run_id), label: `Run #${String(r.run_id)}${r.name ? ` · ${String(r.name)}` : ''}` }))]} />
      </div>
      {rows.length === 0 ? <EmptyState compact icon={Gauge} title="No measurements yet" description="Effectiveness counters are recorded as runs process sources." /> : (
        <ScrollRegion label="Source effectiveness table" className="rounded-lg border bg-card">
          <table className="w-full min-w-[72rem] border-collapse text-left text-sm" data-testid="effectiveness-table">
            <caption className="sr-only">Source effectiveness ({run === 'all' ? 'all time' : `run ${run}`})</caption>
            <thead>
              <tr>
                <th scope="col" className={thClass}>Source</th>
                <th scope="col" className={`${thClass} text-right`}>Records seen</th>
                {COLS.map((c) => <th key={c.key} scope="col" className={`${thClass} whitespace-normal text-right`}><Tooltip content={c.help}><span tabIndex={0} className="cursor-help underline decoration-dotted underline-offset-2">{c.label}</span></Tooltip></th>)}
              </tr>
            </thead>
            <tbody>
              {rows.map((m) => (
                <tr key={`${m.adapter_id}-${m.run_id}`} data-testid={`eff-row-${m.adapter_id}`}>
                  <th scope="row" className={`${tdClass} font-mono text-xs font-medium`}>{m.adapter_id ?? 'all sources'}</th>
                  <td className={`${tdClass} text-right tabular`}>{formatNumber(m.counters.records_seen ?? 0)}</td>
                  {COLS.map((c) => <td key={c.key} className={`${tdClass} text-right`}><Measured value={m.derived[c.key]} minSample={m.min_samples} suffix={c.suffix} digits={c.digits ?? 1} /></td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </ScrollRegion>
      )}
    </section>
  );
}

type Sched = Schema<'SchedulerOut'>;
export function SchedulerSection() {
  const q = useQuery<Sched, ApiError>({ queryKey: qk.custom('discovery', 'scheduler'), queryFn: ({ signal }) => api.get<Sched>('/api/discovery/scheduler', { signal }) });
  if (q.isPending) return <LoadingState rows={6} label="Loading scheduler priorities" />;
  if (q.isError) return <ErrorState error={q.error} onRetry={() => q.refetch()} />;
  const num = (v: unknown) => (typeof v === 'number' ? v.toLocaleString('en-US', { maximumFractionDigits: 3 }) : null);
  return (
    <section aria-labelledby="sched-h" className="space-y-3">
      <SectionTitle id="sched-h" description="Order in which runnable sources are tried: static source hierarchy × measured information gain, with an exploration bonus for sources that have few samples. Penalties apply for duplicates and failures.">Scheduler priorities</SectionTitle>
      {q.data.rows.length === 0 ? <EmptyState compact title="No runnable sources" /> : (
        <ScrollRegion label="Scheduler priorities table" className="rounded-lg border bg-card">
          <table className="w-full min-w-[56rem] border-collapse text-left text-sm" data-testid="scheduler-table">
            <caption className="sr-only">Scheduler priority per source, highest first</caption>
            <thead>
              <tr>
                <th scope="col" className={thClass}>#</th><th scope="col" className={thClass}>Source</th><th scope="col" className={thClass}>State</th>
                <th scope="col" className={`${thClass} text-right`}>Priority score</th><th scope="col" className={`${thClass} text-right`}>Static priority</th>
                <th scope="col" className={`${thClass} text-right`}>Samples</th><th scope="col" className={thClass}>Why</th>
              </tr>
            </thead>
            <tbody>
              {q.data.rows.map((r, i) => {
                const e = r.explanation as Record<string, unknown>;
                return (
                  <tr key={r.adapter_id}>
                    <td className={`${tdClass} tabular text-muted-foreground`}>{i + 1}</td>
                    <th scope="row" className={`${tdClass} font-medium`}>{r.name}<span className="block font-mono text-2xs font-normal text-muted-foreground">{r.adapter_id}</span></th>
                    <td className={tdClass}>{r.enabled ? <Badge variant="success">Enabled</Badge> : <Badge variant="outline">Disabled</Badge>}</td>
                    <td className={`${tdClass} text-right font-semibold tabular`}>{num(r.priority_score)}</td>
                    <td className={`${tdClass} text-right tabular`}>{r.static_priority}<span className="block text-2xs text-muted-foreground">score {num(r.static_score)}</span></td>
                    <td className={`${tdClass} text-right tabular`}>{formatNumber(r.samples)}</td>
                    <td className={`${tdClass} text-xs text-muted-foreground`}>
                      gain {num(e.gain) ?? '—'} · exploration {num(e.exploration) ?? '—'} · duplicates ×{num(e.duplicate_multiplier) ?? '—'} · failures ×{num(e.failure_multiplier) ?? '—'}
                      {r.samples === 0 && <span className="block">No measured samples yet – ranked by hierarchy and exploration only.</span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </ScrollRegion>
      )}
    </section>
  );
}
