'use client';
import { useInfiniteQuery, type QueryKey } from '@tanstack/react-query';
import { api, type Page, type Query } from '@/lib/api';

export interface SortState { id: string; desc: boolean }
export interface PageArgs<F = Record<string, unknown>> {
  cursor: string | null;
  limit: number;
  filters: F;
  /** Server-side sort requested by the table header (undefined = backend default). */
  sort?: SortState | null;
  signal?: AbortSignal;
}
export type FetchPage<T, F = Record<string, unknown>> = (args: PageArgs<F>) => Promise<Page<T>>;

/** Build a `fetchPage` for a backend list endpoint: `listFetcher<RunSummary>('/api/runs')`. Filters are sent as query params. */
export function listFetcher<T, F extends Record<string, unknown> = Record<string, unknown>>(path: string): FetchPage<T, F> {
  return ({ cursor, limit, filters, sort, signal }) =>
    api.get<Page<T>>(path, { query: { ...(filters as Query), limit, cursor, ...(sort ? { sort: sort.id, direction: sort.desc ? 'desc' : 'asc' } : {}) }, signal });
}

export interface UsePagedQueryOptions<T, F> {
  queryKey: QueryKey;
  fetchPage: FetchPage<T, F>;
  filters?: F;
  limit?: number;
  sort?: SortState | null;
  enabled?: boolean;
  refetchInterval?: number | false;
}

/**
 * Cursor-based infinite list. `items` is the flattened concatenation. `total`/`approximate` come from the FIRST page:
 * backends only count on the first (cursor-less) request and send `total_estimate: null` on later pages, so the first
 * page's total is kept for the life of the list (render as "~1,234" when approximate). Filters are part of the query key,
 * so changing them resets paging (and the total).
 */
export function usePagedQuery<T, F = Record<string, unknown>>({ queryKey, fetchPage, filters, limit = 50, sort, enabled, refetchInterval }: UsePagedQueryOptions<T, F>) {
  const f = (filters ?? {}) as F;
  const q = useInfiniteQuery({
    queryKey: [...queryKey, { filters: f, limit, sort: sort ?? null }],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) => fetchPage({ cursor: pageParam, limit, filters: f, sort, signal }),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled,
    refetchInterval,
  });
  const pages = q.data?.pages ?? [];
  // First page carries the count; fall back to the most recent page that reported one (a backend may count every page).
  const counted = pages.find((p) => p.total_estimate !== null && p.total_estimate !== undefined) ?? null;
  return {
    ...q,
    items: pages.flatMap((p) => p.items),
    total: counted?.total_estimate ?? null,
    approximate: counted?.approximate ?? false,
  };
}
