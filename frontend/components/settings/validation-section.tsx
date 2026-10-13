'use client';
import * as React from 'react';
import { Controller, useFieldArray, type Control, type UseFormReturn } from 'react-hook-form';
import { z } from 'zod';
import { Plus, Scale, Trash2 } from 'lucide-react';
import type { Schema } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { FormField } from '@/components/ui/label';
import { SimpleSelect } from '@/components/ui/select';
import { Switch } from '@/components/ui/switch';
import { ScrollRegion, tdClass, thClass } from '@/components/system/scroll-region';
import { FieldGrid, num, SectionForm } from './section-form';

type V = Schema<'ValidationSettings'>;
type Policy = Schema<'CountryMarketingPolicy'>;
const VALUES = ['eligible', 'needs_review', 'consent_required', 'not_permitted'] as const;
const LABEL: Record<(typeof VALUES)[number], string> = { eligible: 'Eligible', needs_review: 'Needs review', consent_required: 'Consent required', not_permitted: 'Not permitted' };
const COLUMNS = [['personal', 'Named person'], ['role', 'Role mailbox'], ['sole_trader', 'Sole trader'], ['free_provider', 'Free-mail'], ['unknown_entity', 'Unknown entity']] as const;

const policyRow = z.object({
  code: z.string().trim().regex(/^([A-Z]{2}|EU|default)$/, 'Use a 2-letter upper-case country code, EU or default'),
  personal: z.enum(VALUES), role: z.enum(VALUES), sole_trader: z.enum(VALUES), free_provider: z.enum(VALUES), unknown_entity: z.enum(VALUES), allow_eligible: z.boolean(),
});
const schema = z.object({
  default_mode: z.enum(['fast', 'deep', 'custom']),
  validity_days: z.number('Validity must be a number').int().min(1, 'At least 1 day').max(730, 'At most 730 days'),
  recheck_after_days: z.number('Re-check must be a number').int().min(1, 'At least 1 day').max(1460, 'At most 1460 days'),
  marketing_policy: z.array(policyRow).superRefine((rows, ctx) => {
    const seen = new Set<string>();
    rows.forEach((r, i) => { if (seen.has(r.code)) ctx.addIssue({ code: 'custom', path: [i, 'code'], message: 'Duplicate code' }); seen.add(r.code); });
  }),
});
type Form = z.infer<typeof schema>;

export function ValidationSection() {
  return (
    <SectionForm<Form, V> name="validation" title="Validation defaults" schema={schema}
      description="Default verification mode, how long a result stays fresh, and the marketing-eligibility policy applied per country."
      toForm={(a) => ({
        default_mode: (a.default_mode ?? 'fast') as Form['default_mode'], validity_days: a.validity_days ?? 30, recheck_after_days: a.recheck_after_days ?? 90,
        marketing_policy: Object.entries(a.marketing_policy ?? {}).map(([code, p]) => ({ code, personal: p.personal ?? 'needs_review', role: p.role ?? 'needs_review', sole_trader: p.sole_trader ?? 'consent_required', free_provider: p.free_provider ?? 'consent_required', unknown_entity: p.unknown_entity ?? 'needs_review', allow_eligible: p.allow_eligible ?? false })),
      })}
      toApi={(v) => ({
        default_mode: v.default_mode, validity_days: v.validity_days, recheck_after_days: v.recheck_after_days,
        marketing_policy: Object.fromEntries(v.marketing_policy.map(({ code, ...p }) => [code, p as Policy])),
      })}>
      {(form, readOnly) => (
        <>
          <FieldGrid>
            <FormField label="Default validation mode" htmlFor="v-mode" hint="Fast = DNS and syntax. Deep adds slower evidence checks.">
              <Controller control={form.control} name="default_mode" render={({ field }) => (
                <SimpleSelect id="v-mode" aria-label="Default validation mode" value={field.value} onValueChange={field.onChange} options={[{ value: 'fast', label: 'Fast' }, { value: 'deep', label: 'Deep' }, { value: 'custom', label: 'Custom' }]} />
              )} />
            </FormField>
            <span className="hidden sm:block" />
            <FormField label="Result stays fresh (days)" htmlFor="v-valid" error={form.formState.errors.validity_days?.message} hint="After this a validation counts as stale (1–730)"><Input id="v-valid" inputMode="numeric" {...num(form.register, 'validity_days')} invalid={!!form.formState.errors.validity_days} /></FormField>
            <FormField label="Re-check after (days)" htmlFor="v-recheck" error={form.formState.errors.recheck_after_days?.message} hint="1–1460"><Input id="v-recheck" inputMode="numeric" {...num(form.register, 'recheck_after_days')} invalid={!!form.formState.errors.recheck_after_days} /></FormField>
          </FieldGrid>
          <MarketingPolicyEditor form={form} readOnly={readOnly} />
        </>
      )}
    </SectionForm>
  );
}

