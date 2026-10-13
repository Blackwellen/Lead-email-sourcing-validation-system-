'use client';
import * as React from 'react';
import { useQuery } from '@tanstack/react-query';
import { ExternalLink, HeartPulse, ShieldAlert } from 'lucide-react';
import { api, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { useApiMutation } from '@/hooks/use-api-mutation';
import { Can, useMe } from '@/lib/auth';
import { formatDateTime, formatNumber, formatPercent } from '@/lib/format';
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { FormField } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { Separator } from '@/components/ui/separator';
import { KeyValueList } from '@/components/composites/key-value-list';
import { StatusBadge } from '@/components/composites/status-badge';
import { RelativeTime } from '@/components/composites/relative-time';
import { LoadingState } from '@/components/composites/loading-state';
import { ErrorState } from '@/components/composites/error-state';
import { accessOf, canToggle, checkConfig, documentedKeys, successRate, type Source } from './source-utils';

const SOURCES_KEY = qk.all('sources');

export function SourceDrawer({ adapterId, onClose }: { adapterId: string | null; onClose: () => void }) {
  const open = adapterId !== null;
  return (
    <Sheet open={open} onOpenChange={(o) => { if (!o) onClose(); }}>
      <SheetContent size="lg" aria-describedby={undefined}>
        {adapterId && <DrawerBody adapterId={adapterId} />}
      </SheetContent>
    </Sheet>
  );
}

function DrawerBody({ adapterId }: { adapterId: string }) {
  const q = useQuery({ queryKey: qk.detail('sources', adapterId), queryFn: ({ signal }) => api.get<Source>(`/api/sources/${encodeURIComponent(adapterId)}`, { signal }) });
  if (q.isPending) return <><SheetHeader><SheetTitle>Source</SheetTitle></SheetHeader><LoadingState variant="skeleton" rows={6} label="Loading source" /></>;
  if (q.isError) return <><SheetHeader><SheetTitle>Source</SheetTitle></SheetHeader><ErrorState error={q.error} onRetry={() => q.refetch()} /></>;
  return <Detail s={q.data} />;
}

function Detail({ s }: { s: Source }) {
  const access = accessOf(s);
  const rate = successRate(s);
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-5 overflow-y-auto pr-1">
      <SheetHeader>
        <SheetTitle>{s.name}</SheetTitle>
        <SheetDescription>
          <span className="font-mono text-xs">{s.adapter_id}</span>
        </SheetDescription>
        <div className="flex flex-wrap items-center gap-2 pt-1">
          <Badge variant={access.variant}>{access.label}</Badge>
          {s.live_verified ? <Badge variant="success">Live-verified</Badge> : <Badge variant="outline">Not live-verified</Badge>}
          <Badge variant="outline">{s.source_type?.replaceAll('_', ' ') ?? 'unknown type'}</Badge>
        </div>
      </SheetHeader>

      {access.kind === 'manual_only' && (
        <div role="note" data-testid="manual-only-note" className="flex gap-2 rounded-md border border-danger-border bg-danger-soft p-3 text-sm text-danger-foreground">
          <ShieldAlert className="mt-0.5 size-4 shrink-0" aria-hidden />
          <p><strong>Automated collection not permitted — manual import only.</strong> {access.explain}</p>
        </div>
      )}
      {access.kind === 'catalogue' && <p role="note" className="rounded-md border bg-muted/40 p-3 text-sm text-muted-foreground">{access.explain}</p>}

      <section aria-labelledby="src-terms" className="space-y-3">
        <h3 id="src-terms" className="text-sm font-semibold">Licence, attribution and terms</h3>
        <KeyValueList columns={1} items={[
          { label: 'Licence', value: s.licence },
          { label: 'Attribution to display', value: s.attribution },
          { label: 'Source URL', value: s.source_url ? <a className="inline-flex items-center gap-1 text-primary underline-offset-2 hover:underline" href={s.source_url} target="_blank" rel="noreferrer noopener">{s.source_url}<ExternalLink className="size-3" aria-hidden /><span className="sr-only"> (opens in a new tab)</span></a> : null },
          { label: 'Access requirements, rate and usage policy', value: <span className="whitespace-pre-wrap">{s.access_requirements}</span>, hint: 'Per-source request limits are applied by the worker; the API exposes this free-text policy only.' },
          { label: 'Terms notes', value: <span className="whitespace-pre-wrap">{s.notes}</span> },
          { label: 'Country coverage', value: s.country_coverage.includes('*') ? 'All countries (*)' : s.country_coverage.join(', ') },
        ]} />
      </section>

      <Separator />
      <section aria-labelledby="src-stats" className="space-y-3">
        <h3 id="src-stats" className="text-sm font-semibold">Usage</h3>
        <KeyValueList columns={2} items={[
          { label: 'Last run', value: s.last_run_at ? <RelativeTime value={s.last_run_at} /> : null },
          { label: 'Last success', value: s.last_success_at ? formatDateTime(s.last_success_at) : null },
          { label: 'Successful / failed runs', value: `${s.runs_ok} / ${s.runs_failed}`, hint: rate === null ? 'No runs yet' : `${formatPercent(rate * 100, 0)} success` },
          { label: 'Businesses recovered (all runs)', value: formatNumber(s.businesses_recovered) },
          { label: 'Businesses in database', value: formatNumber(s.total_businesses) },
          { label: 'Published emails', value: formatNumber(s.total_published_emails) },
          { label: 'Last error', value: s.last_error },
        ]} />
      </section>

      <Separator />
      <HealthSection s={s} />

      <Can role="admin">
        <Separator />
        <ControlsSection s={s} />
      </Can>
    </div>
  );
}

