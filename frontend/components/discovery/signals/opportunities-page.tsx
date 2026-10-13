'use client';
import * as React from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useQuery } from '@tanstack/react-query';
import { Lightbulb } from 'lucide-react';
import { api, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { listFetcher } from '@/hooks/use-paged-query';
import { useUrlFilters } from '@/hooks/use-url-filters';
import { formatDate, formatDateTime, formatNumber, titleCase } from '@/lib/format';
import { PageHeader } from '@/components/composites/page-header';
import { DataTable, createColumnHelper } from '@/components/composites/data-table';
import { FilterBar, FilterSelect, type FilterChip } from '@/components/composites/filter-bar';
import { ConfidenceMeter } from '@/components/composites/confidence-meter';
import { EmptyState } from '@/components/composites/empty-state';
import { Badge } from '@/components/ui/badge';
import { Combobox, MultiSelect } from '@/components/ui/combobox';
import { COUNTRY_OPTIONS, SafeLink, safeHref, useCategoryOptions } from '@/components/contacts/shared';
import { DebouncedTextFilter, SIGNAL_TYPES } from '../shared';

type Signal = Schema<'SignalOut'>;
type Facets = Schema<'SignalFacets'>;
const col = createColumnHelper<Signal>();
const fetchPage = listFetcher<Signal>('/api/signals');
const DEFAULTS = { q: '', country: '', industry: '', category_id: '', signal_type: [] as string[], from: '', to: '', min_confidence: '' };

const columns = [
  col.accessor('title', {
    header: 'Signal', enableSorting: false,
    cell: (i) => {
      const s = i.row.original;
      return (
        <div className="min-w-[14rem] max-w-md space-y-0.5 whitespace-normal">
          <p className="font-medium">{i.getValue()}</p>
          <p className="text-xs"><Link className="text-primary underline-offset-2 hover:underline" href={`/businesses/${s.organisation_id}`}>{s.organisation ?? `Business #${s.organisation_id}`}</Link><span className="text-muted-foreground"> · {s.country_code}{s.industry ? ` · ${s.industry}` : ''}</span></p>
        </div>
      );
    },
  }),
  col.accessor('signal_type', { header: 'Type', enableSorting: false, cell: (i) => <Badge variant="info">{titleCase(i.getValue())}</Badge> }),
  col.accessor('source_url', {
    header: 'Source', enableSorting: false,
    cell: (i) => {
      const href = safeHref(i.getValue());
      let host = i.getValue();
      try { host = new URL(i.getValue()).host || i.getValue(); } catch { /* shown as text */ }
      return (
        <span className="block max-w-[14rem] text-xs" data-testid="signal-source">
          {href ? <SafeLink url={i.getValue()} label={host} /> : <span className="break-all text-muted-foreground" title="Not an http(s) link – shown as text only">{i.getValue()}</span>}
          <span className="block text-muted-foreground">{i.row.original.source_adapter}</span>
        </span>
      );
    },
  }),
  col.accessor('observed_at', { header: 'Observed', enableSorting: true, meta: { className: 'whitespace-nowrap' }, cell: (i) => <time dateTime={i.getValue()} title={formatDateTime(i.getValue())}>{formatDate(i.getValue())}</time> }),
  col.accessor('occurred_at', { header: 'Occurred', enableSorting: false, meta: { hideBelow: 'md', className: 'whitespace-nowrap' }, cell: (i) => (i.getValue() ? <time dateTime={i.getValue()!}>{formatDate(i.getValue())}</time> : <span className="text-muted-foreground">Not stated</span>) }),
  col.accessor('confidence', { header: 'Confidence', enableSorting: true, cell: (i) => <ConfidenceMeter value={i.getValue() * 100} /> }),
];

export function OpportunitiesPage() {
  const router = useRouter();
  const { filters, set, reset } = useUrlFilters(DEFAULTS, { arrayKeys: ['signal_type'] });
  const cats = useCategoryOptions();
  const facets = useQuery({ queryKey: qk.custom('signals', 'facets'), queryFn: ({ signal }) => api.get<Facets>('/api/signals/facets', { signal }), staleTime: 30_000 });
  const types = filters.signal_type as string[];
  const apiFilters = React.useMemo(() => ({
    q: filters.q || undefined, country: filters.country || undefined, industry: filters.industry || undefined, category_id: filters.category_id || undefined,
    signal_type: types, min_confidence: filters.min_confidence || undefined,
    observed_from: filters.from ? `${filters.from}T00:00:00Z` : undefined, observed_to: filters.to ? `${filters.to}T23:59:59Z` : undefined,
  }), [filters.q, filters.country, filters.industry, filters.category_id, types, filters.min_confidence, filters.from, filters.to]);
  const rangeError = filters.from && filters.to && filters.from > filters.to ? 'The “from” date is after the “to” date.' : null;

  const chips: FilterChip[] = [];
  if (filters.country) chips.push({ id: 'cc', label: `Country: ${filters.country}`, onRemove: () => set({ country: '' }) });
  if (filters.industry) chips.push({ id: 'ind', label: `Industry: ${filters.industry}`, onRemove: () => set({ industry: '' }) });
  if (filters.category_id) chips.push({ id: 'cat', label: `Category: ${cats.options.find((o) => o.value === filters.category_id)?.label ?? filters.category_id}`, onRemove: () => set({ category_id: '' }) });
  for (const t of types) chips.push({ id: `t-${t}`, label: `Type: ${titleCase(t)}`, onRemove: () => set({ signal_type: types.filter((x) => x !== t) }) });
  if (filters.from) chips.push({ id: 'from', label: `Observed from ${filters.from}`, onRemove: () => set({ from: '' }) });
  if (filters.to) chips.push({ id: 'to', label: `Observed to ${filters.to}`, onRemove: () => set({ to: '' }) });
  if (filters.min_confidence) chips.push({ id: 'mc', label: `Confidence ≥ ${Math.round(Number(filters.min_confidence) * 100)}`, onRemove: () => set({ min_confidence: '' }) });

  const f = facets.data;
  return (
    <>
      <PageHeader title="Opportunity Signals" description="Observed events – hiring, expansion, contract awards, new locations and more – each with the page it came from, when it was observed and how confident the extraction is. Use them to time outreach." />
      {f && (
        <section aria-label="Signal facets" className="mb-4 space-y-2 rounded-lg border bg-card p-3 shadow-card" data-testid="signal-facets">
          <p className="text-sm"><strong className="tabular">{formatNumber(f.total)}</strong> signals{f.first_observed_at && <> observed {formatDate(f.first_observed_at)} – {formatDate(f.last_observed_at)}</>}.</p>
          <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Filter by signal type">
            <span className="text-xs text-muted-foreground">Types:</span>
            {f.by_type.map((t) => {
              const on = types.includes(t.key);
              return <button key={t.key} type="button" aria-pressed={on} onClick={() => set({ signal_type: on ? types.filter((x) => x !== t.key) : [...types, t.key] })}
                className="rounded-full border px-2.5 py-0.5 text-xs hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring aria-pressed:border-primary aria-pressed:bg-accent aria-pressed:font-medium">{titleCase(t.key)} <span className="tabular text-muted-foreground">{formatNumber(t.count)}</span></button>;
            })}
          </div>
          <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Filter by country">
            <span className="text-xs text-muted-foreground">Countries:</span>
            {f.by_country.slice(0, 20).map((c) => {
              const on = filters.country === c.key;
              return <button key={c.key} type="button" aria-pressed={on} onClick={() => set({ country: on ? '' : c.key })}
                className="rounded-full border px-2.5 py-0.5 text-xs hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring aria-pressed:border-primary aria-pressed:bg-accent aria-pressed:font-medium">{c.key} <span className="tabular text-muted-foreground">{formatNumber(c.count)}</span></button>;
            })}
          </div>
        </section>
      )}
      <FilterBar search={{ value: filters.q, onChange: (v) => set({ q: v }), placeholder: 'Search signal titles…', label: 'Search signals' }} chips={chips} onClearAll={reset}>
        <FilterSelect label="Country" value={filters.country} onChange={(v) => set({ country: v ?? '' })} options={COUNTRY_OPTIONS} allLabel="Any" />
        <div className="w-full sm:w-64"><MultiSelect aria-label="Event types" placeholder="Event types…" value={types} onChange={(v) => set({ signal_type: v })} options={SIGNAL_TYPES.map((t) => ({ value: t, label: titleCase(t) }))} /></div>
        <div className="w-full sm:w-56"><Combobox aria-label="Category" placeholder="Category…" options={cats.options} value={filters.category_id || null} onChange={(v) => set({ category_id: v ?? '' })} clearable loading={cats.isPending} /></div>
        <DebouncedTextFilter label="Industry (exact)" placeholder="Industry (exact)" value={filters.industry} onChange={(v) => set({ industry: v })} />
        <FilterSelect label="Min confidence" value={filters.min_confidence} onChange={(v) => set({ min_confidence: v ?? '' })} allLabel="Any" options={[{ value: '0.5', label: '50+' }, { value: '0.7', label: '70+' }, { value: '0.9', label: '90+' }]} />
        <label className="flex items-center gap-1.5 text-xs text-muted-foreground">Observed from
          <input type="date" value={filters.from} max={filters.to || undefined} onChange={(e) => set({ from: e.target.value })} aria-label="Observed from" className="h-9 rounded-md border border-input bg-background px-2 text-sm text-foreground shadow-card focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" />
        </label>
        <label className="flex items-center gap-1.5 text-xs text-muted-foreground">to
          <input type="date" value={filters.to} min={filters.from || undefined} onChange={(e) => set({ to: e.target.value })} aria-label="Observed to" className="h-9 rounded-md border border-input bg-background px-2 text-sm text-foreground shadow-card focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" />
        </label>
      </FilterBar>
      {rangeError ? <p role="alert" className="text-sm font-medium text-destructive">{rangeError}</p> : (
        <DataTable caption="Opportunity signals" tableId="signals" columns={columns} queryKey={qk.list('signals')} fetchPage={fetchPage} filters={apiFilters}
          defaultSort={{ id: 'observed_at', desc: true }} onRowOpen={(r) => router.push(`/businesses/${r.organisation_id}`)}
          emptyState={<EmptyState icon={Lightbulb} title="No signals match" description="Signals are collected by signal monitors and source adapters. Try widening the filters." />} />
      )}
    </>
  );
}