function MarketingPolicyEditor({ form, readOnly }: { form: UseFormReturn<Form>; readOnly: boolean }) {
  const { fields, append, remove } = useFieldArray({ control: form.control, name: 'marketing_policy' });
  const errs = form.formState.errors.marketing_policy;
  return (
    <section aria-labelledby="mp-h" className="space-y-3 rounded-md border p-4" data-testid="marketing-policy">
      <div>
        <h3 id="mp-h" className="flex items-center gap-2 text-sm font-semibold"><Scale className="size-4" aria-hidden /> Marketing policy by country</h3>
        <p role="note" data-testid="not-legal-advice" className="mt-1 rounded-md border border-warning-border bg-warning-soft p-2 text-xs text-warning-foreground">
          <strong>This is not legal advice.</strong> The table only tells the system how to <em>triage</em> addresses (eligible, needs review, consent required, not permitted); it does not make any sending lawful. Rules differ by country and by type of recipient (PECR, GDPR, CAN-SPAM, CASL and others), and you remain responsible for compliance. Nothing is ever marked <em>eligible</em> unless “Allow eligible” is switched on for that row and the address is not suppressed.
        </p>
        <p className="mt-2 text-xs text-muted-foreground">Rows apply to an exact country code first, then to <code>GB</code> / <code>EU</code> groups, then to <code>default</code>. Countries without a row use cautious built-in defaults.</p>
      </div>
      {fields.length === 0 ? <p className="text-sm text-muted-foreground">No custom rows: built-in cautious defaults apply everywhere.</p> : (
        <ScrollRegion label="Marketing policy table">
          <table className="w-full min-w-[56rem] border-collapse">
            <caption className="sr-only">Marketing policy by country and recipient type</caption>
            <thead><tr>
              <th scope="col" className={thClass}>Country</th>
              {COLUMNS.map(([, l]) => <th key={l} scope="col" className={thClass}>{l}</th>)}
              <th scope="col" className={thClass}>Allow “eligible”</th><th scope="col" className={thClass}><span className="sr-only">Remove</span></th>
            </tr></thead>
            <tbody>
              {fields.map((f, i) => (
                <tr key={f.id}>
                  <td className={tdClass}>
                    <Input aria-label={`Country code, row ${i + 1}`} className="w-24 uppercase" {...form.register(`marketing_policy.${i}.code` as const, { setValueAs: (v: string) => (v?.toLowerCase() === 'default' ? 'default' : (v ?? '').toUpperCase()) })} invalid={!!errs?.[i]?.code} />
                    {errs?.[i]?.code?.message && <p role="alert" className="mt-1 max-w-[10rem] text-xs font-medium text-destructive">{errs[i]?.code?.message}</p>}
                  </td>
                  {COLUMNS.map(([k, l]) => (
                    <td key={k} className={tdClass}>
                      <Controller control={form.control as Control<Form>} name={`marketing_policy.${i}.${k}` as const} render={({ field }) => (
                        <SimpleSelect aria-label={`${l}, row ${i + 1}`} className="w-40" value={field.value as string} onValueChange={field.onChange} options={VALUES.map((v) => ({ value: v, label: LABEL[v] }))} />
                      )} />
                    </td>
                  ))}
                  <td className={tdClass}>
                    <Controller control={form.control} name={`marketing_policy.${i}.allow_eligible` as const} render={({ field }) => (
                      <Switch checked={!!field.value} onCheckedChange={field.onChange} aria-label={`Allow eligible, row ${i + 1}`} />
                    )} />
                  </td>
                  <td className={tdClass}><Button type="button" variant="ghost" size="icon-sm" aria-label={`Remove row ${i + 1}`} onClick={() => remove(i)}><Trash2 /></Button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </ScrollRegion>
      )}
      {typeof errs?.message === 'string' && <p role="alert" className="text-xs font-medium text-destructive">{errs.message}</p>}
      {!readOnly && (
        <Button type="button" variant="outline" size="sm" onClick={() => append({ code: '', personal: 'needs_review', role: 'needs_review', sole_trader: 'consent_required', free_provider: 'consent_required', unknown_entity: 'needs_review', allow_eligible: false })}><Plus /> Add country row</Button>
      )}
    </section>
  );
}
