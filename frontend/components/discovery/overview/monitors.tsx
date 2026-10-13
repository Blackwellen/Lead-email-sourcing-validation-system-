'use client';
import * as React from 'react';
import { BellRing, Play, Plus, Trash2 } from 'lucide-react';
import { api, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { listFetcher, usePagedQuery } from '@/hooks/use-paged-query';
import { useApiMutation } from '@/hooks/use-api-mutation';
import { Can, useMe } from '@/lib/auth';
import { formatDateTime, titleCase } from '@/lib/format';
import { EmptyState } from '@/components/composites/empty-state';
import { ErrorState } from '@/components/composites/error-state';
import { LoadingState } from '@/components/composites/loading-state';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { FormField } from '@/components/ui/label';
import { SimpleSelect } from '@/components/ui/select';
import { Switch } from '@/components/ui/switch';
import { MultiSelect } from '@/components/ui/combobox';
import { ConfirmDialog } from '@/components/ui/alert-dialog';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog';
import { SIGNAL_TYPES, SectionTitle } from '../shared';

type Monitor = Schema<'MonitorOut'>;
type MonitorIn = Schema<'MonitorIn'>;
const KEY = qk.all('monitors');
const fetchPage = listFetcher<Monitor>('/api/discovery/monitors');

export const MONITOR_KINDS: Record<string, { label: string; help: string }> = {
  new_registrations: { label: 'New registrations', help: 'New companies in the official register (needs the register file or URL configured on the source).' },
  website_changes: { label: 'Website changes', help: 'Re-crawls known websites older than N days and records changes.' },
  signals: { label: 'Signals', help: 'Collects fresh business signals (news, hiring, awards…) for known businesses.' },
};
const FREQS = [{ value: 'daily', label: 'Daily' }, { value: 'weekly', label: 'Weekly' }, { value: 'monthly', label: 'Monthly' }];

/** Integer settings per monitor kind, with the bounds the API enforces. */
const INT_FIELDS: Record<string, { key: string; label: string; min: number; max: number; hint: string }[]> = {
  website_changes: [
    { key: 'older_than_days', label: 'Re-check sites older than (days)', min: 1, max: 365, hint: '1–365' },
    { key: 'limit', label: 'Businesses per run', min: 1, max: 200, hint: '1–200' },
    { key: 'max_pages', label: 'Pages per site', min: 1, max: 20, hint: '1–20' },
  ],
  signals: [
    { key: 'limit', label: 'Businesses per run', min: 1, max: 200, hint: '1–200' },
    { key: 'freshness_days', label: 'Only signals newer than (days)', min: 1, max: 3650, hint: '1–3650' },
  ],
  new_registrations: [
    { key: 'days', label: 'Registered within (days)', min: 1, max: 400, hint: '1–400' },
    { key: 'limit', label: 'Maximum new businesses', min: 1, max: 100000, hint: '1–100,000' },
  ],
};

export function MonitorsSection() {
  const me = useMe();
  const q = usePagedQuery({ queryKey: qk.list('monitors'), fetchPage, limit: 50 });
  const patch = useApiMutation({
    mutationFn: ({ id, body }: { id: number; body: Schema<'MonitorPatch'> }) => api.patch<Monitor>(`/api/discovery/monitors/${id}`, body),
    successMessage: (m, v) => (v.body.enabled === true ? `Monitor “${m.name}” enabled` : v.body.enabled === false ? `Monitor “${m.name}” disabled` : 'Monitor updated'),
    invalidate: [KEY],
  });
  const runNow = useApiMutation({
    mutationFn: (id: number) => api.post<Schema<'RunNowOut'>>(`/api/discovery/monitors/${id}/run-now`),
    successMessage: (r) => (r.queued ? `Queued (job #${r.job_id})` : 'Already queued – the worker will pick it up'),
    invalidate: [KEY],
  });
  const del = useApiMutation({
    mutationFn: (id: number) => api.delete(`/api/discovery/monitors/${id}`),
    successMessage: 'Monitor deleted',
    invalidate: [KEY],
  });
  const canEdit = me.can('operator');
  return (
    <section aria-labelledby="mon-h" className="space-y-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <SectionTitle id="mon-h" description="Recurring checks the worker runs on a schedule. Running one now only queues it; results appear after the worker processes the job.">Monitors</SectionTitle>
        <Can role="operator"><CreateMonitorDialog /></Can>
      </div>
      {q.isPending ? <LoadingState rows={4} label="Loading monitors" /> : q.isError ? <ErrorState error={q.error} onRetry={() => q.refetch()} /> : q.items.length === 0 ? (
        <EmptyState compact icon={BellRing} title="No monitors yet" description={canEdit ? 'Create one to watch for new registrations, website changes or signals.' : 'Operators can create monitors.'} />
      ) : (
        <ul className="grid gap-3 lg:grid-cols-2" aria-label="Monitors" data-testid="monitors-list">
          {q.items.map((m) => {
            const result = Object.entries(m.last_result ?? {}).filter(([, v]) => typeof v !== 'object').slice(0, 4);
            return (
              <li key={m.id} className="space-y-3 rounded-lg border bg-card p-4 shadow-card" data-testid={`monitor-${m.id}`}>
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <h3 className="break-words text-sm font-semibold">{m.name}</h3>
                    <p className="text-xs text-muted-foreground">{MONITOR_KINDS[m.kind]?.label ?? titleCase(m.kind)} · #{m.id}</p>
                  </div>
                  {m.enabled ? <Badge variant="success">Enabled</Badge> : <Badge variant="outline">Disabled</Badge>}
                </div>
                <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
                  <div><dt className="text-muted-foreground">Last run</dt><dd>{m.last_run_at ? formatDateTime(m.last_run_at) : 'Never'}</dd></div>
                  <div><dt className="text-muted-foreground">Next run</dt><dd>{m.next_run_at ? formatDateTime(m.next_run_at) : m.enabled ? 'On the next scheduler tick' : '—'}</dd></div>
                  {Object.keys(m.config ?? {}).length > 0 && <div className="col-span-2"><dt className="text-muted-foreground">Settings</dt><dd className="break-words">{Object.entries(m.config).map(([k, v]) => `${titleCase(k)}: ${Array.isArray(v) ? v.join(', ') : String(v)}`).join(' · ')}</dd></div>}
                  {result.length > 0 && <div className="col-span-2"><dt className="text-muted-foreground">Last result</dt><dd className="break-words">{result.map(([k, v]) => `${titleCase(k)}: ${String(v)}`).join(' · ')}</dd></div>}
                </dl>
                {canEdit ? (
                  <div className="flex flex-wrap items-center gap-2 border-t pt-3">
                    <label className="flex items-center gap-2 text-xs">
                      <Switch checked={m.enabled} disabled={patch.isPending && patch.variables?.id === m.id} onCheckedChange={(v) => patch.mutate({ id: m.id, body: { enabled: v } })} aria-label={`Enable monitor ${m.name}`} />
                      Enabled
                    </label>
                    <SimpleSelect aria-label={`Frequency of ${m.name}`} className="h-8 w-28 text-xs" value={m.frequency} options={FREQS}
                      onValueChange={(v) => { if (v !== m.frequency) patch.mutate({ id: m.id, body: { frequency: v as 'daily' | 'weekly' | 'monthly' } }); }} />
                    <Button size="sm" variant="outline" onClick={() => runNow.mutate(m.id)} loading={runNow.isPending && runNow.variables === m.id} disabled={!m.enabled}
                      title={m.enabled ? undefined : 'Enable the monitor first'}><Play /> Run now</Button>
                    <Can role="admin">
                      <ConfirmDialog title={`Delete monitor “${m.name}”?`} description="The schedule is removed. Data it already collected is kept. This is recorded in the audit log." confirmLabel="Delete" destructive
                        trigger={<Button size="sm" variant="ghost" className="ml-auto text-destructive" aria-label={`Delete monitor ${m.name}`}><Trash2 /> Delete</Button>} onConfirm={() => del.mutateAsync(m.id)} />
                    </Can>
                  </div>
                ) : <p className="border-t pt-3 text-xs text-muted-foreground">Frequency: {titleCase(m.frequency)}</p>}
              </li>
            );
          })}
        </ul>
      )}
      {q.hasNextPage && <Button variant="outline" onClick={() => q.fetchNextPage()} loading={q.isFetchingNextPage}>Load more</Button>}
    </section>
  );
}

function CreateMonitorDialog() {
  const [open, setOpen] = React.useState(false);
  const [kind, setKind] = React.useState<MonitorIn['kind']>('signals');
  const [name, setName] = React.useState('');
  const [frequency, setFrequency] = React.useState<'daily' | 'weekly' | 'monthly'>('weekly');
  const [enabled, setEnabled] = React.useState(true);
  const [ints, setInts] = React.useState<Record<string, string>>({});
  const [types, setTypes] = React.useState<string[]>([]);
  const [keywords, setKeywords] = React.useState('');
  const [errors, setErrors] = React.useState<Record<string, string>>({});
  const reset = () => { setKind('signals'); setName(''); setFrequency('weekly'); setEnabled(true); setInts({}); setTypes([]); setKeywords(''); setErrors({}); };
  const create = useApiMutation({
    mutationFn: (body: MonitorIn) => api.post<Monitor>('/api/discovery/monitors', body),
    successMessage: (m) => `Monitor “${m.name}” created`,
    errorMessage: false,
    onError: (e) => setErrors({ form: e.message, ...e.fields }),
    onSuccess: () => { setOpen(false); reset(); },
    invalidate: [KEY],
  });
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const errs: Record<string, string> = {};
    if (!name.trim()) errs.name = 'Give the monitor a name.';
    const config: Record<string, unknown> = {};
    for (const f of INT_FIELDS[kind]) {
      const raw = ints[f.key]?.trim();
      if (!raw) continue;
      const n = Number(raw);
      if (!Number.isInteger(n) || n < f.min || n > f.max) errs[f.key] = `Enter a whole number between ${f.min} and ${f.max}.`;
      else config[f.key] = n;
    }
    if (kind === 'signals' && types.length) config.types = types;
    if (kind === 'new_registrations' && keywords.trim()) config.keywords = keywords.split(',').map((k) => k.trim()).filter(Boolean).slice(0, 100);
    setErrors(errs);
    if (Object.keys(errs).length) return;
    create.mutate({ kind, name: name.trim(), frequency, enabled, config });
  };
  return (
    <Dialog open={open} onOpenChange={(o) => { setOpen(o); if (!o) reset(); }}>
      <DialogTrigger asChild><Button data-testid="new-monitor"><Plus /> New monitor</Button></DialogTrigger>
      <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>New monitor</DialogTitle>
          <DialogDescription>{MONITOR_KINDS[kind].help}</DialogDescription>
        </DialogHeader>
        <form className="space-y-3" onSubmit={submit} noValidate>
          <FormField label="What to watch" htmlFor="mon-kind">
            <SimpleSelect id="mon-kind" aria-label="What to watch" value={kind} onValueChange={(v) => { setKind(v as MonitorIn['kind']); setInts({}); }}
              options={Object.entries(MONITOR_KINDS).map(([value, k]) => ({ value, label: k.label }))} />
          </FormField>
          <FormField label="Name" htmlFor="mon-name" required error={errors.name}>
            <Input id="mon-name" value={name} maxLength={200} onChange={(e) => setName(e.target.value)} invalid={!!errors.name} aria-describedby={errors.name ? 'mon-name-error' : undefined} />
          </FormField>
          <FormField label="Frequency" htmlFor="mon-freq">
            <SimpleSelect id="mon-freq" aria-label="Frequency" value={frequency} onValueChange={(v) => setFrequency(v as typeof frequency)} options={FREQS} />
          </FormField>
          {INT_FIELDS[kind].map((f) => (
            <FormField key={`${kind}-${f.key}`} label={f.label} htmlFor={`mon-${f.key}`} hint={`Optional, ${f.hint}`} error={errors[f.key] ?? errors[`config.${f.key}`]}>
              <Input id={`mon-${f.key}`} inputMode="numeric" value={ints[f.key] ?? ''} onChange={(e) => setInts((s) => ({ ...s, [f.key]: e.target.value }))} invalid={!!errors[f.key]}
                aria-describedby={errors[f.key] ? `mon-${f.key}-error` : `mon-${f.key}-hint`} />
            </FormField>
          ))}
          {kind === 'signals' && (
            <FormField label="Signal types" htmlFor="mon-types" hint="Optional; all types when empty.">
              <MultiSelect id="mon-types" aria-label="Signal types" value={types} onChange={setTypes} options={SIGNAL_TYPES.map((t) => ({ value: t, label: titleCase(t) }))} />
            </FormField>
          )}
          {kind === 'new_registrations' && (
            <FormField label="Keywords" htmlFor="mon-kw" hint="Optional, comma separated (matched against company names).">
              <Input id="mon-kw" value={keywords} onChange={(e) => setKeywords(e.target.value)} />
            </FormField>
          )}
          <label className="flex items-center gap-2 text-sm"><Switch checked={enabled} onCheckedChange={setEnabled} aria-label="Enabled" /> Enabled</label>
          {errors.form && <p role="alert" className="text-sm font-medium text-destructive" data-testid="monitor-error">{errors.form}</p>}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setOpen(false)}>Cancel</Button>
            <Button type="submit" loading={create.isPending}>Create monitor</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
