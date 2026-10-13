'use client';
import * as React from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useQuery } from '@tanstack/react-query';
import { Plus } from 'lucide-react';
import { api, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { listFetcher } from '@/hooks/use-paged-query';
import { useUrlFilters } from '@/hooks/use-url-filters';
import { Button } from '@/components/ui/button';
import { Can } from '@/lib/auth';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { ConfirmDialog } from '@/components/ui/alert-dialog';
import { Progress } from '@/components/ui/progress';
import { PageHeader } from '@/components/composites/page-header';
import { FilterBar } from '@/components/composites/filter-bar';
import { DataTable, createColumnHelper, type RowAction } from '@/components/composites/data-table';
import { StatusBadge } from '@/components/composites/status-badge';
import { RelativeTime } from '@/components/composites/relative-time';
import { EmptyState } from '@/components/composites/empty-state';
import { formatNumber, formatPercent } from '@/lib/format';
import { Play } from 'lucide-react';
import { ACTION_LABEL, availableActions, useRunLifecycle, type RunSummary } from './run-actions';
import { useCategoryTree } from './use-categories';

const TABS = [
  { id: 'active', label: 'Active' },
  { id: 'scheduled', label: 'Scheduled' },
  { id: 'paused', label: 'Paused' },
  { id: 'completed', label: 'Completed' },
  { id: 'failed', label: 'Failed' },
  { id: 'drafts', label: 'Drafts' },
  { id: 'archived', label: 'Archived' },
] as const;
type TabId = (typeof TABS)[number]['id'];
const EMPTY_COPY: Record<TabId, string> = {
  active: 'No runs are queued or running right now.',
  scheduled: 'No runs are scheduled for later.',
  paused: 'No runs are paused.',
  completed: 'No runs have finished yet.',
  failed: 'No runs have failed.',
  drafts: 'No saved drafts. The wizard autosaves a draft once the configuration is complete enough to be stored.',
  archived: 'No archived runs.',
};

/** Per-row detail (countries, collected, eligible, speed). RunSummary does not carry them, so the real run detail is read lazily when the row is visible. */
function useRowDetail(id: number, status: string, ref: React.RefObject<HTMLElement | null>) {
  const [visible, setVisible] = React.useState(false);
  React.useEffect(() => {
    const el = ref.current;
    if (!el || typeof IntersectionObserver === 'undefined') { setVisible(true); return; }
    const io = new IntersectionObserver((es) => { if (es.some((e) => e.isIntersecting)) { setVisible(true); io.disconnect(); } });
    io.observe(el);
    return () => io.disconnect();
  }, [ref]);
  return useQuery({
    queryKey: qk.custom('runs', 'row-detail', id),
    queryFn: ({ signal }) => api.get<Schema<'RunDetail'>>(`/api/runs/${id}`, { signal, query: { window_minutes: 15 } }),
    enabled: visible,
    staleTime: 10_000,
    refetchInterval: ['running', 'queued', 'pausing', 'stopping'].includes(status) ? 10_000 : false,
  });
}

function Extra({ run, pick }: { run: RunSummary; pick: (d: Schema<'RunDetail'>) => React.ReactNode }) {
  const ref = React.useRef<HTMLSpanElement>(null);
  const q = useRowDetail(run.id, run.status, ref);
  return <span ref={ref} className="tabular">{q.isSuccess ? pick(q.data) : q.isError ? <span title="Detail unavailable" className="text-muted-foreground">n/a</span> : <span className="text-muted-foreground">…</span>}</span>;
}

const col = createColumnHelper<RunSummary>();

export function RunsList() {
  const router = useRouter();
  const { filters, set } = useUrlFilters({ tab: 'active', q: '' });
  const tab = (TABS.some((t) => t.id === filters.tab) ? filters.tab : 'active') as TabId;
  const { flat } = useCategoryTree();
  const { lifecycle, deleteDraft, exportRun } = useRunLifecycle();
  const [confirm, setConfirm] = React.useState<{ kind: 'stop' | 'archive' | 'delete'; run: RunSummary } | null>(null);

  const counts = useQuery({
    queryKey: qk.custom('runs', 'tab-counts'),
    queryFn: ({ signal }) => api.get<Schema<'TabCounts'>>('/api/runs/tab-counts', { signal }),
    refetchInterval: 8000,
  });

  const columns = React.useMemo(() => [
    col.accessor('name', {
      header: 'Run name', enableSorting: true,
      cell: (i) => <Link href={`/runs/${i.row.original.id}`} className="font-medium text-foreground hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">{i.getValue()}</Link>,
    }),
    col.display({
      id: 'category', header: 'Category', meta: { hideBelow: 'lg' },
      cell: (i) => {
        const id = i.row.original.destination_category_id ?? i.row.original.category_id;
        return id ? <span className="text-sm">{flat.get(id)?.name ?? `#${id}`}</span> : <span className="text-muted-foreground">—</span>;
      },
    }),
    col.display({ id: 'countries', header: 'Countries', meta: { hideBelow: 'md' }, cell: (i) => <Extra run={i.row.original} pick={(d) => ((d.config?.countries as string[] | undefined) ?? []).join(', ') || '—'} /> }),
    col.accessor('target_count', {
      header: 'Target', enableSorting: false, meta: { hideBelow: 'sm' },
      cell: (i) => <span className="tabular" title={i.row.original.target_definition.replaceAll('_', ' ')}>{formatNumber(i.getValue())}</span>,
    }),
    col.accessor('percent', {
      header: 'Progress', enableSorting: false,
      cell: (i) => (
        <div className="min-w-28">
          <Progress value={i.getValue()} aria-label={`Progress ${formatPercent(i.getValue())}`} tone={i.row.original.status === 'failed' ? 'danger' : i.row.original.status === 'completed' ? 'success' : 'primary'} />
          <span className="tabular mt-0.5 block text-xs text-muted-foreground">{formatNumber(i.row.original.progress)} / {formatNumber(i.row.original.target_count)} ({formatPercent(i.getValue())})</span>
        </div>
      ),
    }),
    col.display({ id: 'collected', header: 'Collected', meta: { hideBelow: 'lg' }, cell: (i) => <Extra run={i.row.original} pick={(d) => formatNumber(Number((d.counters as Record<string, unknown>).emails_found ?? 0))} /> }),
    col.display({ id: 'eligible', header: 'Eligible', meta: { hideBelow: 'lg' }, cell: (i) => <Extra run={i.row.original} pick={(d) => formatNumber(Number((d.counters as Record<string, unknown>).qualified ?? 0))} /> }),
    col.display({
      id: 'speed', header: 'Speed', meta: { hideBelow: 'lg' },
      cell: (i) => <Extra run={i.row.original} pick={(d) => (d.status === 'running' ? `${formatNumber(Math.round(d.throughput.yield_per_minute * 60 * 10) / 10)}/h` : '—')} />,
    }),
    col.accessor('started_at', { header: 'Started', enableSorting: false, meta: { hideBelow: 'md' }, cell: (i) => <RelativeTime value={i.getValue() ?? i.row.original.scheduled_for} fallback="Not started" /> }),
    col.accessor('status', { header: 'Status', enableSorting: false, cell: (i) => <StatusBadge kind="run" status={i.getValue()} /> }),
  ], [flat]);

  const rowActions = (r: RunSummary): RowAction<RunSummary>[] => {
    const a = availableActions(r.status, r.archived);
    const go = (action: Parameters<typeof lifecycle.mutate>[0]['action']) => lifecycle.mutate({ id: r.id, action });
    return [
      { label: 'View', onSelect: () => router.push(`/runs/${r.id}`) },
      { label: 'Edit draft in wizard', hidden: r.status !== 'draft', onSelect: () => router.push(`/runs/new?draft=${r.id}`) },
      { label: ACTION_LABEL.start, hidden: !a.start, onSelect: () => go('start') },
      { label: ACTION_LABEL.pause, hidden: !a.pause, onSelect: () => go('pause') },
      { label: ACTION_LABEL.resume, hidden: !a.resume, onSelect: () => go('resume') },
      { label: ACTION_LABEL.stop, hidden: !a.stop, destructive: true, onSelect: () => setConfirm({ kind: 'stop', run: r }) },
      { label: 'Retry', hidden: !(r.status === 'failed' || r.status === 'completed'), onSelect: () => go('retry-failed') },
      'separator',
      { label: ACTION_LABEL.duplicate, onSelect: () => go('duplicate') },
      { label: 'Export', hidden: !a.export, onSelect: () => exportRun.mutate({ id: r.id }) },
      'separator',
      { label: ACTION_LABEL.archive, hidden: !a.archive, destructive: true, onSelect: () => setConfirm({ kind: 'archive', run: r }) },
      { label: ACTION_LABEL.delete, hidden: !a.delete, destructive: true, onSelect: () => setConfirm({ kind: 'delete', run: r }) },
    ];
  };

  const confirmCopy = confirm && {
    stop: { title: `Gracefully stop “${confirm.run.name}”?`, desc: 'No new work is handed out and queued jobs are cancelled. Jobs already running may finish and commit before the run completes.', label: 'Stop run' },
    archive: { title: `Archive “${confirm.run.name}”?`, desc: 'Archived runs leave the main tabs and can be found under Archived. Collected data is kept.', label: 'Archive' },
    delete: { title: `Delete draft “${confirm.run.name}”?`, desc: 'The draft configuration is permanently removed.', label: 'Delete draft' },
  }[confirm.kind];

  return (
    <>
      <PageHeader
        title="Runs"
        description="Create, monitor and manage sourcing runs."
        actions={<Can role="operator"><Button asChild><Link href="/runs/new"><Plus /> New sourcing run</Link></Button></Can>}
      />
      <Tabs value={tab} onValueChange={(v) => set({ tab: v === 'active' ? undefined : v })}>
        <TabsList aria-label="Run status">
          {TABS.map((t) => (
            <TabsTrigger key={t.id} value={t.id} count={counts.data ? counts.data[t.id] : undefined}>{t.label}</TabsTrigger>
          ))}
        </TabsList>
        {TABS.map((t) => (
          <TabsContent key={t.id} value={t.id}>
            {tab === t.id && (
              <>
                <FilterBar search={{ value: filters.q ?? '', onChange: (v) => set({ q: v }), placeholder: 'Search run name…', label: 'Search runs' }} />
                <DataTable<RunSummary>
                  caption={`${t.label} runs`}
                  tableId="runs"
                  columns={columns}
                  queryKey={qk.list('runs')}
                  fetchPage={listFetcher<RunSummary>('/api/runs')}
                  filters={{ tab, q: filters.q || undefined }}
                  defaultPageSize={25}
                  refetchInterval={tab === 'active' ? 5000 : false}
                  onRowOpen={(r) => router.push(r.status === 'draft' ? `/runs/new?draft=${r.id}` : `/runs/${r.id}`)}
                  rowActions={rowActions}
                  emptyState={
                    <EmptyState icon={Play} title={`No ${t.label.toLowerCase()} runs`} description={filters.q ? 'No run name matches your search.' : EMPTY_COPY[t.id]}
                      action={<Can role="operator"><Button asChild size="sm"><Link href="/runs/new"><Plus /> New sourcing run</Link></Button></Can>} />
                  }
                />
              </>
            )}
          </TabsContent>
        ))}
      </Tabs>
      {confirm && confirmCopy && (
        <ConfirmDialog
          open
          onOpenChange={(o) => { if (!o) setConfirm(null); }}
          title={confirmCopy.title}
          description={confirmCopy.desc}
          confirmLabel={confirmCopy.label}
          destructive
          onConfirm={async () => {
            if (confirm.kind === 'delete') await deleteDraft.mutateAsync({ id: confirm.run.id });
            else await lifecycle.mutateAsync({ id: confirm.run.id, action: confirm.kind === 'stop' ? 'stop' : 'archive' });
          }}
        />
      )}
    </>
  );
}

