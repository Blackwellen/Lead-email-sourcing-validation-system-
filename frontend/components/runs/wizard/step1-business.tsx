'use client';
import * as React from 'react';
import { Controller, useFormContext, useWatch } from 'react-hook-form';
import { useQueries, useQuery } from '@tanstack/react-query';
import { api, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { useDebounce } from '@/hooks/use-debounce';
import { Combobox, MultiSelect, type ComboboxOption } from '@/components/ui/combobox';
import { FormField } from '@/components/ui/label';
import { KeyValueList } from '@/components/composites/key-value-list';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { useCategoryTree } from '../use-categories';
import { CategoryTreeSelect } from './category-tree';
import { ChipsField, Section, SwitchField, TextField, idOf, useFieldError } from './fields';
import type { WizardValues } from './schema';

type Country = Schema<'CountryRow'>;

export function useCountries() {
  return useQuery({
    queryKey: qk.custom('geo', 'countries'),
    queryFn: ({ signal }) => api.get<Country[]>('/api/geo/countries', { signal }),
    staleTime: 10 * 60_000,
  });
}

export function Step1Business() {
  const { control } = useFormContext<WizardValues>();
  const countriesQ = useCountries();
  const countries = useWatch({ control, name: 'countries' });
  const { flat } = useCategoryTree();

  const countryOptions: ComboboxOption[] = React.useMemo(
    () => (countriesQ.data ?? []).map((c) => ({ value: c.code, label: `${c.name} (${c.code})`, description: `${c.sources_available} source${c.sources_available === 1 ? '' : 's'} listed${c.businesses ? ` · ${c.businesses.toLocaleString('en-US')} businesses already collected` : ''}` })),
    [countriesQ.data],
  );

  const subCountries = countries.slice(0, 6);
  const subQs = useQueries({
    queries: subCountries.map((cc) => ({
      queryKey: qk.custom('geo', 'subdivisions', cc),
      queryFn: ({ signal }: { signal: AbortSignal }) => api.get<Schema<'SubdivisionRow'>[]>(`/api/geo/countries/${cc}/subdivisions`, { signal }),
      staleTime: 10 * 60_000,
    })),
  });
  const regionOptions: ComboboxOption[] = subQs.flatMap((q, i) => (q.data ?? []).map((s) => ({ value: s.name, label: s.name, description: `${s.type} · ${subCountries[i]}` })));
  const regionsLoading = subQs.some((q) => q.isPending);

  const categoryOptions: ComboboxOption[] = React.useMemo(() => [...flat.values()].map((c) => ({ value: String(c.id), label: c.path })), [flat]);
  const catErr = useFieldError('category_ids');
  const countryErr = useFieldError('countries');
  const destErr = useFieldError('destination_category_id');

  return (
    <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_20rem]">
      <div className="space-y-5">
        <Section title="Run basics">
          <TextField name="name" label="Run name" required placeholder="e.g. UK electricians – Q4" maxLength={200} />
          <FormField label="Destination category" htmlFor={idOf('destination_category_id')} hint="Optional. Results from this run are filed under this category." error={destErr}>
            <Controller control={control} name="destination_category_id" render={({ field }) => (
              <Combobox id={idOf('destination_category_id')} options={categoryOptions} value={field.value === null ? null : String(field.value)} clearable
                onChange={(v) => field.onChange(v === null ? null : Number(v))} placeholder="No destination category" searchPlaceholder="Search categories…" invalid={!!destErr} />
            )} />
          </FormField>
        </Section>

        <Section title="Where" description="Countries are required; narrow with regions, localities or postcode areas.">
          <FormField label="Countries" htmlFor={idOf('countries')} required error={countryErr} hint={countriesQ.isError ? undefined : 'Searchable list of all ISO countries. The number of listed sources per country is shown.'}>
            <Controller control={control} name="countries" render={({ field }) => (
              <MultiSelect id={idOf('countries')} aria-label="Countries" options={countryOptions} value={field.value} onChange={field.onChange} loading={countriesQ.isPending}
                placeholder="Select countries" searchPlaceholder="Search countries…" invalid={!!countryErr} />
            )} />
          </FormField>
          {countriesQ.isError && <p role="alert" className="text-xs text-destructive">Could not load the country list: {countriesQ.error.message}. Reload the page to retry.</p>}
          <FormField label="Regions / subdivisions" htmlFor={idOf('regions')} hint={countries.length > 6 ? 'Subdivision lists load for the first 6 selected countries.' : 'ISO 3166-2 subdivisions of the selected countries. Leave empty for the whole country.'}>
            <Controller control={control} name="regions" render={({ field }) => (
              <MultiSelect id={idOf('regions')} aria-label="Regions" options={regionOptions} value={field.value} onChange={field.onChange} loading={regionsLoading}
                placeholder="Whole country" searchPlaceholder="Search regions…" emptyText="No subdivisions for the selected countries." allowCreate />
            )} />
          </FormField>
          {countries.includes('GB') && <UkLocalities />}
        </Section>

        <Section title="What kind of businesses" description="Pick categories, add keywords, or both.">
          <FormField label="Categories" htmlFor={idOf('category_ids')} error={catErr} hint="Selecting a category also includes everything beneath it.">
            <Controller control={control} name="category_ids" render={({ field }) => <CategoryTreeSelect value={field.value} onChange={field.onChange} invalid={!!catErr} />} />
          </FormField>
          <div className="grid gap-4 sm:grid-cols-2">
            <ChipsField name="keywords" label="Keywords" hint="Extra search terms, e.g. “boiler repair”." />
            <ChipsField name="exclusions" label="Exclusions" hint="Skip businesses matching these terms." />
          </div>
          <div className="divide-y rounded-md border px-3">
            <SwitchField name="active_only" label="Active businesses only" hint="Skip dissolved, dormant or closed businesses where the source says so." />
            <SwitchField name="refresh_existing" label="Refresh businesses we already hold" hint="Re-collect businesses already in the database instead of skipping them." />
          </div>
        </Section>
      </div>
      <TargetingSummary />
    </div>
  );
}

