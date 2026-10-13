'use client';
import * as React from 'react';
import Link from 'next/link';
import { useQuery } from '@tanstack/react-query';
import { Activity, AlertTriangle, BadgeCheck, Building2, Cpu, Download, Globe2, Inbox, Mail, MailQuestion, Plus, Play, ServerCrash, ShieldQuestion, Sparkles, Users } from 'lucide-react';
import { Area, AreaChart, Bar, BarChart, CartesianGrid, Legend, Tooltip as RTooltip, XAxis, YAxis } from 'recharts';
import { api, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { Can } from '@/lib/auth';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Progress } from '@/components/ui/progress';
import { PageHeader } from '@/components/composites/page-header';
import { StatCard } from '@/components/composites/stat-card';
import { ChartCard, chartAxisProps, chartColor, chartGridProps, chartTooltipStyle } from '@/components/composites/chart-card';
import { StatusBadge } from '@/components/composites/status-badge';
import { EmptyState } from '@/components/composites/empty-state';
import { ErrorState } from '@/components/composites/error-state';
import { RelativeTime } from '@/components/composites/relative-time';
import { formatBytes, formatDateTime, formatNumber, formatPercent, titleCase } from '@/lib/format';

type Overview = Schema<'Overview'>;

export function DashboardView() {
  const q = useQuery({
    queryKey: qk.custom('dashboard', 'overview'),
    queryFn: ({ signal }) => api.get<Overview>('/api/dashboard/overview', { signal }),
    refetchInterval: 15_000,
  });
  const d = q.data;
  const loading = q.isPending;

  return (
    <>
      <PageHeader
        title="Dashboard"
        description="Operational overview of runs, yield and data health."
        meta={d ? (
          <span className="text-xs text-muted-foreground">
            Updated <RelativeTime value={d.generated_at} />{d.approximate && ' · some figures are approximate (large tables are sampled)'}
          </span>
        ) : undefined}
        actions={(
          <div className="flex flex-wrap gap-2">
            <Can role="operator"><Button asChild><Link href="/runs/new"><Plus /> New sourcing run</Link></Button></Can>
            <Button asChild variant="outline"><Link href="/runs"><Play /> View runs</Link></Button>
            <Button asChild variant="outline"><Link href="/contacts"><Users /> Explore contacts</Link></Button>
            <Button asChild variant="outline"><Link href="/exports"><Download /> Export</Link></Button>
          </div>
        )}
      />

      {q.isError && <ErrorState error={q.error} onRetry={() => q.refetch()} retrying={q.isFetching} />}

      {!q.isError && (
        <div className="space-y-6">
          {d && <WorkerBanner d={d} />}

          <section aria-label="Key figures" className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <StatCard label="Businesses" icon={<Building2 />} loading={loading} value={d?.total_businesses.value} approximate={d?.total_businesses.approximate} href="/businesses" />
            <StatCard label="With verified websites" icon={<Globe2 />} loading={loading} value={d?.businesses_with_confirmed_domains.value} approximate={d?.businesses_with_confirmed_domains.approximate}
              hint="Domain match confirmed or high" />
            <StatCard label="Published contacts" icon={<Mail />} loading={loading} value={d?.published_contacts.value} approximate={d?.published_contacts.approximate} href="/contacts"
              hint="Published or corroborated on the business’s own pages" />
            <StatCard label="Inferred candidates" icon={<Sparkles />} loading={loading} value={d?.inferred_candidates.value} approximate={d?.inferred_candidates.approximate}
              hint="Pattern-inferred or role-speculative; not published" />
            <StatCard label="Catch-all domains (contacts)" icon={<MailQuestion />} loading={loading} value={d?.catch_all_count.value} approximate={d?.catch_all_count.approximate} hint="Confirmed or suspected catch-all" />
            <StatCard label="Unknown reachability" icon={<ShieldQuestion />} loading={loading} value={d?.unknown_count.value} approximate={d?.unknown_count.approximate} hint="Not enough evidence either way" />
            <StatCard label="Active runs" icon={<Activity />} loading={loading} value={d?.active_runs_count} href="/runs" />
            <StatCard label="Healthy workers" icon={<Cpu />} loading={loading} value={d ? `${d.workers_healthy} / ${d.workers.length}` : undefined}
              hint={d && d.workers.length === 0 ? 'No worker has ever reported a heartbeat' : 'Based on recent heartbeats'} />
          </section>

          <section aria-label="Charts" className="grid gap-4 lg:grid-cols-2">
            <TimeSeries d={d} loading={loading} />
            <Distribution title="Source distribution" description="Businesses by discovery source" rows={d?.source_distribution.map((b) => ({ name: b.key, count: b.count }))} loading={loading} approximate={d?.approximate} colorIndex={0} />
            <Distribution title="Geographic distribution" description="Businesses by country (top 15)" rows={d?.country_distribution.slice(0, 15).map((b) => ({ name: b.key, count: b.count }))} loading={loading} approximate={d?.approximate} colorIndex={2} />
            <Distribution title="Reachability distribution" description="Contacts by overall reachability" rows={d?.reachability_distribution.map((b) => ({ name: titleCase(b.key), count: b.count }))} loading={loading} approximate={d?.approximate} colorIndex={4} />
          </section>

          <section aria-label="Operations" className="grid gap-4 lg:grid-cols-3">
            <ActiveRuns d={d} loading={loading} />
            <WorkersCard d={d} loading={loading} />
            <Freshness d={d} loading={loading} />
            <RecentExports d={d} loading={loading} />
            <Failures d={d} loading={loading} />
          </section>
        </div>
      )}
    </>
  );
}

