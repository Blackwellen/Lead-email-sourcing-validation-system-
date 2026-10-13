'use client';
import * as React from 'react';
import { useQuery } from '@tanstack/react-query';
import { Info, RefreshCw, ShieldAlert } from 'lucide-react';
import { api } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { useApiMutation } from '@/hooks/use-api-mutation';
import { useMe } from '@/lib/auth';
import { formatNumber, formatPercent } from '@/lib/format';
import { PageHeader } from '@/components/composites/page-header';
import { FilterBar, FilterSelect, type FilterChip } from '@/components/composites/filter-bar';
import { SimpleSelect } from '@/components/ui/select';
import { LoadingState } from '@/components/composites/loading-state';
import { ErrorState } from '@/components/composites/error-state';
import { EmptyState } from '@/components/composites/empty-state';
import { RelativeTime } from '@/components/composites/relative-time';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Switch } from '@/components/ui/switch';
import { Tooltip } from '@/components/ui/tooltip';
import { ScrollRegion, tdClass, thClass } from '@/components/system/scroll-region';
import { accessOf, canToggle, successRate, type AccessKind, type Source } from './source-utils';
import { SourceDrawer } from './source-drawer';
import { UploadSourceDialog } from './upload-dialog';

type StatusFilter = 'all' | AccessKind;
type SortKey = 'priority' | 'name' | 'businesses' | 'emails' | 'last_run';

