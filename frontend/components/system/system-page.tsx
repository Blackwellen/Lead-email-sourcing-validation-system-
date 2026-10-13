'use client';
import * as React from 'react';
import { useQuery } from '@tanstack/react-query';
import { Activity, AlertTriangle, Cpu, Database, HardDrive, MemoryStick, Network, RefreshCw, Server, ShieldQuestion } from 'lucide-react';
import { api, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { useApiMutation } from '@/hooks/use-api-mutation';
import { RequireRole } from '@/lib/auth';
import { formatDateTime, formatDuration, formatNumber, formatPercent } from '@/lib/format';
import { PageHeader } from '@/components/composites/page-header';
import { LoadingState } from '@/components/composites/loading-state';
import { ErrorState } from '@/components/composites/error-state';
import { StatusBadge } from '@/components/composites/status-badge';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Progress } from '@/components/ui/progress';
import { Switch } from '@/components/ui/switch';
import { Label } from '@/components/ui/label';
import { ConfirmDialog } from '@/components/ui/alert-dialog';
import { ScrollRegion, tdClass, thClass } from './scroll-region';
import { SystemControls } from './system-controls';
import { FailedJobs, QueueSection, type FailedJob } from './queue-section';

type Health = Schema<'SystemHealth'>;
type Worker = Schema<'WorkerHealth'>;

export function SystemPage() {
  return (
    <RequireRole role="admin"><Inner /></RequireRole>
  );
}

function Inner() {
  const [auto, setAuto] = React.useState(true);
  const health = useQuery({
    queryKey: qk.custom('system', 'health'),
    queryFn: ({ signal }) => api.get<Health>('/api/system/health', { signal }),
    refetchInterval: auto ? 10_000 : false,
  });
  const failed = useQuery({
    queryKey: qk.custom('system', 'failed-jobs'),
    queryFn: async ({ signal }) => (await api.get<{ latest_failed_jobs: FailedJob[] }>('/api/dashboard/overview', { signal })).latest_failed_jobs,
    refetchInterval: auto ? 10_000 : false,
  });
  return (
    <>
      <PageHeader
        title="System"
        description="Health of the API, workers and database, queue backlog, and administrator controls."
        actions={<>
          <div className="flex items-center gap-2">
            <Switch id="auto-refresh" checked={auto} onCheckedChange={setAuto} />
            <Label htmlFor="auto-refresh" className="text-sm font-normal">Auto-refresh (10 s)</Label>
          </div>
          <Button variant="outline" onClick={() => { health.refetch(); failed.refetch(); }} loading={health.isFetching && !health.isPending}><RefreshCw /> Refresh health</Button>
        </>}
      />
      {health.isPending ? <LoadingState variant="cards" label="Loading system health" /> : health.isError ? <ErrorState error={health.error} onRetry={() => health.refetch()} /> : (
        <div className="space-y-6">
          <Banners h={health.data} />
          <StatusCards h={health.data} updatedAt={health.dataUpdatedAt} />
          <WorkersCard workers={health.data.workers} />
          <Resources r={health.data.resources} />
          <QueueSection health={health.data} />
          {failed.isError ? <ErrorState error={failed.error} onRetry={() => failed.refetch()} title="Could not load failed jobs" /> : <FailedJobs jobs={failed.data ?? []} />}
          <Diagnostics h={health.data} />
          <SystemControls health={health.data} />
        </div>
      )}
    </>
  );
}