function HealthSection({ s }: { s: Source }) {
  const refresh = useApiMutation({
    mutationFn: () => api.post<Schema<'SourceHealthOut'>>(`/api/sources/${encodeURIComponent(s.adapter_id)}/health`),
    successMessage: 'Health check finished',
    invalidate: [SOURCES_KEY],
  });
  const h = s.health;
  return (
    <section aria-labelledby="src-health" className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <h3 id="src-health" className="text-sm font-semibold">Health</h3>
        <Can role="operator">
          <Button size="sm" variant="outline" loading={refresh.isPending} onClick={() => refresh.mutate()} disabled={!s.registered}><HeartPulse /> Refresh health</Button>
        </Can>
      </div>
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <StatusBadge kind="health" status={h.ok === true ? 'ok' : h.ok === false ? (h.status === 'degraded' ? 'degraded' : 'down') : 'unknown'} />
        <span className="text-muted-foreground">{h.status}{h.checked_at ? ` · checked ${formatDateTime(h.checked_at)}` : ' · never checked'}</span>
      </div>
      {h.message && <p className="break-words text-sm text-muted-foreground">{h.message}</p>}
    </section>
  );
}

function ControlsSection({ s }: { s: Source }) {
  const me = useMe();
  const toggle = useApiMutation({
    mutationFn: (enable: boolean) => api.post<Source>(`/api/sources/${encodeURIComponent(s.adapter_id)}/${enable ? 'enable' : 'disable'}`),
    successMessage: (_d, enable) => (enable ? 'Source enabled' : 'Source disabled'),
    invalidate: [SOURCES_KEY],
  });
  const [priority, setPriority] = React.useState(String(s.priority));
  React.useEffect(() => setPriority(String(s.priority)), [s.priority]);
  const [prioErr, setPrioErr] = React.useState<string>();
  const savePrio = useApiMutation({
    mutationFn: (p: number) => api.put<Source>(`/api/sources/${encodeURIComponent(s.adapter_id)}/priority`, { priority: p }),
    successMessage: 'Priority saved',
    errorMessage: false,
    onError: (e) => setPrioErr(e.fields.priority ?? e.message),
    invalidate: [SOURCES_KEY],
  });
  const toggleable = canToggle(s);
  return (
    <section aria-labelledby="src-admin" className="space-y-5">
      <h3 id="src-admin" className="text-sm font-semibold">Administrator controls</h3>

      <div className="flex items-start justify-between gap-4">
        <div className="space-y-0.5">
          <label htmlFor="src-enabled" className="text-sm font-medium">Enabled for runs</label>
          <p className="text-xs text-muted-foreground">
            {toggleable ? 'Disabled sources are never used by new runs. Existing data is kept.' : 'This source does not permit automated collection, so it cannot be enabled. Use Upload file for a manual import.'}
          </p>
        </div>
        <Switch id="src-enabled" checked={s.enabled} disabled={!toggleable || toggle.isPending} onCheckedChange={(v) => toggle.mutate(v)} aria-label={`Enable ${s.name}`} />
      </div>

      <form className="flex flex-wrap items-end gap-2" onSubmit={(e) => {
        e.preventDefault();
        const n = Number(priority);
        if (!Number.isInteger(n) || n < 0 || n > 10000) { setPrioErr('Enter a whole number between 0 and 10000.'); return; }
        setPrioErr(undefined);
        savePrio.mutate(n);
      }}>
        <FormField label="Priority" htmlFor="src-priority" hint="Lower numbers run first (0–10000)." error={prioErr} className="w-40">
          <Input id="src-priority" inputMode="numeric" value={priority} onChange={(e) => setPriority(e.target.value)} invalid={!!prioErr} aria-describedby={prioErr ? 'src-priority-error' : 'src-priority-hint'} />
        </FormField>
        <Button type="submit" size="md" variant="outline" loading={savePrio.isPending} disabled={priority === String(s.priority)}>Save priority</Button>
      </form>

      <ConfigEditor s={s} key={`${s.adapter_id}:${JSON.stringify(s.config)}`} disabled={!s.registered || s.info_only} />
      {me.user && <p className="text-xs text-muted-foreground">Changes are recorded in the audit log as {me.user.email}.</p>}
    </section>
  );
}

