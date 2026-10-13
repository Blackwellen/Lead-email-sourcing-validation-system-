'use client';
import * as React from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowRight, Boxes, Check, CircleX, Clock, Cog, Globe, Radar, ScanSearch, ShieldCheck } from 'lucide-react';
import { CartesianGrid, Line, LineChart, Legend, Tooltip as RTooltip, XAxis, YAxis, Area, ComposedChart } from 'recharts';
import { api, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { listFetcher } from '@/hooks/use-paged-query';
import { toast } from '@/lib/toast';
import { Can } from '@/lib/auth';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Label } from '@/components/ui/label';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { ChartCard, chartAxisProps, chartColor, chartGridProps, chartTooltipStyle } from '@/components/composites/chart-card';
import { DataTable, createColumnHelper } from '@/components/composites/data-table';
import { StatusBadge } from '@/components/composites/status-badge';
import { ConfidenceMeter } from '@/components/composites/confidence-meter';
import { RelativeTime } from '@/components/composites/relative-time';
import { EmptyState } from '@/components/composites/empty-state';
import { ErrorState } from '@/components/composites/error-state';
import { LoadingState } from '@/components/composites/loading-state';
import { KeyValueList } from '@/components/composites/key-value-list';
import { formatNumber, formatPercent, titleCase } from '@/lib/format';
import { cn } from '@/lib/utils';
import type { LivePoint } from './use-run-live';
import type { RunDetail } from './run-actions';

/* ------------------------------------------------------------------ pipeline */
const PIPE = [
  { id: 'discovery', label: 'Discovery', stages: ['discover'], icon: Radar, note: 'Businesses found by sources' },
  { id: 'domains', label: 'Domains', stages: ['import_domain', 'resolve_domain'], icon: Globe, note: 'Websites imported or matched' },
  { id: 'crawl', label: 'Crawl', stages: ['crawl'], icon: Boxes, note: 'Pages fetched from company sites' },
  { id: 'extraction', label: 'Extraction', stages: [] as string[], icon: ScanSearch, note: 'Done inside the crawl step' },
  { id: 'validation', label: 'Validation', stages: ['verify', 'revalidate'], icon: Cog, note: 'Addresses checked' },
  { id: 'qualified', label: 'Qualified', stages: [] as string[], icon: ShieldCheck, note: 'Counted toward the target' },
] as const;

