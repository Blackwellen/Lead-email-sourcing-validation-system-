'use client';
import * as React from 'react';
import { Controller, useFormContext, useWatch } from 'react-hook-form';
import { useMutation, useQuery } from '@tanstack/react-query';
import { AlertTriangle, ExternalLink, FileUp, Info, Sparkles } from 'lucide-react';
import { api, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { toast } from '@/lib/toast';
import { Badge, type BadgeVariant } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { FormField } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { Switch } from '@/components/ui/switch';
import { Label } from '@/components/ui/label';
import { AdvancedPanel } from '@/components/ui/collapsible';
import { LoadingState } from '@/components/composites/loading-state';
import { ErrorState } from '@/components/composites/error-state';
import { cn } from '@/lib/utils';
import { NumField, Section, idOf, useFieldError } from './fields';
import { parseDomains, type WizardValues } from './schema';

type Source = Schema<'SourceOut'>;
type Avail = Schema<'SourceAvail'>;

const STATUS_META: Record<string, { label: string; variant: BadgeVariant; usable: boolean; why?: string }> = {
  available: { label: 'Available', variant: 'success', usable: true },
  unconfigured: { label: 'Needs configuration', variant: 'warning', usable: true, why: 'Needs an API key, file path or URL before it can produce records.' },
  disabled: { label: 'Disabled', variant: 'neutral', usable: false, why: 'An administrator has not enabled this source (Sources page).' },
  info_only: { label: 'Catalogue only', variant: 'neutral', usable: false, why: 'Catalogue entry only; it yields no records.' },
  not_permitted: { label: 'Not permitted', variant: 'danger', usable: false, why: 'Its terms do not permit automated collection.' },
};
const RANK = ['available', 'unconfigured', 'disabled', 'info_only', 'not_permitted'];

export function Step2Sources() {
  const { control, setValue, getValues } = useFormContext<WizardValues>();
  const countries = useWatch({ control, name: 'countries' });
  const selected = useWatch({ control, name: 'sources' });
  const sourcesErr = useFieldError('sources');
  const [showCatalogue, setShowCatalogue] = React.useState(false);
  const ccKey = countries.join(',');

  const sourcesQ = useQuery({ queryKey: qk.custom('sources', 'list'), queryFn: ({ signal }) => api.get<Source[]>('/api/sources', { signal }), staleTime: 30_000 });
  const availQ = useQuery({
    queryKey: qk.custom('geo', 'source-availability', ccKey),
    queryFn: ({ signal }) => api.get<Schema<'AvailabilityOut'>>('/api/geo/source-availability', { query: { countries: ccKey }, signal }),
    staleTime: 30_000,
  });
  const planQ = useQuery({
    queryKey: qk.custom('geo', 'coverage-plan', ccKey),
    queryFn: ({ signal }) => api.get<Schema<'CoveragePlan'>>('/api/geo/coverage-plan', { query: { countries: ccKey }, signal }),
    staleTime: 30_000,
  });

  const rows = React.useMemo(() => {
    const byId = new Map((sourcesQ.data ?? []).map((s) => [s.adapter_id, s]));
    const out = new Map<string, { src: Source | undefined; avail: Avail; perCountry: { cc: string; status: string }[] }>();
    for (const c of availQ.data?.countries ?? []) {
      for (const a of c.sources) {
        const cur = out.get(a.adapter_id);
        const entry = { cc: c.country, status: a.status };
        if (!cur) out.set(a.adapter_id, { src: byId.get(a.adapter_id), avail: a, perCountry: [entry] });
        else {
          cur.perCountry.push(entry);
          if (RANK.indexOf(a.status) < RANK.indexOf(cur.avail.status)) cur.avail = a;
        }
      }
    }
    return [...out.values()].sort((a, b) => a.avail.priority - b.avail.priority || a.avail.adapter_id.localeCompare(b.avail.adapter_id));
  }, [sourcesQ.data, availQ.data]);

  const catalogueOnly = rows.filter((r) => r.avail.status === 'info_only');
  const shown = rows.filter((r) => showCatalogue || r.avail.status !== 'info_only');
  const recommended = React.useMemo(() => {
    const ids = new Set<string>();
    for (const c of planQ.data?.countries ?? []) for (const p of c.plan) ids.add(p.adapter_id);
    return [...ids];
  }, [planQ.data]);

  const toggle = (id: string, on: boolean) => {
    const cur = getValues('sources');
    setValue('sources', on ? [...cur, id] : cur.filter((s) => s !== id), { shouldDirty: true, shouldValidate: true });
  };

  return (
    <div className="space-y-5">
      <Section title="Sources" description="Where the run discovers businesses. Availability is checked for the countries you selected.">
        <div className="flex flex-wrap items-center gap-2">
          <Button type="button" variant="outline" size="sm" disabled={!recommended.length} onClick={() => setValue('sources', recommended, { shouldDirty: true, shouldValidate: true })}>
            <Sparkles /> Use recommended preset
          </Button>
          <span className="text-xs text-muted-foreground" aria-live="polite">
            {planQ.isPending ? 'Checking coverage…' : recommended.length ? `Recommended: ${recommended.join(', ')} (available now for ${countries.join(', ')})` : 'No source is available for these countries without extra configuration.'}
          </span>
        </div>
        {sourcesErr && <p id={`${idOf('sources')}-error`} role="alert" className="text-xs font-medium text-destructive">{sourcesErr}</p>}
        {sourcesQ.isPending || availQ.isPending ? <LoadingState label="Loading sources…" rows={5} /> :
          sourcesQ.isError ? <ErrorState compact error={sourcesQ.error} onRetry={() => sourcesQ.refetch()} /> :
          availQ.isError ? <ErrorState compact error={availQ.error} onRetry={() => availQ.refetch()} /> : (
            <>
              {availQ.data && !availQ.data.registry_loaded && (
                <p role="alert" className="rounded-md border border-warning-border bg-warning-soft p-2 text-xs text-warning-foreground">The source registry is not loaded on this deployment, so availability could not be checked.</p>
              )}
              <ul className="space-y-2" aria-label="Sources">
                {shown.map((r) => <SourceRow key={r.avail.adapter_id} row={r} checked={selected.includes(r.avail.adapter_id)} onToggle={(on) => toggle(r.avail.adapter_id, on)} />)}
              </ul>
              {shown.length === 0 && <p className="rounded-md border border-dashed p-4 text-center text-sm text-muted-foreground">No runnable source covers {countries.join(', ')}. You can still import a list of domains below.</p>}
              {catalogueOnly.length > 0 && (
                <div className="flex items-center gap-2">
                  <Switch id="show-catalogue" checked={showCatalogue} onCheckedChange={setShowCatalogue} />
                  <Label htmlFor="show-catalogue" className="text-sm font-normal">Show {catalogueOnly.length} catalogue-only entr{catalogueOnly.length === 1 ? 'y' : 'ies'} (listed for reference, they yield no records)</Label>
                </div>
              )}
            </>
          )}
        <NumField name="crawl_max_pages" label="Pages crawled per website" hint="Default 12. Higher values find more emails but take longer and hit each site more." min={1} max={100} className="max-w-xs" />
      </Section>

      <CoverageHint plan={planQ.data} loading={planQ.isPending} />
      <ImportedDomains />
    </div>
  );
}

function SourceRow({ row, checked, onToggle }: { row: { src?: Source; avail: Avail; perCountry: { cc: string; status: string }[] }; checked: boolean; onToggle: (on: boolean) => void }) {
  const { avail, src, perCountry } = row;
  const meta = STATUS_META[avail.status] ?? { label: avail.status, variant: 'neutral' as BadgeVariant, usable: false };
  const id = `src-${avail.adapter_id}`;
  const disabled = !meta.usable;
  return (
    <li className={cn('rounded-md border p-3', checked && 'border-primary bg-accent/30', disabled && 'bg-muted/30')}>
      <div className="flex items-start gap-3">
        <Checkbox id={id} checked={checked} disabled={disabled} onCheckedChange={(v) => onToggle(v === true)} className="mt-1" aria-describedby={`${id}-d`} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <label htmlFor={id} className={cn('text-sm font-medium', !disabled && 'cursor-pointer')}>{avail.name}</label>
            <Badge variant={meta.variant}>{meta.label}</Badge>
            <Badge variant="outline">{avail.source_type.replaceAll('_', ' ') || 'source'}</Badge>
          </div>
          <p id={`${id}-d`} className="mt-1 text-xs text-muted-foreground">
            {avail.licence ? <>Licence: {avail.licence}. </> : null}
            {avail.note ?? meta.why ?? ''}
            {src?.access_requirements && avail.status !== 'available' && !avail.note ? ` ${src.access_requirements}` : ''}
          </p>
          {src && (
            <p className="mt-0.5 text-xs text-muted-foreground">
              {src.total_businesses > 0 ? `${src.total_businesses.toLocaleString('en-US')} businesses collected so far. ` : ''}
              {src.live_verified ? '' : 'Not live-verified. '}
              {src.source_url && <a href={src.source_url} target="_blank" rel="noreferrer noopener" className="inline-flex items-center gap-0.5 text-primary hover:underline">Source site<ExternalLink className="size-3" aria-hidden /><span className="sr-only"> (opens in a new tab)</span></a>}
            </p>
          )}
          {perCountry.length > 1 && <p className="mt-0.5 text-xs text-muted-foreground">Per country: {perCountry.map((p) => `${p.cc} ${STATUS_META[p.status]?.label.toLowerCase() ?? p.status}`).join(' · ')}</p>}
          {checked && avail.status === 'unconfigured' && (
            <p role="status" className="mt-2 flex items-start gap-1.5 text-xs text-warning-foreground"><AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden /> This source has no stored configuration. Supply a file or URL below or ask an administrator to configure it, otherwise it will produce no records.</p>
          )}
          {checked && (src?.requires_config || avail.status === 'unconfigured') && <SourceParams id={avail.adapter_id} />}
        </div>
      </div>
    </li>
  );
}

function SourceParams({ id }: { id: string }) {
  const { control } = useFormContext<WizardValues>();
  const fileRef = React.useRef<HTMLInputElement>(null);
  const upload = useMutation({
    mutationFn: (file: File) => { const fd = new FormData(); fd.append('file', file); return api.post<Schema<'UploadOut'>>('/api/sources/upload', fd); },
    onError: (e: Error) => toast.error('Upload failed', e.message),
  });
  return (
    <AdvancedPanel className="mt-3" title="Source parameters (file path or URL)" description="Only for file-based sources">
      <Controller control={control} name="source_params" render={({ field }) => {
        const cur = (field.value[id] ?? {}) as Record<string, unknown>;
        const set = (k: string, val: string) => field.onChange({ ...field.value, [id]: { ...cur, [k]: val } });
        return (
          <div className="space-y-3">
            <FormField label="Uploaded file" htmlFor={`${id}-path`} hint="Files must be uploaded here first; arbitrary server paths are rejected.">
              <div className="flex gap-2">
                <Input id={`${id}-path`} readOnly value={String(cur.path ?? '')} placeholder="No file uploaded" />
                <input ref={fileRef} type="file" accept=".csv,.xlsx,.txt" className="sr-only" tabIndex={-1} aria-label={`Upload file for ${id}`}
                  onChange={async (e) => { const f = e.target.files?.[0]; e.target.value = ''; if (!f) return; const r = await upload.mutateAsync(f).catch(() => null); if (r) { set('path', r.path); toast.success('File uploaded', `${r.kind.toUpperCase()}, ${r.size.toLocaleString('en-US')} bytes`); } }} />
                <Button type="button" variant="outline" loading={upload.isPending} onClick={() => fileRef.current?.click()}><FileUp /> Upload</Button>
              </div>
            </FormField>
            <FormField label="Dataset URL" htmlFor={`${id}-url`} hint="Optional, for adapters that download a dataset.">
              <Input id={`${id}-url`} type="url" placeholder="https://…" value={String(cur.url ?? '')} onChange={(e) => set('url', e.target.value)} />
            </FormField>
          </div>
        );
      }} />
    </AdvancedPanel>
  );
}

function CoverageHint({ plan, loading }: { plan?: Schema<'CoveragePlan'>; loading: boolean }) {
  const { control } = useFormContext<WizardValues>();
  const v = useWatch({ control });
  const regions = v.regions?.length ?? 0, towns = v.localities?.length ?? 0, areas = v.postcodes?.length ?? 0;
  // Thresholds mirror the backend planner (app/geo.py: more than 10 postcode areas or more than 8 localities counts as broad).
  const broad = areas > 10 || towns > 8 || regions + towns + areas === 0;
  return (
    <section aria-label="Coverage plan" className="space-y-2 rounded-lg border bg-card p-4 sm:p-5">
      <h2 className="flex items-center gap-2 text-base font-semibold"><Info className="size-4 text-info" aria-hidden /> Coverage plan</h2>
      <p className="text-sm">
        {broad
          ? <>This is a <strong>broad selection</strong> ({regions + towns + areas === 0 ? 'whole country' : `${regions} regions, ${towns} towns, ${areas} postcode areas`}). Instead of one query per locality, the run reads each national bulk dataset once and applies your region, town and postcode filters to it. Cost does not grow with the number of localities, and search-API discovery is not used for broad areas.</>
          : <>A <strong>narrow selection</strong> ({regions} regions, {towns} towns, {areas} postcode areas). Bulk datasets are still read in a single filtered pass; per-locality queries are only suitable for metered or rate-limited sources.</>}
      </p>
      {loading ? <p className="text-xs text-muted-foreground">Checking coverage…</p> : plan && (
        <>
          {plan.warnings.map((w) => <p key={w} role="alert" className="text-xs font-medium text-warning-foreground">{w}</p>)}
          <ul className="space-y-1 text-xs text-muted-foreground">
            {plan.countries.map((c) => (
              <li key={c.country}>
                <strong className="text-foreground">{c.name ?? c.country}:</strong> {c.plan.length} source{c.plan.length === 1 ? '' : 's'} ready now; {c.existing_businesses.toLocaleString('en-US')} businesses and {c.existing_published_emails.toLocaleString('en-US')} published emails already held.
                {(c.gaps ?? []).length > 0 && <details className="mt-1"><summary className="cursor-pointer text-foreground">{(c.gaps ?? []).length} gap{(c.gaps ?? []).length === 1 ? '' : 's'}</summary><ul className="ml-4 list-disc">{(c.gaps ?? []).map((g) => <li key={g}>{g}</li>)}</ul></details>}
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

function ImportedDomains() {
  const { control, setValue } = useFormContext<WizardValues>();
  const text = useWatch({ control, name: 'imported_domains_text' });
  const err = useFieldError('imported_domains_text');
  const parsed = React.useMemo(() => parseDomains(text), [text]);
  const fileRef = React.useRef<HTMLInputElement>(null);
  const id = idOf('imported_domains_text');
  const onFile = async (f: File | undefined) => {
    if (!f) return;
    if (f.size > 5 * 1024 * 1024) { toast.error('File too large', 'Domain lists are limited to 5 MB here.'); return; }
    const t = await f.text();
    setValue('imported_domains_text', text ? `${text.trimEnd()}\n${t}` : t, { shouldDirty: true, shouldValidate: true });
    toast.success('List loaded', `${f.name} added to the domain list.`);
  };
  return (
    <Section title="Imported domain list" description="Optional. Provide domains you already know; they are looked up and crawled directly without a discovery source.">
      <FormField label="Domains" htmlFor={id} error={err} hint="One per line, or comma separated. Schemes, www. and paths are stripped.">
        <Controller control={control} name="imported_domains_text" render={({ field }) => (
          <Textarea id={id} rows={5} spellCheck={false} placeholder={'example.com\nacme-electrical.co.uk'} aria-invalid={!!err} aria-describedby={err ? `${id}-error` : `${id}-hint`} {...field} />
        )} />
      </FormField>
      <div className="flex flex-wrap items-center gap-3">
        <input ref={fileRef} type="file" accept=".csv,.txt,text/csv,text/plain" className="sr-only" tabIndex={-1} aria-label="Upload a CSV or text file of domains" onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ''; void onFile(f); }} />
        <Button type="button" variant="outline" size="sm" onClick={() => fileRef.current?.click()}><FileUp /> Upload CSV / text</Button>
        {text.trim() && (
          <p className="tabular text-xs text-muted-foreground" aria-live="polite">
            {parsed.valid.length.toLocaleString('en-US')} valid domain{parsed.valid.length === 1 ? '' : 's'}
            {parsed.duplicates > 0 && <> · {parsed.duplicates} duplicate{parsed.duplicates === 1 ? '' : 's'} removed</>}
            {parsed.invalid.length > 0 && <span className="font-medium text-destructive"> · {parsed.invalid.length} invalid</span>}
          </p>
        )}
        {text.trim() && <Button type="button" variant="ghost" size="sm" onClick={() => setValue('imported_domains_text', '', { shouldDirty: true, shouldValidate: true })}>Clear</Button>}
      </div>
      {parsed.invalid.length > 0 && (
        <ul tabIndex={0} className="max-h-28 list-disc overflow-auto rounded-md border border-destructive/40 bg-destructive/5 py-2 pl-7 pr-3 text-xs" aria-label="Invalid entries">
          {parsed.invalid.slice(0, 20).map((i, n) => <li key={n}>Line {i.line}: <span className="font-mono">{i.value}</span></li>)}
          {parsed.invalid.length > 20 && <li>…and {parsed.invalid.length - 20} more</li>}
        </ul>
      )}
    </Section>
  );
}
