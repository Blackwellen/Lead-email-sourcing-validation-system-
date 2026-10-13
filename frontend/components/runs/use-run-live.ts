'use client';
import * as React from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { useSSE, type SSEStatus } from '@/hooks/use-sse';
import { isTerminal, type RunDetail } from './run-actions';

export interface RunSnapshot {
  run_id: number;
  status: string;
  stop_reason: string | null;
  at: string;
  counters: Record<string, number | undefined>;
  progress: number;
  target: number;
  percent: number;
  job_states: Record<string, number>;
  throughput: { jobs_done_last_minute: number; qualified_last_minute: number };
  qualified_total: number;
}

export interface LivePoint {
  t: number;
  done: number;
  failed: number;
  queued: number;
  leased: number;
  jobsPerMin: number;
  qualifiedPerMin: number;
  progress: number;
}

export const BUFFER_LIMIT = 300;

/** Run detail (polled) + SSE snapshots accumulated in a bounded client-side buffer. Streaming stops when the run is terminal. */
export function useRunLive(id: number) {
  const qc = useQueryClient();
  const [buffer, setBuffer] = React.useState<LivePoint[]>([]);
  const [latest, setLatest] = React.useState<RunSnapshot | null>(null);
  const [closed, setClosed] = React.useState(false);
  const lastStatus = React.useRef<string | null>(null);
  const sseOpen = React.useRef<SSEStatus>('idle');

  const detail = useQuery({
    queryKey: qk.detail('runs', id),
    queryFn: ({ signal }) => api.get<RunDetail>(`/api/runs/${id}`, { signal, query: { window_minutes: 30 } }),
    refetchInterval: (q) => {
      const s = q.state.data?.status;
      if (s && isTerminal(s)) return false;
      return sseOpen.current === 'open' ? 10_000 : 4_000; // polling fallback when the stream is not open
    },
  });

  const status = latest?.status ?? detail.data?.status;
  const terminal = !!status && isTerminal(status);
  const sse = useSSE<RunSnapshot>(`/api/runs/${id}/events?interval=3`, {
    events: ['snapshot', 'closed'],
    enabled: !terminal && !closed && Number.isFinite(id),
    onEvent: (ev, data) => {
      if (ev === 'closed') { setClosed(true); void qc.invalidateQueries({ queryKey: qk.detail('runs', id) }); return; }
      if (!data || typeof data !== 'object' || !('job_states' in data)) return;
      setLatest(data);
      const js = data.job_states ?? {};
      setBuffer((b) => {
        const next = [...b, {
          t: Date.parse(data.at) || b.length,
          done: js.done ?? 0, failed: (js.failed ?? 0) + (js.dead ?? 0), queued: (js.queued ?? 0) + (js.paused ?? 0), leased: js.leased ?? 0,
          jobsPerMin: data.throughput?.jobs_done_last_minute ?? 0, qualifiedPerMin: data.throughput?.qualified_last_minute ?? 0, progress: data.progress,
        }];
        return next.length > BUFFER_LIMIT ? next.slice(next.length - BUFFER_LIMIT) : next;
      });
      if (lastStatus.current !== data.status) {
        if (lastStatus.current !== null) { void qc.invalidateQueries({ queryKey: qk.detail('runs', id) }); void qc.invalidateQueries({ queryKey: qk.all('runs') }); }
        lastStatus.current = data.status;
      }
    },
  });
  React.useEffect(() => { sseOpen.current = sse.status; }, [sse.status]);

  // once a run is terminal, take one last authoritative read
  React.useEffect(() => { if (terminal) void qc.invalidateQueries({ queryKey: qk.detail('runs', id) }); }, [terminal, qc, id]);

  const mode: 'live' | 'reconnecting' | 'polling' | 'finished' = terminal ? 'finished' : sse.status === 'open' ? 'live' : sse.status === 'reconnecting' || sse.status === 'connecting' ? 'reconnecting' : 'polling';
  return { detail, buffer, latest, mode, terminal };
}