function Banners({ h }: { h: Health }) {
  const stale = h.workers.filter((w) => !w.healthy);
  const maint = h.maintenance as { enabled: boolean; message: string };
  return (
    <div className="space-y-2">
      {h.workers.length === 0 && (
        <div role="alert" data-testid="worker-outage-banner" className="flex gap-2 rounded-md border border-danger-border bg-danger-soft p-3 text-sm text-danger-foreground">
          <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
          <p><strong>No worker heartbeat.</strong> No worker has reported in, so queued jobs will not be processed. Check that the worker service is running and can reach the database. Runs will stay queued until a worker returns; nothing is lost.</p>
        </div>
      )}
      {h.workers.length > 0 && h.workers_healthy === 0 && (
        <div role="alert" data-testid="worker-outage-banner" className="flex gap-2 rounded-md border border-danger-border bg-danger-soft p-3 text-sm text-danger-foreground">
          <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
          <p><strong>All workers are stale.</strong> None has sent a heartbeat recently ({stale.map((w) => w.worker_id).join(', ')}). Jobs are not being processed.</p>
        </div>
      )}
      {stale.length > 0 && h.workers_healthy > 0 && (
        <div role="alert" data-testid="stale-worker-warning" className="flex gap-2 rounded-md border border-warning-border bg-warning-soft p-3 text-sm text-warning-foreground">
          <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
          <p><strong>{stale.length} stale {stale.length === 1 ? 'worker' : 'workers'}:</strong> {stale.map((w) => w.worker_id).join(', ')} stopped reporting. Their leased jobs are re-queued automatically when the lease expires.</p>
        </div>
      )}
      {maint.enabled && <p role="status" className="rounded-md border border-warning-border bg-warning-soft p-3 text-sm text-warning-foreground"><strong>Maintenance mode is on.</strong> {maint.message || 'API changes are rejected until it is turned off.'}</p>}
      {h.paused_by_pause_all.length > 0 && <p role="status" className="rounded-md border border-warning-border bg-warning-soft p-3 text-sm text-warning-foreground"><strong>{h.paused_by_pause_all.length} runs are paused by “Pause all”.</strong> Use Resume processing to continue them.</p>}
    </div>
  );
}

function StatusCards({ h, updatedAt }: { h: Health; updatedAt: number }) {
  const api_ = h.api as { status: string; version?: string; env?: string };
  const db = h.database as { status: string; latency_ms?: number };
  return (
    <section aria-label="Service health" className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
      <HealthCard icon={<Server />} title="API" status={api_.status === 'ok' ? 'ok' : 'down'} lines={[`Version ${api_.version ?? '—'}`, `Environment: ${api_.env ?? '—'}`]} testId="card-api" />
      <HealthCard icon={<Database />} title="Database" status={db.status === 'ok' ? 'ok' : 'down'} lines={[`Round-trip ${db.latency_ms ?? '—'} ms`, `Checked ${updatedAt ? formatDateTime(new Date(updatedAt)) : '—'}`]} testId="card-db" />
      <HealthCard icon={<Activity />} title="Workers" status={h.workers.length === 0 ? 'down' : h.workers_healthy === h.workers.length ? 'healthy' : h.workers_healthy === 0 ? 'down' : 'degraded'}
        lines={[`${h.workers_healthy} of ${h.workers.length} healthy`, h.workers.length === 0 ? 'No heartbeat received' : `Active jobs: ${h.workers.reduce((n, w) => n + w.active_jobs, 0)}`]} testId="card-workers" />
    </section>
  );
}

function HealthCard({ icon, title, status, lines, testId }: { icon: React.ReactNode; title: string; status: string; lines: string[]; testId: string }) {
  return (
    <Card data-testid={testId}>
      <CardHeader className="flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className="flex items-center gap-2 text-sm [&_svg]:size-4 [&_svg]:text-muted-foreground"><span aria-hidden>{icon}</span>{title}</CardTitle>
        <StatusBadge kind="health" status={status} />
      </CardHeader>
      <CardContent className="space-y-0.5 text-sm text-muted-foreground">{lines.map((l) => <p key={l}>{l}</p>)}</CardContent>
    </Card>
  );
}

