'use client';
import * as React from 'react';
import { Controller, type Control, type FieldValues, type Path } from 'react-hook-form';
import { useQuery } from '@tanstack/react-query';
import { z } from 'zod';
import { api, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { FormField, Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { SimpleSelect } from '@/components/ui/select';
import { FieldGrid, num, SectionForm } from './section-form';
import { RetentionRunner } from './retention-runner';

type S = Schema<'WorkerSettings'>;
const nn = (label: string) => `${label} must be a number`;
const int = (label: string, min: number, max?: number) => { const b = z.number(nn(label)).int(`${label} must be a whole number`).min(min, `${label} must be at least ${min}`); return max === undefined ? b : b.max(max, `${label} must be at most ${max}`); };

export function ErrText({ id, msg }: { id: string; msg?: string }) {
  return msg ? <p id={`${id}-error`} role="alert" className="text-xs font-medium text-destructive">{msg}</p> : null;
}

export function SwitchRow<V extends FieldValues>({ control, name, label, hint }: { control: Control<V>; name: Path<V>; label: string; hint?: string }) {
  const id = `sw-${name}`;
  return (
    <Controller control={control} name={name} render={({ field }) => (
      <div className="flex items-start justify-between gap-4 rounded-md border p-3">
        <div className="space-y-0.5"><Label htmlFor={id}>{label}</Label>{hint && <p id={`${id}-hint`} className="text-xs text-muted-foreground">{hint}</p>}</div>
        <Switch id={id} checked={!!field.value} onCheckedChange={field.onChange} aria-describedby={hint ? `${id}-hint` : undefined} />
      </div>
    )} />
  );
}

/* ------------------------------------------------------------------ worker */
const workerSchema = z.object({
  concurrency: int('Concurrency', 1, 512), poll_interval_s: z.number(nn('Poll interval')).min(0.05, 'At least 0.05 seconds').max(60, 'At most 60 seconds'),
  lease_seconds: int('Lease', 10, 3600), max_job_attempts: int('Max attempts', 1, 20), max_rss_mb: z.number(nn('Memory limit')).int().min(128, 'At least 128 MB').nullable(),
});
export function WorkerSection() {
  return (
    <SectionForm<z.infer<typeof workerSchema>, S> name="worker" title="Worker settings" schema={workerSchema}
      description="How each worker process picks up and retries jobs. Changes apply when workers next reload settings (restart workers to be sure)."
      toForm={(a) => ({ concurrency: a.concurrency ?? 8, poll_interval_s: a.poll_interval_s ?? 1, lease_seconds: a.lease_seconds ?? 120, max_job_attempts: a.max_job_attempts ?? 4, max_rss_mb: a.max_rss_mb ?? null })}
      toApi={(v) => v}>
      {({ register, formState: { errors } }) => (
        <FieldGrid>
          <FormField label="Concurrent jobs per worker" htmlFor="w-conc" error={errors.concurrency?.message} hint="1–512"><Input id="w-conc" inputMode="numeric" {...num(register, 'concurrency')} invalid={!!errors.concurrency} /></FormField>
          <FormField label="Poll interval (seconds)" htmlFor="w-poll" error={errors.poll_interval_s?.message} hint="How often an idle worker checks for jobs"><Input id="w-poll" inputMode="decimal" {...num(register, 'poll_interval_s')} invalid={!!errors.poll_interval_s} /></FormField>
          <FormField label="Job lease (seconds)" htmlFor="w-lease" error={errors.lease_seconds?.message} hint="A job not finished or renewed in this time is handed to another worker"><Input id="w-lease" inputMode="numeric" {...num(register, 'lease_seconds')} invalid={!!errors.lease_seconds} /></FormField>
          <FormField label="Maximum attempts per job" htmlFor="w-att" error={errors.max_job_attempts?.message} hint="After this many failures a job is marked dead"><Input id="w-att" inputMode="numeric" {...num(register, 'max_job_attempts')} invalid={!!errors.max_job_attempts} /></FormField>
          <FormField label="Restart worker above (MB RSS)" htmlFor="w-rss" error={errors.max_rss_mb?.message} hint="Leave empty for no limit"><Input id="w-rss" inputMode="numeric" {...num(register, 'max_rss_mb', true)} invalid={!!errors.max_rss_mb} /></FormField>
        </FieldGrid>
      )}
    </SectionForm>
  );
}

/* ------------------------------------------------------------------ source policies */
const domainRe = /^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$/i;
const policySchema = z.object({
  respect_robots_txt: z.boolean(), default_crawl_delay_s: z.number(nn('Crawl delay')).min(0, 'At least 0').max(60, 'At most 60'),
  max_requests_per_host_per_min: int('Requests per host', 1, 6000),
  blocked_domains: z.string().refine((t) => t.split('\n').map((l) => l.trim()).filter(Boolean).every((d) => domainRe.test(d)), 'One domain per line, for example example.com')
    .refine((t) => t.split('\n').filter((l) => l.trim()).length <= 5000, 'At most 5000 domains'),
  contact_email: z.string().max(320, 'Too long').refine((v) => v === '' || /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(v), 'Enter a valid email address'),
});
export function SourcePolicySection() {
  return (
    <SectionForm<z.infer<typeof policySchema>, Schema<'SourcePolicySettings'>> name="source_policies" title="Source policies" schema={policySchema}
      description="Politeness rules for every outbound request: robots.txt, crawl delay, per-host rate and blocked domains."
      toForm={(a) => ({ respect_robots_txt: a.respect_robots_txt ?? true, default_crawl_delay_s: a.default_crawl_delay_s ?? 1, max_requests_per_host_per_min: a.max_requests_per_host_per_min ?? 30, blocked_domains: (a.blocked_domains ?? []).join('\n'), contact_email: a.contact_email ?? '' })}
      toApi={(v) => ({ ...v, blocked_domains: v.blocked_domains.split('\n').map((d) => d.trim().toLowerCase()).filter(Boolean) })}>
      {({ register, control, formState: { errors } }) => (
        <>
          <SwitchRow control={control} name="respect_robots_txt" label="Respect robots.txt" hint="Strongly recommended. Disabling it may breach site terms." />
          <FieldGrid>
            <FormField label="Default crawl delay (seconds)" htmlFor="p-delay" error={errors.default_crawl_delay_s?.message} hint="Minimum gap between requests to one host"><Input id="p-delay" inputMode="decimal" {...num(register, 'default_crawl_delay_s')} invalid={!!errors.default_crawl_delay_s} /></FormField>
            <FormField label="Max requests per host per minute" htmlFor="p-rate" error={errors.max_requests_per_host_per_min?.message}><Input id="p-rate" inputMode="numeric" {...num(register, 'max_requests_per_host_per_min')} invalid={!!errors.max_requests_per_host_per_min} /></FormField>
            <FormField label="Contact email for site owners" htmlFor="p-contact" error={errors.contact_email?.message} hint="Shown in the crawler’s User-Agent so site owners can reach you"><Input id="p-contact" type="email" {...register('contact_email')} invalid={!!errors.contact_email} /></FormField>
          </FieldGrid>
          <FormField label="Blocked domains" htmlFor="p-blocked" error={errors.blocked_domains?.message} hint="Never fetched. One domain per line; they are lower-cased and de-duplicated on save.">
            <Textarea id="p-blocked" rows={5} spellCheck={false} className="font-mono text-xs" {...register('blocked_domains')} aria-invalid={!!errors.blocked_domains} />
          </FormField>
        </>
      )}
    </SectionForm>
  );
}

/* ------------------------------------------------------------------ concurrency */
const concSchema = z.object({ http: int('HTTP', 1, 2000), dns: int('DNS', 1, 2000), per_host: int('Per host', 1, 16), browser_workers: int('Browser workers', 0, 8), batch_size: int('Batch size', 10, 10000) });
export function ConcurrencySection() {
  const f = (id: string, label: string, name: keyof z.infer<typeof concSchema>, hint: string, errors: Record<string, { message?: string } | undefined>, register: Parameters<typeof num<z.infer<typeof concSchema>>>[0]) => (
    <FormField label={label} htmlFor={id} error={errors[name]?.message} hint={hint}><Input id={id} inputMode="numeric" {...num(register, name)} invalid={!!errors[name]} /></FormField>
  );
  return (
    <SectionForm<z.infer<typeof concSchema>, Schema<'ConcurrencySettings'>> name="concurrency" title="Concurrency" schema={concSchema}
      description="Limits on parallel work. Higher values are faster but use more memory and network, and increase the chance of being rate-limited."
      toForm={(a) => ({ http: a.http ?? 16, dns: a.dns ?? 64, per_host: a.per_host ?? 1, browser_workers: a.browser_workers ?? 0, batch_size: a.batch_size ?? 250 })} toApi={(v) => v}>
      {({ register, formState: { errors } }) => (
        <FieldGrid>
          {f('c-http', 'Parallel HTTP requests', 'http', '1–2000', errors, register)}
          {f('c-dns', 'Parallel DNS lookups', 'dns', '1–2000', errors, register)}
          {f('c-host', 'Parallel requests per host', 'per_host', '1–16. Keep at 1 to be polite.', errors, register)}
          {f('c-browser', 'Browser workers', 'browser_workers', '0–8. Needs Playwright installed (see System).', errors, register)}
          {f('c-batch', 'Batch size', 'batch_size', '10–10,000 records per batch', errors, register)}
        </FieldGrid>
      )}
    </SectionForm>
  );
}

/* ------------------------------------------------------------------ retention */
const nullableInt = (label: string, min: number) => z.number(nn(label)).int(`${label} must be a whole number`).min(min, `At least ${min} days (or leave empty to keep forever)`).nullable();
const retSchema = z.object({ evidence_days: nullableInt('Evidence', 30), validation_history_days: nullableInt('Validation history', 7), exports_days: nullableInt('Export files', 1), audit_days: nullableInt('Audit log', 90) });
export function RetentionSection() {
  return (
    <SectionForm<z.infer<typeof retSchema>, Schema<'RetentionSettings'>> name="retention" title="Data retention" schema={retSchema}
      description="How long data is kept before the retention job deletes it. Leave a field empty to keep that data forever."
      toForm={(a) => ({ evidence_days: a.evidence_days ?? null, validation_history_days: a.validation_history_days ?? null, exports_days: a.exports_days ?? null, audit_days: a.audit_days ?? null })} toApi={(v) => v}
      footer={<RetentionRunner />}>
      {({ register, formState: { errors } }) => (
        <FieldGrid>
          <FormField label="Contact evidence (days)" htmlFor="r-ev" error={errors.evidence_days?.message} hint="Minimum 30. Empty = keep. Deletes evidence older than this."><Input id="r-ev" inputMode="numeric" {...num(register, 'evidence_days', true)} invalid={!!errors.evidence_days} /></FormField>
          <FormField label="Validation history (days)" htmlFor="r-vh" error={errors.validation_history_days?.message} hint="Minimum 7. The latest result per check is always kept."><Input id="r-vh" inputMode="numeric" {...num(register, 'validation_history_days', true)} invalid={!!errors.validation_history_days} /></FormField>
          <FormField label="Export files (days)" htmlFor="r-ex" error={errors.exports_days?.message} hint="Minimum 1. Files older than this are removed."><Input id="r-ex" inputMode="numeric" {...num(register, 'exports_days', true)} invalid={!!errors.exports_days} /></FormField>
          <FormField label="Audit log (days)" htmlFor="r-au" error={errors.audit_days?.message} hint="Minimum 90. Empty = keep forever (recommended for accountability)."><Input id="r-au" inputMode="numeric" {...num(register, 'audit_days', true)} invalid={!!errors.audit_days} /></FormField>
        </FieldGrid>
      )}
    </SectionForm>
  );
}

/* ------------------------------------------------------------------ exports */
const expSchema = z.object({ default_format: z.enum(['csv', 'xlsx', 'jsonl']), default_preset: z.string().min(1, 'Choose a preset').max(60), max_rows: int('Max rows', 1000), inline_threshold: int('Inline threshold', 0, 50000) });
export function ExportDefaultsSection() {
  const presets = useQuery({ queryKey: qk.custom('exports', 'presets'), queryFn: ({ signal }) => api.get<Schema<'PresetList'>>('/api/exports/presets', { signal }), staleTime: 10 * 60_000 });
  return (
    <SectionForm<z.infer<typeof expSchema>, Schema<'ExportSettings'>> name="exports" title="Export defaults" schema={expSchema}
      description="Defaults used by the export builder and limits for generated files."
      toForm={(a) => ({ default_format: (a.default_format ?? 'csv') as 'csv', default_preset: a.default_preset ?? 'published_business_emails', max_rows: a.max_rows ?? 5_000_000, inline_threshold: a.inline_threshold ?? 5000 })} toApi={(v) => v}>
      {({ register, control, formState: { errors } }) => (
        <FieldGrid>
          <FormField label="Default format" htmlFor="e-fmt">
            <Controller control={control} name="default_format" render={({ field }) => <SimpleSelect id="e-fmt" aria-label="Default format" value={field.value} onValueChange={field.onChange} options={[{ value: 'csv', label: 'CSV' }, { value: 'xlsx', label: 'Excel (XLSX)' }, { value: 'jsonl', label: 'JSON Lines' }]} />} />
          </FormField>
          <FormField label="Default preset" htmlFor="e-preset" error={errors.default_preset?.message}>
            <Controller control={control} name="default_preset" render={({ field }) => <SimpleSelect id="e-preset" aria-label="Default preset" value={field.value} onValueChange={field.onChange} options={(presets.data?.presets ?? [{ id: field.value, label: field.value }]).map((p) => ({ value: p.id, label: p.label }))} />} />
          </FormField>
          <FormField label="Maximum rows per export" htmlFor="e-max" error={errors.max_rows?.message} hint="At least 1000. Larger result sets are truncated and flagged."><Input id="e-max" inputMode="numeric" {...num(register, 'max_rows')} invalid={!!errors.max_rows} /></FormField>
          <FormField label="Inline threshold (rows)" htmlFor="e-inline" error={errors.inline_threshold?.message} hint="Exports estimated below this are generated immediately; larger ones run in the background. 0–50,000."><Input id="e-inline" inputMode="numeric" {...num(register, 'inline_threshold')} invalid={!!errors.inline_threshold} /></FormField>
        </FieldGrid>
      )}
    </SectionForm>
  );
}

/* ------------------------------------------------------------------ notifications */
const emailRe = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const notifSchema = z.object({
  email_recipients: z.string().refine((t) => t.split('\n').map((l) => l.trim()).filter(Boolean).every((e) => emailRe.test(e)), 'One valid email address per line')
    .refine((t) => t.split('\n').filter((l) => l.trim()).length <= 50, 'At most 50 recipients'),
  notify_on_run_complete: z.boolean(), notify_on_run_failure: z.boolean(),
  webhook_url: z.string().max(500, 'At most 500 characters').refine((v) => v === '' || /^https:\/\//i.test(v), 'The webhook must use https://'),
});
export function NotificationsSection() {
  return (
    <SectionForm<z.infer<typeof notifSchema>, Schema<'NotificationSettings'>> name="notifications" title="Notifications" schema={notifSchema}
      description="Who should be told when runs finish or fail."
      toForm={(a) => ({ email_recipients: (a.email_recipients ?? []).join('\n'), notify_on_run_complete: a.notify_on_run_complete ?? false, notify_on_run_failure: a.notify_on_run_failure ?? true, webhook_url: a.webhook_url ?? '' })}
      toApi={(v) => ({ email_recipients: v.email_recipients.split('\n').map((e) => e.trim()).filter(Boolean), notify_on_run_complete: v.notify_on_run_complete, notify_on_run_failure: v.notify_on_run_failure, webhook_url: v.webhook_url.trim() || null })}>
      {({ register, control, formState: { errors } }) => (
        <>
          <p role="note" className="rounded-md border border-warning-border bg-warning-soft p-3 text-sm text-warning-foreground">These preferences are saved, but this version of the server does not deliver e-mail or webhook notifications yet. Watch the Runs page and the dashboard for run outcomes.</p>
          <SwitchRow control={control} name="notify_on_run_failure" label="Notify when a run fails" />
          <SwitchRow control={control} name="notify_on_run_complete" label="Notify when a run completes" />
          <FormField label="E-mail recipients" htmlFor="n-mail" error={errors.email_recipients?.message} hint="One address per line, up to 50.">
            <Textarea id="n-mail" rows={3} spellCheck={false} {...register('email_recipients')} aria-invalid={!!errors.email_recipients} />
          </FormField>
          <FormField label="Webhook URL" htmlFor="n-hook" error={errors.webhook_url?.message} hint="HTTPS only. Leave empty for none."><Input id="n-hook" type="url" {...register('webhook_url')} invalid={!!errors.webhook_url} /></FormField>
        </>
      )}
    </SectionForm>
  );
}

/* ------------------------------------------------------------------ backups */
const backupSchema = z.object({
  enabled: z.boolean(), schedule_cron: z.string().max(100, 'At most 100 characters'), destination_note: z.string().max(500, 'At most 500 characters'),
  retention_days: int('Retention', 1, 3650), last_verified_restore_at: z.string(),
});
export function BackupSection() {
  return (
    <SectionForm<z.infer<typeof backupSchema>, Schema<'BackupSettings'>> name="backups" title="Backup configuration" schema={backupSchema}
      description="Where your backup policy is documented. These fields are a record for operators; they do not schedule or run anything."
      toForm={(a) => ({ enabled: a.enabled ?? false, schedule_cron: a.schedule_cron ?? '', destination_note: a.destination_note ?? '', retention_days: a.retention_days ?? 14, last_verified_restore_at: a.last_verified_restore_at ? String(a.last_verified_restore_at).slice(0, 10) : '' })}
      toApi={(v) => ({ ...v, last_verified_restore_at: v.last_verified_restore_at ? `${v.last_verified_restore_at}T00:00:00Z` : null })}>
      {({ register, control, formState: { errors } }) => (
        <>
          <div role="note" className="rounded-md border border-info-border bg-info-soft p-3 text-sm text-info-foreground space-y-1">
            <p><strong>How backups actually run.</strong> The deployment’s <code>backup</code> container takes a compressed <code>pg_dump</code> once a day into the <code>backups</code> volume and deletes dumps older than <code>BACKUP_KEEP_DAYS</code> (default 14), configured in the environment file, not here.</p>
            <p>A backup you have never restored is not a backup: restore one to a scratch database periodically and record the date below. Steps are in the operations guide.</p>
          </div>
          <SwitchRow control={control} name="enabled" label="Backups are configured" hint="A label for your records." />
          <FieldGrid>
            <FormField label="Schedule (cron, informational)" htmlFor="b-cron" error={errors.schedule_cron?.message}><Input id="b-cron" placeholder="0 3 * * *" {...register('schedule_cron')} invalid={!!errors.schedule_cron} /></FormField>
            <FormField label="Retention (days)" htmlFor="b-ret" error={errors.retention_days?.message} hint="1–3650"><Input id="b-ret" inputMode="numeric" {...num(register, 'retention_days')} invalid={!!errors.retention_days} /></FormField>
            <FormField label="Last verified restore" htmlFor="b-ver" hint="Date you last restored a backup successfully"><Input id="b-ver" type="date" {...register('last_verified_restore_at')} /></FormField>
          </FieldGrid>
          <FormField label="Destination notes" htmlFor="b-dest" error={errors.destination_note?.message} hint="For example: off-site copy to object storage, who holds the keys.">
            <Textarea id="b-dest" rows={3} {...register('destination_note')} aria-invalid={!!errors.destination_note} />
          </FormField>
        </>
      )}
    </SectionForm>
  );
}
