'use client';
import * as React from 'react';
import { FilterBar, FilterSelect, type FilterChip } from '@/components/composites/filter-bar';
import { Combobox } from '@/components/ui/combobox';
import { Checkbox } from '@/components/ui/checkbox';
import { FormField } from '@/components/ui/label';
import { AdvancedPanel } from '@/components/ui/collapsible';
import { useSavedViews, type SavedView } from '@/hooks/use-saved-views';
import { useUrlFilters } from '@/hooks/use-url-filters';
import { COUNTRY_OPTIONS, CommitInput, SaveViewDialog, activeFilters, useCategoryOptions, useSourceOptions } from '@/components/contacts/shared';
import { DOMAIN_STATUS_OPTIONS } from './domain-badge';

export const BUSINESS_DEFAULTS = {
  q: '', country: '', region: '', city: '', category_id: '', include_subcategories: '', domain_status: '', company_type: '', source: '',
  email_availability: '', min_confidence: '', updated_after: '', updated_before: '', include_merged: '',
};
export type BusinessFilterState = typeof BUSINESS_DEFAULTS;
export const useBusinessFilters = () => useUrlFilters(BUSINESS_DEFAULTS);

export function businessQuery(f: BusinessFilterState): Record<string, string> {
  const a = activeFilters(f) as Record<string, string>;
  if (a.updated_after) a.updated_after = new Date(`${a.updated_after}T00:00:00`).toISOString();
  if (a.updated_before) a.updated_before = new Date(`${a.updated_before}T23:59:59`).toISOString();
  return a;
}

const LABELS: Record<string, string> = {
  country: 'Country', region: 'Region', city: 'City', category_id: 'Category', domain_status: 'Domain', company_type: 'Company type', source: 'Source',
  email_availability: 'Emails', min_confidence: 'Min confidence', updated_after: 'Updated after', updated_before: 'Updated before', include_merged: 'Merged', include_subcategories: 'Sub-categories',
};

export function BusinessFilterBar({ filters, set, reset }: { filters: BusinessFilterState; set: (p: Partial<BusinessFilterState>) => void; reset: () => void }) {
  const cats = useCategoryOptions();
  const sources = useSourceOptions();
  const saved = useSavedViews<Partial<BusinessFilterState>>('businesses');
  const [saveOpen, setSaveOpen] = React.useState(false);

  const chips: FilterChip[] = [];
  for (const [k, v] of Object.entries(filters)) {
    if (k === 'q' || !v || (k === 'include_subcategories' && !filters.category_id)) continue;
    const value = k === 'category_id' ? cats.options.find((o) => o.value === v)?.label ?? `#${v}` : k === 'include_subcategories' ? 'excluded' : k === 'include_merged' ? 'included' : v;
    chips.push({ id: k, label: `${LABELS[k] ?? k}: ${value}`, onRemove: () => set({ [k]: '' } as Partial<BusinessFilterState>) });
  }
  const applyView = (v: SavedView<Partial<BusinessFilterState>>) => {
    const patch: Record<string, string> = {};
    for (const k of Object.keys(BUSINESS_DEFAULTS)) patch[k] = (v.filters as Record<string, string>)[k] ?? '';
    set(patch as Partial<BusinessFilterState>);
  };

  return (
    <>
      <FilterBar<Partial<BusinessFilterState>>
        search={{ value: filters.q, onChange: (q) => set({ q }), placeholder: 'Search business name or domain (prefix)…', label: 'Search businesses' }}
        chips={chips}
        onClearAll={reset}
        views={{ items: saved.views, onApply: applyView, onSave: () => setSaveOpen(true), onRemove: saved.remove }}
      >
        <FilterSelect label="Country" value={filters.country} onChange={(v) => set({ country: v ?? '' })} options={COUNTRY_OPTIONS.map((o) => ({ value: o.value, label: o.label }))} />
        <div className="w-60"><Combobox aria-label="Category" clearable value={filters.category_id || null} onChange={(v) => set({ category_id: v ?? '' })} options={cats.options} placeholder="Category" /></div>
        <FilterSelect label="Domain" value={filters.domain_status} onChange={(v) => set({ domain_status: v ?? '' })} options={DOMAIN_STATUS_OPTIONS} />
        <FilterSelect label="Emails" value={filters.email_availability} onChange={(v) => set({ email_availability: v ?? '' })}
          options={[{ value: 'any', label: 'Has any email' }, { value: 'published', label: 'Has published email' }, { value: 'none', label: 'No email found' }]} />
      </FilterBar>
      <div className="-mt-1 mb-3">
        <AdvancedPanel title="More filters">
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            <FormField label="Region (exact)" htmlFor="b-region"><CommitInput id="b-region" value={filters.region} onCommit={(v) => set({ region: v })} /></FormField>
            <FormField label="City (exact)" htmlFor="b-city"><CommitInput id="b-city" value={filters.city} onCommit={(v) => set({ city: v })} /></FormField>
            <FormField label="Company type (exact)" htmlFor="b-type" hint="e.g. ltd, plc"><CommitInput id="b-type" value={filters.company_type} onCommit={(v) => set({ company_type: v })} /></FormField>
            <FormField label="Source" htmlFor="b-source">
              <Combobox id="b-source" aria-label="Source" clearable value={filters.source || null} onChange={(v) => set({ source: v ?? '' })} options={sources.options} placeholder="Any source" allowCreate />
            </FormField>
            <FormField label="Min best email confidence (0-100)" htmlFor="b-minc"><CommitInput id="b-minc" type="number" min={0} max={100} value={filters.min_confidence} onCommit={(v) => set({ min_confidence: v })} /></FormField>
            <div />
            <FormField label="Updated after" htmlFor="b-ua"><CommitInput id="b-ua" type="date" value={filters.updated_after} onCommit={(v) => set({ updated_after: v })} /></FormField>
            <FormField label="Updated before" htmlFor="b-ub"><CommitInput id="b-ub" type="date" value={filters.updated_before} onCommit={(v) => set({ updated_before: v })} /></FormField>
            <div className="flex flex-col justify-end gap-2">
              <label className="flex items-center gap-2 text-sm"><Checkbox aria-label="Include sub-categories" checked={filters.include_subcategories !== 'false'} onCheckedChange={(c) => set({ include_subcategories: c === false ? 'false' : '' })} />Include sub-categories</label>
              <label className="flex items-center gap-2 text-sm"><Checkbox aria-label="Include merged duplicates" checked={filters.include_merged === 'true'} onCheckedChange={(c) => set({ include_merged: c === true ? 'true' : '' })} />Include merged duplicates</label>
            </div>
          </div>
        </AdvancedPanel>
      </div>
      <SaveViewDialog open={saveOpen} onOpenChange={setSaveOpen} onSave={(name) => saved.save(name, activeFilters(filters) as Partial<BusinessFilterState>)} />
    </>
  );
}
