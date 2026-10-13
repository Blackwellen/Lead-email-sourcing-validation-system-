'use client';
import * as React from 'react';
import Link from 'next/link';
import { useParams } from 'next/navigation';
import { AlertTriangle, CheckCircle2, Download, Pause, Play, RotateCcw, Square, ServerCrash, Wifi, WifiOff } from 'lucide-react';
import { Can } from '@/lib/auth';
import { Button } from '@/components/ui/button';
import { Progress } from '@/components/ui/progress';
import { ConfirmDialog } from '@/components/ui/alert-dialog';
import { Card } from '@/components/ui/card';
import { AdvancedPanel } from '@/components/ui/collapsible';
import { PageHeader } from '@/components/composites/page-header';
import { StatCard } from '@/components/composites/stat-card';
import { StatusBadge } from '@/components/composites/status-badge';
import { ErrorState } from '@/components/composites/error-state';
import { LoadingState } from '@/components/composites/loading-state';
import { CodeBlock } from '@/components/composites/code-block';
import { formatDateTime, formatDuration, formatNumber, formatPercent, titleCase } from '@/lib/format';
import { availableActions, useNow, useRunLifecycle, type RunDetail as RunDetailT } from './run-actions';
import { useRunLive } from './use-run-live';
import { ContactQuality, CurrentWork, FailuresTable, PerformanceCharts, Pipeline, ResultsTabs, ThroughputPanel } from './run-detail-parts';
import { TARGET_DEFINITIONS } from './wizard/schema';

const STOP_REASON: Record<string, string> = {
  target_reached: 'The target was reached.',
  sources_exhausted: 'All queued work finished before the target was reached (sources exhausted).',
  stopped_by_user: 'Stopped by an operator.',
  max_runtime: 'Stopped because the maximum duration was reached.',
  work_budget: 'Stopped because the job budget was used up.',
};

export function RunDetailView() {
  const params = useParams<{ id: string }>();
  const id = Number(params.id);
  if (!Number.isFinite(id)) return <ErrorState title="Invalid run" message="The run id in the address is not a number." />;
  return <RunDetailInner id={id} />;
}

function RunDetailInner({ id }: { id: number }) {
  const { detail, buffer, latest, mode, terminal } = useRunLive(id);
  if (detail.isPending) return <><PageHeader title="Run" breadcrumbs={[{ label: 'Runs', href: '/runs' }, { label: `#${id}` }]} /><LoadingState label="Loading run…" rows={6} /></>;
  if (detail.isError) {
    const notFound = (detail.error as { status?: number }).status === 404;
    return <><PageHeader title="Run" breadcrumbs={[{ label: 'Runs', href: '/runs' }, { label: `#${id}` }]} />
      <ErrorState error={detail.error} title={notFound ? 'Run not found' : undefined} message={notFound ? 'This run does not exist or was deleted.' : undefined} onRetry={notFound ? undefined : () => detail.refetch()} retrying={detail.isFetching} />
      {notFound && <p className="mt-3 text-center"><Link className="text-primary underline" href="/runs">Back to runs</Link></p>}</>;
  }
  return <Loaded d={detail.data} mode={mode} terminal={terminal} buffer={buffer} latest={latest} />;
}

