'use client';
import * as React from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { Download, Eye, FolderPlus, GitMerge, RefreshCw } from 'lucide-react';
import { api, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { useMe } from '@/lib/auth';
import { useApiMutation } from '@/hooks/use-api-mutation';
import { listFetcher } from '@/hooks/use-paged-query';
import { DataTable, createColumnHelper } from '@/components/composites/data-table';
import { ConfidenceMeter } from '@/components/composites/confidence-meter';
import { RelativeTime } from '@/components/composites/relative-time';
import { EmptyState } from '@/components/composites/empty-state';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/alert-dialog';
import { BusinessFilterBar, businessQuery, useBusinessFilters } from './business-filters';
import { AddToCategoryDialog, ExportBusinessesDialog, MergeDialog } from './business-bulk';
import { DomainBadge } from './domain-badge';

type Row = Schema<'BusinessRow'>;
const col = createColumnHelper<Row>();
const fetchPage = listFetcher<Row>('/api/businesses');
const dash = <span className="text-muted-foreground">—</span>;

export function BusinessesPage() {
  const router = useRouter();
  const { can } = useMe();
  const { filters, set, reset } = useBusinessFilters();
  const query = React.useMemo(() => businessQuery(filters), [filters]);
  const [dialog, setDialog] = React.useState<null | { kind: 'category' | 'export' | 'merge'; rows: Row[]; clear: () => void }>(null);

  const refresh = useApiMutation<Schema<'BusinessBulkOut'>, number[]>({
    mutationFn: (ids) => api.post('/api/businesses/bulk', { action: 'queue_refresh', ids }),
    successMessage: (d) => `Refresh queued: ${d.jobs_queued ?? 0} job${d.jobs_queued === 1 ? '' : 's'} for ${d.affected} business${d.affected === 1 ? '' : 'es'}`,
    invalidate: [qk.all('businesses')],
  });

  const columns = React.useMemo(() => [
    col.accessor((r) => r.legal_name ?? r.trading_name ?? `Business #${r.id}`, {
      id: 'name', header: 'Business', enableSorting: true, meta: { className: 'min-w-48' },
      cell: (i) => (
        <span className="flex flex-wrap items-center gap-1.5">
          <Link href={`/businesses/${i.row.original.id}`} className="font-medium text-primary underline-offset-2 hover:underline">{i.getValue()}</Link>
          {i.row.original.merged_into_id && <Badge variant="neutral">Merged into #{i.row.original.merged_into_id}</Badge>}
        </span>
      ),
    }),
    col.accessor('trading_name', { header: 'Trading name', enableSorting: false, meta: { hideBelow: 'lg' }, cell: (i) => i.getValue() ?? dash }),
    col.accessor('category_name', { header: 'Category', enableSorting: false, meta: { hideBelow: 'md' }, cell: (i) => i.getValue() ?? dash }),
    col.accessor('country_code', { header: 'Country', enableSorting: false }),
    col.accessor((r) => [r.city, r.region].filter(Boolean).join(', '), { id: 'area', header: 'Area', enableSorting: false, meta: { hideBelow: 'lg' }, cell: (i) => i.getValue() || dash }),
    col.accessor('domain', {
      header: 'Website', enableSorting: false,
      cell: (i) => (
        <span className="flex flex-col items-start gap-1">
          {i.getValue() ? <span className="break-all text-xs">{i.getValue()}</span> : null}
          <DomainBadge status={i.row.original.domain_status} />
        </span>
      ),
    }),
    col.accessor('email_count', { header: 'Emails found', enableSorting: false, meta: { className: 'tabular text-right', hideBelow: 'md' } }),
    col.accessor('published_email_count', { header: 'Published', enableSorting: false, meta: { className: 'tabular text-right', hideBelow: 'md' } }),
    col.accessor('best_confidence', { header: 'Data quality', enableSorting: false, meta: { hideBelow: 'lg' }, cell: (i) => <ConfidenceMeter value={i.getValue()} /> }),
    col.accessor('last_checked_at', { header: 'Last checked', enableSorting: true, meta: { hideBelow: 'md' }, cell: (i) => (i.getValue() ? <RelativeTime value={i.getValue()} /> : <span className="text-muted-foreground">Never</span>) }),
  ], []);

  return (
    <>
      <BusinessFilterBar filters={filters} set={set} reset={reset} />
      <DataTable<Row>
        caption="Businesses"
        tableId="businesses"
        columns={columns}
        queryKey={qk.list('businesses')}
        fetchPage={fetchPage}
        filters={query}
        selectable
        onRowOpen={(r) => router.push(`/businesses/${r.id}`)}
        emptyState={Object.keys(query).length ? <EmptyState title="No businesses match these filters" description="Try removing a filter." /> : <EmptyState title="No businesses yet" description="Businesses appear here after a run discovers organisations." />}
        rowActions={(r) => [{ label: 'View details', icon: <Eye />, onSelect: () => router.push(`/businesses/${r.id}`) }]}
        bulkActions={({ rows, ids, clear }) => (
          <div className="flex flex-wrap items-center gap-2">
            {can('operator') && <Button size="sm" variant="outline" onClick={() => setDialog({ kind: 'category', rows, clear })}><FolderPlus /> Add to category</Button>}
            {can('operator') && (
              <ConfirmDialog
                trigger={<Button size="sm" variant="outline"><RefreshCw /> Queue refresh</Button>}
                title={`Queue a refresh for ${ids.length} business${ids.length === 1 ? '' : 'es'}?`}
                description="Re-resolves domains and re-crawls confirmed websites in the background, politely and rate-limited. Results appear as the jobs complete."
                confirmLabel="Queue refresh"
                onConfirm={async () => { await refresh.mutateAsync(ids.map(Number)); clear(); }}
              />
            )}
            {can('operator') && <Button size="sm" variant="outline" onClick={() => setDialog({ kind: 'export', rows, clear })}><Download /> Export selection</Button>}
            {can('admin') && <Button size="sm" variant="outline" disabled={rows.length < 2 || rows.length > 51} title={rows.length < 2 ? 'Select at least two businesses' : undefined} onClick={() => setDialog({ kind: 'merge', rows, clear })}><GitMerge /> Merge duplicates…</Button>}
          </div>
        )}
      />
      <AddToCategoryDialog open={dialog?.kind === 'category'} onOpenChange={(o) => !o && setDialog(null)} ids={(dialog?.rows ?? []).map((r) => r.id)} onDone={() => dialog?.clear()} />
      <ExportBusinessesDialog open={dialog?.kind === 'export'} onOpenChange={(o) => !o && setDialog(null)} ids={(dialog?.rows ?? []).map((r) => r.id)} onDone={() => dialog?.clear()} />
      <MergeDialog open={dialog?.kind === 'merge'} onOpenChange={(o) => !o && setDialog(null)} rows={dialog?.rows ?? []} onDone={() => dialog?.clear()} />
    </>
  );
}
