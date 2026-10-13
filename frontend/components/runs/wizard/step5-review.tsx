'use client';
import * as React from 'react';
import { useFormContext, useWatch } from 'react-hook-form';
import { Play, Save } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Label } from '@/components/ui/label';
import { KeyValueList } from '@/components/composites/key-value-list';
import { LoadingState } from '@/components/composites/loading-state';
import { formatDateTime, titleCase } from '@/lib/format';
import { useCategoryTree } from '../use-categories';
import { EstimateSummary, Notice } from './estimate-panel';
import { Section } from './fields';
import { SavePresetDialog } from './save-preset-dialog';
import { SOURCE_CATEGORY_OPTIONS, TARGET_DEFINITIONS, UNIQUENESS_MODES, parseDomains, stepOfField, STEP_TITLES, type WizardValues } from './schema';
import { useRunEstimate, useRunValidation } from './use-estimate';
import { useCountries } from './step1-business';

export function Step5Review({ onGoto, onSaveDraft, savingDraft, onStart, starting, draftId, serverErrors }: {
  onGoto: (step: number) => void; onSaveDraft: () => void; savingDraft: boolean; onStart: () => void; starting: boolean; draftId: number | null;
  serverErrors: { field: string; message: string }[];
}) {
  const { control } = useFormContext<WizardValues>();
  const v = useWatch({ control }) as WizardValues;
  const est = useRunEstimate(v);
  const val = useRunValidation(v);
  const { flat } = useCategoryTree();
  const countries = useCountries();
  const [ack, setAck] = React.useState(false);

  const dom = parseDomains(v.imported_domains_text);
  const t = v.target;
  const nameOf = (cc: string) => countries.data?.find((c) => c.code === cc)?.name ?? cc;
  const blocking = est.data?.valid ? (est.data.restrictions ?? []).filter((r) => r.severity === 'blocking') : [];
  const errors = [...(val.data?.errors ?? []), ...serverErrors.filter((s) => !(val.data?.errors ?? []).some((e) => e.field === s.field))];
  const canStart = !val.isPending && errors.length === 0 && (blocking.length === 0 || ack);
  const infra = est.data?.valid ? (est.data.infra as Record<string, number>) : undefined;

  const restrictionNotes = est.data?.valid ? (est.data.restrictions ?? []) : [];
  const warnings = [...(val.data?.warnings ?? [])];
  const allWarnings = [...new Set([...warnings, ...(est.data?.valid ? (est.data.warnings ?? []) : [])])];

  const edit = (step: number) => <Button type="button" variant="link" size="sm" onClick={() => onGoto(step)}>Edit<span className="sr-only"> {STEP_TITLES[step]}</span></Button>;

  return (
    <div className="space-y-5">
      {(errors.length > 0 || val.isPending) && (
        <section aria-label="Problems to fix" className="rounded-lg border border-danger-border bg-danger-soft p-4 text-danger-foreground">
          <h2 className="text-sm font-semibold">{val.isPending ? 'Checking configuration…' : `${errors.length} problem${errors.length === 1 ? '' : 's'} to fix before starting`}</h2>
          <ul className="mt-2 space-y-1 text-sm">
            {errors.map((e, i) => (
              <li key={i}>
                <span className="font-mono text-xs">{e.field || 'config'}</span>: {e.message}{' '}
                <button type="button" className="underline underline-offset-2" onClick={() => onGoto(stepOfField(e.field))}>Fix in step {stepOfField(e.field) + 1}</button>
              </li>
            ))}
          </ul>
        </section>
      )}

      <div className="grid gap-5 lg:grid-cols-2">
        <Section title="Businesses" className="h-fit">
          <div className="flex justify-end">{edit(0)}</div>
          <KeyValueList items={[
            { label: 'Run name', value: v.name },
            { label: 'Destination category', value: v.destination_category_id ? flat.get(v.destination_category_id)?.path ?? `#${v.destination_category_id}` : 'None' },
            { label: 'Countries', value: v.countries.map(nameOf).join(', ') },
            { label: 'Regions', value: v.regions.length ? v.regions.join(', ') : 'Whole country' },
            ...(v.localities.length || v.postcodes.length ? [{ label: 'Towns / postcode areas', value: [...v.localities, ...v.postcodes].join(', ') }] : []),
            { label: 'Categories', value: v.category_ids.length ? v.category_ids.map((id) => flat.get(id)?.path ?? `#${id}`).join('; ') : 'None selected' },
            { label: 'Keywords / exclusions', value: `${v.keywords.join(', ') || 'no keywords'} / ${v.exclusions.join(', ') || 'no exclusions'}` },
            { label: 'Scope', value: `${v.active_only ? 'Active only' : 'Including inactive'}, ${v.refresh_existing ? 'refreshing known businesses' : 'new businesses only'}` },
          ]} />
        </Section>
        <Section title="Contacts" className="h-fit">
          <div className="flex justify-end">{edit(2)}</div>
          <KeyValueList items={[
            { label: 'Target', value: `${t.count.toLocaleString('en-US')} × ${TARGET_DEFINITIONS.find((d) => d.value === t.definition)?.label ?? t.definition}` },
            { label: 'Uniqueness', value: UNIQUENESS_MODES.find((u) => u.value === t.uniqueness_mode)?.label + (t.uniqueness_mode === 'multi' ? ` (max ${t.max_contacts_per_company})` : '') },
            { label: 'Contact type', value: titleCase(t.contact_type) },
            { label: 'Origin', value: t.published_only ? 'Published (or corroborated) only' : t.allow_inferred ? 'Published plus inferred research candidates' : 'Published, not limited to published-only' },
            { label: 'Minimum confidence', value: String(t.min_confidence) },
            { label: 'Exclusions', value: [t.exclude_free_providers && 'free providers', t.exclude_catchall_or_inconclusive && 'catch-all / inconclusive', t.exclude_previous_bounces && 'previous bounces', t.exclude_suppressed && 'suppressed'].filter(Boolean).join(', ') || 'None' },
          ]} />
        </Section>
        <Section title="Sources" className="h-fit">
          <div className="flex justify-end">{edit(1)}</div>
          <KeyValueList items={[
            { label: 'Sources', value: v.sources.length ? v.sources.join(', ') : 'None (imported domains only)' },
            { label: 'Imported domains', value: dom.valid.length ? `${dom.valid.length.toLocaleString('en-US')} domains` : 'None' },
            { label: 'Pages crawled per site', value: String(v.crawl_max_pages) },
            { label: 'Discovery strategy', value: `${titleCase(v.discovery.strategy)}; ${v.discovery.source_categories.map((c) => SOURCE_CATEGORY_OPTIONS.find((o) => o.value === c)?.label ?? c).join(', ') || 'no source categories'}` },
            { label: 'Contact discovery', value: titleCase(v.discovery.contact_discovery) },
            { label: 'Network expansion', value: v.discovery.network.enabled ? `On, depth ${v.discovery.network.max_depth}, ${v.discovery.network.source_budget} pages per seed` : 'Off' },
            { label: 'Activity signals', value: v.discovery.signals.enabled ? `On, ${v.discovery.signals.types.length ? v.discovery.signals.types.map(titleCase).join(', ') : 'all types'}, last ${v.discovery.signals.freshness_days} days` : 'Off' },
          ]} />
        </Section>
        <Section title="Validation and processing" className="h-fit">
          <div className="flex justify-end">{edit(3)}</div>
          <KeyValueList items={[
            { label: 'Verification mode', value: titleCase(v.verification.mode) },
            { label: 'Worker preset', value: titleCase(v.performance.preset) },
            { label: 'Desired rate (not guaranteed)', value: [v.performance.desired_websites_per_hour && `${v.performance.desired_websites_per_hour} websites/h`, v.performance.desired_businesses_per_hour && `${v.performance.desired_businesses_per_hour} businesses/h`, v.performance.desired_records_per_hour && `${v.performance.desired_records_per_hour} records/h`].filter(Boolean).join(', ') || 'No target set' },
            { label: 'Retries / resume', value: `${v.performance.retry_max_attempts} attempts; ${v.performance.resume_existing ? 'resumes existing work' : 'starts fresh'}` },
            { label: 'Limits', value: [t.max_runtime_minutes && `${t.max_runtime_minutes} min`, t.max_work_jobs && `${t.max_work_jobs} jobs`, v.discovery.overall_max_work && `${v.discovery.overall_max_work} work units`].filter(Boolean).join(', ') || 'None' },
            { label: 'Start', value: v.schedule_at ? `Scheduled for ${formatDateTime(v.schedule_at)}` : 'As soon as you press Start' },
          ]} />
        </Section>
      </div>

      <Section title="Estimated infrastructure and expectations" description="From POST /api/runs/estimate. No duration is predicted before a run starts; ETAs appear from measured throughput.">
        <EstimateSummary q={est} showInfra />
        {infra && <p className="sr-only">Estimate loaded</p>}
      </Section>

      <Section title="Data restrictions and warnings">
        {est.isPending || val.isPending ? <LoadingState label="Checking…" rows={2} /> : restrictionNotes.length + allWarnings.length === 0 ? (
          <p className="text-sm text-muted-foreground">No restrictions or warnings were reported for this configuration.</p>
        ) : (
          <ul className="space-y-1.5">
            {restrictionNotes.map((r) => <Notice key={r.code} tone={r.severity === 'blocking' ? 'blocking' : r.severity === 'warning' ? 'warning' : 'info'}>{r.message}</Notice>)}
            {allWarnings.map((w, i) => <Notice key={i} tone="warning">{w}</Notice>)}
          </ul>
        )}
        {blocking.length > 0 && (
          <div className="flex items-start gap-2 rounded-md border p-3">
            <Checkbox id="ack-blocking" checked={ack} onCheckedChange={(c) => setAck(c === true)} className="mt-0.5" />
            <Label htmlFor="ack-blocking" className="font-normal">I understand a selected source is not configured and will produce no records, and want to start anyway.</Label>
          </div>
        )}
      </Section>

      <div className="flex flex-wrap items-center gap-2 border-t pt-4" role="group" aria-label="Run actions">
        <Button type="button" variant="outline" onClick={onSaveDraft} loading={savingDraft}><Save /> {draftId ? 'Save draft' : 'Save draft'}</Button>
        <SavePresetDialog />
        <div className="ml-auto flex items-center gap-3">
          {!canStart && !starting && <span className="text-xs text-muted-foreground">{errors.length ? 'Fix the problems above to start.' : val.isPending ? 'Checking…' : 'Acknowledge the warning above to start.'}</span>}
          <Button type="button" size="lg" onClick={onStart} loading={starting} disabled={!canStart}><Play /> {v.schedule_at ? 'Schedule run' : 'Start run'}</Button>
        </div>
      </div>
    </div>
  );
}
