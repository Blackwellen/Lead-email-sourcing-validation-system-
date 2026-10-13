/**
 * Query-key conventions.
 *   qk.me                                  -> ['me']
 *   qk.list('runs', {tab:'active'})        -> ['runs','list',{tab:'active'}]
 *   qk.detail('runs', 12)                  -> ['runs','detail','12']
 *   qk.custom('runs', 'tab-counts')        -> ['runs','tab-counts']
 * Invalidate a whole resource with `queryClient.invalidateQueries({ queryKey: qk.all('runs') })`.
 * Always pass filters as a plain object so keys are structurally compared by TanStack Query.
 */
export type Filters = Record<string, unknown>;

export const qk = {
  me: ['me'] as const,
  maintenance: ['system', 'maintenance'] as const,
  all: (resource: string) => [resource] as const,
  list: (resource: string, filters: Filters = {}) => [resource, 'list', filters] as const,
  detail: (resource: string, id: string | number) => [resource, 'detail', String(id)] as const,
  custom: (resource: string, ...parts: (string | number | Filters)[]) => [resource, ...parts] as const,
};
