'use client';
import * as React from 'react';
import { FilterBar, FilterSelect, type FilterChip } from '@/components/composites/filter-bar';
import { FormField } from '@/components/ui/label';
import { Checkbox } from '@/components/ui/checkbox';
import { Combobox, MultiSelect } from '@/components/ui/combobox';
import { AdvancedPanel } from '@/components/ui/collapsible';
import { SimpleSelect } from '@/components/ui/select';
import { useSavedViews, type SavedView } from '@/hooks/use-saved-views';
import { useUrlFilters } from '@/hooks/use-url-filters';
import {
  CATCHALL_OPTIONS, COUNTRY_OPTIONS, MAIL_OPTIONS, MARKETING_OPTIONS, ORIGIN_OPTIONS, REACHABILITY_OPTIONS, RECIPIENT_OPTIONS, SYNTAX_OPTIONS,
  CommitInput, SaveViewDialog, activeFilters, useCategoryOptions, useSourceOptions,
} from './shared';

/** URL-backed filter state for every contact list (contacts page, verification tabs, category directory). */
export const CONTACT_DEFAULTS = {
  q: '', country: [] as string[], region: [] as string[], city: '', category_id: '', include_subcategories: '', origin: [] as string[],
  reachability: [] as string[], syntax_status: [] as string[], domain_mail_status: [] as string[], recipient_evidence_status: [] as string[],
  catch_all_status: [] as string[], marketing_status: [] as string[], contact_type: '', min_confidence: '', max_confidence: '', freshness: '',
  source: [] as string[], has_name: '', suppressed: '', needs_review: '', free_provider: '', previously_bounced: '', exclude_catch_all: '', exclude_unknown: '',
  validated_after: '', validated_before: '', domain: '',
};
export type ContactFilterState = typeof CONTACT_DEFAULTS;
export const CONTACT_ARRAY_KEYS = ['country', 'region', 'origin', 'reachability', 'syntax_status', 'domain_mail_status', 'recipient_evidence_status', 'catch_all_status', 'marketing_status', 'source'] as const;

export function useContactFilters() {
  return useUrlFilters(CONTACT_DEFAULTS, { arrayKeys: [...CONTACT_ARRAY_KEYS] });
}

/** Query params for the API: only non-empty values; dates become ISO timestamps. */
export function contactQuery(f: ContactFilterState): Record<string, string | string[]> {
  const a = activeFilters(f) as Record<string, string | string[]>;
  if (a.validated_after) a.validated_after = new Date(`${a.validated_after as string}T00:00:00`).toISOString();
  if (a.validated_before) a.validated_before = new Date(`${a.validated_before as string}T23:59:59`).toISOString();
  return a;
}

const LABELS: Record<string, string> = {
  q: 'Search', country: 'Country', region: 'Region', city: 'City', category_id: 'Category', origin: 'Origin', reachability: 'Reachability', syntax_status: 'Syntax',
  domain_mail_status: 'Mail routing', recipient_evidence_status: 'Mailbox evidence', catch_all_status: 'Catch-all', marketing_status: 'Marketing', contact_type: 'Type',
  min_confidence: 'Min confidence', max_confidence: 'Max confidence', freshness: 'Freshness', source: 'Source', has_name: 'Has name', suppressed: 'Suppressed',
  needs_review: 'Needs review', free_provider: 'Free provider', previously_bounced: 'Bounced before', exclude_catch_all: 'Excluding catch-all', exclude_unknown: 'Excluding unknown',
  validated_after: 'Validated after', validated_before: 'Validated before', domain: 'Domain', include_subcategories: 'Sub-categories',
};

const TRI = [{ value: 'true', label: 'Yes' }, { value: 'false', label: 'No' }];

function MultiField({ id, label, value, onChange, options, allowCreate }: {
  id: string; label: string; value: string[]; onChange: (v: string[]) => void; options: { value: string; label: string; description?: string }[]; allowCreate?: boolean;
}) {
  return (
    <FormField label={label} htmlFor={id}>
      <MultiSelect id={id} aria-label={label} value={value} onChange={onChange} options={options} placeholder="Any" allowCreate={allowCreate} />
    </FormField>
  );
}

