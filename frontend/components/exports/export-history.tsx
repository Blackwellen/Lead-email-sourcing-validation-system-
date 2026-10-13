'use client';
import * as React from 'react';
import { useQuery } from '@tanstack/react-query';
import { Download, Filter, Trash2, TriangleAlert } from 'lucide-react';
import { api, downloadBlob, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { listFetcher, usePagedQuery } from '@/hooks/use-paged-query';
import { useApiMutation } from '@/hooks/use-api-mutation';
import { Can, useMe } from '@/lib/auth';
import { formatBytes, formatNumber } from '@/lib/format';
import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/alert-dialog';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { StatusBadge } from '@/components/composites/status-badge';
import { RelativeTime } from '@/components/composites/relative-time';
import { EmptyState } from '@/components/composites/empty-state';
import { ErrorState } from '@/components/composites/error-state';
import { LoadingState } from '@/components/composites/loading-state';
import { ScrollRegion, tdClass, thClass } from '@/components/system/scroll-region';
import { summariseFilters, type ExportRow, type PresetInfo } from './export-model';

const ACTIVE = new Set(['queued', 'running']);

export function ExportHistory({ presets }: { presets: PresetInfo[] }) {
  const me = useMe();
  const [poll, setPoll] = React.useState(false);
  const q = usePagedQuery<ExportRow>({ queryKey: qk.list('exports'), fetchPage: listFetcher<ExportRow>('/api/exports'), limit: 25, refetchInterval: poll ? 2000 : false });
  React.useEffect(() => { setPoll(q.items.some((e) => ACTIVE.has(e.status))); }, [q.items]);

  // creator names are admin-visible only (GET /api/users is admin-only); everyone else only sees their own exports
  const users = useQuery({
    queryKey: qk.custom('users', 'names'), enabled: me.can('admin'),
    queryFn: ({ signal }) => api.get<{ items: Schema<'UserOut'>[] }>('/api/users', { query: { limit: 200 }, signal }),
    staleTime: 60_000,
  });
  const nameOf = (id: number | null) => {
    if (id === null) return 'System';
    if (id === me.user?.id) return 'You';
    return users.data?.items.find((u) => u.id === id)?.email ?? `User #${id}`;
  };
  const presetLabel = (id: string) => presets.find((p) => p.id === id)?.label ?? id;

  const dl = useApiMutation({
    mutationFn: async (e: ExportRow) => { const blob = await api.get<Blob>(`/api/exports/${e.id}/download`); downloadBlob(blob, `export-${e.id}-${e.preset}.${e.format}`); return e; },
    successMessage: 'Download started',
  });
  const del = useApiMutation({ mutationFn: (e: ExportRow) => api.delete(`/api/exports/${e.id}`), successMessage: 'Export deleted', invalidate: [qk.all('exports')] });

  return (
    <section aria-labelledby="hist-h" className="mt-8">
      <div className="mb-3 flex items-baseline justify-between gap-2">
        <h2 id="hist-h" className="text-base font-semibold">Export history</h2>
        {poll && <span className="text-xs text-muted-foreground" role="status">Updating while exports are running…</span>}
      </div>
      {q.isPending ? <LoadingState variant="skeleton" rows={4} label="Loading exports" /> : q.isError ? <ErrorState error={q.error} onRetry={() => q.refetch()} /> : q.items.length === 0 ? (
        <EmptyState compact title="No exports yet" description={me.can('operator') ? 'Create one above; it will appear here.' : 'Exports created by operators are visible to their creator and administrators.'} />
      ) : (
        <>
          <ScrollRegion label="Export history table">
            <table className="w-full min-w-[44rem] border-collapse" data-testid="exports-table">
              <caption className="sr-only">Export jobs with status, row count, size, filters, creator and creation time</caption>
              <thead>
                <tr>
                  <th scope="col" className={thClass}>Export</th>
                  <th scope="col" className={thClass}>Status</th>
                  <th scope="col" className={`${thClass} text-right`}>Rows</th>
                  <th scope="col" className={`${thClass} text-right max-sm:hidden`}>Size</th>
                  <th scope="col" className={thClass}>Filters</th>
                  <th scope="col" className={`${thClass} max-md:hidden`}>Created by</th>
                  <th scope="col" className={`${thClass} max-sm:hidden`}>Created</th>
                  <th scope="col" className={thClass}><span className="sr-only">Actions</span></th>
                </tr>
              </thead>
              <tbody>
                {q.items.map((e) => {
                  const f = summariseFilters(e.filters);
                  return (
                    <tr key={e.id} data-testid={`export-row-${e.id}`}>
                      <th scope="row" className={`${tdClass} text-left font-medium`}>
                        #{e.id} · {presetLabel(e.preset)}
                        <span className="block text-xs font-normal uppercase text-muted-foreground">{e.format}</span>
                      </th>
                      <td className={tdClass}>
                        <StatusBadge kind="job" status={e.status} />
                        {e.status === 'failed' && e.error && <span className="mt-1 flex max-w-[16rem] items-start gap-1 text-xs text-danger-foreground"><TriangleAlert className="mt-0.5 size-3 shrink-0" aria-hidden />{e.error}</span>}
                      </td>
                      <td className={`${tdClass} tabular text-right`}>{e.status === 'done' ? formatNumber(e.row_count) : '—'}</td>
                      <td className={`${tdClass} tabular text-right max-sm:hidden`}>{e.file_size != null ? formatBytes(e.file_size) : '—'}</td>
                      <td className={tdClass}>
                        <Popover>
                          <PopoverTrigger asChild><Button variant="ghost" size="sm" aria-label={`Filters for export ${e.id}`}><Filter /> {f.length ? `${f.length} filter${f.length === 1 ? '' : 's'}` : 'None'}</Button></PopoverTrigger>
                          <PopoverContent className="w-80">
                            <p className="mb-2 text-xs font-medium">Filters used (preset: {presetLabel(e.preset)})</p>
                            {f.length === 0 ? <p className="text-xs text-muted-foreground">No filters beyond the preset defaults.</p> : (
                              <dl className="space-y-1 text-xs">{f.map(([k, v]) => <div key={k} className="flex gap-2"><dt className="w-32 shrink-0 text-muted-foreground">{k}</dt><dd className="min-w-0 break-words">{v}</dd></div>)}</dl>
                            )}
                          </PopoverContent>
                        </Popover>
                      </td>
                      <td className={`${tdClass} max-md:hidden`}>{nameOf(e.created_by)}</td>
                      <td className={`${tdClass} max-sm:hidden`}><RelativeTime value={e.created_at} /></td>
                      <td className={`${tdClass} whitespace-nowrap text-right`}>
                        <Button variant="outline" size="sm" disabled={e.status !== 'done'} loading={dl.isPending && dl.variables?.id === e.id} onClick={() => dl.mutate(e)} aria-label={`Download export ${e.id}`}><Download /> Download</Button>
                        <Can role="operator">
                          <ConfirmDialog
                            trigger={<Button variant="ghost" size="icon-sm" disabled={e.status === 'running'} aria-label={`Delete export ${e.id}`}><Trash2 /></Button>}
                            title={`Delete export #${e.id}?`} destructive confirmLabel="Delete export"
                            description="The file is removed from the server and cannot be restored. You can create the export again at any time; the data itself is not affected."
                            onConfirm={() => del.mutateAsync(e)}
                          />
                        </Can>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </ScrollRegion>
          {q.hasNextPage && <div className="mt-3 text-center"><Button variant="outline" onClick={() => q.fetchNextPage()} loading={q.isFetchingNextPage}>Load more</Button></div>}
        </>
      )}
    </section>
  );
}
