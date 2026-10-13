'use client';
import { useInfiniteQuery, type QueryKey } from '@tanstack/react-query';
import { api, type Page, type Query } from '@/lib/api';

export interface PageArgs<F = Record<string, unknown>> {
  cursor: string | null;
  limit: number;
  filters: F;
  signal?: AbortSignal;
}
export type FetchPage<T, F = Record<string, unknown>> = (args: PageArgs<F>) => Promise<Page<T>>;

/** Build a `fetchPage` for a backend list endpoint: `listFetcher<RunSummary>('/api/runs')`. Filters are sent as query params. */
export function listFetcher<T, F extends Record<string, unknown> = Record<string, unknown>>(path: string): FetchPage<T, F> {
  return ({ cursor, limit, filters, signal }) => api.get<Page<T>>(path, { query: { ...(filters as Query), limit, cursor }, signal });
}

export interface UsePagedQueryOptions<T, F> {
  queryKey: QueryKey;
  fetchPage: FetchPage<T, F>;
  filters?: F;
  limit?: number;
  enabled?: boolean;
  refetchInterval?: number | false;
}

/**
 * Cursor-based infinite list. `items` is the flattened concatenation; `total`/`approximate` come from the
 * latest page (render as "~1,234" when approximate). Filters are part of the query key, so changing them resets paging.
 */
export function usePagedQuery<T, F = Record<string, unknown>>({ queryKey, fetchPage, filters, limit = 50, enabled, refetchInterval }: UsePagedQueryOptions<T, F>) {
  const f = (filters ?? {}) as F;
  const q = useInfiniteQuery({
    queryKey: [...queryKey, { filters: f, limit }],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) => fetchPage({ cursor: pageParam, limit, filters: f, signal }),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled,
    refetchInterval,
  });
  const pages = q.data?.pages ?? [];
  const lastPage = pages[pages.length - 1];
  return {
    ...q,
    items: pages.flatMap((p) => p.items),
    total: lastPage?.total_estimate ?? null,
    approximate: lastPage?.approximate ?? false,
  };
}