function WorkersCard({ workers }: { workers: Worker[] }) {
  return (
    <Card>
      <CardHeader><CardTitle>Workers</CardTitle><CardDescription>A worker is stale when it has not sent a heartbeat for three heartbeat intervals.</CardDescription></CardHeader>
      <CardContent>
        {workers.length === 0 ? <p className="text-sm text-muted-foreground" data-testid="no-workers">No workers have reported in.</p> : (
          <ScrollRegion label="Workers table">
            <table className="w-full min-w-[34rem] border-collapse" data-testid="workers-table">
              <caption className="sr-only">Workers with memory, CPU, active jobs and last heartbeat</caption>
              <thead><tr>
                <th scope="col" className={thClass}>Worker</th><th scope="col" className={thClass}>Status</th><th scope="col" className={`${thClass} text-right`}>RSS</th>
                <th scope="col" className={`${thClass} text-right`}>CPU</th><th scope="col" className={`${thClass} text-right`}>Active jobs</th><th scope="col" className={thClass}>Last seen</th>
              </tr></thead>
              <tbody>
                {workers.map((w) => (
                  <tr key={w.worker_id}>
                    <th scope="row" className={`${tdClass} text-left font-medium`}>{w.worker_id}{w.hostname && <span className="block text-xs font-normal text-muted-foreground">{w.hostname}</span>}</th>
                    <td className={tdClass}><StatusBadge kind="health" status={w.healthy ? 'healthy' : 'down'} />{!w.healthy && <span className="ml-1 text-xs text-warning-foreground">stale</span>}</td>
                    <td className={`${tdClass} tabular text-right`}>{w.rss_mb != null ? `${formatNumber(Math.round(w.rss_mb))} MB` : '—'}</td>
                    <td className={`${tdClass} tabular text-right`}>{w.cpu_percent != null ? `${w.cpu_percent.toFixed(0)}%` : '—'}</td>
                    <td className={`${tdClass} tabular text-right`}>{w.active_jobs}</td>
                    <td className={tdClass}>{formatDuration(w.last_seen_seconds_ago)} ago</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </ScrollRegion>
        )}
      </CardContent>
    </Card>
  );
}

function Gauge({ icon, label, percent, detail }: { icon: React.ReactNode; label: string; percent: number; detail: string }) {
  const tone = percent >= 90 ? 'danger' : percent >= 75 ? 'warning' : 'primary';
  const level = percent >= 90 ? 'critical' : percent >= 75 ? 'high' : 'normal';
  return (
    <div className="space-y-2 rounded-md border p-4">
      <div className="flex items-center justify-between text-sm"><span className="flex items-center gap-2 font-medium [&_svg]:size-4 [&_svg]:text-muted-foreground"><span aria-hidden>{icon}</span>{label}</span><span className="tabular font-semibold">{formatPercent(percent, 0)} <span className="text-xs font-normal text-muted-foreground">({level})</span></span></div>
      <Progress value={percent} tone={tone} aria-label={`${label} usage`} />
      <p className="text-xs text-muted-foreground">{detail}</p>
    </div>
  );
}

function Resources({ r }: { r: Schema<'ResourceInfo'> }) {
  return (
    <section aria-label="Server resources" className="grid grid-cols-1 gap-4 md:grid-cols-3">
      <Gauge icon={<Cpu />} label="CPU" percent={r.cpu_percent} detail={`${r.cpu_count ?? '?'} cores`} />
      <Gauge icon={<MemoryStick />} label="Memory" percent={r.ram_percent} detail={`${formatNumber(r.ram_available_mb)} MB free of ${formatNumber(r.ram_total_mb)} MB`} />
      <Gauge icon={<HardDrive />} label="Disk" percent={r.disk_percent} detail={`${r.disk_free_gb} GB free of ${r.disk_total_gb} GB`} />
    </section>
  );
}

function Diagnostics({ h }: { h: Health }) {
  const egress = useApiMutation({
    mutationFn: () => api.post<Schema<'EgressResult'>>('/api/system/smtp-egress-check'),
    invalidate: [qk.all('system')],
    successMessage: (d) => (d.status === 'open' ? 'Port 25 appears open' : d.status === 'blocked' ? 'Port 25 appears blocked' : 'Could not determine port 25 status'),
  });
  const bw = h.browser_workers as { playwright_installed: boolean; available: boolean; note: string | null };
  const dns = h.dns_timeout_rate;
  const live = egress.data;
  const status = live?.status ?? h.smtp.egress_port_25;
  const detail = live?.detail;
  const checkedAt = live?.checked_at ?? h.smtp.egress_checked_at;
  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
      <Card data-testid="card-dns">
        <CardHeader><CardTitle className="text-sm">DNS timeouts</CardTitle><CardDescription>Share of recent DNS and MX lookups that timed out or failed temporarily</CardDescription></CardHeader>
        <CardContent>
          <p className="text-2xl font-semibold tabular">{dns === null ? '—' : formatPercent(dns * 100, 1)}</p>
          <p className="text-xs text-muted-foreground">{dns === null ? 'No lookups recorded yet.' : `Based on the latest ${formatNumber(h.dns_sample_size)} lookups.`}</p>
          {dns !== null && dns > 0.1 && <p className="mt-2 flex gap-1.5 text-xs text-warning-foreground"><AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />High. Verification results may be marked unknown; check the resolver and outbound DNS.</p>}
        </CardContent>
      </Card>

      <Card data-testid="card-smtp">
        <CardHeader><CardTitle className="text-sm">SMTP diagnostics</CardTitle><CardDescription>Mailbox probes need outbound port 25</CardDescription></CardHeader>
        <CardContent className="space-y-3 text-sm">
          <p className="flex flex-wrap items-center gap-2">Diagnostics: <Badge variant={h.smtp.diagnostics_enabled ? 'success' : 'neutral'}>{h.smtp.diagnostics_enabled ? 'Enabled' : 'Disabled'}</Badge>
            {h.smtp.helo_host && <span className="text-xs text-muted-foreground">HELO {h.smtp.helo_host}</span>}</p>
          <p className="flex flex-wrap items-center gap-2" data-testid="egress-status">Outbound port 25:
            <Badge variant={status === 'open' ? 'success' : status === 'blocked' ? 'danger' : 'neutral'}>{status === 'open' ? 'Open' : status === 'blocked' ? 'Blocked' : 'Unknown'}</Badge></p>
          {detail && <p className="text-xs text-muted-foreground" data-testid="egress-detail">{detail}</p>}
          <p className="text-xs text-muted-foreground">{checkedAt ? `Last checked ${formatDateTime(checkedAt)}.` : 'Never checked.'} Most cloud providers block outbound port 25 by default (and some never unblock it), so mailbox-level checks are often impossible there; results then stay “unknown” rather than guessed.</p>
          <ConfirmDialog
            trigger={<Button variant="outline" size="sm" loading={egress.isPending}><Network /> Check outbound port 25</Button>}
            title="Check outbound port 25?"
            description={<p>This opens a single TCP connection to a public mail server (gmail-smtp-in.l.google.com:25) and closes it again. No e-mail or data is sent. “Blocked” means the connection timed out or was refused; “unknown” means the test itself could not be completed.</p>}
            confirmLabel="Run check" onConfirm={() => egress.mutateAsync()}
          />
        </CardContent>
      </Card>

      <Card data-testid="card-browser">
        <CardHeader><CardTitle className="text-sm">Browser workers</CardTitle><CardDescription>Used to render JavaScript-heavy pages</CardDescription></CardHeader>
        <CardContent className="space-y-2 text-sm">
          <p className="flex items-center gap-2">Playwright: <Badge variant={bw.playwright_installed ? 'success' : 'warning'}>{bw.playwright_installed ? 'Installed' : 'Not installed'}</Badge></p>
          <p className="flex items-center gap-2">Available to workers: <Badge variant={bw.available ? 'success' : 'neutral'}>{bw.available ? 'Yes' : 'No'}</Badge></p>
          {bw.note && <p className="flex gap-1.5 text-xs text-muted-foreground"><ShieldQuestion className="mt-0.5 size-3.5 shrink-0" aria-hidden />{bw.note}</p>}
        </CardContent>
      </Card>
    </div>
  );
}
