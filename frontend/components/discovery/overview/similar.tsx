'use client';
import * as React from 'react';
import Link from 'next/link';
import { useQuery } from '@tanstack/react-query';
import { Sparkles, Users } from 'lucide-react';
import { api, type Page, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { useApiMutation } from '@/hooks/use-api-mutation';
import { Can } from '@/lib/auth';
import { formatNumber, titleCase } from '@/lib/format';
import { EmptyState } from '@/components/composites/empty-state';
import { ConfidenceMeter } from '@/components/composites/confidence-meter';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { FormField } from '@/components/ui/label';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import { Combobox, MultiSelect } from '@/components/ui/combobox';
import { ConfirmDialog } from '@/components/ui/alert-dialog';
import { COUNTRY_OPTIONS, useCategoryOptions } from '@/components/contacts/shared';
import { OrgCombobox, SectionTitle } from '../shared';

type SimilarOut = Schema<'SimilarOut'>;
type SeedIn = Schema<'SeedIn'>;
type Run = Schema<'RunSummary'>;
const ACTIVE_RUN = new Set(['draft', 'scheduled', 'queued', 'running', 'paused']);
const FACTOR_LABEL: Record<string, string> = { sic: 'Industry code (SIC)', category: 'Category', keywords: 'Name / industry words', geography: 'Location', size: 'Size and legal type', accreditations: 'Accreditations' };

export function SimilarFinder() {
  return (
    <section aria-labelledby="sim-h" className="space-y-4">
      <SectionTitle id="sim-h" description="Deterministic lookalike search: each result shows exactly how its score is composed. Nothing is queued until you expand the results into a run.">Similar-business finder</SectionTitle>
      <Can role="operator" fallback={<EmptyState compact icon={Users} title="Operators only" description="Finding lookalikes and queueing them into a run needs the Operator role." />}>
        <Finder />
      </Can>
    </section>
  );
}

function useRunOptions() {
  const q = useQuery({ queryKey: qk.list('runs', { picker: 'similar' }), queryFn: ({ signal }) => api.get<Page<Run>>('/api/runs', { query: { limit: 200 }, signal }), staleTime: 15_000 });
  return { ...q, runs: (q.data?.items ?? []).filter((r) => ACTIVE_RUN.has(r.status)), all: q.data?.items ?? [] };
}

function Finder() {
  const [seedKind, setSeedKind] = React.useState<'org' | 'category' | 'run'>('org');
  const [org, setOrg] = React.useState<{ id: number; label?: string } | null>(null);
  const [cat, setCat] = React.useState<string | null>(null);
  const [run, setRun] = React.useState<string | null>(null);
  const [countries, setCountries] = React.useState<string[]>([]);
  const [minScore, setMinScore] = React.useState('0.35');
  const [limit, setLimit] = React.useState('20');
  const [err, setErr] = React.useState<string>();
  const [target, setTarget] = React.useState<string | null>(null);
  const [lastSeed, setLastSeed] = React.useState<SeedIn | null>(null);
  const cats = useCategoryOptions();
  const runs = useRunOptions();

  const find = useApiMutation({
    mutationFn: (body: SeedIn) => api.post<SimilarOut>('/api/discovery/similar', body),
    errorMessage: false,
    onError: (e) => setErr(e.message),
  });
  const expand = useApiMutation({
    mutationFn: (body: Schema<'SimilarExpandIn'>) => api.post<Schema<'SimilarExpandOut'>>('/api/discovery/similar/expand', body),
    successMessage: (r) => `Queued ${r.queued.length} lookalike${r.queued.length === 1 ? '' : 's'} into run #${r.target_run_id}`,
    invalidate: [qk.all('runs')],
  });

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    setErr(undefined);
    const ms = Number(minScore); const lim = Number(limit);
    if (!(ms >= 0 && ms <= 1)) { setErr('Minimum score must be between 0 and 1.'); return; }
    if (!Number.isInteger(lim) || lim < 1 || lim > 100) { setErr('Results must be a whole number between 1 and 100.'); return; }
    const body: SeedIn = { limit: lim, min_score: ms, countries };
    if (seedKind === 'org') { if (!org) { setErr('Choose a seed business.'); return; } body.org_id = org.id; }
    if (seedKind === 'category') { if (!cat) { setErr('Choose a seed category.'); return; } body.category_id = Number(cat); }
    if (seedKind === 'run') { if (!run) { setErr('Choose a seed run.'); return; } body.run_id = Number(run); }
    setLastSeed(body);
    expand.reset();
    find.mutate(body);
  };

  const items = find.data?.items ?? [];
  const runLabel = (r: Run) => `#${r.id} · ${r.name} (${r.status})`;
  return (
    <div className="space-y-4">
      <form onSubmit={submit} className="space-y-4 rounded-lg border bg-card p-4 shadow-card" aria-label="Similar business search" data-testid="similar-form">
        <fieldset className="space-y-2">
          <legend className="text-sm font-medium">Seed</legend>
          <RadioGroup value={seedKind} onValueChange={(v) => setSeedKind(v as typeof seedKind)} className="flex flex-wrap gap-4" aria-label="Seed type">
            {(['org', 'category', 'run'] as const).map((k) => (
              <label key={k} className="flex items-center gap-2 text-sm"><RadioGroupItem value={k} />{k === 'org' ? 'A business' : k === 'category' ? 'A category' : 'A run’s businesses'}</label>
            ))}
          </RadioGroup>
        </fieldset>
        <div className="grid gap-3 md:grid-cols-2">
          {seedKind === 'org' && <FormField label="Seed business" htmlFor="sim-org"><OrgCombobox id="sim-org" aria-label="Seed business" value={org?.id ?? null} selectedLabel={org?.label} onChange={(id, label) => setOrg(id ? { id, label } : null)} /></FormField>}
          {seedKind === 'category' && <FormField label="Seed category" htmlFor="sim-cat"><Combobox id="sim-cat" aria-label="Seed category" options={cats.options} value={cat} onChange={setCat} loading={cats.isPending} clearable placeholder="Choose a category…" /></FormField>}
          {seedKind === 'run' && <FormField label="Seed run" htmlFor="sim-run" error={runs.isError ? `Could not load runs: ${runs.error.message}` : undefined}><Combobox id="sim-run" aria-label="Seed run" options={runs.all.map((r) => ({ value: String(r.id), label: runLabel(r) }))} value={run} onChange={setRun} loading={runs.isPending} clearable placeholder="Choose a run…" /></FormField>}
          <FormField label="Limit to countries" htmlFor="sim-cc" hint="Optional; any country when empty."><MultiSelect id="sim-cc" aria-label="Limit to countries" options={COUNTRY_OPTIONS} value={countries} onChange={setCountries} max={20} /></FormField>
          <FormField label="Minimum score (0–1)" htmlFor="sim-min"><Input id="sim-min" inputMode="decimal" value={minScore} onChange={(e) => setMinScore(e.target.value)} /></FormField>
          <FormField label="Results (1–100)" htmlFor="sim-lim"><Input id="sim-lim" inputMode="numeric" value={limit} onChange={(e) => setLimit(e.target.value)} /></FormField>
        </div>
        {err && <p role="alert" className="text-sm font-medium text-destructive">{err}</p>}
        <Button type="submit" loading={find.isPending}><Sparkles /> Find similar businesses</Button>
      </form>

      {find.isSuccess && (
        items.length === 0 ? <EmptyState compact icon={Users} title="No lookalikes above the minimum score" description="Lower the minimum score, remove country limits, or pick a seed with more data (industry code, category, location)." /> : (
          <div className="space-y-3" data-testid="similar-results">
            <div className="flex flex-wrap items-end justify-between gap-3 rounded-lg border bg-muted/30 p-3">
              <p className="text-sm"><strong className="tabular">{formatNumber(items.length)}</strong> lookalikes found. Expand them into a run to resolve their websites and crawl them.</p>
              <div className="flex flex-wrap items-end gap-2">
                <FormField label="Target run" htmlFor="sim-target" className="w-64" error={runs.isError ? `Could not load runs: ${runs.error.message}` : undefined}>
                  <Combobox id="sim-target" aria-label="Target run" options={runs.runs.map((r) => ({ value: String(r.id), label: runLabel(r) }))} value={target} onChange={setTarget} placeholder={runs.runs.length ? 'Choose a draft, running or paused run…' : 'No active runs'} />
                </FormField>
                <ConfirmDialog
                  title="Expand lookalikes into this run?"
                  description={<p>Up to {lastSeed?.limit ?? items.length} businesses scoring at least {lastSeed?.min_score} will be queued for domain resolution and crawling in run #{target}. Businesses already in the run are skipped. This is recorded in the audit log.</p>}
                  confirmLabel="Queue into run"
                  trigger={<Button disabled={!target} data-testid="expand-into-run">Expand into run</Button>}
                  onConfirm={() => expand.mutateAsync({ ...(lastSeed as SeedIn), target_run_id: Number(target) })} />
              </div>
            </div>
            {expand.data && (
              <p role="status" className="rounded-md border border-success-border bg-success-soft p-3 text-sm text-success-foreground" data-testid="expand-result">
                Queued {expand.data.queued.length} business{expand.data.queued.length === 1 ? '' : 'es'} into <Link className="underline" href={`/runs/${expand.data.target_run_id}`}>run #{expand.data.target_run_id}</Link>.
              </p>
            )}
            <ol className="space-y-2" aria-label="Lookalike businesses, best first">
              {items.map((it) => (
                <li key={it.org_id} className="rounded-lg border bg-card p-3" data-testid="similar-item">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div className="min-w-0">
                      <Link href={`/businesses/${it.org_id}`} className="font-medium text-primary underline-offset-2 hover:underline">{it.name ?? `Business #${it.org_id}`}</Link>
                      <p className="text-xs text-muted-foreground">{[it.city, it.country_code].filter(Boolean).join(', ') || '—'}</p>
                    </div>
                    <span className="flex items-center gap-2 text-xs"><span className="text-muted-foreground">Similarity</span><ConfidenceMeter value={it.score * 100} showLabel={false} /></span>
                  </div>
                  <details className="mt-2 text-xs">
                    <summary className="cursor-pointer rounded text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">Why: <span className="font-mono">{it.explanation}</span></summary>
                    <ul className="mt-2 grid gap-1 sm:grid-cols-2">
                      {Object.entries(it.factors ?? {}).map(([k, f]) => {
                        const used = !!f.used;
                        return (
                          <li key={k} className={used ? '' : 'text-muted-foreground'}>
                            <span className="font-medium">{FACTOR_LABEL[k] ?? titleCase(k)}</span>: {used ? `${Number(f.score).toFixed(2)} × weight ${Number(f.weight).toFixed(2)}` : 'not used'}
                            {f.detail ? <span className="block text-muted-foreground">{String(f.detail)}</span> : null}
                          </li>
                        );
                      })}
                    </ul>
                  </details>
                </li>
              ))}
            </ol>
          </div>
        )
      )}
    </div>
  );
}
