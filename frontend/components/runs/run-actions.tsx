'use client';
import * as React from 'react';
import { useRouter } from 'next/navigation';
import { api, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { toast } from '@/lib/toast';
import { useApiMutation } from '@/hooks/use-api-mutation';

export type RunSummary = Schema<'RunSummary'>;
export type RunDetail = Schema<'RunDetail'>;

export type LifecycleAction = 'start' | 'pause' | 'resume' | 'stop' | 'retry-failed' | 'archive' | 'duplicate';

/** Which lifecycle actions the backend accepts for a run in this status (mirrors app/runs.py state checks). */
export function availableActions(status: string, archived = false): Record<LifecycleAction | 'export' | 'delete', boolean> {
  const s = status;
  return {
    start: ['draft', 'scheduled', 'queued'].includes(s) && !archived,
    pause: s === 'running',
    resume: s === 'paused',
    stop: ['running', 'queued', 'paused', 'scheduled', 'pausing'].includes(s),
    'retry-failed': ['failed', 'completed', 'running', 'paused'].includes(s),
    archive: !archived && !['running', 'stopping', 'paused'].includes(s),
    duplicate: true,
    export: s !== 'draft' && s !== 'scheduled',
    delete: s === 'draft',
  };
}

const SUCCESS: Record<LifecycleAction, string> = {
  start: 'Run started',
  pause: 'Run paused',
  resume: 'Run resumed',
  stop: 'Graceful stop requested',
  'retry-failed': 'Failed jobs re-queued',
  archive: 'Run archived',
  duplicate: 'Run duplicated as a draft',
};

export const ACTION_LABEL: Record<LifecycleAction | 'export' | 'delete', string> = {
  start: 'Start', pause: 'Pause', resume: 'Resume', stop: 'Graceful stop', 'retry-failed': 'Retry failed jobs',
  archive: 'Archive', duplicate: 'Duplicate', export: 'Export results', delete: 'Delete draft',
};

/** Lifecycle actions against the real endpoints; invalidates run + dashboard queries; 409 messages are toasted by useApiMutation. */
export function useRunLifecycle() {
  const router = useRouter();
  const lifecycle = useApiMutation<unknown, { id: number; action: LifecycleAction }>({
    mutationFn: ({ id, action }) => api.post(`/api/runs/${id}/${action}`),
    successMessage: (_d, v) => SUCCESS[v.action],
    errorMessage: 'Run action failed',
    invalidate: [qk.all('runs'), qk.all('dashboard')],
    onSuccess: (data, v) => {
      if (v.action === 'duplicate') {
        const copy = data as RunSummary;
        toast.info({ title: 'Draft created', description: copy.name, action: { label: 'Open in wizard', onClick: () => router.push(`/runs/new?draft=${copy.id}`) } });
      }
    },
  });
  const deleteDraft = useApiMutation<unknown, { id: number }>({
    mutationFn: ({ id }) => api.delete(`/api/runs/${id}`),
    successMessage: 'Draft deleted',
    errorMessage: 'Could not delete draft',
    invalidate: [qk.all('runs')],
  });
  const exportRun = useApiMutation<Schema<'ExportOut'>, { id: number }>({
    mutationFn: ({ id }) => api.post<Schema<'ExportOut'>>('/api/exports', { preset: 'full_intelligence', format: 'csv', run_id: id, wait: false }),
    errorMessage: 'Export failed',
    invalidate: [qk.all('exports')],
    onSuccess: (ex) => {
      toast.success({ title: 'Export created', description: `Export #${ex.id} (${ex.status}) is filtered to this run.`, action: { label: 'Open Exports', onClick: () => router.push('/exports') } });
    },
  });
  return { lifecycle, deleteDraft, exportRun };
}

/** Which tab (top-level bucket) a status belongs to; used for display only. */
export function isTerminal(status: string) {
  return ['completed', 'failed', 'cancelled', 'archived'].includes(status);
}

export function useNow(intervalMs = 1000) {
  const [now, setNow] = React.useState<number | null>(null);
  React.useEffect(() => {
    setNow(Date.now());
    const t = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(t);
  }, [intervalMs]);
  return now;
}