export function Pipeline({ d }: { d: RunDetail }) {
  const counters = d.counters as Record<string, number | undefined>;
  const by = (stages: readonly string[], states: string[]) => d.funnel.filter((f) => stages.includes(f.stage) && states.includes(f.state)).reduce((a, f) => a + f.count, 0);
  return (
    <section aria-label="Pipeline" className="rounded-lg border bg-card p-4">
      <h2 className="mb-3 text-base font-semibold">Pipeline</h2>
      <ol className="grid gap-3 sm:grid-cols-3 xl:grid-cols-6">
        {PIPE.map((p, i) => {
          const done = by(p.stages, ['done']), wip = by(p.stages, ['queued', 'leased', 'paused']), failed = by(p.stages, ['failed', 'dead']);
          const headline = p.id === 'discovery' ? counters.companies : p.id === 'domains' ? counters.companies_with_domains : p.id === 'extraction' ? counters.emails_found : p.id === 'qualified' ? counters.qualified : done;
          const headlineLabel = p.id === 'discovery' ? 'businesses' : p.id === 'domains' ? 'with verified site' : p.id === 'extraction' ? 'emails found' : p.id === 'qualified' ? 'qualified' : 'jobs done';
          return (
            <li key={p.id} className="relative rounded-md border p-3">
              <div className="flex items-center gap-2 text-sm font-medium"><p.icon className="size-4 text-muted-foreground" aria-hidden /> {p.label}
                {i < PIPE.length - 1 && <ArrowRight className="ml-auto size-4 text-muted-foreground max-xl:hidden" aria-hidden />}
              </div>
              <p className="tabular mt-2 text-2xl font-semibold">{headline === undefined ? '—' : formatNumber(headline)}</p>
              <p className="text-xs text-muted-foreground">{headlineLabel}</p>
              {p.stages.length > 0 && (
                <p className="tabular mt-2 flex flex-wrap gap-x-3 text-xs text-muted-foreground">
                  <span className="inline-flex items-center gap-1"><Clock className="size-3" aria-hidden />{formatNumber(wip)} waiting or running</span>
                  <span className={cn('inline-flex items-center gap-1', failed > 0 && 'font-medium text-danger')}><CircleX className="size-3" aria-hidden />{formatNumber(failed)} failed</span>
                </p>
              )}
              <p className="mt-1 text-2xs text-muted-foreground">{p.note}</p>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

/* ------------------------------------------------------------------ charts */
const clock = (t: number) => new Date(t).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
const minuteClock = (t: number) => new Date(t).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });

export function PerformanceCharts({ d, buffer }: { d: RunDetail; buffer: LivePoint[] }) {
  // server history: per-minute completed jobs (all stages) + qualified results
  const hist = React.useMemo(() => {
    const m = new Map<number, { t: number; serverJobs: number; serverQualified: number }>();
    for (const p of d.throughput.series) {
      const t = Date.parse(p.minute);
      const cur = m.get(t) ?? { t, serverJobs: 0, serverQualified: 0 };
      cur.serverJobs += p.completed;
      m.set(t, cur);
    }
    for (const y of d.throughput.yield_series as { minute: string; count: number }[]) {
      const t = Date.parse(y.minute);
      const cur = m.get(t) ?? { t, serverJobs: 0, serverQualified: 0 };
      cur.serverQualified += y.count;
      m.set(t, cur);
    }
    return [...m.values()];
  }, [d.throughput]);
  const merged = React.useMemo(() => [
    ...hist.map((h) => ({ ...h })),
    ...buffer.map((b) => ({ t: b.t, liveJobs: b.jobsPerMin, liveQualified: b.qualifiedPerMin })),
  ].sort((a, b) => a.t - b.t), [hist, buffer]);
  const noLive = buffer.length === 0;
  const liveNote = 'Live points are accumulated in your browser from the event stream since this page was opened (bounded buffer); the server keeps no queue-length or failure history.';
  return (
    <section aria-label="Performance" className="grid gap-4 lg:grid-cols-3">
      <ChartCard
        title="Throughput"
        description="Jobs finished and results qualified per minute"
        height={240}
        table={{ columns: ['Time', 'Jobs/min (history)', 'Qualified/min (history)', 'Jobs/min (live)', 'Qualified/min (live)'], rows: merged.map((r) => [minuteClock(r.t), 'serverJobs' in r ? (r as { serverJobs: number }).serverJobs : null, 'serverQualified' in r ? (r as { serverQualified: number }).serverQualified : null, 'liveJobs' in r ? (r as { liveJobs: number }).liveJobs : null, 'liveQualified' in r ? (r as { liveQualified: number }).liveQualified : null]) }}
        empty={merged.length === 0 ? <EmptyState compact icon={Clock} title="No throughput yet" description="Per-minute history appears once jobs finish." /> : undefined}
        footer="History comes from the server (last 30 minutes). Live points are rolling one-minute counts."
      >
        <ComposedChart data={merged} margin={{ left: 0, right: 8, top: 8 }}>
          <CartesianGrid {...chartGridProps} />
          <XAxis dataKey="t" type="number" domain={['dataMin', 'dataMax']} scale="time" tickFormatter={minuteClock} {...chartAxisProps} />
          <YAxis {...chartAxisProps} allowDecimals={false} width={36} />
          <RTooltip {...chartTooltipStyle} labelFormatter={(t) => clock(Number(t))} />
          <Legend />
          <Area type="monotone" dataKey="serverJobs" name="Jobs/min (history)" stroke={chartColor(0)} fill={chartColor(0)} fillOpacity={0.12} connectNulls isAnimationActive={false} />
          <Area type="monotone" dataKey="serverQualified" name="Qualified/min (history)" stroke={chartColor(2)} fill={chartColor(2)} fillOpacity={0.12} connectNulls isAnimationActive={false} />
          <Line type="monotone" dataKey="liveJobs" name="Jobs/min (live)" stroke={chartColor(0)} strokeDasharray="4 3" dot={false} connectNulls isAnimationActive={false} />
          <Line type="monotone" dataKey="liveQualified" name="Qualified/min (live)" stroke={chartColor(2)} strokeDasharray="4 3" dot={false} connectNulls isAnimationActive={false} />
        </ComposedChart>
      </ChartCard>
      <ChartCard
        title="Success and failure"
        description="Cumulative finished and failed jobs (live)"
        height={240}
        table={{ columns: ['Time', 'Done', 'Failed'], rows: buffer.map((b) => [clock(b.t), b.done, b.failed]) }}
        empty={noLive ? <EmptyState compact icon={Clock} title="Waiting for live data" description="Points appear as the event stream delivers snapshots." /> : undefined}
        footer={liveNote}
      >
        <LineChart data={buffer} margin={{ left: 0, right: 8, top: 8 }}>
          <CartesianGrid {...chartGridProps} />
          <XAxis dataKey="t" type="number" domain={['dataMin', 'dataMax']} scale="time" tickFormatter={clock} {...chartAxisProps} />
          <YAxis {...chartAxisProps} allowDecimals={false} width={36} />
          <RTooltip {...chartTooltipStyle} labelFormatter={(t) => clock(Number(t))} />
          <Legend />
          <Line type="monotone" dataKey="done" name="Done" stroke={chartColor(2)} dot={false} strokeWidth={2} isAnimationActive={false} />
          <Line type="monotone" dataKey="failed" name="Failed" stroke={chartColor(4)} strokeDasharray="5 3" dot={false} strokeWidth={2} isAnimationActive={false} />
        </LineChart>
      </ChartCard>
      <ChartCard
        title="Queue length"
        description="Jobs waiting and running (live)"
        height={240}
        table={{ columns: ['Time', 'Waiting', 'Running'], rows: buffer.map((b) => [clock(b.t), b.queued, b.leased]) }}
        empty={noLive ? <EmptyState compact icon={Clock} title="Waiting for live data" description="Points appear as the event stream delivers snapshots." /> : undefined}
        footer={liveNote}
      >
        <LineChart data={buffer} margin={{ left: 0, right: 8, top: 8 }}>
          <CartesianGrid {...chartGridProps} />
          <XAxis dataKey="t" type="number" domain={['dataMin', 'dataMax']} scale="time" tickFormatter={clock} {...chartAxisProps} />
          <YAxis {...chartAxisProps} allowDecimals={false} width={36} />
          <RTooltip {...chartTooltipStyle} labelFormatter={(t) => clock(Number(t))} />
          <Legend />
          <Line type="stepAfter" dataKey="queued" name="Waiting" stroke={chartColor(0)} dot={false} strokeWidth={2} isAnimationActive={false} />
          <Line type="stepAfter" dataKey="leased" name="Running" stroke={chartColor(3)} strokeDasharray="5 3" dot={false} strokeWidth={2} isAnimationActive={false} />
        </LineChart>
      </ChartCard>
    </section>
  );
}

/* ------------------------------------------------------------------ requested vs actual */
export function ThroughputPanel({ d }: { d: RunDetail }) {
  const cfg = (d.config?.performance ?? {}) as Record<string, number | null | undefined>;
  const per = d.throughput.per_stage_per_minute;
  const perHour = (x: number | undefined) => (x === undefined ? 'no jobs finished in window' : `${formatNumber(Math.round(x * 60 * 10) / 10)} / h`);
  const conc = d.effective_concurrency as Record<string, number | string | null | undefined>;
  const requested = [
    cfg.desired_websites_per_hour && `${formatNumber(cfg.desired_websites_per_hour)} websites/h`,
    cfg.desired_businesses_per_hour && `${formatNumber(cfg.desired_businesses_per_hour)} businesses/h`,
    cfg.desired_records_per_hour && `${formatNumber(cfg.desired_records_per_hour)} records/h`,
  ].filter(Boolean).join(', ');
  return (
    <Card>
      <CardHeader><CardTitle>Requested vs actual throughput</CardTitle><CardDescription>Desired rates are wishes; measured rates come from finished jobs in the last {d.throughput.window_minutes} minutes.</CardDescription></CardHeader>
      <CardContent className="space-y-4">
        <KeyValueList columns={2} items={[
          { label: 'Requested', value: requested || 'No target set' },
          { label: 'Measured: qualified results', value: `${formatNumber(Math.round(d.throughput.yield_per_minute * 60 * 10) / 10)} / h` },
          ...Object.entries(per).map(([stage, v]) => ({ label: `Measured: ${titleCase(stage)} jobs`, value: perHour(v) })),
        ]} />
        <div>
          <h3 className="text-sm font-medium">Limiting stage</h3>
          <p className="mt-0.5 text-sm text-muted-foreground">{d.limiting_stage ? <><strong className="text-foreground">{titleCase(d.limiting_stage)}</strong>. {d.limiting_reason}</> : 'No backlog, so no stage is currently limiting.'}</p>
        </div>
        <div>
          <h3 className="text-sm font-medium">Queue backlog by stage</h3>
          {Object.keys(d.queue_backlog).length === 0 ? <p className="text-sm text-muted-foreground">Nothing waiting.</p> : (
            <ul className="mt-1 flex flex-wrap gap-2">{Object.entries(d.queue_backlog).map(([s, n]) => <li key={s} className="tabular rounded-md border px-2 py-1 text-xs">{titleCase(s)}: {formatNumber(n)}</li>)}</ul>
          )}
        </div>
        <div>
          <h3 className="text-sm font-medium">Effective concurrency</h3>
          <KeyValueList columns={2} className="mt-1" items={[
            { label: 'Workers online', value: String(conc.workers_online ?? 0) },
            { label: 'Worker slots', value: String(conc.worker_slots_total ?? 0) },
            { label: 'Jobs running now', value: String(conc.jobs_running_now ?? 0) },
            { label: 'Effective limit (lowest of worker slots, HTTP concurrency, worker limit)', value: String(conc.effective ?? 0) },
            { label: 'Configured HTTP concurrency', value: conc.configured_http_concurrency ? String(conc.configured_http_concurrency) : `from ${String(conc.preset ?? 'preset')} preset` },
            { label: 'Per-host limit', value: String(conc.configured_per_host ?? '—') },
          ]} />
        </div>
        <div>
          <h3 className="text-sm font-medium">Rate-limited sources</h3>
          {d.rate_limited_sources.length === 0 ? <p className="text-sm text-muted-foreground">None reported.</p> : (
            <ul className="mt-1 space-y-1 text-sm">{d.rate_limited_sources.map((r) => <li key={r.source}><span className="font-mono text-xs">{r.source}</span>: {r.jobs} job{r.jobs === 1 ? '' : 's'} ({r.last_error_code})</li>)}</ul>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

/* ------------------------------------------------------------------ current work */
export function CurrentWork({ id, active }: { id: number; active: boolean }) {
  const q = useQuery({
    queryKey: qk.custom('runs', 'current-work', id),
    queryFn: ({ signal }) => api.get<{ items: Schema<'JobRow'>[] }>(`/api/runs/${id}/jobs`, { query: { state: 'leased', limit: 10 }, signal }),
    refetchInterval: active ? 4000 : false,
  });
  return (
    <Card>
      <CardHeader><CardTitle>Current work</CardTitle><CardDescription>Jobs being processed right now</CardDescription></CardHeader>
      <CardContent>
        {q.isPending ? <LoadingState label="Loading current work…" rows={3} /> : q.isError ? <ErrorState compact error={q.error} onRetry={() => q.refetch()} /> : q.data.items.length === 0 ? (
          <EmptyState compact icon={Cog} title="Nothing is running" description={active ? 'No job is leased right now. Without a running worker, queued jobs wait.' : 'This run has no jobs in progress.'} />
        ) : (
          <ul className="divide-y">
            {q.data.items.map((j) => {
              const p = j.payload as Record<string, unknown>;
              const what = String(p.domain ?? p.adapter_id ?? p.organisation_id ?? j.dedupe_key);
              return (
                <li key={j.id} className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm">
                  <span className="min-w-0"><span className="font-medium">{titleCase(j.stage)}</span> <span className="break-all font-mono text-xs text-muted-foreground">{what}</span></span>
                  <span className="text-xs text-muted-foreground">attempt {j.attempts} · started <RelativeTime value={j.started_at} /></span>
                </li>
              );
            })}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

/* ------------------------------------------------------------------ contact quality */
type ContactRow = Schema<'ContactRow'>;
function QualityGroup({ title, rows, kind, n }: { title: string; rows: [string, number][]; kind: 'origin' | 'reachability' | 'catchall'; n: number }) {
  return (
    <div>
      <h3 className="text-sm font-medium">{title}</h3>
      <ul className="mt-1.5 space-y-1">
        {rows.map(([k, c]) => (
          <li key={k} className="flex items-center justify-between gap-2 text-sm"><StatusBadge kind={kind} status={k} /><span className="tabular">{formatNumber(c)} <span className="text-xs text-muted-foreground">({formatPercent((100 * c) / n, 0)})</span></span></li>
        ))}
      </ul>
    </div>
  );
}
export function ContactQuality({ id }: { id: number }) {
  const q = useQuery({
    queryKey: qk.custom('runs', 'quality', id),
    queryFn: ({ signal }) => api.get<{ items: ContactRow[]; next_cursor: string | null }>(`/api/runs/${id}/contacts`, { query: { qualified_only: true, limit: 500 }, signal }),
    refetchInterval: 15_000,
  });
  const tally = (key: (c: ContactRow) => string) => {
    const m = new Map<string, number>();
    for (const c of q.data?.items ?? []) m.set(key(c), (m.get(key(c)) ?? 0) + 1);
    return [...m.entries()].sort((a, b) => b[1] - a[1]);
  };
  const n = q.data?.items.length ?? 0;
  const partial = !!q.data?.next_cursor;
  return (
    <Card>
      <CardHeader><CardTitle>Contact quality</CardTitle><CardDescription>Qualified contacts of this run</CardDescription></CardHeader>
      <CardContent>
        {q.isPending ? <LoadingState label="Loading contacts…" rows={3} /> : q.isError ? <ErrorState compact error={q.error} onRetry={() => q.refetch()} /> : n === 0 ? (
          <EmptyState compact icon={ShieldCheck} title="No qualified contacts yet" />
        ) : (
          <div className="space-y-4">
            {partial && <p className="text-xs text-muted-foreground">Approximate: based on the first {formatNumber(n)} qualified contacts only.</p>}
            <div className="grid gap-4 sm:grid-cols-3">
              <QualityGroup n={n} title="Published vs inferred" kind="origin" rows={tally((c) => c.origin)} />
              <QualityGroup n={n} title="Reachability" kind="reachability" rows={tally((c) => c.overall_reachability)} />
              <QualityGroup n={n} title="Catch-all" kind="catchall" rows={tally((c) => c.catch_all_status)} />
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

/* ------------------------------------------------------------------ failures */
const jobCol = createColumnHelper<Schema<'JobRow'>>();
export function FailuresTable({ id, active }: { id: number; active: boolean }) {
  const qc = useQueryClient();
  const retry = useMutation({
    mutationFn: (jobId: number) => api.post(`/api/runs/${id}/jobs/${jobId}/retry`),
    onSuccess: () => { toast.success('Job re-queued'); void qc.invalidateQueries({ queryKey: qk.custom('runs', 'failures', id) }); void qc.invalidateQueries({ queryKey: qk.detail('runs', id) }); },
    onError: (e: Error) => toast.error('Could not retry job', e.message),
  });
  const columns = React.useMemo(() => [
    jobCol.accessor('stage', { header: 'Stage', enableSorting: false, cell: (i) => titleCase(i.getValue()) }),
    jobCol.accessor('error_code', { header: 'Error', enableSorting: false, cell: (i) => <span className="font-mono text-xs">{i.getValue() ?? 'unknown'}</span> }),
    jobCol.accessor('error_detail', { header: 'Detail', enableSorting: false, meta: { hideBelow: 'md' }, cell: (i) => <span className="block max-w-md truncate text-xs text-muted-foreground" title={i.getValue() ?? ''}>{i.getValue() ?? '—'}</span> }),
    jobCol.accessor('host_key', { header: 'Target', enableSorting: false, meta: { hideBelow: 'lg' }, cell: (i) => <span className="break-all font-mono text-xs">{i.getValue() ?? String((i.row.original.payload as Record<string, unknown>).domain ?? '—')}</span> }),
    jobCol.accessor('attempts', { header: 'Attempts', enableSorting: false, meta: { hideBelow: 'sm' }, cell: (i) => <span className="tabular">{i.getValue()}</span> }),
    jobCol.accessor('state', { header: 'State', enableSorting: false, cell: (i) => <StatusBadge kind="job" status={i.getValue()} /> }),
    jobCol.display({
      id: 'retry', header: () => <span className="sr-only">Retry</span>,
      cell: (i) => (
        <Can role="operator">
          <Button size="sm" variant="outline" loading={retry.isPending && retry.variables === i.row.original.id} aria-label={`Retry job ${i.row.original.id}`} onClick={() => retry.mutate(i.row.original.id)}>Retry</Button>
        </Can>
      ),
    }),
  ], [retry]);
  return (
    <section aria-label="Failures" className="space-y-2">
      <h2 className="text-base font-semibold">Failures</h2>
      <DataTable<Schema<'JobRow'>>
        caption="Failed jobs"
        tableId="run-failures"
        columns={columns}
        queryKey={qk.custom('runs', 'failures', id)}
        fetchPage={listFetcher<Schema<'JobRow'>>(`/api/runs/${id}/jobs`)}
        filters={{ state: 'failed,dead' }}
        defaultPageSize={25}
        pageSizes={[25, 50, 100]}
        refetchInterval={active ? 8000 : false}
        maxHeightClass="max-h-96"
        emptyState={<EmptyState compact icon={Check} title="No failed jobs" description="Failed and dead jobs of this run appear here with their error code." />}
      />
    </section>
  );
}

/* ------------------------------------------------------------------ results */
const cCol = createColumnHelper<ContactRow>();
const oCol = createColumnHelper<Schema<'OrgRow'>>();
export function ResultsTabs({ id }: { id: number }) {
  const [tab, setTab] = React.useState('contacts');
  const [qualified, setQualified] = React.useState(true);
  const contactCols = React.useMemo(() => [
    cCol.accessor('email', { header: 'Email', enableSorting: false, cell: (i) => <span className="break-all font-medium">{i.getValue()}</span> }),
    cCol.accessor('company_name', { header: 'Business', enableSorting: false, meta: { hideBelow: 'md' }, cell: (i) => i.getValue() ?? '—' }),
    cCol.accessor('origin', { header: 'Origin', enableSorting: false, meta: { hideBelow: 'sm' }, cell: (i) => <StatusBadge kind="origin" status={i.getValue()} /> }),
    cCol.accessor('overall_reachability', { header: 'Reachability', enableSorting: false, cell: (i) => <StatusBadge kind="reachability" status={i.getValue()} /> }),
    cCol.accessor('confidence', { header: 'Confidence', enableSorting: false, meta: { hideBelow: 'lg' }, cell: (i) => <ConfidenceMeter value={i.getValue()} /> }),
  ], []);
  const orgCols = React.useMemo(() => [
    oCol.accessor('name', { header: 'Business', enableSorting: false, cell: (i) => <span className="font-medium">{i.getValue() ?? `#${i.row.original.organisation_id}`}</span> }),
    oCol.accessor('country_code', { header: 'Country', enableSorting: false, cell: (i) => i.getValue() }),
    oCol.accessor('city', { header: 'City', enableSorting: false, meta: { hideBelow: 'md' }, cell: (i) => i.getValue() ?? '—' }),
    oCol.accessor('stage', { header: 'Stage', enableSorting: false, cell: (i) => titleCase(i.getValue()) }),
    oCol.accessor('discovery_source', { header: 'Source', enableSorting: false, meta: { hideBelow: 'lg' }, cell: (i) => i.getValue() ?? '—' }),
  ], []);
  return (
    <section aria-label="Results" className="space-y-2">
      <h2 className="text-base font-semibold">Results</h2>
      <Tabs value={tab} onValueChange={setTab}>
        <TabsList aria-label="Result type">
          <TabsTrigger value="contacts">Contacts</TabsTrigger>
          <TabsTrigger value="organisations">Organisations</TabsTrigger>
        </TabsList>
        <TabsContent value="contacts">
          <div className="mb-2 flex items-center gap-2">
            <Checkbox id="qualified-only" checked={qualified} onCheckedChange={(c) => setQualified(c === true)} />
            <Label htmlFor="qualified-only" className="font-normal">Qualified contacts only (counted toward the target)</Label>
          </div>
          <DataTable<ContactRow> caption="Run contacts" tableId="run-contacts" columns={contactCols} queryKey={qk.custom('runs', 'contacts', id)} fetchPage={listFetcher<ContactRow>(`/api/runs/${id}/contacts`)}
            filters={{ qualified_only: qualified }} defaultPageSize={50} maxHeightClass="max-h-[28rem]"
            emptyState={<EmptyState compact title={qualified ? 'No qualified contacts yet' : 'No contacts found yet'} description="Contacts appear as the run discovers and validates addresses." />} />
        </TabsContent>
        <TabsContent value="organisations">
          <DataTable<Schema<'OrgRow'>> caption="Run organisations" tableId="run-orgs" getRowId={(r) => String(r.organisation_id)} columns={orgCols} queryKey={qk.custom('runs', 'orgs', id)} fetchPage={listFetcher<Schema<'OrgRow'>>(`/api/runs/${id}/organisations`)}
            defaultPageSize={50} maxHeightClass="max-h-[28rem]" emptyState={<EmptyState compact title="No organisations yet" description="Businesses discovered by this run are listed here." />} />
        </TabsContent>
      </Tabs>
    </section>
  );
}