function WorkerBanner({ d }: { d: Overview }) {
  if (d.workers_healthy > 0) return null;
  return (
    <div role="alert" className="flex items-start gap-3 rounded-lg border border-warning-border bg-warning-soft p-4 text-warning-foreground">
      <ServerCrash className="mt-0.5 size-5 shrink-0" aria-hidden />
      <div className="text-sm">
        <p className="font-semibold">{d.workers.length === 0 ? 'No workers running' : 'No healthy workers'}</p>
        <p className="mt-0.5">
          {d.workers.length === 0
            ? 'No worker process has reported a heartbeat. Runs can be created and controlled, but queued jobs will not be processed until a worker is started (python -m app.worker).'
            : 'Every worker heartbeat is stale. Queued jobs will not progress until a worker reports in again.'}
        </p>
      </div>
    </div>
  );
}

function TimeSeries({ d, loading }: { d?: Overview; loading: boolean }) {
  const rows = d?.time_series ?? [];
  return (
    <ChartCard
      title="Discoveries and validations per day"
      description="Last 30 days"
      loading={loading}
      table={{ columns: ['Day', 'Discoveries', 'Validations'], rows: rows.map((r) => [r.day, r.discoveries, r.validations]) }}
      empty={!loading && rows.length === 0 ? <EmptyState compact icon={Inbox} title="No activity in the last 30 days" description="Charts fill in once businesses are discovered or contacts validated." /> : undefined}
      footer={d?.approximate ? 'Approximate: counts come from a sample of large tables and are scaled up.' : undefined}
    >
      <AreaChart data={rows} margin={{ left: 0, right: 8, top: 8 }}>
        <CartesianGrid {...chartGridProps} />
        <XAxis dataKey="day" {...chartAxisProps} />
        <YAxis {...chartAxisProps} allowDecimals={false} width={44} />
        <RTooltip {...chartTooltipStyle} />
        <Legend />
        <Area type="monotone" dataKey="discoveries" name="Discoveries" stroke={chartColor(0)} fill={chartColor(0)} fillOpacity={0.15} strokeWidth={2} />
        <Area type="monotone" dataKey="validations" name="Validations" stroke={chartColor(2)} fill={chartColor(2)} fillOpacity={0.15} strokeWidth={2} strokeDasharray="5 3" />
      </AreaChart>
    </ChartCard>
  );
}

