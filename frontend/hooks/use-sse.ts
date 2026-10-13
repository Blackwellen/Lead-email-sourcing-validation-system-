'use client';
import { useEffect, useRef, useState } from 'react';

export type SSEStatus = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'closed';

export interface UseSSEOptions<T> {
  /** Named SSE events to listen for (default: ['message','snapshot']). */
  events?: string[];
  /** Called for every parsed event. JSON is parsed when possible. */
  onEvent?: (event: string, data: T) => void;
  enabled?: boolean;
  /** Backoff bounds in ms (default 1000 -> 30000, exponential with jitter). */
  minDelay?: number;
  maxDelay?: number;
  /** Stop reconnecting after this many consecutive failures (default: unlimited). */
  maxRetries?: number;
}

/**
 * EventSource with automatic reconnect + exponential backoff. Closes on unmount or when `url`/`enabled` change.
 * Returns the latest payload per event name plus connection status.
 *   const { status, last, data } = useSSE<RunSnapshot>(`/api/runs/${id}/events`, { events: ['snapshot'] });
 */
export function useSSE<T = unknown>(url: string | null | undefined, opts: UseSSEOptions<T> = {}) {
  const { events = ['message', 'snapshot'], enabled = true, minDelay = 1000, maxDelay = 30000, maxRetries } = opts;
  const [status, setStatus] = useState<SSEStatus>('idle');
  const [data, setData] = useState<Record<string, T>>({});
  const [last, setLast] = useState<{ event: string; data: T } | null>(null);
  const [retries, setRetries] = useState(0);
  const onEventRef = useRef(opts.onEvent);
  useEffect(() => { onEventRef.current = opts.onEvent; });
  const eventsKey = events.join('|');

  useEffect(() => {
    if (!url || !enabled || typeof EventSource === 'undefined') { setStatus('idle'); return; }
    let es: EventSource | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;
    let disposed = false;

    const connect = () => {
      if (disposed) return;
      setStatus(attempt === 0 ? 'connecting' : 'reconnecting');
      es = new EventSource(url, { withCredentials: true });
      es.onopen = () => { attempt = 0; setRetries(0); setStatus('open'); };
      const handler = (name: string) => (ev: MessageEvent) => {
        let payload: unknown = ev.data;
        try { payload = JSON.parse(ev.data); } catch { /* keep raw string */ }
        setData((d) => ({ ...d, [name]: payload as T }));
        setLast({ event: name, data: payload as T });
        onEventRef.current?.(name, payload as T);
      };
      for (const name of eventsKey.split('|')) es.addEventListener(name, handler(name) as EventListener);
      es.onerror = () => {
        es?.close(); es = null;
        if (disposed) return;
        attempt += 1; setRetries(attempt);
        if (maxRetries !== undefined && attempt > maxRetries) { setStatus('closed'); return; }
        setStatus('reconnecting');
        const base = Math.min(maxDelay, minDelay * 2 ** (attempt - 1));
        timer = setTimeout(connect, base / 2 + Math.random() * (base / 2));
      };
    };
    connect();
    return () => { disposed = true; if (timer) clearTimeout(timer); es?.close(); setStatus('closed'); };
  }, [url, enabled, eventsKey, minDelay, maxDelay, maxRetries]);

  return { status, data, last, retries };
}
