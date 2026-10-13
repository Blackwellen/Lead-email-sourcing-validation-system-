'use client';
import * as React from 'react';
import { Controller, useFieldArray, useFormContext, useWatch } from 'react-hook-form';
import { Plus, Trash2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { AdvancedPanel } from '@/components/ui/collapsible';
import { titleCase } from '@/lib/format';
import { NumField, RadioField, SwitchField, idOf, useFieldError } from './fields';
import { SIGNAL_TYPES, SOURCE_CATEGORY_OPTIONS, type WizardValues } from './schema';

function CheckGrid({ name, options, legend }: { name: 'discovery.source_categories' | 'discovery.signals.types'; options: readonly { value: string; label: string; hint?: string }[]; legend: string }) {
  const { control } = useFormContext<WizardValues>();
  const id = idOf(name);
  return (
    <fieldset className="space-y-2">
      <legend className="text-sm font-medium">{legend}</legend>
      <Controller control={control} name={name} render={({ field }) => {
        const cur = (field.value as string[]) ?? [];
        return (
          <div className="grid gap-2 sm:grid-cols-2">
            {options.map((o) => (
              <label key={o.value} htmlFor={`${id}-${o.value}`} className="flex cursor-pointer items-start gap-2.5 rounded-md border p-2.5 text-sm hover:bg-muted/50 has-[[data-state=checked]]:border-primary has-[[data-state=checked]]:bg-accent/40">
                <Checkbox id={`${id}-${o.value}`} className="mt-0.5" checked={cur.includes(o.value)} onCheckedChange={(v) => field.onChange(v === true ? [...cur, o.value] : cur.filter((x) => x !== o.value))} />
                <span><span className="block font-medium">{o.label}</span>{o.hint && <span className="block text-xs text-muted-foreground">{o.hint}</span>}</span>
              </label>
            ))}
          </div>
        );
      }} />
    </fieldset>
  );
}

/** Advanced discovery: bound to RunConfig.discovery (collapsed by default). */
export function DiscoveryPanel() {
  const { control, register } = useFormContext<WizardValues>();
  const d = useWatch({ control, name: 'discovery' });
  const { fields, append, remove } = useFieldArray({ control, name: 'discovery.quota_rows' });
  const quotaErr = useFieldError('discovery.quota_rows');
  return (
    <AdvancedPanel title="Advanced discovery" description="Strategy, source categories, network expansion, signals and budgets">
      <div className="space-y-6">
        <RadioField name="discovery.strategy" legend="Strategy" columns={2} options={[
          { value: 'fast', label: 'Fast', hint: 'Fewest stages; quickest results, least thorough.' },
          { value: 'balanced', label: 'Balanced', hint: 'Default mix of speed and thoroughness.' },
          { value: 'deep', label: 'Deep', hint: 'Most stages enabled; slower, uses more requests.' },
          { value: 'custom', label: 'Custom', hint: 'Starts from Balanced and follows your explicit choices below.' },
        ]} />
        <CheckGrid name="discovery.source_categories" legend="Source categories allowed" options={SOURCE_CATEGORY_OPTIONS} />
        <RadioField name="discovery.contact_discovery" legend="Contact discovery" options={[
          { value: 'published_only', label: 'Published only', hint: 'Only addresses published by the business.' },
          { value: 'published_plus_corroborated', label: 'Published and corroborated', hint: 'Also addresses confirmed by a second source.' },
          { value: 'include_inferred', label: 'Include inferred research candidates', hint: 'Adds a controlled research stage for role and pattern candidates. They stay labelled as inferred.' },
        ]} />
        <RadioField name="discovery.business_scope" legend="Business scope" columns={3} options={[
          { value: 'new_only', label: 'New businesses only' },
          { value: 'refresh_existing', label: 'Refresh existing' },
          { value: 'both', label: 'Both' },
        ]} />

        <div className="space-y-3 rounded-md border p-3">
          <SwitchField name="discovery.network.enabled" label="Business network expansion" hint="Follow partner, member and supplier links from businesses already found." />
          {d.network.enabled && (
            <div className="grid gap-3 sm:grid-cols-2">
              <NumField name="discovery.network.max_depth" label="Maximum depth (1–3)" min={1} max={3} hint="Default 1." />
              <NumField name="discovery.network.source_budget" label="Page budget per seed business" min={1} max={100000} hint="Default 50 page fetches." />
            </div>
          )}
        </div>

        <div className="space-y-3 rounded-md border p-3">
          <SwitchField name="discovery.signals.enabled" label="Activity signals" hint="Detect hiring, expansion, contract awards and similar from sources that provide a URL." />
          {d.signals.enabled && (
            <>
              <CheckGrid name="discovery.signals.types" legend="Signal types (none selected = all types)" options={SIGNAL_TYPES.map((t) => ({ value: t, label: titleCase(t) }))} />
              <NumField name="discovery.signals.freshness_days" label="Freshness (days)" min={1} max={3650} hint="Default 180." className="max-w-xs" />
            </>
          )}
        </div>

        <fieldset className="space-y-2">
          <legend className="text-sm font-medium">Source quotas</legend>
          <p className="text-xs text-muted-foreground">Cap the work units a single source (adapter id) or source category may use. Leave empty for no per-source cap.</p>
          {fields.map((f, i) => (
            <div key={f.id} className="flex flex-wrap items-start gap-2">
              <div className="min-w-40 flex-1">
                <Label htmlFor={`quota-key-${i}`} className="sr-only">Source or category {i + 1}</Label>
                <Input id={`quota-key-${i}`} placeholder="e.g. companies_house_bulk" {...register(`discovery.quota_rows.${i}.key` as const)} />
              </div>
              <div className="w-36">
                <Label htmlFor={`quota-val-${i}`} className="sr-only">Maximum work units {i + 1}</Label>
                <Controller control={control} name={`discovery.quota_rows.${i}.value` as const} render={({ field }) => (
                  <Input id={`quota-val-${i}`} type="number" min={0} placeholder="Max units" value={Number.isNaN(field.value) ? '' : field.value} onChange={(e) => field.onChange(e.target.value === '' ? Number.NaN : Number(e.target.value))} />
                )} />
              </div>
              <Button type="button" variant="ghost" size="icon" aria-label={`Remove quota ${i + 1}`} onClick={() => remove(i)}><Trash2 /></Button>
            </div>
          ))}
          {quotaErr && <p role="alert" className="text-xs font-medium text-destructive">{quotaErr}</p>}
          <Button type="button" variant="outline" size="sm" onClick={() => append({ key: '', value: 1000 })}><Plus /> Add quota</Button>
        </fieldset>

        <div className="grid gap-4 sm:grid-cols-2">
          <NumField name="discovery.overall_max_work" label="Overall maximum work units" nullable min={1} placeholder="No limit" hint="Hard budget across all sources. Empty = no limit." />
          <NumField name="discovery.search_budget" label="Search budget per company (0–50)" min={0} max={50} hint="Search-provider queries allowed per company. Default 0 = none." />
        </div>
        <RadioField name="discovery.fallback_research" legend="When a business has no website" options={[
          { value: 'stop_if_no_website', label: 'Stop for that business', hint: 'Do not try further.' },
          { value: 'try_other_sources', label: 'Try other source categories', hint: 'Look in registers, archives and documents.' },
          { value: 'flag_for_review', label: 'Flag for review', hint: 'Keep the business and mark it for a person to check.' },
        ]} />
      </div>
    </AdvancedPanel>
  );
}