function Distribution({ title, description, rows, loading, approximate, colorIndex }: {
  title: string; description: string; rows?: { name: string; count: number }[]; loading: boolean; approximate?: boolean; colorIndex: number;
}) {
  const data = rows ?? [];
  return (
    <ChartCard
      title={title}
      description={description}
      loading={loading}
      height={Math.max(220, Math.min(420, data.length * 28 + 40))}
      table={{ columns: ['Name', approximate ? 'Count (approx.)' : 'Count'], rows: data.map((r) => [r.name, r.count]) }}
      empty={!loading && data.length === 0 ? <EmptyState compact icon={Inbox} title="No data yet" description="This fills in as businesses and contacts are collected." /> : undefined}
    >
      <BarChart data={data} layout="vertical" margin={{ left: 8, right: 24 }}>
        <CartesianGrid {...chartGridProps} horizontal={false} vertical />
        <XAxis type="number" {...chartAxisProps} allowDecimals={false} />
        <YAxis type="category" dataKey="name" {...chartAxisProps} width={110} interval={0} />
        <RTooltip {...chartTooltipStyle} cursor={{ fill: 'hsl(var(--muted))' }} />
        <Bar dataKey="count" name={approximate ? 'Count (approx.)' : 'Count'} fill={chartColor(colorIndex)} radius={[0, 4, 4, 0]} label={{ position: 'right', fontSize: 11, fill: 'hsl(var(--muted-foreground))' }} />
      </BarChart>
    </ChartCard>
  );
}

function Panel({ title, description, children, className }: { title: string; description?: string; children: React.ReactNode; className?: string }) {
  return (
    <Card className={className}>
      <CardHeader><CardTitle>{title}</CardTitle>{description && <CardDescription>{description}</CardDescription>}</CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  );
}

function Skel() { return <div className="h-24 animate-pulse rounded-md bg-muted" aria-hidden />; }