export function ContactFilterBar({ filters, set, reset, scope, hide = [], actions }: {
  filters: ContactFilterState;
  set: (patch: Partial<ContactFilterState>) => void;
  reset: () => void;
  scope: string;
  hide?: (keyof ContactFilterState)[];
  actions?: React.ReactNode;
}) {
  const cats = useCategoryOptions();
  const sources = useSourceOptions();
  const saved = useSavedViews<Partial<ContactFilterState>>(scope);
  const [saveOpen, setSaveOpen] = React.useState(false);
  const catLabel = (id: string) => cats.options.find((o) => o.value === id)?.label ?? `#${id}`;
  const show = (k: keyof ContactFilterState) => !hide.includes(k);

  const chips = React.useMemo<FilterChip[]>(() => {
    const out: FilterChip[] = [];
    for (const [k, v] of Object.entries(filters)) {
      if (k === 'q' || (hide as string[]).includes(k)) continue;
      if (k === 'include_subcategories' && !filters.category_id) continue;
      const empty = v === '' || v === undefined || (Array.isArray(v) && v.length === 0);
      if (empty) continue;
      const value = k === 'category_id' ? catLabel(v as string) : Array.isArray(v) ? v.join(', ') : k === 'include_subcategories' ? (v === 'false' ? 'excluded' : 'included') : String(v);
      out.push({ id: k, label: `${LABELS[k] ?? k}: ${value}`, onRemove: () => set({ [k]: Array.isArray(v) ? [] : '' } as Partial<ContactFilterState>) });
    }
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters, cats.options, hide]);

  const applyView = (v: SavedView<Partial<ContactFilterState>>) => {
    const patch: Record<string, unknown> = {};
    for (const [k, d] of Object.entries(CONTACT_DEFAULTS)) patch[k] = (v.filters as Record<string, unknown>)[k] ?? (Array.isArray(d) ? [] : '');
    set(patch as Partial<ContactFilterState>);
  };

  return (
    <>
      <FilterBar<Partial<ContactFilterState>>
        search={{ value: filters.q, onChange: (q) => set({ q }), placeholder: 'Search email, domain or business (prefix)…', label: 'Search contacts' }}
        chips={chips}
        onClearAll={reset}
        views={{ items: saved.views, onApply: applyView, onSave: () => setSaveOpen(true), onRemove: saved.remove }}
        actions={actions}
      >
        {show('country') && (
          <div className="w-52"><MultiSelect aria-label="Country" value={filters.country} onChange={(country) => set({ country })} options={COUNTRY_OPTIONS} placeholder="Country" allowCreate /></div>
        )}
        {show('category_id') && (
          <div className="w-60"><Combobox aria-label="Category" clearable value={filters.category_id || null} onChange={(v) => set({ category_id: v ?? '' })} options={cats.options} placeholder="Category" loading={cats.isPending} /></div>
        )}
        <FilterSelect label="Reachability" value={filters.reachability[0]} onChange={(v) => set({ reachability: v ? [v] : [] })} options={REACHABILITY_OPTIONS} />
        <FilterSelect label="Marketing" value={filters.marketing_status[0]} onChange={(v) => set({ marketing_status: v ? [v] : [] })} options={MARKETING_OPTIONS} />
      </FilterBar>
      <div className="-mt-1 mb-3">
        <AdvancedPanel title="Advanced filters" description="Combine any evidence statuses, confidence range, freshness and flags.">
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            <MultiField id="f-origin" label="Origin" value={filters.origin} onChange={(origin) => set({ origin })} options={ORIGIN_OPTIONS} />
            <MultiField id="f-reach" label="Reachability (multi)" value={filters.reachability} onChange={(reachability) => set({ reachability })} options={REACHABILITY_OPTIONS} />
            <MultiField id="f-syntax" label="Syntax" value={filters.syntax_status} onChange={(syntax_status) => set({ syntax_status })} options={SYNTAX_OPTIONS} />
            <MultiField id="f-mail" label="Mail routing" value={filters.domain_mail_status} onChange={(domain_mail_status) => set({ domain_mail_status })} options={MAIL_OPTIONS} />
            <MultiField id="f-rec" label="Mailbox evidence" value={filters.recipient_evidence_status} onChange={(recipient_evidence_status) => set({ recipient_evidence_status })} options={RECIPIENT_OPTIONS} />
            <MultiField id="f-ca" label="Catch-all" value={filters.catch_all_status} onChange={(catch_all_status) => set({ catch_all_status })} options={CATCHALL_OPTIONS} />
            <MultiField id="f-mkt" label="Marketing (multi)" value={filters.marketing_status} onChange={(marketing_status) => set({ marketing_status })} options={MARKETING_OPTIONS} />
            {show('source') && <MultiField id="f-src" label="Discovery source" value={filters.source} onChange={(source) => set({ source })} options={sources.options} allowCreate />}
            <MultiField id="f-region" label="Region" value={filters.region} onChange={(region) => set({ region })} options={[]} allowCreate />
            <FormField label="City (exact)" htmlFor="f-city"><CommitInput id="f-city" value={filters.city} onCommit={(v) => set({ city: v })} /></FormField>
            <FormField label="Domain (exact)" htmlFor="f-domain"><CommitInput id="f-domain" placeholder="example.com" value={filters.domain} onCommit={(v) => set({ domain: v })} /></FormField>
            <FormField label="Address type" htmlFor="f-type">
              <SimpleSelect id="f-type" aria-label="Address type" value={filters.contact_type || 'any'} onValueChange={(v) => set({ contact_type: v === 'any' ? '' : v })}
                options={[{ value: 'any', label: 'Any' }, { value: 'named', label: 'Named person' }, { value: 'role', label: 'Role mailbox' }]} />
            </FormField>
            <FormField label="Min confidence (0-100)" htmlFor="f-minc"><CommitInput id="f-minc" type="number" min={0} max={100} value={filters.min_confidence} onCommit={(v) => set({ min_confidence: v })} /></FormField>
            <FormField label="Max confidence (0-100)" htmlFor="f-maxc"><CommitInput id="f-maxc" type="number" min={0} max={100} value={filters.max_confidence} onCommit={(v) => set({ max_confidence: v })} /></FormField>
            <FormField label="Validation freshness" htmlFor="f-fresh">
              <SimpleSelect id="f-fresh" aria-label="Validation freshness" value={filters.freshness || 'any'} onValueChange={(v) => set({ freshness: v === 'any' ? '' : v })}
                options={[{ value: 'any', label: 'Any' }, { value: 'fresh', label: 'Fresh' }, { value: 'stale', label: 'Stale' }, { value: 'never', label: 'Never validated' }]} />
            </FormField>
            <FormField label="Validated after" htmlFor="f-va"><CommitInput id="f-va" type="date" value={filters.validated_after} onCommit={(v) => set({ validated_after: v })} /></FormField>
            <FormField label="Validated before" htmlFor="f-vb"><CommitInput id="f-vb" type="date" value={filters.validated_before} onCommit={(v) => set({ validated_before: v })} /></FormField>
            {([['has_name', 'Has contact name'], ['suppressed', 'Suppressed'], ['needs_review', 'Needs review'], ['free_provider', 'Free e-mail provider'], ['previously_bounced', 'Previously bounced']] as const).map(([k, label]) => (
              <FormField key={k} label={label} htmlFor={`f-${k}`}>
                <SimpleSelect id={`f-${k}`} aria-label={label} value={filters[k] || 'any'} onValueChange={(v) => set({ [k]: v === 'any' ? '' : v } as Partial<ContactFilterState>)} options={[{ value: 'any', label: 'Any' }, ...TRI]} />
              </FormField>
            ))}
            {show('category_id') && (
              <label className="flex items-center gap-2 text-sm"><Checkbox aria-label="Include sub-categories" checked={filters.include_subcategories !== 'false'} onCheckedChange={(c) => set({ include_subcategories: c === false ? 'false' : '' })} />Include sub-categories</label>
            )}
            <label className="flex items-center gap-2 text-sm"><Checkbox aria-label="Exclude catch-all domains" checked={filters.exclude_catch_all === 'true'} onCheckedChange={(c) => set({ exclude_catch_all: c === true ? 'true' : '' })} />Exclude catch-all domains</label>
            <label className="flex items-center gap-2 text-sm"><Checkbox aria-label="Exclude unknown results" checked={filters.exclude_unknown === 'true'} onCheckedChange={(c) => set({ exclude_unknown: c === true ? 'true' : '' })} />Exclude unknown / inconclusive</label>
          </div>
        </AdvancedPanel>
      </div>
      <SaveViewDialog open={saveOpen} onOpenChange={setSaveOpen} onSave={(name) => saved.save(name, activeFilters(filters) as Partial<ContactFilterState>)} />
    </>
  );
}