function UkLocalities() {
  const { control } = useFormContext<WizardValues>();
  return (
    <div className="space-y-4 rounded-md border border-dashed p-3">
      <p className="text-xs text-muted-foreground">United Kingdom: choose towns or postcode areas. Suggestions come from businesses already collected; type to add any other value.</p>
      <Controller control={control} name="localities" render={({ field }) => <GeoSuggest kind="city" id="localities" label="Towns / cities" value={field.value} onChange={field.onChange} />} />
      <Controller control={control} name="postcodes" render={({ field }) => <GeoSuggest kind="postcode_area" id="postcodes" label="Postcode areas" value={field.value} onChange={field.onChange} />} />
    </div>
  );
}

function GeoSuggest({ kind, id, label, value, onChange }: { kind: 'city' | 'postcode_area'; id: string; label: string; value: string[]; onChange: (v: string[]) => void }) {
  const [text, setText] = React.useState('');
  const q = useDebounce(text, 250);
  const res = useQuery({
    queryKey: qk.custom('geo', 'localities', kind, q),
    queryFn: ({ signal }) => api.get<Schema<'LocalityRow'>[]>('/api/geo/localities', { query: { country: 'GB', kind, q, limit: 50 }, signal }),
    staleTime: 60_000,
  });
  const options: ComboboxOption[] = (res.data ?? []).map((r) => ({ value: r.name, label: r.name, description: `${r.count.toLocaleString('en-US')} known` }));
  return (
    <FormField label={label} htmlFor={idOf(id)} hint={res.data && res.data.length === 0 && !q ? 'No collected businesses yet, so there are no suggestions. Type a value and press Enter.' : undefined}>
      <MultiSelect id={idOf(id)} aria-label={label} options={options} value={value} onChange={onChange} onSearchChange={setText} allowCreate loading={res.isFetching}
        placeholder={kind === 'city' ? 'Any town' : 'Any postcode area'} searchPlaceholder={kind === 'city' ? 'Search towns…' : 'e.g. SW, M, EH'} emptyText="Type a value and press Enter" />
    </FormField>
  );
}

function TargetingSummary() {
  const { control } = useFormContext<WizardValues>();
  const v = useWatch({ control });
  const countries = useCountries();
  const { flat } = useCategoryTree();
  const nameOf = (cc: string) => countries.data?.find((c) => c.code === cc)?.name ?? cc;
  const cats = (v.category_ids ?? []).map((id) => flat.get(id)?.name ?? `#${id}`);
  const none = <span className="text-muted-foreground">Not set</span>;
  return (
    <aside aria-label="Targeting summary" className="lg:sticky lg:top-4 lg:self-start">
      <Card>
        <CardHeader><CardTitle className="text-base">Targeting summary</CardTitle></CardHeader>
        <CardContent>
          <KeyValueList items={[
            { label: 'Run', value: v.name?.trim() || none },
            { label: 'Countries', value: v.countries?.length ? v.countries.map(nameOf).join(', ') : none },
            { label: 'Regions', value: v.regions?.length ? v.regions.join(', ') : 'Whole country' },
            ...(v.countries?.includes('GB') ? [{ label: 'Towns / postcode areas', value: [...(v.localities ?? []), ...(v.postcodes ?? [])].join(', ') || 'Any' }] : []),
            { label: 'Categories', value: cats.length ? cats.join(', ') : 'None selected' },
            { label: 'Keywords', value: v.keywords?.length ? v.keywords.join(', ') : 'None' },
            { label: 'Exclusions', value: v.exclusions?.length ? v.exclusions.join(', ') : 'None' },
            { label: 'Scope', value: `${v.active_only ? 'Active only' : 'Including inactive'} · ${v.refresh_existing ? 'refreshing known businesses' : 'new businesses only'}` },
          ]} />
          {!cats.length && !v.keywords?.length && <p className="mt-3 rounded-md bg-warning-soft p-2 text-xs text-warning-foreground">No category or keyword chosen: the run will not be narrowed by industry.</p>}
        </CardContent>
      </Card>
    </aside>
  );
}