function Loaded({ d, mode, terminal, buffer, latest }: { d: RunDetailT; mode: 'live' | 'reconnecting' | 'polling' | 'finished'; terminal: boolean; buffer: ReturnType<typeof useRunLive>['buffer']; latest: ReturnType<typeof useRunLive>['latest'] }) {
  const { lifecycle, exportRun } = useRunLifecycle();
  const [stopOpen, setStopOpen] = React.useState(false);
  const status = latest?.status ?? d.status;
  const a = availableActions(status, d.archived);
  const counters = { ...(d.counters as Record<string, number | undefined>), ...(latest?.counters ?? {}) };
  const progress = latest?.progress ?? d.stop_condition.progress;
  const percent = latest?.percent ?? d.stop_condition.percent;
  const target = d.stop_condition.target;
  const defLabel = TARGET_DEFINITIONS.find((t) => t.value === d.stop_condition.definition)?.label ?? titleCase(d.stop_condition.definition);
  const js = latest?.job_states;
  const queued = js ? (js.queued ?? 0) + (js.leased ?? 0) + (js.paused ?? 0) : (d.queue_backlog ? Object.values(d.queue_backlog).reduce((x, y) => x + y, 0) : 0);
  const errors = js ? (js.failed ?? 0) + (js.dead ?? 0) : Number(counters.q_failed ?? 0);
  const rate = latest ? latest.throughput.qualified_last_minute : d.throughput.yield_per_minute;
  const targetReached = (d.stop_reason ?? latest?.stop_reason) === 'target_reached' || (progress >= target && target > 0);
  const workers = Number((d.effective_concurrency as Record<string, unknown>).workers_online ?? 0);
  const stopReason = latest?.stop_reason ?? d.stop_reason;

  // elapsed: wall clock since start (frozen at finish)
  const now = useNow(1000);
  const startMs = d.started_at ? Date.parse(d.started_at) : null;
  const endMs = d.finished_at ? Date.parse(d.finished_at) : terminal ? null : now;
  const elapsed = startMs && endMs ? Math.max(0, (endMs - startMs) / 1000) : null;

  const eta = d.eta;
  const etaValue = eta.status === 'estimated' && eta.eta_seconds_min != null && eta.eta_seconds_max != null
    ? `${formatDuration(eta.eta_seconds_min)} – ${formatDuration(eta.eta_seconds_max)}`
    : eta.status === 'complete' ? 'Complete' : 'Unknown — not enough data';

  return (
    <>
      <PageHeader
        title={d.name}
        breadcrumbs={[{ label: 'Runs', href: '/runs' }, { label: `#${d.id}` }]}
        meta={(
          <span className="flex flex-wrap items-center gap-3 text-sm">
            <StatusBadge kind="run" status={status} />
            <span className="tabular font-medium" data-testid="run-percent">{formatPercent(percent)} of target</span>
            <ConnectionBadge mode={mode} />
          </span>
        )}
        actions={(
          <Can role="operator">
            <div className="flex flex-wrap gap-2" role="group" aria-label="Run controls">
              {a.start && <Button onClick={() => lifecycle.mutate({ id: d.id, action: 'start' })} loading={lifecycle.isPending && lifecycle.variables?.action === 'start'}><Play /> Start</Button>}
              {a.pause && <Button variant="outline" onClick={() => lifecycle.mutate({ id: d.id, action: 'pause' })} loading={lifecycle.isPending && lifecycle.variables?.action === 'pause'}><Pause /> Pause</Button>}
              {a.resume && <Button onClick={() => lifecycle.mutate({ id: d.id, action: 'resume' })} loading={lifecycle.isPending && lifecycle.variables?.action === 'resume'}><Play /> Resume</Button>}
              {a.stop && <Button variant="destructive" onClick={() => setStopOpen(true)}><Square /> Graceful stop</Button>}
              {a['retry-failed'] && <Button variant="outline" onClick={() => lifecycle.mutate({ id: d.id, action: 'retry-failed' })} loading={lifecycle.isPending && lifecycle.variables?.action === 'retry-failed'} disabled={errors === 0}><RotateCcw /> Retry failed</Button>}
              {a.export && <Button variant="outline" onClick={() => exportRun.mutate({ id: d.id })} loading={exportRun.isPending}><Download /> Export current results</Button>}
            </div>
          </Can>
        )}
      />
      <ConfirmDialog open={stopOpen} onOpenChange={setStopOpen} destructive title="Gracefully stop this run?"
        description="No new work is handed out and queued jobs are cancelled. Jobs already running may finish and commit. Results collected so far are kept and can be exported."
        confirmLabel="Stop run" onConfirm={() => lifecycle.mutateAsync({ id: d.id, action: 'stop' })} />

      <div className="space-y-5">
        {exportRun.data && (
          <p role="status" className="rounded-md border border-success-border bg-success-soft p-3 text-sm text-success-foreground">
            Export #{exportRun.data.id} ({exportRun.data.status}) was created with this run as its filter. <Link href="/exports" className="font-medium underline">Open Exports</Link>
          </p>
        )}
        {targetReached && ['completed', 'stopping'].includes(status) && (
          <div role="status" className="flex flex-wrap items-center gap-3 rounded-lg border border-success-border bg-success-soft p-4 text-success-foreground">
            <CheckCircle2 className="size-5 shrink-0" aria-hidden />
            <div className="min-w-0 flex-1 text-sm"><p className="font-semibold">Target reached</p><p>{formatNumber(progress)} of {formatNumber(target)} {defLabel.toLowerCase()} qualified. Export the results or review them below.</p></div>
            <Button asChild variant="outline" size="sm"><Link href="/exports"><Download /> Go to Exports</Link></Button>
          </div>
        )}
        {status === 'completed' && !targetReached && stopReason && (
          <p role="status" className="rounded-md border border-info-border bg-info-soft p-3 text-sm text-info-foreground">Finished short of the target. {STOP_REASON[stopReason] ?? titleCase(stopReason)}</p>
        )}
        {status === 'failed' && (
          <p role="alert" className="flex items-center gap-2 rounded-md border border-danger-border bg-danger-soft p-3 text-sm text-danger-foreground"><AlertTriangle className="size-4" aria-hidden /> This run failed{stopReason ? `: ${STOP_REASON[stopReason] ?? titleCase(stopReason)}` : '.'} Review the failures below, then retry the failed jobs.</p>
        )}
        {!terminal && ['running', 'queued', 'stopping', 'pausing'].includes(status) && workers === 0 && (
          <div role="alert" className="flex items-start gap-3 rounded-lg border border-warning-border bg-warning-soft p-3 text-sm text-warning-foreground">
            <ServerCrash className="mt-0.5 size-4 shrink-0" aria-hidden />
            <p><strong>No workers online.</strong> Nothing is processing this run, so progress will not change until a worker is started (python -m app.worker).</p>
          </div>
        )}
        {d.config_errors && d.config_errors.length > 0 && (
          <p role="alert" className="rounded-md border border-warning-border bg-warning-soft p-3 text-sm text-warning-foreground">The stored configuration has problems: {d.config_errors.map((e) => `${e.field}: ${e.message}`).join('; ')}</p>
        )}

        <section aria-label="Run summary" className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Card className="p-4 sm:col-span-2" aria-label="Stop condition">
            <p className="text-xs font-medium text-muted-foreground">Stop condition</p>
            <p className="mt-1 text-sm font-medium">{defLabel}</p>
            <p className="tabular mt-1 text-2xl font-semibold" data-testid="run-progress">{formatNumber(progress)} <span className="text-base font-normal text-muted-foreground">of {formatNumber(target)}</span></p>
            <Progress className="mt-2" value={percent} aria-label={`Progress ${formatPercent(percent)}`} tone={status === 'failed' ? 'danger' : status === 'completed' && targetReached ? 'success' : 'primary'} />
            <p className="mt-2 text-xs text-muted-foreground">
              Stops when the target is reached
              {d.stop_condition.max_runtime_minutes ? `, after ${d.stop_condition.max_runtime_minutes} min` : ''}
              {d.stop_condition.max_work_jobs ? `, or after ${formatNumber(d.stop_condition.max_work_jobs)} jobs` : ''}
              , or when all queued work is done.{stopReason ? ` Reason recorded: ${STOP_REASON[stopReason] ?? titleCase(stopReason)}` : ''}
            </p>
          </Card>
          <StatCard label="Current throughput" value={status === 'running' || latest ? `${formatNumber(Math.round(rate * 60 * 10) / 10)} / h` : '—'} hint={latest ? `${latest.throughput.qualified_last_minute} qualified in the last minute (measured)` : 'Measured from qualified results'} />
          <StatCard label="Verified contacts" value={Number(counters.qualified ?? progress)} hint={`${formatNumber(Number(counters.emails_found ?? 0))} emails found in total`} />
          <StatCard label="Pending queue" value={queued} hint="Jobs waiting or running" />
          <StatCard label="Errors" value={errors} hint={errors ? 'Failed or dead jobs — see Failures' : 'No failed jobs'} />
          <StatCard label="Elapsed" value={elapsed === null ? (d.started_at ? '—' : 'Not started') : formatDuration(elapsed)} hint={d.started_at ? `Started ${formatDateTime(d.started_at)}` : d.scheduled_for ? `Scheduled for ${formatDateTime(d.scheduled_for)}` : undefined} />
          <StatCard label="ETA" value={<span className="text-lg" data-testid="run-eta">{etaValue}</span>} hint={eta.status === 'estimated' ? eta.basis ?? undefined : eta.reason ?? undefined} />
        </section>

        <Pipeline d={d} />
        <PerformanceCharts d={d} buffer={buffer} />
        <div className="grid gap-4 lg:grid-cols-2">
          <ThroughputPanel d={d} />
          <div className="space-y-4">
            <CurrentWork id={d.id} active={!terminal} />
            <ContactQuality id={d.id} />
          </div>
        </div>
        <FailuresTable id={d.id} active={!terminal} />
        <ResultsTabs id={d.id} />
        <AdvancedPanel title="Configuration snapshot" description="The exact settings this run was started with">
          <CodeBlock code={JSON.stringify(d.config, null, 2)} language="json" />
        </AdvancedPanel>
      </div>
    </>
  );
}

function ConnectionBadge({ mode }: { mode: 'live' | 'reconnecting' | 'polling' | 'finished' }) {
  const m = {
    live: { Icon: Wifi, text: 'Live', cls: 'text-success' },
    reconnecting: { Icon: WifiOff, text: 'Reconnecting… (refreshing by polling)', cls: 'text-warning' },
    polling: { Icon: WifiOff, text: 'Live stream unavailable — refreshing every few seconds', cls: 'text-warning' },
    finished: { Icon: CheckCircle2, text: 'Finished — streaming stopped', cls: 'text-muted-foreground' },
  }[mode];
  return <span role="status" data-testid="live-status" className={`inline-flex items-center gap-1 text-xs ${m.cls}`}><m.Icon className="size-3.5" aria-hidden />{m.text}</span>;
}
