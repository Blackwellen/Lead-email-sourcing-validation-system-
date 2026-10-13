'use client';
import { useSyncExternalStore } from 'react';

/** Global transport-level state surfaced by the shell banners. */
export interface ApiStatus {
  /** Backend unreachable (network error / 502 / 504). */
  unreachable: boolean;
  /** Set when the API answered 503 maintenance_mode. */
  maintenance: { message: string } | null;
}

let state: ApiStatus = { unreachable: false, maintenance: null };
const listeners = new Set<() => void>();

export function setApiStatus(patch: Partial<ApiStatus>) {
  const next = { ...state, ...patch };
  if (next.unreachable === state.unreachable && next.maintenance?.message === state.maintenance?.message) return;
  state = next;
  listeners.forEach((l) => l());
}

export function getApiStatus() {
  return state;
}

const SERVER: ApiStatus = { unreachable: false, maintenance: null };

export function useApiStatus(): ApiStatus {
  return useSyncExternalStore(
    (cb) => { listeners.add(cb); return () => { listeners.delete(cb); }; },
    () => state,
    () => SERVER,
  );
}
