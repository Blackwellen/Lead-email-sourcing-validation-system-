'use client';
import * as React from 'react';
import { Info, KeyRound, RefreshCw, Search, ShieldAlert } from 'lucide-react';
import { qk } from '@/lib/query-keys';
import { usePagedQuery, listFetcher } from '@/hooks/use-paged-query';
import { useUrlFilters } from '@/hooks/use-url-filters';
import { formatNumber } from '@/lib/format';
import { PageHeader } from '@/components/composites/page-header';
import { FilterBar, FilterSelect, type FilterChip } from '@/components/composites/filter-bar';
import { LoadingState } from '@/components/composites/loading-state';
import { ErrorState } from '@/components/composites/error-state';
import { EmptyState } from '@/components/composites/empty-state';
import { StatusBadge } from '@/components/composites/status-badge';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { COUNTRY_OPTIONS } from '@/components/contacts/shared';
import { DebouncedTextFilter } from '../shared';
import { ScoreBreakdown } from './score-breakdown';
import { ExplorerDrawer } from './explorer-drawer';
import { ACCESS_LABEL, COST_LABEL, CREDENTIALS_LABEL, automationStatus, type ExplorerItem } from './explorer-utils';

const DEFAULTS = { q: '', country: '', business_type: '', access_method: '', licence: '', cost: '', open: '' };
const fetchPage = listFetcher<ExplorerItem>('/api/source-explorer');

export function ExplorerPage() {
  const { filters, set, reset } = useUrlFilters(DEFAULTS);
  const apiFilters = React.useMemo(() => ({
    q: filters.q || undefined, country: filters.country || undefined, business_type: filters.business_type || undefined,
    access_method: filters.access_method || undefined, licence: filters.licence || undefined, cost: filters.cost || undefined,
  }), [filters.q, filters.country, filters.business_type, filters.access_method, filters.licence, filters.cost]);
  const q = usePagedQuery({ queryKey: qk.list('source-explorer'), fetchPage, filters: apiFilters, limit: 30 });

  const chips: FilterChip[] = [];
  if (filters.country) chips.push({ id: 'country', label: `Country: ${filters.country}`, onRemove: () => set({ country: '' }) });
  if (filters.business_type) chips.push({ id: 'bt', label: `Business type: ${filters.business_type}`, onRemove: () => set({ business_type: '' }) });
  if (filters.access_method) chips.push({ id: 'am', label: `Access: ${ACCESS_LABEL[filters.access_method] ?? filters.access_method}`, onRemove: () => set({ access_method: '' }) });
  if (filters.licence) chips.push({ id: 'lic', label: `Licence: ${filters.licence}`, onRemove: () => set({ licence: '' }) });
  if (filters.cost) chips.push({ id: 'cost', label: `Cost: ${COST_LABEL[filters.cost] ?? filters.cost}`, onRemove: () => set({ cost: '' }) });

  return (
    <>
      <PageHeader
        title="Source Explorer"
        description="Search the full source catalogue – adapters and reference entries – by coverage, business type, access method, licence and cost. Every result shows why it ranks where it does."
        actions={<Button variant="outline" onClick={() => q.refetch()} loading={q.isFetching && !q.isPending && !q.isFetchingNextPage}><RefreshCw /> Refresh</Button>}
      />
      <div role="note" className="mb-4 flex gap-2 rounded-md border border-info-border bg-info-soft p-3 text-sm text-info-foreground">
        <Info className="mt-0.5 size-4 shrink-0" aria-hidden />
        <p>Sources whose terms do not permit automated collection are marked <strong>Restricted — manual import or written permission only</strong>. They cannot be enabled; import a licensed dataset or record written permission instead. Secrets you enter are stored encrypted and never shown again.</p>
      </div>

      <FilterBar
        search={{ value: filters.q, onChange: (v) => set({ q: v }), placeholder: 'Search sources…', label: 'Search sources' }}
        chips={chips}
        onClearAll={reset}
        actions={q.isSuccess ? <span className="tabular text-xs text-muted-foreground" aria-live="polite" data-testid="explorer-count">{formatNumber(q.items.length)} shown{q.total !== null && <> of {formatNumber(q.total)}</>}</span> : null}
      >
        <FilterSelect label="Country" value={filters.country} onChange={(v) => set({ country: v ?? '' })} options={COUNTRY_OPTIONS} allLabel="Any" />
        <DebouncedTextFilter label="Business type" placeholder="Business type (e.g. software)" value={filters.business_type} onChange={(v) => set({ business_type: v })} />
        <FilterSelect label="Access" value={filters.access_method} onChange={(v) => set({ access_method: v ?? '' })} allLabel="Any" options={Object.entries(ACCESS_LABEL).map(([value, label]) => ({ value, label }))} />
        <FilterSelect label="Cost" value={filters.cost} onChange={(v) => set({ cost: v ?? '' })} allLabel="Any" options={Object.entries(COST_LABEL).map(([value, label]) => ({ value, label }))} />
        <DebouncedTextFilter label="Licence contains" placeholder="Licence contains…" value={filters.licence} onChange={(v) => set({ licence: v })} />
      </FilterBar>

      {q.isPending ? <LoadingState variant="cards" rows={6} label="Searching sources" /> : q.isError && q.items.length === 0 ? <ErrorState error={q.error} onRetry={() => q.refetch()} /> : q.items.length === 0 ? (
        <EmptyState icon={Search} title="No sources match" description="Try fewer filters, another country, or a broader business type." action={<Button variant="outline" onClick={reset}>Clear filters</Button>} />
      ) : (
        <>
          <ol className="grid gap-3 lg:grid-cols-2" aria-label="Matching sources, best match first" data-testid="explorer-results">
            {q.items.map((s, i) => <li key={s.id}><ResultCard s={s} rank={i + 1} onOpen={() => set({ open: s.id })} /></li>)}
          </ol>
          <div className="mt-4 flex justify-center">
            {q.hasNextPage ? <Button variant="outline" onClick={() => q.fetchNextPage()} loading={q.isFetchingNextPage}>Load more</Button> : <span className="text-xs text-muted-foreground">End of results</span>}
          </div>
        </>
      )}
      <ExplorerDrawer sourceId={filters.open || null} onClose={() => set({ open: '' })} />
    </>
  );
}