function ActiveRuns({ d, loading }: { d?: Overview; loading: boolean }) {
  return (
    <Panel title="Active runs" description={d ? `${d.active_runs_count} running, queued, pausing or stopping` : undefined}>
      {loading ? <Skel /> : !d || d.active_runs.length === 0 ? (
        <EmptyState compact icon={Play} title="No active runs" action={<Can role="operator"><Button asChild size="sm"><Link href="/runs/new">New sourcing run</Link></Button></Can>} />
      ) : (
        <ul className="space-y-3">
          {d.active_runs.map((r) => {
            const pct = r.target_count ? Math.min(100, (100 * r.progress) / r.target_count) : 0;
            return (
              <li key={r.id}>
                <div className="flex items-center justify-between gap-2">
                  <Link href={`/runs/${r.id}`} className="truncate text-sm font-medium hover:underline">{r.name}</Link>
                  <StatusBadge kind="run" status={r.status} />
                </div>
                <Progress value={pct} className="mt-1.5" aria-label={`${r.name} progress`} />
                <p className="tabular mt-1 text-xs text-muted-foreground">{formatNumber(r.progress)} of {formatNumber(r.target_count)} ({formatPercent(pct)})</p>
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}

function WorkersCard({ d, loading }: { d?: Overview; loading: boolean }) {
  return (
    <Panel title="Worker health" description="Live heartbeats from worker processes">
      {loading ? <Skel /> : !d || d.workers.length === 0 ? (
        <div role="status" className="rounded-md border border-warning-border bg-warning-soft p-3 text-sm text-warning-foreground">
          <p className="flex items-center gap-2 font-medium"><AlertTriangle className="size-4" aria-hidden /> No workers running</p>
          <p className="mt-1 text-xs">No heartbeat has been recorded. Start a worker to process queued jobs.</p>
        </div>
      ) : (
        <ul className="space-y-2">
          {d.workers.map((w) => (
            <li key={w.worker_id} className="rounded-md border p-2.5 text-sm">
              <div className="flex items-center justify-between gap-2">
                <span className="min-w-0 truncate font-mono text-xs" title={w.worker_id}>{w.hostname ?? w.worker_id}</span>
                <StatusBadge kind="health" status={w.healthy ? 'healthy' : 'down'} label={w.healthy ? 'Healthy' : 'Stale'} />
              </div>
              <p className="tabular mt-1 text-xs text-muted-foreground">
                Seen {formatNumber(Math.round(w.last_seen_seconds_ago))}s ago · {w.active_jobs} active job{w.active_jobs === 1 ? '' : 's'}
                {w.cpu_percent != null && <> · CPU {formatPercent(w.cpu_percent, 0)}</>}{w.rss_mb != null && <> · {formatBytes(w.rss_mb * 1024 * 1024)} RAM</>}
              </p>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

function Freshness({ d, loading }: { d?: Overview; loading: boolean }) {
  const total = (d?.validation_freshness ?? []).reduce((a, b) => a + b.count, 0);
  const label: Record<string, string> = { never: 'Never validated', '<7d': 'Within 7 days', '7-30d': '7–30 days', '30-90d': '30–90 days', '>90d': 'Over 90 days' };
  return (
    <Panel title="Validation freshness" description="When contacts were last validated">
      {loading ? <Skel /> : total === 0 ? <EmptyState compact icon={BadgeCheck} title="No contacts yet" /> : (
        <ul className="space-y-2.5">
          {d!.validation_freshness.map((b) => (
            <li key={b.key}>
              <div className="flex justify-between text-sm"><span>{label[b.key] ?? b.key}</span><span className="tabular">{d!.approximate ? '~' : ''}{formatNumber(b.count)}</span></div>
              <Progress value={total ? (100 * b.count) / total : 0} className="mt-1 h-1.5" aria-label={`${label[b.key] ?? b.key}: ${formatPercent(total ? (100 * b.count) / total : 0)}`} />
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

function RecentExports({ d, loading }: { d?: Overview; loading: boolean }) {
  return (
    <Panel title="Recent exports">
      {loading ? <Skel /> : !d || d.recent_exports.length === 0 ? <EmptyState compact icon={Download} title="No exports yet" /> : (
        <ul className="divide-y">
          {d.recent_exports.map((e) => (
            <li key={e.id} className="flex items-center justify-between gap-2 py-2 text-sm">
              <span className="min-w-0">
                <Link href="/exports" className="font-medium hover:underline">#{e.id} {titleCase(e.preset)}</Link>
                <span className="block text-xs text-muted-foreground">{e.format.toUpperCase()} · {formatNumber(e.row_count)} rows · {formatDateTime(e.created_at)}</span>
              </span>
              <StatusBadge kind="job" status={e.status === 'done' ? 'done' : e.status} />
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

function Failures({ d, loading }: { d?: Overview; loading: boolean }) {
  return (
    <Panel title="Latest failures" description="Failed or dead jobs, newest first" className="lg:col-span-2">
      {loading ? <Skel /> : !d || d.latest_failed_jobs.length === 0 ? <EmptyState compact icon={BadgeCheck} title="No failed jobs" description="Nothing has failed recently." /> : (
        <div className="relative overflow-x-auto">
          <table className="w-full min-w-[32rem] text-left text-sm">
            <caption className="sr-only">Latest failed jobs</caption>
            <thead><tr className="text-xs text-muted-foreground">
              <th scope="col" className="py-1.5 pr-3 font-medium">Run</th><th scope="col" className="py-1.5 pr-3 font-medium">Stage</th>
              <th scope="col" className="py-1.5 pr-3 font-medium">Error</th><th scope="col" className="py-1.5 pr-3 font-medium">Attempts</th><th scope="col" className="py-1.5 font-medium">When</th>
            </tr></thead>
            <tbody>
              {d.latest_failed_jobs.map((j) => (
                <tr key={j.id} className="border-t align-top">
                  <td className="py-1.5 pr-3">{j.run_id ? <Link href={`/runs/${j.run_id}`} className="hover:underline">#{j.run_id}</Link> : '—'}</td>
                  <td className="py-1.5 pr-3">{titleCase(j.stage)}</td>
                  <td className="py-1.5 pr-3"><span className="font-mono text-xs">{j.error_code ?? 'unknown'}</span>{j.error_detail && <span className="block max-w-md truncate text-xs text-muted-foreground" title={j.error_detail}>{j.error_detail}</span>}</td>
                  <td className="tabular py-1.5 pr-3">{j.attempts}</td>
                  <td className="py-1.5"><RelativeTime value={j.finished_at} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}
