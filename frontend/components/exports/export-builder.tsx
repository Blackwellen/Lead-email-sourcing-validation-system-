'use client';
import * as React from 'react';
import { Controller, useForm, useWatch } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { AlertTriangle, FileDown, Info, Loader2 } from 'lucide-react';
import { api, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { applyFieldErrors, useApiMutation } from '@/hooks/use-api-mutation';
import { useDebounce } from '@/hooks/use-debounce';
import { formatNumber } from '@/lib/format';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { FormField, Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import { SimpleSelect } from '@/components/ui/select';
import { Combobox, MultiSelect, type ComboboxOption } from '@/components/ui/combobox';
import { AdvancedPanel } from '@/components/ui/collapsible';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { builderSchema, defaultValues, PRESET_EXTRAS, REACHABILITY, toBody, type BuilderValues, type ExportRow } from './export-model';

type Presets = Schema<'PresetList'>;
type CategoryNode = Schema<'CategoryNode'>;

function flattenCategories(nodes: CategoryNode[], depth = 0): ComboboxOption[] {
  return nodes.flatMap((n) => [{ value: String(n.id), label: `${'— '.repeat(depth)}${n.name}`, description: n.business_count ? `${n.business_count} businesses` : undefined, disabled: !n.enabled }, ...flattenCategories(n.children ?? [], depth + 1)]);
}

export function ExportBuilder({ presets, onCreated }: { presets: Presets; onCreated: (e: ExportRow) => void }) {
  const first = presets.presets.find((p) => p.id === 'published_business_emails')?.id ?? presets.presets[0]?.id ?? 'full_intelligence';
  const form = useForm<BuilderValues>({ resolver: zodResolver(builderSchema), defaultValues: defaultValues(first), mode: 'onChange' });
  const { control, register, setValue, handleSubmit, formState: { errors, isValid }, setError } = form;
  const values = useWatch({ control }) as BuilderValues;
  const preset = values.preset;
  const isListmonk = preset === 'listmonk_ready';
  const presetInfo = presets.presets.find((p) => p.id === preset);

  const cats = useQuery({ queryKey: qk.custom('categories', 'tree-for-export'), queryFn: ({ signal }) => api.get<CategoryNode[]>('/api/categories', { signal }), staleTime: 5 * 60_000 });
  const countries = useQuery({ queryKey: qk.custom('geo', 'countries'), queryFn: ({ signal }) => api.get<Schema<'CountryRow'>[]>('/api/geo/countries', { signal }), staleTime: 10 * 60_000 });
  const sources = useQuery({ queryKey: qk.list('sources'), queryFn: ({ signal }) => api.get<Schema<'SourceOut'>[]>('/api/sources', { signal }), staleTime: 60_000 });
  const catOptions = React.useMemo(() => flattenCategories(cats.data ?? []), [cats.data]);
  const countryOptions = React.useMemo<ComboboxOption[]>(() => (countries.data ?? []).map((c) => ({ value: c.code, label: `${c.name} (${c.code})`, description: c.businesses ? `${c.businesses} businesses` : undefined })), [countries.data]);
  const sourceOptions = React.useMemo<ComboboxOption[]>(() => (sources.data ?? []).filter((s) => s.registered && !s.info_only).map((s) => ({ value: s.adapter_id, label: s.name, description: s.adapter_id })), [sources.data]);

  const changePreset = (id: string) => {
    const d = defaultValues(id);
    setValue('preset', id, { shouldValidate: true });
    (['published_only', 'uniqueness', 'exclude_suppressed', 'contact_type'] as const).forEach((k) => setValue(k, d[k] as never));
    if (id === 'listmonk_ready') setValue('format', 'csv');
  };

  // live preview
  const debounced = useDebounce(values, 500);
  const parsed = builderSchema.safeParse(debounced);
  const body = parsed.success ? toBody(parsed.data) : null;
  const preview = useQuery({
    queryKey: qk.custom('exports', 'preview', (body ?? {}) as Record<string, unknown>),
    queryFn: ({ signal }) => api.post<Schema<'PreviewOut'>>('/api/exports/preview', body, { signal }),
    enabled: !!body,
    placeholderData: keepPreviousData,
    retry: false,
    staleTime: 10_000,
  });

  const create = useApiMutation({
    mutationFn: (b: ReturnType<typeof toBody>) => api.post<ExportRow>('/api/exports', b),
    errorMessage: false,
    onSuccess: (e) => onCreated(e),
    onError: (err) => {
      const mapped = applyFieldErrors(err, (n, e) => setError((String(n).replace(/^filters\./, '') as keyof BuilderValues) ?? 'preset', e));
      if (!mapped) setError('root', { message: err.message });
    },
    invalidate: [qk.all('exports')],
  });
  const submit = handleSubmit((v) => create.mutate({ ...toBody(v), wait: null }));

  const staleBody = JSON.stringify(debounced) !== JSON.stringify(values);

  return (
    <Card>
      <CardHeader>
        <CardTitle>New export</CardTitle>
        <CardDescription>Choose a preset, narrow the rows, and create a file. The row count updates as you change filters.</CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={submit} noValidate className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1fr)_20rem]" aria-label="Export builder">
          <div className="space-y-6">
            <fieldset className="space-y-2">
              <legend className="mb-1 text-sm font-medium">Preset</legend>
              <RadioGroup value={preset} onValueChange={changePreset} className="grid grid-cols-1 gap-2 sm:grid-cols-2" aria-label="Export preset">
                {presets.presets.map((p) => (
                  <label key={p.id} htmlFor={`preset-${p.id}`} data-testid={`preset-${p.id}`} className={`flex cursor-pointer items-start gap-3 rounded-md border p-3 text-sm transition-colors hover:bg-muted/40 has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-ring ${preset === p.id ? 'border-primary bg-primary/5' : ''}`}>
                    <RadioGroupItem id={`preset-${p.id}`} value={p.id} className="mt-0.5" />
                    <span className="min-w-0"><span className="block font-medium">{p.label}</span><span className="block text-xs text-muted-foreground">{p.description}</span></span>
                  </label>
                ))}
              </RadioGroup>
              {presetInfo && (
                <p className="text-xs text-muted-foreground" data-testid="preset-columns">
                  <Popover>
                    <PopoverTrigger asChild><button type="button" className="font-medium text-primary underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">{presetInfo.columns.length} columns</button></PopoverTrigger>
                    <PopoverContent className="max-h-72 w-80 overflow-auto"><p className="mb-2 text-xs font-medium">Columns in “{presetInfo.label}”</p><ol className="list-decimal space-y-0.5 pl-4 text-xs">{presetInfo.columns.map((c) => <li key={c}>{c}</li>)}</ol></PopoverContent>
                  </Popover>
                  {' '}in the file{PRESET_EXTRAS[preset] ? ` · ${PRESET_EXTRAS[preset]}` : ''}
                </p>
              )}
              {isListmonk && (
                <div role="note" data-testid="listmonk-note" className="flex gap-2 rounded-md border border-info-border bg-info-soft p-3 text-sm text-info-foreground">
                  <Info className="mt-0.5 size-4 shrink-0" aria-hidden />
                  <div className="space-y-1">
                    <p><strong>What a Listmonk-ready export contains.</strong> A subscriber CSV with exactly three columns: <code>email</code>, <code>name</code> and <code>attributes</code> (a JSON object with company, domain, category, country, region, confidence and similar fields, ready for Listmonk’s import).</p>
                    <p>Only addresses whose marketing status is <em>eligible</em> and that are <em>not suppressed</em> are included; this is enforced by the server and cannot be switched off. The format is always CSV. Eligibility is policy triage, not legal advice: confirm your lawful basis before sending.</p>
                  </div>
                </div>
              )}
            </fieldset>

            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <FormField label="Category" htmlFor="x-category" hint="Includes subcategories.">
                <Controller control={control} name="category_id" render={({ field }) => (
                  <Combobox id="x-category" aria-label="Category" options={catOptions} value={field.value || null} onChange={(v) => field.onChange(v ?? '')} clearable placeholder="Any category" loading={cats.isPending} />
                )} />
              </FormField>
              <FormField label="Country" htmlFor="x-country">
                <Controller control={control} name="country" render={({ field }) => (
                  <MultiSelect id="x-country" aria-label="Country" options={countryOptions} value={field.value} onChange={field.onChange} placeholder="Any country" loading={countries.isPending} />
                )} />
              </FormField>
              <FormField label="Region" htmlFor="x-region" hint="Type a region exactly as stored (e.g. “England”) and press Enter.">
                <Controller control={control} name="region" render={({ field }) => (
                  <MultiSelect id="x-region" aria-label="Region" options={[]} value={field.value} onChange={field.onChange} allowCreate placeholder="Any region" />
                )} />
              </FormField>
              <FormField label="Industry" htmlFor="x-industry" error={errors.industry?.message} hint="Starts-with match on the business’s industry text.">
                <Input id="x-industry" {...register('industry')} invalid={!!errors.industry} aria-describedby="x-industry-hint" />
              </FormField>
              <FormField label="Verification status" htmlFor="x-reach" hint="Leave empty to include every status.">
                <Controller control={control} name="reachability" render={({ field }) => (
                  <MultiSelect id="x-reach" aria-label="Verification status" options={REACHABILITY} value={field.value} onChange={field.onChange} placeholder="Any status" />
                )} />
              </FormField>
              <FormField label="Minimum confidence (0–100)" htmlFor="x-minconf" error={errors.min_confidence?.message}>
                <Input id="x-minconf" inputMode="decimal" {...register('min_confidence')} invalid={!!errors.min_confidence} aria-describedby={errors.min_confidence ? 'x-minconf-error' : undefined} />
              </FormField>
              <FormField label="Uniqueness" htmlFor="x-unique" hint="Which single address to keep when a business or domain has several.">
                <Controller control={control} name="uniqueness" render={({ field }) => (
                  <SimpleSelect id="x-unique" aria-label="Uniqueness" value={field.value} onValueChange={field.onChange} options={[
                    { value: 'email', label: 'Every address (global, de-duplicated)' }, { value: 'organisation', label: 'One per business' }, { value: 'domain', label: 'One per domain' },
                  ]} />
                )} />
              </FormField>
              <FormField label="Role vs named" htmlFor="x-ctype" hint="Role = info@, sales@ and similar mailboxes.">
                <Controller control={control} name="contact_type" render={({ field }) => (
                  <SimpleSelect id="x-ctype" aria-label="Role versus named" value={field.value} onValueChange={field.onChange} options={[
                    { value: 'both', label: 'Both named and role' }, { value: 'named', label: 'Named people only' }, { value: 'role', label: 'Role mailboxes only' },
                  ]} />
                )} />
              </FormField>
            </div>

            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <Toggle control={control} name="published_only" label="Published only" hint="Only addresses found published (or corroborated) on a source; no inferred patterns." />
              <Toggle control={control} name="exclude_suppressed" label="Exclude suppressed" hint={isListmonk ? 'Always on for Listmonk-ready.' : 'Drop addresses on the suppression list or opted out.'} disabled={isListmonk} forceOn={isListmonk} />
              <Toggle control={control} name="exclude_catch_all" label="Exclude catch-all domains" hint="Their servers accept any address, so the mailbox cannot be confirmed." />
              <Toggle control={control} name="exclude_unknown" label="Exclude unknown" hint="Drop addresses whose reachability or evidence is unknown or inconclusive." />
            </div>

            <AdvancedPanel title="More filters" description="Freshness, sources and file format">
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                <FormField label="Validated within (days)" htmlFor="x-fresh" error={errors.freshness_days?.message} hint="Only addresses validated this recently.">
                  <Input id="x-fresh" inputMode="numeric" {...register('freshness_days')} invalid={!!errors.freshness_days} />
                </FormField>
                <FormField label="Discovery sources" htmlFor="x-source" hint="Restrict to businesses discovered via these sources.">
                  <Controller control={control} name="source" render={({ field }) => (
                    <MultiSelect id="x-source" aria-label="Discovery sources" options={sourceOptions} value={field.value} onChange={field.onChange} placeholder="Any source" />
                  )} />
                </FormField>
              </div>
            </AdvancedPanel>

            <fieldset className="space-y-2">
              <legend className="text-sm font-medium">File format</legend>
              <Controller control={control} name="format" render={({ field }) => (
                <RadioGroup value={isListmonk ? 'csv' : field.value} onValueChange={field.onChange} className="flex flex-wrap gap-4" aria-label="File format">
                  {([['csv', 'CSV'], ['xlsx', 'Excel (XLSX)'], ['jsonl', 'JSON Lines']] as const).map(([v, l]) => (
                    <label key={v} htmlFor={`fmt-${v}`} className="flex cursor-pointer items-center gap-2 text-sm">
                      <RadioGroupItem id={`fmt-${v}`} value={v} disabled={isListmonk && v !== 'csv'} /> {l}
                    </label>
                  ))}
                </RadioGroup>
              )} />
              <p role="note" data-testid="formula-note" className="text-xs text-muted-foreground">
                Spreadsheet-safety: any text cell starting with <code>=</code>, <code>+</code>, <code>-</code> or <code>@</code> is prefixed with an apostrophe in CSV and XLSX files so Excel or Sheets cannot run it as a formula. This is deliberate, and it means such values (for example a phone number written <code>+44…</code>) appear with a leading <code>&apos;</code>. JSON Lines is not altered.
              </p>
            </fieldset>
          </div>

          <aside aria-label="Export summary" className="space-y-4 lg:sticky lg:top-4 lg:self-start">
            <div className="rounded-md border bg-muted/30 p-4">
              <p className="text-xs font-medium text-muted-foreground">Matching rows</p>
              <p className="mt-1 text-2xl font-semibold tabular" data-testid="preview-count" aria-live="polite">
                {!body ? '—' : preview.isPending ? <Loader2 className="size-5 animate-spin text-muted-foreground" aria-label="Counting" /> : preview.isError ? <span className="text-sm text-danger-foreground">Unavailable</span> : preview.data.estimated_rows === null ? <span className="text-sm text-muted-foreground">Too slow to count</span> : (
                  <>{preview.data.capped ? `${formatNumber(preview.data.estimated_rows)}+` : preview.data.estimated_rows === undefined ? '—' : formatNumber(preview.data.estimated_rows)}</>
                )}
                {(preview.isFetching || staleBody) && !preview.isPending && <Loader2 className="ml-2 inline size-4 animate-spin text-muted-foreground" aria-label="Updating" />}
              </p>
              <p className="mt-1 text-xs text-muted-foreground" data-testid="preview-note">
                {preview.data?.capped ? 'Approximate: counting stops at 100,000. The file will contain every matching row.' : preview.data && preview.data.estimated_rows === null ? 'The count timed out; the export itself is not affected.' : preview.isError ? preview.error.message : 'An estimate; the file’s final row count is shown in the history.'}
              </p>
            </div>
            {errors.root && <p role="alert" className="flex gap-2 text-sm font-medium text-destructive"><AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />{errors.root.message}</p>}
            <Button type="submit" className="w-full" loading={create.isPending} disabled={!isValid} data-testid="create-export"><FileDown /> Create export</Button>
            <p className="text-xs text-muted-foreground">Small exports finish immediately; larger ones run in the background and appear in the history below.</p>
          </aside>
        </form>
      </CardContent>
    </Card>
  );
}

function Toggle({ control, name, label, hint, disabled, forceOn }: {
  control: ReturnType<typeof useForm<BuilderValues>>['control']; name: 'published_only' | 'exclude_suppressed' | 'exclude_catch_all' | 'exclude_unknown'; label: string; hint: string; disabled?: boolean; forceOn?: boolean;
}) {
  const id = `x-${name}`;
  return (
    <Controller control={control} name={name} render={({ field }) => (
      <div className="flex items-start justify-between gap-3 rounded-md border p-3">
        <div className="space-y-0.5">
          <Label htmlFor={id}>{label}</Label>
          <p id={`${id}-hint`} className="text-xs text-muted-foreground">{hint}</p>
        </div>
        <Switch id={id} checked={forceOn ? true : field.value} onCheckedChange={field.onChange} disabled={disabled} aria-describedby={`${id}-hint`} />
      </div>
    )} />
  );
}

