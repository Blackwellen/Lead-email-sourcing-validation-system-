'use client';
import * as React from 'react';
import { useFormContext, useWatch } from 'react-hook-form';
import { Section, NumField, RadioField, SelectField, SwitchField } from './fields';
import { EstimateSummary } from './estimate-panel';
import { TARGET_DEFINITIONS, UNIQUENESS_MODES, type WizardValues } from './schema';
import { useRunEstimate } from './use-estimate';

export function Step3Contacts() {
  const { control, setValue } = useFormContext<WizardValues>();
  const values = useWatch({ control }) as WizardValues;
  const t = values.target;
  const est = useRunEstimate(values, true);
  const def = TARGET_DEFINITIONS.find((d) => d.value === t.definition);

  return (
    <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_22rem]">
      <div className="space-y-5">
        <Section title="Target" description="The run stops when this many results qualify (or another stop condition is hit).">
          <SelectField name="target.definition" label="What counts toward the target" hint={def?.hint}
            options={TARGET_DEFINITIONS.map((d) => ({ value: d.value, label: d.label }))} />
          <NumField name="target.count" label="Target number" min={1} max={50_000_000} className="max-w-xs" />
          <RadioField name="target.uniqueness_mode" legend="Uniqueness" options={UNIQUENESS_MODES.map((u) => ({ value: u.value, label: u.label, hint: u.hint }))} columns={2} />
          {t.uniqueness_mode === 'multi' && <NumField name="target.max_contacts_per_company" label="Maximum contacts per company" min={1} max={1000} className="max-w-xs" />}
        </Section>

        <Section title="Which contacts qualify">
          <RadioField name="target.contact_type" legend="Contact type" columns={3} options={[
            { value: 'named', label: 'Named people', hint: 'Addresses tied to a person’s name.' },
            { value: 'role', label: 'Role mailboxes', hint: 'info@, sales@, office@ and similar.' },
            { value: 'both', label: 'Both', hint: 'No restriction on contact type.' },
          ]} />
          <div className="divide-y rounded-md border px-3">
            <SwitchField name="target.published_only" label="Published addresses only" hint="Only addresses found on the business’s own pages (or corroborated by a second source)."
              onChange={(v) => { if (v) setValue('target.allow_inferred', false, { shouldDirty: true }); }} />
            <SwitchField name="target.allow_inferred" label="Allow inferred research candidates" disabled={t.published_only}
              hint={t.published_only ? 'Turn off “Published addresses only” to allow pattern-inferred or role-speculative candidates. Inferred addresses are research leads, not published contacts.' : 'Pattern-inferred or role-speculative addresses may count. They are never presented as published.'} />
          </div>
          <NumField name="target.min_confidence" label="Minimum confidence" hint="0–100. Addresses below this score do not count. Default 40." min={0} max={100} className="max-w-xs" />
          <div className="divide-y rounded-md border px-3">
            <SwitchField name="target.exclude_free_providers" label="Exclude free providers" hint="gmail.com, outlook.com and similar." />
            <SwitchField name="target.exclude_catchall_or_inconclusive" label="Exclude catch-all or inconclusive domains" hint="Stricter, but reduces yield: many small businesses sit on catch-all hosts." />
            <SwitchField name="target.exclude_previous_bounces" label="Exclude addresses that bounced before" />
            <SwitchField name="target.exclude_suppressed" label="Exclude suppressed and opted-out addresses" />
          </div>
        </Section>
      </div>

      <aside aria-label="Expected trade-offs" className="space-y-3 lg:sticky lg:top-4 lg:self-start">
        <Section title="Expected trade-offs" className="space-y-3">
          <ul className="list-disc space-y-1 pl-5 text-xs text-muted-foreground">
            <li>Stricter rules (published only, higher confidence, excluding catch-all or free providers) give a cleaner list but need more businesses to reach the same target.</li>
            <li>One email per business or per domain caps how many addresses each business can contribute.</li>
            <li>The target is a stop condition, not a promise: if sources run out the run finishes short and says so.</li>
          </ul>
          <h3 className="text-sm font-medium">From the estimate</h3>
          <EstimateSummary q={est} />
        </Section>
      </aside>
    </div>
  );
}
