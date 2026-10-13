'use client';
import * as React from 'react';
import { Construction, Pause, Play, RotateCcw } from 'lucide-react';
import { api, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { useApiMutation } from '@/hooks/use-api-mutation';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { ConfirmDialog } from '@/components/ui/alert-dialog';
import { Textarea } from '@/components/ui/textarea';
import { FormField } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';

type Health = Schema<'SystemHealth'>;
const KEYS = [qk.all('system'), qk.all('runs'), qk.all('dashboard')];

export function SystemControls({ health }: { health: Health }) {
  const pause = useApiMutation({
    mutationFn: () => api.post<Schema<'PauseAllOut'>>('/api/system/pause-all'),
    successMessage: (d) => `Paused ${d.paused_run_ids.length} running ${d.paused_run_ids.length === 1 ? 'run' : 'runs'}`,
    invalidate: KEYS,
  });
  const resume = useApiMutation({
    mutationFn: () => api.post<Schema<'ResumeOut'>>('/api/system/resume-processing'),
    successMessage: (d) => `Resumed ${d.resumed_run_ids.length} ${d.resumed_run_ids.length === 1 ? 'run' : 'runs'}${d.skipped_run_ids.length ? ` (${d.skipped_run_ids.length} skipped: no longer paused)` : ''}`,
    invalidate: KEYS,
  });
  const restart = useApiMutation({
    mutationFn: () => api.post<Schema<'RestartOut'>>('/api/system/restart-failed'),
    successMessage: (d) => `Re-queued ${d.retried_jobs} failed ${d.retried_jobs === 1 ? 'job' : 'jobs'}`,
    invalidate: KEYS,
  });
  const maintenance = useApiMutation({
    mutationFn: (v: { enabled: boolean; message: string }) => api.put<Schema<'MaintenanceOut'>>('/api/system/maintenance', v),
    successMessage: (d) => (d.enabled ? 'Maintenance mode is on' : 'Maintenance mode is off'),
    invalidate: [qk.all('system'), qk.maintenance],
  });
  const [message, setMessage] = React.useState('');
  const maint = health.maintenance as { enabled: boolean; message: string };
  const pausedCount = health.paused_by_pause_all.length;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Controls</CardTitle>
        <CardDescription>Administrator actions. None of them delete data; each asks for confirmation and is written to the audit log.</CardDescription>
      </CardHeader>
      <CardContent>
        <ul className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <ControlItem title="Pause all runs" body="Sets every running run to paused. Jobs already being processed finish their current step; no new work starts. Nothing is lost.">
            <ConfirmDialog
              trigger={<Button variant="outline" loading={pause.isPending}><Pause /> Pause all runs</Button>}
              title="Pause all running runs?"
              description={<p>Every run that is currently running will be set to <strong>paused</strong>. Work in flight completes its current step so records stay consistent; queued work is simply not picked up. Use <em>Resume processing</em> to continue exactly where they stopped. Runs you paused yourself are not touched by resume.</p>}
              confirmLabel="Pause all runs" onConfirm={() => pause.mutateAsync()}
            />
          </ControlItem>
          <ControlItem title="Resume processing" body="Resumes only the runs that were paused by “Pause all”, not runs paused by a person for another reason.">
            <ConfirmDialog
              trigger={<Button variant="outline" loading={resume.isPending}><Play /> Resume processing</Button>}
              title="Resume processing?"
              description={<p>Runs that were paused by <em>Pause all runs</em>{pausedCount ? <> ({pausedCount} currently recorded)</> : null} are set back to <strong>running</strong> and workers pick up their queued jobs. Runs paused individually stay paused.</p>}
              confirmLabel="Resume processing" onConfirm={() => resume.mutateAsync()}
            />
            {pausedCount > 0 && <Badge variant="warning" className="ml-2">{pausedCount} paused by Pause all</Badge>}
          </ControlItem>
          <ControlItem title="Restart failed jobs" body={`Re-queues every failed or dead job across all runs (currently ${health.failed_jobs} failed, ${health.dead_jobs} dead) with the attempt counter reset.`}>
            <ConfirmDialog
              trigger={<Button variant="outline" loading={restart.isPending} disabled={health.failed_jobs + health.dead_jobs === 0}><RotateCcw /> Restart failed jobs</Button>}
              title="Restart all failed jobs?"
              description={<p>All <strong>{health.failed_jobs} failed</strong> and <strong>{health.dead_jobs} dead</strong> jobs are put back in the queue with their retry count reset. Jobs are de-duplicated by key, so records are not duplicated, but sources that caused the failure may be contacted again. If a source is down or rate-limiting you, fix that first to avoid another round of failures.</p>}
              confirmLabel="Restart failed jobs" onConfirm={() => restart.mutateAsync()}
            />
          </ControlItem>
          <ControlItem title="Maintenance mode" body={maint.enabled ? 'On: changes through the API are rejected with a 503 and a banner is shown to every user.' : 'Off. Turn on before backups, migrations or restores.'}>
            <ConfirmDialog
              trigger={<Button variant={maint.enabled ? 'default' : 'outline'} loading={maintenance.isPending} onClick={() => setMessage(maint.message || '')}><Construction /> {maint.enabled ? 'Turn maintenance off' : 'Turn maintenance on'}</Button>}
              title={maint.enabled ? 'Turn maintenance mode off?' : 'Turn maintenance mode on?'}
              description={maint.enabled ? (
                <p>Normal operation resumes: users can change data again and the banner disappears.</p>
              ) : (
                <div className="space-y-3">
                  <p>While on, every create, update or delete through the API (except sign-in and these system controls) is rejected with a 503, and everyone sees a banner. Data is not modified or deleted, and signed-in users stay signed in. It does <strong>not</strong> stop jobs already running; use <em>Pause all runs</em> first if you need a quiet system.</p>
                  <FormField label="Message shown to users (optional)" htmlFor="maint-message">
                    <Textarea id="maint-message" rows={2} maxLength={300} value={message} onChange={(e) => setMessage(e.target.value)} placeholder="Back at 14:00 UTC after a database upgrade." />
                  </FormField>
                </div>
              )}
              confirmLabel={maint.enabled ? 'Turn off' : 'Turn on'} destructive={!maint.enabled}
              onConfirm={() => maintenance.mutateAsync({ enabled: !maint.enabled, message: maint.enabled ? '' : message })}
            />
          </ControlItem>
        </ul>
      </CardContent>
    </Card>
  );
}

function ControlItem({ title, body, children }: { title: string; body: string; children: React.ReactNode }) {
  return (
    <li className="flex flex-col gap-3 rounded-md border p-4">
      <div><h3 className="text-sm font-semibold">{title}</h3><p className="mt-1 text-xs text-muted-foreground">{body}</p></div>
      <div className="mt-auto flex flex-wrap items-center">{children}</div>
    </li>
  );
}