export function SourcesPage() {
  const me = useMe();
  const isAdmin = me.can('admin');
  const q = useQuery({ queryKey: qk.list('sources'), queryFn: ({ signal }) => api.get<Source[]>('/api/sources', { signal }), refetchInterval: 30_000 });
  const [search, setSearch] = React.useState('');
  const [status, setStatus] = React.useState<StatusFilter>('all');
  const [sort, setSort] = React.useState<SortKey>('priority');
  const [open, setOpen] = React.useState<string | null>(null);

  const toggle = useApiMutation({
    mutationFn: ({ id, enable }: { id: string; enable: boolean }) => api.post<Source>(`/api/sources/${encodeURIComponent(id)}/${enable ? 'enable' : 'disable'}`),
    successMessage: (_d, v) => (v.enable ? 'Source enabled' : 'Source disabled'),
    invalidate: [qk.all('sources')],
  });
  const applyUpload = useApiMutation({
    mutationFn: ({ path }: { path: string }) => {
      const existing = (q.data ?? []).find((s) => s.adapter_id === 'csv_import')?.config ?? {};
      return api.put<Source>('/api/sources/csv_import/config', { config: { ...existing, path } });
    },
    successMessage: 'Imported business list source now points at the uploaded file',
    invalidate: [qk.all('sources')],
  });

  const rows = React.useMemo(() => {
    const needle = search.trim().toLowerCase();
    let list = (q.data ?? []).filter((s) => {
      if (status !== 'all' && accessOf(s).kind !== status) return false;
      if (!needle) return true;
      return [s.name, s.adapter_id, s.licence ?? '', s.source_type ?? '', s.country_coverage.join(' ')].some((t) => t.toLowerCase().includes(needle));
    });
    list = [...list].sort((a, b) => {
      switch (sort) {
        case 'name': return a.name.localeCompare(b.name);
        case 'businesses': return b.total_businesses - a.total_businesses;
        case 'emails': return b.total_published_emails - a.total_published_emails;
        case 'last_run': return (b.last_run_at ?? '').localeCompare(a.last_run_at ?? '');
        default: return a.priority - b.priority || a.adapter_id.localeCompare(b.adapter_id);
      }
    });
    return list;
  }, [q.data, search, status, sort]);

  const counts = React.useMemo(() => {
    const c: Record<string, number> = {};
    for (const s of q.data ?? []) c[accessOf(s).kind] = (c[accessOf(s).kind] ?? 0) + 1;
    return c;
  }, [q.data]);

  const chips: FilterChip[] = [];
  if (status !== 'all') chips.push({ id: 'status', label: `Status: ${STATUS_OPTIONS.find((o) => o.value === status)?.label}`, onRemove: () => setStatus('all') });

  return (
    <>
      <PageHeader
        title="Sources"
        description="Registered data source adapters, their licence terms and usage. Only sources that permit automated collection can be enabled; everything else is manual import only."
        actions={<>
          <Button variant="outline" onClick={() => q.refetch()} loading={q.isFetching && !q.isPending} aria-label="Refresh sources"><RefreshCw /> Refresh</Button>
          <UploadSourceDialog onApply={isAdmin ? (u) => applyUpload.mutate({ path: u.path }) : undefined} />
        </>}
      />

      <div role="note" className="mb-4 flex gap-2 rounded-md border border-info-border bg-info-soft p-3 text-sm text-info-foreground">
        <Info className="mt-0.5 size-4 shrink-0" aria-hidden />
        <p>
          <strong>Respecting source terms.</strong> Sources marked <Badge variant="danger" className="mx-0.5">Manual import only</Badge> do not permit automated collection. They are catalogued so you know what exists, but the system will not scrape or query them. If you hold a licensed export, upload it as a CSV/XLSX file instead.
        </p>
      </div>

      {q.isPending ? <LoadingState variant="skeleton" rows={8} label="Loading sources" /> : q.isError ? <ErrorState error={q.error} onRetry={() => q.refetch()} /> : (
        <>
          <FilterBar
            search={{ value: search, onChange: setSearch, placeholder: 'Search name, country, licence…', label: 'Search sources' }}
            chips={chips}
            onClearAll={() => { setSearch(''); setStatus('all'); }}
            actions={<span className="text-xs text-muted-foreground tabular" aria-live="polite">{rows.length} of {q.data.length} sources</span>}
          >
            <FilterSelect label="Status" value={status === 'all' ? undefined : status} onChange={(v) => setStatus((v ?? 'all') as StatusFilter)} allLabel="All"
              options={STATUS_OPTIONS.filter((o) => o.value !== 'all').map((o) => ({ value: o.value, label: `${o.label} (${counts[o.value] ?? 0})` }))} />
            <SimpleSelect aria-label="Sort by" className="w-auto min-w-36" value={sort} onValueChange={(v) => setSort(v as SortKey)} options={[
              { value: 'priority', label: 'Sort: Priority' }, { value: 'name', label: 'Sort: Name' }, { value: 'businesses', label: 'Sort: Most businesses' }, { value: 'emails', label: 'Sort: Most published emails' }, { value: 'last_run', label: 'Sort: Last run' },
            ]} />
          </FilterBar>

          {rows.length === 0 ? <EmptyState icon={ShieldAlert} title="No sources match" description="Try a different search or clear the status filter." compact /> : (
            <ScrollRegion label="Source adapters table">
              <table className="w-full min-w-[48rem] border-collapse text-left" data-testid="sources-table">
                <caption className="sr-only">Source adapters with coverage, licence, status and usage</caption>
                <thead>
                  <tr>
                    <th scope="col" className={thClass}>Source and licence</th>
                    <th scope="col" className={`${thClass} max-md:hidden`}>Coverage and type</th>
                    <th scope="col" className={thClass}>Access</th>
                    <th scope="col" className={`${thClass} max-lg:hidden`}>Last run</th>
                    <th scope="col" className={`${thClass} max-lg:hidden text-right`}>Success</th>
                    <th scope="col" className={`${thClass} text-right`}>Businesses</th>
                    <th scope="col" className={`${thClass} text-right`}>Emails</th>
                    <th scope="col" className={`${thClass} max-md:hidden text-right`}>Errors</th>
                    <th scope="col" className={thClass}>Enabled</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((s) => <Row key={s.adapter_id} s={s} isAdmin={isAdmin} busy={toggle.isPending && toggle.variables?.id === s.adapter_id} onOpen={() => setOpen(s.adapter_id)} onToggle={(enable) => toggle.mutate({ id: s.adapter_id, enable })} />)}
                </tbody>
              </table>
            </ScrollRegion>
          )}
        </>
      )}
      <SourceDrawer adapterId={open} onClose={() => setOpen(null)} />
    </>
  );
}