function ResultCard({ s, rank, onOpen }: { s: ExplorerItem; rank: number; onOpen: () => void }) {
  const auto = automationStatus(s);
  const cov = s.countries.includes('*') ? 'All countries' : s.countries.length > 5 ? `${s.countries.slice(0, 5).join(', ')} +${s.countries.length - 5}` : s.countries.join(', ') || '—';
  return (
    <article className="flex h-full flex-col gap-3 rounded-lg border bg-card p-4 shadow-card" aria-labelledby={`src-${s.id}`} data-testid={`explorer-card-${s.id}`}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 id={`src-${s.id}`} className="text-sm font-semibold leading-snug">
            <span className="sr-only">Rank {rank}: </span>
            <button type="button" onClick={onOpen} className="rounded text-left hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">{s.name}</button>
          </h2>
          <p className="truncate font-mono text-2xs text-muted-foreground">{s.id}</p>
        </div>
        <ScoreBreakdown score={s.score ?? null} breakdown={s.score_breakdown ?? {}} name={s.name} />
      </div>
      <div className="flex flex-wrap gap-1.5">
        <Badge variant={auto.variant} data-testid="automation-status">{auto.restricted && <ShieldAlert aria-hidden />}{auto.label}</Badge>
        <StatusBadge kind="health" status={s.health.status} />
        {s.adapter_id && (s.enabled ? <Badge variant="success">Enabled</Badge> : <Badge variant="outline">Disabled</Badge>)}
        {s.adapter_id && !s.info_only && (s.configured ? <Badge variant="outline">Configured</Badge> : <Badge variant="warning">Needs configuration</Badge>)}
      </div>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
        <div className="min-w-0"><dt className="text-muted-foreground">Coverage</dt><dd className="truncate">{cov}</dd></div>
        <div className="min-w-0"><dt className="text-muted-foreground">Access</dt><dd>{ACCESS_LABEL[s.access_method] ?? s.access_method}</dd></div>
        <div className="min-w-0"><dt className="text-muted-foreground">Cost</dt><dd>{COST_LABEL[s.cost] ?? s.cost}</dd></div>
        <div className="min-w-0"><dt className="text-muted-foreground">Credentials</dt><dd className="flex items-center gap-1">{s.credentials !== 'none' && <KeyRound className="size-3" aria-hidden />}{CREDENTIALS_LABEL[s.credentials] ?? s.credentials}</dd></div>
        <div className="col-span-2 min-w-0"><dt className="text-muted-foreground">Licence</dt><dd className="line-clamp-2 break-words" title={s.licence}>{s.licence || 'Not recorded'}</dd></div>
        <div className="col-span-2 min-w-0"><dt className="text-muted-foreground">Refresh frequency</dt><dd className="line-clamp-2 break-words">{s.refresh_frequency || <span className="text-muted-foreground">Not stated</span>}</dd></div>
      </dl>
      <div className="mt-auto flex justify-end">
        <Button size="sm" variant="outline" onClick={onOpen} aria-label={`Details and configuration for ${s.name}`}>Details</Button>
      </div>
    </article>
  );
}
