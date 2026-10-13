'use client';
import * as React from 'react';
import { Bar, BarChart, CartesianGrid, Legend, Tooltip as RTooltip, XAxis, YAxis } from 'recharts';
import { Inbox, RotateCcw } from 'lucide-react';
import { api, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { useApiMutation } from '@/hooks/use-api-mutation';
import { formatNumber, titleCase } from '@/lib/format';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { ChartCard, chartAxisProps, chartColor, chartGridProps, chartTooltipStyle } from '@/components/composites/chart-card';
import { EmptyState } from '@/components/composites/empty-state';
import { StatusBadge } from '@/components/composites/status-badge';
import { RelativeTime } from '@/components/composites/relative-time';
import { ScrollRegion, tdClass, thClass } from './scroll-region';

type Health = Schema<'SystemHealth'>;
export type FailedJob = Schema<'FailedJob'>;
const STATES = ['queued', 'leased', 'paused', 'failed', 'dead'] as const;

export function QueueSection({ health }: { health: Health }) {
  const stages = [...new Set(health.queue.map((r) => r.stage))].sort();
  const data = stages.map((stage) => {
    const row: Record<string, string | number> = { stage };
    for (const s of STATES) row[s] = health.queue.find((r) => r.stage === stage && r.state === s)?.count ?? 0;
    return row;
  });
  const used = STATES.filter((s) => data.some((d) => (d[s] as number) > 0));
  const total = health.queue.reduce((n, r) => n + r.count, 0);
  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <ChartCard
        title="Queue backlog by stage" description={`${formatNumber(total)} jobs waiting, running, paused or failed`}
        height={Math.max(180, 40 * stages.length + 60)}
        table={{ columns: ['Stage', ...STATES.map(titleCase)], rows: data.map((d) => [d.stage as string, ...STATES.map((s) => d[s] as number)]) }}
        empty={stages.length === 0 ? <EmptyState compact icon={Inbox} title="Queue is empty" description="No queued, leased, paused, failed or dead jobs." /> : undefined}
      >
        <BarChart data={data} layout="vertical" margin={{ left: 8, right: 16 }}>
          <CartesianGrid {...chartGridProps} horizontal={false} vertical />
          <XAxis type="number" allowDecimals={false} {...chartAxisProps} />
          <YAxis type="category" dataKey="stage" width={80} {...chartAxisProps} />
          <RTooltip {...chartTooltipStyle} cursor={{ fill: 'hsl(var(--muted))' }} />
          <Legend wrapperStyle={{ fontSize: 12 }} />
          {used.map((s) => <Bar key={s} dataKey={s} name={titleCase(s)} stackId="q" fill={chartColor(STATES.indexOf(s))} />)}
        </BarChart>
      </ChartCard>

      <Card>
        <CardHeader><CardTitle>Backlog table</CardTitle><CardDescription>Jobs by stage and state</CardDescription></CardHeader>
        <CardContent>
          {health.queue.length === 0 ? <p className="text-sm text-muted-foreground">Nothing in the queue.</p> : (
            <ScrollRegion label="Queue backlog table">
              <table className="w-full border-collapse" data-testid="queue-table">
                <caption className="sr-only">Number of jobs per stage and state</caption>
                <thead><tr><th scope="col" className={thClass}>Stage</th><th scope="col" className={thClass}>State</th><th scope="col" className={`${thClass} text-right`}>Jobs</th></tr></thead>
                <tbody>
                  {health.queue.map((r) => (
                    <tr key={`${r.stage}-${r.state}`}><th scope="row" className={`${tdClass} text-left font-medium`}>{r.stage}</th><td className={tdClass}>{titleCase(r.state)}</td><td className={`${tdClass} tabular text-right`}>{formatNumber(r.count)}</td></tr>
                  ))}
                </tbody>
              </table>
            </ScrollRegion>
          )}
          <dl className="mt-4 grid grid-cols-3 gap-3 text-sm">
            <div><dt className="text-xs text-muted-foreground">Failed</dt><dd className="tabular text-lg font-semibold" data-testid="failed-count">{formatNumber(health.failed_jobs)}</dd></div>
            <div><dt className="text-xs text-muted-foreground">Dead (gave up)</dt><dd className="tabular text-lg font-semibold">{formatNumber(health.dead_jobs)}</dd></div>
            <div><dt className="text-xs text-muted-foreground">Being retried</dt><dd className="tabular text-lg font-semibold">{formatNumber(health.retrying_jobs)}</dd></div>
          </dl>
          {Object.keys(health.jobs_by_attempts).length > 0 && (
            <p className="mt-2 text-xs text-muted-foreground">Retry counts (queued or running jobs): {Object.entries(health.jobs_by_attempts).sort().map(([a, n]) => `${a === '5' ? '5+' : a} ${a === '1' ? 'attempt' : 'attempts'}: ${n}`).join(' · ')}</p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

export function FailedJobs({ jobs }: { jobs: FailedJob[] }) {
  const retry = useApiMutation({
    mutationFn: (j: FailedJob) => api.post(`/api/runs/${j.run_id}/jobs/${j.id}/retry`),
    successMessage: 'Job re-queued',
    invalidate: [qk.all('system'), qk.all('dashboard'), qk.all('runs')],
  });
  return (
    <Card>
      <CardHeader><CardTitle>Failed and dead jobs</CardTitle><CardDescription>Most recent failures across all runs. “Dead” jobs exhausted their retries.</CardDescription></CardHeader>
      <CardContent>
        {jobs.length === 0 ? <EmptyState compact title="No failed jobs" description="Nothing needs attention right now." /> : (
          <ScrollRegion label="Failed jobs table">
            <table className="w-full min-w-[40rem] border-collapse" data-testid="failed-jobs-table">
              <caption className="sr-only">Recently failed jobs with stage, error and attempts</caption>
              <thead><tr>
                <th scope="col" className={thClass}>Job</th><th scope="col" className={thClass}>Stage</th><th scope="col" className={thClass}>State</th>
                <th scope="col" className={thClass}>Error</th><th scope="col" className={`${thClass} text-right`}>Attempts</th><th scope="col" className={thClass}>When</th><th scope="col" className={thClass}><span className="sr-only">Retry</span></th>
              </tr></thead>
              <tbody>
                {jobs.map((j) => (
                  <tr key={j.id}>
                    <th scope="row" className={`${tdClass} text-left font-mono text-xs`}>#{j.id}{j.run_id != null && <span className="block font-sans text-muted-foreground">run {j.run_id}</span>}</th>
                    <td className={tdClass}>{j.stage}</td>
                    <td className={tdClass}><StatusBadge kind="job" status={j.state === 'dead' ? 'failed' : j.state} />{j.state === 'dead' && <span className="ml-1 text-xs text-muted-foreground">dead</span>}</td>
                    <td className={`${tdClass} max-w-[22rem]`}><span className="font-mono text-xs">{j.error_code ?? '—'}</span>{j.error_detail && <span className="line-clamp-2 block break-words text-xs text-muted-foreground">{j.error_detail}</span>}</td>
                    <td className={`${tdClass} tabular text-right`}>{j.attempts}</td>
                    <td className={tdClass}><RelativeTime value={j.finished_at} /></td>
                    <td className={tdClass}>
                      <Button size="sm" variant="outline" disabled={j.run_id == null} loading={retry.isPending && retry.variables?.id === j.id} onClick={() => retry.mutate(j)} aria-label={`Retry job ${j.id}`}><RotateCcw /> Retry</Button>
                    </td>
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
