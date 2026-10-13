'use client';
import * as React from 'react';
import { Controller, useFormContext, useWatch } from 'react-hook-form';
import { Info } from 'lucide-react';
import { Switch } from '@/components/ui/switch';
import { Input } from '@/components/ui/input';
import { FormField, Label } from '@/components/ui/label';
import { AdvancedPanel } from '@/components/ui/collapsible';
import { LoadingState } from '@/components/composites/loading-state';
import { DiscoveryPanel } from './discovery-panel';
import { Notice } from './estimate-panel';
import { NumField, RadioField, Section, SelectField, SwitchField, idOf, useFieldError } from './fields';
import { VERIFY_STAGES, type WizardValues } from './schema';
import { useRunEstimate, useVerificationEstimate } from './use-estimate';

export function Step4Validation() {
  const { control, register } = useFormContext<WizardValues>();
  const values = useWatch({ control }) as WizardValues;
  const mode = values.verification.mode;
  const vEst = useVerificationEstimate(values);
  const rEst = useRunEstimate(values);
  const scheduleErr = useFieldError('schedule_at');
  const stageOptions = (vEst.data?.cost.options ?? {}) as Record<string, unknown>;
  const eff = rEst.data?.valid ? (rEst.data.infra as Record<string, number>).effective_http_concurrency : undefined;
  const procs = rEst.data?.valid ? (rEst.data.infra as Record<string, number>) : undefined;

  return (
    <div className="space-y-5">
      <Section title="Validation depth" description="How thoroughly each found address is checked.">
        <RadioField name="verification.mode" legend="Mode" columns={3} options={[
          { value: 'fast', label: 'Fast', hint: 'Syntax, DNS/MX and flag checks. Cheap and quick.' },
          { value: 'deep', label: 'Deep', hint: 'Adds provider, catch-all, SMTP diagnostics and delivery history where this deployment allows it.' },
          { value: 'custom', label: 'Custom', hint: 'Choose the stages yourself.' },
        ]} />
        <fieldset className="space-y-1">
          <legend className="text-sm font-medium">Stages {mode !== 'custom' && <span className="font-normal text-muted-foreground">(set by the {mode} preset)</span>}</legend>
          <div className="divide-y rounded-md border px-3">
            {VERIFY_STAGES.map((s) => {
              const id = idOf(`verification.custom.${s.key}`);
              return (
                <div key={s.key} className="flex items-start justify-between gap-4 py-2">
                  <div className="min-w-0"><Label htmlFor={id}>{s.label}</Label><p className="text-xs text-muted-foreground">{s.hint}</p></div>
                  {mode === 'custom' ? (
                    <Controller control={control} name={`verification.custom.${s.key}` as const} render={({ field }) => <Switch id={id} checked={!!field.value} onCheckedChange={field.onChange} />} />
                  ) : (
                    <Switch id={id} checked={!!stageOptions[s.key]} disabled aria-label={`${s.label} (${mode} preset)`} />
                  )}
                </div>
              );
            })}
          </div>
        </fieldset>
        <div aria-live="polite" className="space-y-2">
          {vEst.isPending ? <LoadingState label="Estimating validation cost…" rows={2} /> : vEst.isError ? (
            <p role="alert" className="text-sm text-destructive">Validation cost could not be estimated: {vEst.error.message}</p>
          ) : (
            <>
              <h3 className="text-sm font-medium">Cost estimate</h3>
              <ul className="list-disc space-y-0.5 pl-5 text-xs text-muted-foreground">{(vEst.data.cost.resources as string[] | undefined)?.map((r, i) => <li key={i}>{r}</li>)}</ul>
              <p className="text-xs text-muted-foreground">{String(vEst.data.cost.total_note ?? '')}</p>
              <ul className="space-y-1.5">{vEst.data.warnings.map((w, i) => <Notice key={i} tone="warning">{w}</Notice>)}</ul>
              <details className="text-xs text-muted-foreground">
                <summary className="cursor-pointer text-foreground">What validation can and cannot tell you</summary>
                <ul className="ml-4 mt-1 list-disc space-y-0.5">{(vEst.data.cost.limitations as string[] | undefined)?.map((l, i) => <li key={i}>{l}</li>)}</ul>
              </details>
            </>
          )}
        </div>
      </Section>

      <Section title="Processing and speed">
        <RadioField name="performance.preset" legend="Worker preset" columns={3} options={[
          { value: 'conservative', label: 'Conservative', hint: 'Gentle on sites and on this machine. Default.' },
          { value: 'balanced', label: 'Balanced', hint: 'More parallel fetches; needs more workers and memory.' },
          { value: 'advanced', label: 'Advanced', hint: 'Highest parallelism. Use with care; you may need to set limits below.' },
        ]} />
        {eff !== undefined && procs && (
          <p className="text-xs text-muted-foreground" aria-live="polite">
            Uses up to {eff} concurrent fetches and about {procs.worker_processes_needed} worker process{procs.worker_processes_needed === 1 ? '' : 'es'}; {procs.worker_processes_online} online now.
          </p>
        )}
        <div role="note" className="flex items-start gap-2 rounded-md border border-info-border bg-info-soft p-3 text-xs text-info-foreground">
          <Info className="mt-0.5 size-4 shrink-0" aria-hidden />
          <p><strong>Desired is not measured.</strong> The rates below are targets you would like, not promises. Real throughput depends on workers, politeness limits and the sites crawled; the run screen shows the measured rate and what limits it.</p>
        </div>
        <div className="grid gap-4 sm:grid-cols-3">
          <NumField name="performance.desired_websites_per_hour" label="Desired websites / hour" nullable min={1} placeholder="No target" />
          <NumField name="performance.desired_businesses_per_hour" label="Desired businesses / hour" nullable min={1} placeholder="No target" />
          <NumField name="performance.desired_records_per_hour" label="Desired records / hour" nullable min={1} placeholder="No target" />
        </div>
        <div className="grid gap-4 sm:grid-cols-2">
          <NumField name="performance.retry_max_attempts" label="Retry attempts per job" min={1} max={10} hint="Default 4. A job that fails this many times is marked failed." />
          <NumField name="target.max_runtime_minutes" label="Maximum duration (minutes)" nullable min={1} placeholder="No limit" hint="The run stops gracefully after this long." />
        </div>
        <SwitchField name="performance.resume_existing" label="Resume existing work" hint="Reuse results already stored for the same businesses instead of repeating them." />
        <FormField label="Start at (optional)" htmlFor={idOf('schedule_at')} error={scheduleErr} hint="Local time. Leave empty to start as soon as you press Start. A worker must be running to begin a scheduled run.">
          <Input id={idOf('schedule_at')} type="datetime-local" className="max-w-xs" invalid={!!scheduleErr} {...register('schedule_at')} />
        </FormField>

        <AdvancedPanel title="Advanced settings" description="Rarely needed; defaults are shown">
          <div className="grid gap-4 sm:grid-cols-2">
            <SelectField name="performance.batch_size" label="Batch size" hint="Default 250." options={[250, 500, 1000, 2500, 5000, 10000].map((n) => ({ value: String(n), label: String(n) }))}
              numeric />
            <NumField name="performance.http_concurrency" label="HTTP concurrency" nullable min={1} max={2000} placeholder="From preset" hint="Default: from the worker preset." />
            <NumField name="performance.per_host_concurrency" label="Concurrent requests per host" min={1} max={16} hint="Default 1 (polite)." />
            <NumField name="performance.max_requests_per_host_per_min" label="Max requests per host per minute" min={1} hint="Default 30." />
            <NumField name="performance.dns_concurrency" label="DNS concurrency" nullable min={1} max={2000} placeholder="Default" />
            <NumField name="performance.worker_limit" label="Worker process limit" nullable min={1} max={512} placeholder="No limit" />
            <NumField name="performance.browser_workers" label="Browser workers" min={0} max={8} hint="Default 0. JavaScript rendering needs Playwright installed." />
            <NumField name="performance.max_ram_mb" label="Memory ceiling (MB)" nullable min={128} placeholder="No ceiling" />
            <NumField name="performance.max_daily_operations" label="Max operations per day" nullable min={1} placeholder="No limit" />
            <NumField name="target.max_work_jobs" label="Max jobs for this run" nullable min={1} placeholder="No limit" hint="Stops the run once this many jobs have finished or failed." />
          </div>
        </AdvancedPanel>
      </Section>

      <DiscoveryPanel />
    </div>
  );
}