const STATUS_OPTIONS: { value: StatusFilter; label: string }[] = [
  { value: 'all', label: 'All statuses' },
  { value: 'enabled', label: 'Enabled' },
  { value: 'disabled', label: 'Disabled' },
  { value: 'needs_config', label: 'Needs configuration' },
  { value: 'manual_only', label: 'Manual import only' },
  { value: 'catalogue', label: 'Catalogue only' },
];

function Row({ s, isAdmin, busy, onOpen, onToggle }: { s: Source; isAdmin: boolean; busy: boolean; onOpen: () => void; onToggle: (enable: boolean) => void }) {
  const access = accessOf(s);
  const rate = successRate(s);
  const toggleable = canToggle(s);
  const cov = s.country_coverage.includes('*') ? 'All' : s.country_coverage.length > 4 ? `${s.country_coverage.slice(0, 4).join(', ')} +${s.country_coverage.length - 4}` : s.country_coverage.join(', ') || '—';
  return (
    <tr data-testid={`source-row-${s.adapter_id}`} className="hover:bg-muted/30">
      <th scope="row" className={`${tdClass} max-w-[18rem] text-left font-medium`}>
        <button type="button" onClick={onOpen} className="max-w-full rounded text-left hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" aria-label={`Details for ${s.name}`}>
          <span className="line-clamp-2">{s.name}</span>
        </button>
        <span className="line-clamp-1 block text-xs font-normal text-muted-foreground" title={s.licence ?? undefined}>{s.licence || 'No licence recorded'}</span>
        <span className="block font-mono text-2xs font-normal text-muted-foreground">{s.adapter_id} · priority {s.priority}</span>
      </th>
      <td className={`${tdClass} max-md:hidden`}>{cov}<span className="block text-xs text-muted-foreground">{s.source_type?.replaceAll('_', ' ') ?? '—'}</span></td>
      <td className={tdClass}>
        <Tooltip content={access.explain}><span><Badge variant={access.variant}>{access.label}</Badge></span></Tooltip>
        {access.kind === 'manual_only' && <span className="mt-1 block text-2xs text-danger-foreground">Automated collection not permitted</span>}
      </td>
      <td className={`${tdClass} max-lg:hidden`}>{s.last_run_at ? <RelativeTime value={s.last_run_at} /> : <span className="text-muted-foreground">Never</span>}</td>
      <td className={`${tdClass} tabular max-lg:hidden text-right`}>{rate === null ? <span className="text-muted-foreground">—</span> : formatPercent(rate * 100, 0)}</td>
      <td className={`${tdClass} tabular text-right`}>{formatNumber(s.total_businesses)}</td>
      <td className={`${tdClass} tabular text-right`}>{formatNumber(s.total_published_emails)}</td>
      <td className={`${tdClass} tabular max-md:hidden text-right`}>
        {s.runs_failed > 0 || s.last_error ? <Tooltip content={s.last_error ?? `${s.runs_failed} failed runs`}><span className="text-danger-foreground">{s.runs_failed}</span></Tooltip> : <span className="text-muted-foreground">0</span>}
      </td>
      <td className={tdClass}>
        {isAdmin ? (
          <Switch checked={s.enabled} disabled={!toggleable || busy} onCheckedChange={onToggle} aria-label={`Enable ${s.name}`} data-testid={`toggle-${s.adapter_id}`} />
        ) : <span className="text-xs text-muted-foreground">{s.enabled ? 'On' : 'Off'}</span>}
      </td>
    </tr>
  );
}
