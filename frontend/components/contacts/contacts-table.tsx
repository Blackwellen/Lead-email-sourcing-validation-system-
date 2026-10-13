'use client';
import * as React from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { Download, Eye, Flag, RefreshCw, Ban, ShieldOff } from 'lucide-react';
import { api, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { useMe } from '@/lib/auth';
import { useApiMutation } from '@/hooks/use-api-mutation';
import { listFetcher } from '@/hooks/use-paged-query';
import { DataTable, createColumnHelper } from '@/components/composites/data-table';
import { StatusBadge } from '@/components/composites/status-badge';
import { ConfidenceMeter } from '@/components/composites/confidence-meter';
import { RelativeTime } from '@/components/composites/relative-time';
import { EmptyState } from '@/components/composites/empty-state';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { ConfirmDialog } from '@/components/ui/alert-dialog';
import { SuppressDialog, useContactMutations } from './contact-actions';
import { RecipientBadge } from './shared';

type Row = Schema<'ContactRow'>;
const col = createColumnHelper<Row>();

/** Backend vocabulary for catch-all includes not_catch_all in explanations; table uses the persisted status. */
export function ContactsTable({
  caption, tableId, queryKey, endpoint, filters, extraParams, toolbar, emptyFiltered,
}: {
  caption: string;
  tableId: string;
  queryKey: readonly unknown[];
  endpoint: string;
  filters: Record<string, unknown>;
  /** Fixed params merged into every request (e.g. `{ tab: 'invalid' }`). */
  extraParams?: Record<string, string>;
  toolbar?: React.ReactNode;
  emptyFiltered?: boolean;
}) {
  const router = useRouter();
  const { can } = useMe();
  const operator = can('operator');
  const m = useContactMutations();
  const [suppressTarget, setSuppressTarget] = React.useState<{ ids: number[]; label: string } | null>(null);
  const [clearSel, setClearSel] = React.useState<(() => void) | null>(null);

  const merged = React.useMemo(() => ({ ...filters, ...extraParams }), [filters, extraParams]);
  const fetchPage = React.useMemo(() => listFetcher<Row>(endpoint), [endpoint]);

  const columns = React.useMemo(() => [
    col.accessor('email', {
      header: 'Email', enableSorting: true, meta: { className: 'min-w-56' },
      cell: (i) => <Link href={`/contacts/${i.row.original.id}`} className="font-medium text-primary underline-offset-2 hover:underline break-all">{i.getValue()}</Link>,
    }),
    col.accessor('full_name', {
      id: 'person', header: 'Contact person', enableSorting: false, meta: { hideBelow: 'lg' },
      cell: (i) => i.getValue() ?? (i.row.original.role_type ? <span className="text-muted-foreground">Role mailbox ({i.row.original.role_type})</span> : <span className="text-muted-foreground">—</span>),
    }),
    col.accessor('job_title', { header: 'Position', enableSorting: false, meta: { hideBelow: 'lg' }, cell: (i) => i.getValue() ?? <span className="text-muted-foreground">—</span> }),
    col.accessor('company_name', {
      header: 'Business', enableSorting: false,
      cell: (i) => <Link href={`/businesses/${i.row.original.organisation_id}`} className="underline-offset-2 hover:underline">{i.getValue() ?? `Business #${i.row.original.organisation_id}`}</Link>,
    }),
    col.accessor('domain', { header: 'Website (mail domain)', enableSorting: false, meta: { hideBelow: 'lg' }, cell: (i) => <span className="break-all text-xs">{i.getValue()}</span> }),
    col.accessor('category_name', { header: 'Category', enableSorting: false, meta: { hideBelow: 'lg' }, cell: (i) => i.getValue() ?? <span className="text-muted-foreground">—</span> }),
    col.accessor('country_code', { header: 'Country', enableSorting: false, meta: { hideBelow: 'md' } }),
    col.accessor('origin', {
      header: 'Origin', enableSorting: false, meta: { hideBelow: 'lg' },
      cell: (i) => <StatusBadge kind="origin" status={i.getValue()} />,
    }),
    col.accessor('confidence', { header: 'Confidence', enableSorting: true, cell: (i) => <ConfidenceMeter value={i.getValue()} /> }),
    col.accessor('domain_mail_status', { header: 'Mail routing', enableSorting: false, meta: { hideBelow: 'md' }, cell: (i) => <StatusBadge kind="mail" status={i.getValue()} /> }),
    col.accessor('overall_reachability', {
      header: 'Reachability', enableSorting: false,
      cell: (i) => (
        <span className="flex flex-wrap items-center gap-1">
          <StatusBadge kind="reachability" status={i.getValue()} />
          {i.row.original.suppressed && <Badge variant="danger">Suppressed</Badge>}
          {i.row.original.needs_review && <Badge variant="warning">Review</Badge>}
        </span>
      ),
    }),
    col.accessor('recipient_evidence_status', { header: 'Mailbox evidence', enableSorting: false, meta: { hideBelow: 'lg' }, cell: (i) => <RecipientBadge status={i.getValue()} /> }),
    col.accessor('catch_all_status', { header: 'Catch-all', enableSorting: false, meta: { hideBelow: 'lg' }, cell: (i) => <StatusBadge kind="catchall" status={i.getValue()} /> }),
    col.accessor('last_validated_at', {
      header: 'Last checked', enableSorting: true, meta: { hideBelow: 'md' },
      cell: (i) => (i.getValue() ? <RelativeTime value={i.getValue()} /> : <span className="text-muted-foreground">Never</span>),
    }),
  ], []);

  const exportPermitted = useApiMutation<Schema<'ExportOut'>, number[]>({
    mutationFn: (ids) => api.post('/api/exports', { preset: 'full_intelligence', format: 'csv', filters: { email_ids: ids, exclude_suppressed: true }, wait: false }),
    successMessage: (d) => `Export #${d.id} created (suppressed addresses excluded). Find it on the Exports page.`,
    invalidate: [qk.all('exports')],
  });
  const bulk = useApiMutation<Schema<'ContactBulkOut'>, { action: 'revalidate' | 'mark_needs_review' | 'clear_needs_review' | 'unsuppress'; ids: number[] }>({
    mutationFn: (b) => api.post('/api/contacts/bulk', { ...b, mode: 'fast' }),
    successMessage: (d) => `${d.action.replaceAll('_', ' ')}: ${d.affected} of ${d.requested} addresses`,
    invalidate: [qk.all('contacts'), qk.all('verification'), qk.all('categories')],
  });

  return (
    <>
      <DataTable<Row>
        caption={caption}
        tableId={tableId}
        columns={columns}
        queryKey={queryKey as unknown[]}
        fetchPage={fetchPage}
        filters={merged}
        selectable
        defaultPageSize={50}
        onRowOpen={(r) => router.push(`/contacts/${r.id}`)}
        toolbar={toolbar}
        emptyState={emptyFiltered ? <EmptyState title="No contacts match these filters" description="Try removing a filter or broadening the search." /> : <EmptyState title="No contacts yet" description="Contacts appear here once a run has discovered and validated addresses." />}
        rowActions={(r) => [
          { label: 'View details', icon: <Eye />, onSelect: () => router.push(`/contacts/${r.id}`) },
          { label: 'Re-validate', icon: <RefreshCw />, hidden: !operator, onSelect: () => m.revalidate.mutate({ id: r.id }) },
          { label: r.needs_review ? 'Clear needs review' : 'Mark needs review', icon: <Flag />, hidden: !operator, onSelect: () => m.needsReview.mutate({ id: r.id, value: !r.needs_review }) },
          'separator',
          { label: 'Suppress…', icon: <Ban />, destructive: true, hidden: !operator || r.suppressed, onSelect: () => setSuppressTarget({ ids: [r.id], label: r.email }) },
          { label: 'Unsuppress (address)', icon: <ShieldOff />, hidden: !operator || !r.suppressed, onSelect: () => m.unsuppress.mutate({ id: r.id, scope: 'email' }) },
        ]}
        bulkActions={({ ids, rows, clear }) => {
          const numeric = ids.map(Number);
          const permitted = rows.filter((r) => !r.suppressed).length;
          return (
            <div className="flex flex-wrap items-center gap-2">
              {operator && <Button size="sm" variant="outline" loading={bulk.isPending} onClick={() => bulk.mutate({ action: 'revalidate', ids: numeric }, { onSuccess: clear })}><RefreshCw /> Re-validate</Button>}
              {operator && <Button size="sm" variant="outline" onClick={() => bulk.mutate({ action: 'mark_needs_review', ids: numeric }, { onSuccess: clear })}><Flag /> Needs review</Button>}
              {operator && <Button size="sm" variant="outline" onClick={() => { setClearSel(() => clear); setSuppressTarget({ ids: numeric, label: `${ids.length} addresses` }); }}><Ban /> Suppress…</Button>}
              {operator && (
                <ConfirmDialog
                  trigger={<Button size="sm" variant="outline"><Download /> Export permitted</Button>}
                  title={`Export ${permitted} permitted address${permitted === 1 ? '' : 'es'}?`}
                  description={<>Creates a CSV export of the selected addresses, <strong>excluding {ids.length - permitted} suppressed</strong>. Marketing eligibility is not implied; check the “marketing” column before use.</>}
                  confirmLabel="Create export"
                  onConfirm={async () => { await exportPermitted.mutateAsync(numeric); clear(); }}
                />
              )}
            </div>
          );
        }}
      />
      <SuppressDialog open={!!suppressTarget} onOpenChange={(o) => { if (!o) setSuppressTarget(null); }} target={suppressTarget} onDone={() => clearSel?.()} />
    </>
  );
}