function ConfigEditor({ s, disabled }: { s: Source; disabled: boolean }) {
  const initial = React.useMemo(() => JSON.stringify(s.config ?? {}, null, 2), [s.config]);
  const [text, setText] = React.useState(initial);
  const [serverError, setServerError] = React.useState<string>();
  const check = React.useMemo(() => checkConfig(text, s), [text, s]);
  const keys = documentedKeys(s);
  const save = useApiMutation({
    mutationFn: (config: Record<string, unknown>) => api.put<Source>(`/api/sources/${encodeURIComponent(s.adapter_id)}/config`, { config }),
    successMessage: 'Configuration saved',
    errorMessage: false,
    onError: (e) => setServerError(e.message),
    invalidate: [SOURCES_KEY],
  });
  const dirty = text !== initial;
  return (
    <form className="space-y-2" onSubmit={(e) => { e.preventDefault(); setServerError(undefined); if (check.value) save.mutate(check.value); }}>
      <FormField label="Configuration (JSON)" htmlFor="src-config" hint={keys.length ? <>Permitted keys: {keys.map((k) => <code key={k} className="mr-1 rounded bg-muted px-1">{k}</code>)}</> : 'No documented settings for this source.'}>
        <Textarea id="src-config" value={text} onChange={(e) => setText(e.target.value)} rows={8} spellCheck={false} className="font-mono text-xs" disabled={disabled}
          aria-invalid={check.errors.length > 0} aria-describedby="src-config-problems" />
      </FormField>
      <div id="src-config-problems" aria-live="polite" className="space-y-1">
        {check.errors.map((m) => <p key={m} role="alert" className="text-xs font-medium text-destructive">{m}</p>)}
        {check.warnings.map((m) => <p key={m} className="text-xs text-warning-foreground">{m}</p>)}
        {serverError && <p role="alert" data-testid="config-server-error" className="text-xs font-medium text-destructive">Server: {serverError}</p>}
      </div>
      <p className="text-xs text-muted-foreground">Values shown as ******** are secrets; keep the mask to leave them unchanged. File paths must point to a file uploaded through this page.</p>
      <Button type="submit" size="md" loading={save.isPending} disabled={disabled || !dirty || !check.value}>Save configuration</Button>
    </form>
  );
}
