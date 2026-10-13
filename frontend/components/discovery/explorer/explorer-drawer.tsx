'use client';
import * as React from 'react';
import { useQuery } from '@tanstack/react-query';
import { FileUp, KeyRound, Lock, Plus, ShieldAlert, Trash2 } from 'lucide-react';
import { api, type ApiError, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { useApiMutation } from '@/hooks/use-api-mutation';
import { Can, useMe } from '@/lib/auth';
import { formatBytes, formatDateTime, formatNumber } from '@/lib/format';
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { FormField } from '@/components/ui/label';
import { SimpleSelect } from '@/components/ui/select';
import { Separator } from '@/components/ui/separator';
import { ConfirmDialog } from '@/components/ui/alert-dialog';
import { KeyValueList } from '@/components/composites/key-value-list';
import { StatusBadge } from '@/components/composites/status-badge';
import { LoadingState } from '@/components/composites/loading-state';
import { ErrorState } from '@/components/composites/error-state';
import { SafeLink } from '@/components/contacts/shared';
import {
  ACCESS_LABEL, BASIS_LABEL, CONFIG_KEY_RE, COST_LABEL, CREDENTIALS_LABEL, MASK, SECRET_KEY_RE, automationStatus, parseConfigValue, permissionLabel, suggestedKeys,
  type ExplorerDetail,
} from './explorer-utils';

const KEY = qk.all('source-explorer');
type ImportOut = Schema<'app__api__source_explorer__ImportOut'>;

export function ExplorerDrawer({ sourceId, onClose }: { sourceId: string | null; onClose: () => void }) {
  return (
    <Sheet open={sourceId !== null} onOpenChange={(o) => { if (!o) onClose(); }}>
      <SheetContent size="lg" aria-describedby={undefined} data-testid="explorer-drawer">
        {sourceId && <Body sourceId={sourceId} />}
      </SheetContent>
    </Sheet>
  );
}

function Body({ sourceId }: { sourceId: string }) {
  const q = useQuery<ExplorerDetail, ApiError>({
    queryKey: qk.detail('source-explorer', sourceId),
    queryFn: ({ signal }) => api.get<ExplorerDetail>(`/api/source-explorer/${encodeURIComponent(sourceId)}`, { signal }),
  });
  if (q.isPending) return <><SheetHeader><SheetTitle>Source</SheetTitle></SheetHeader><LoadingState rows={6} label="Loading source" /></>;
  if (q.isError) return <><SheetHeader><SheetTitle>Source</SheetTitle></SheetHeader><ErrorState error={q.error} onRetry={() => q.refetch()} title={q.error.status === 404 ? 'Source not found' : undefined} /></>;
  return <Detail d={q.data} />;
}

function Detail({ d }: { d: ExplorerDetail }) {
  const auto = automationStatus(d);
  const perm = permissionLabel[d.permission.status] ?? { label: d.permission.status, variant: 'neutral' as const };
  const hasAdapter = !!d.adapter_id && !d.info_only;
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-5 overflow-y-auto pr-1">
      <SheetHeader>
        <SheetTitle>{d.name}</SheetTitle>
        <SheetDescription><span className="font-mono text-xs">{d.id}</span></SheetDescription>
        <div className="flex flex-wrap items-center gap-2 pt-1">
          <Badge variant={auto.variant}>{auto.restricted && <ShieldAlert aria-hidden />}{auto.label}</Badge>
          <StatusBadge kind="health" status={d.health.status} />
          {d.adapter_id && (d.state.enabled ? <Badge variant="success">Enabled</Badge> : <Badge variant="outline">Disabled</Badge>)}
        </div>
      </SheetHeader>

      {auto.restricted && (
        <div role="note" className="flex gap-2 rounded-md border border-danger-border bg-danger-soft p-3 text-sm text-danger-foreground" data-testid="restricted-note">
          <ShieldAlert className="mt-0.5 size-4 shrink-0" aria-hidden />
          <p><strong>Restricted — manual import or written permission only.</strong> The source’s terms do not permit automated collection, so it cannot be enabled here. Import a dataset you are licensed to use, or obtain written permission.</p>
        </div>
      )}

      <section aria-labelledby="ex-terms" className="space-y-3">
        <h3 id="ex-terms" className="text-sm font-semibold">Licence, cost and access</h3>
        <KeyValueList columns={2} items={[
          { label: 'Licence', value: d.licence },
          { label: 'Cost', value: COST_LABEL[d.cost] ?? d.cost },
          { label: 'Access method', value: ACCESS_LABEL[d.access_method] ?? d.access_method },
          { label: 'Credentials', value: CREDENTIALS_LABEL[d.credentials] ?? d.credentials, hint: d.requirements.credentials_note || undefined },
          { label: 'Refresh frequency', value: d.refresh_frequency || 'Not stated' },
          { label: 'Coverage', value: d.countries.includes('*') ? 'All countries' : d.countries.join(', ') },
          { label: 'Industries', value: d.industries.includes('*') ? 'All industries' : d.industries.join(', ') },
          { label: 'Source URL', value: d.url ? <SafeLink url={d.url} /> : null },
        ]} />
        {d.notes && <p className="whitespace-pre-wrap break-words text-sm text-muted-foreground">{d.notes}</p>}
      </section>

      <Separator />
      <section aria-labelledby="ex-req" className="space-y-3" data-testid="config-requirements">
        <h3 id="ex-req" className="text-sm font-semibold">Configuration requirements</h3>
        <KeyValueList columns={1} items={[
          { label: 'Needs configuration before use', value: d.requirements.requires_config ? 'Yes' : 'No' },
          { label: 'Access requirements', value: <span className="whitespace-pre-wrap">{d.requirements.access_requirements || '—'}</span> },
          { label: 'Accepts an uploaded dataset', value: d.requirements.accepts_file_upload ? 'Yes – import a file below' : 'No' },
          { label: 'Settings already set', value: d.requirements.config_keys_set.length ? d.requirements.config_keys_set.join(', ') : 'None' },
          { label: 'Secrets stored (values never shown)', value: d.requirements.secret_keys_set.length ? <span className="inline-flex flex-wrap gap-1">{d.requirements.secret_keys_set.map((k) => <Badge key={k} variant="outline"><Lock aria-hidden />{k}</Badge>)}</span> : 'None' },
          { label: 'Request rate policy', value: d.rate_policy && Object.keys(d.rate_policy).length ? `${d.rate_policy.requests_per_minute ?? 'no fixed'} requests/min · concurrency ${d.rate_policy.concurrency ?? '—'}` : null },
        ]} />
      </section>

      <Separator />
      <section aria-labelledby="ex-perm" className="space-y-2">
        <h3 id="ex-perm" className="text-sm font-semibold">Automated-access permission</h3>
        <div className="flex flex-wrap items-center gap-2"><Badge variant={perm.variant} data-testid="permission-status">{perm.label}</Badge>{d.permission.basis && <span className="text-xs text-muted-foreground">Basis: {BASIS_LABEL[d.permission.basis] ?? d.permission.basis}</span>}</div>
        <p className="text-sm text-muted-foreground" data-testid="permission-reason">{d.permission.reason}</p>
        {d.permission.note && <p className="text-xs text-muted-foreground">Note: {d.permission.note}</p>}
      </section>

      {d.adapter_id && (
        <>
          <Separator />
          <section aria-labelledby="ex-state" className="space-y-3">
            <h3 id="ex-state" className="text-sm font-semibold">Usage and health</h3>
            <KeyValueList columns={2} items={[
              { label: 'Last run', value: d.state.last_run_at ? formatDateTime(d.state.last_run_at) : 'Never' },
              { label: 'Last success', value: d.state.last_success_at ? formatDateTime(d.state.last_success_at) : 'Never' },
              { label: 'Runs ok / failed', value: `${d.state.runs_ok} / ${d.state.runs_failed}` },
              { label: 'Businesses recovered', value: formatNumber(d.state.businesses_recovered) },
              { label: 'Health', value: d.health.message || d.health.status, hint: d.health.checked_at ? `Checked ${formatDateTime(d.health.checked_at)}` : 'Not probed' },
              { label: 'Last error', value: d.state.last_error },
            ]} />
          </section>
        </>
      )}

      {hasAdapter && (
        <Can role="operator" fallback={<p className="rounded-md border bg-muted/40 p-3 text-sm text-muted-foreground" data-testid="viewer-readonly">You have read-only access. Operators can enable, configure and import datasets for sources.</p>}>
          <Separator />
          <OperatorControls d={d} />
        </Can>
      )}
    </div>
  );
}

function OperatorControls({ d }: { d: ExplorerDetail }) {
  const me = useMe();
  const toggle = useApiMutation({
    mutationFn: (enable: boolean) => api.post<Schema<'ToggleOut'>>(`/api/source-explorer/${encodeURIComponent(d.id)}/${enable ? 'enable' : 'disable'}`),
    successMessage: (_r, enable) => (enable ? 'Source enabled' : 'Source disabled'),
    invalidate: [KEY, qk.all('sources')],
  });
  const canEnable = d.permission.can_enable && !(d.requirements.requires_config && d.requirements.config_keys_set.length === 0);
  const blockReason = !d.permission.can_enable ? d.permission.reason : d.requirements.requires_config && d.requirements.config_keys_set.length === 0 ? 'Configure this source (credentials / dataset) before enabling it.' : null;
  return (
    <section aria-labelledby="ex-ops" className="space-y-5">
      <h3 id="ex-ops" className="text-sm font-semibold">Operator actions</h3>
      <div className="flex flex-wrap items-center gap-2">
        {d.state.enabled ? (
          <ConfirmDialog title={`Disable ${d.name}?`} description="New runs will stop using this source. Existing data is kept." confirmLabel="Disable" destructive
            trigger={<Button variant="outline" data-testid="disable-source">Disable source</Button>} onConfirm={() => toggle.mutateAsync(false)} />
        ) : (
          <Button onClick={() => toggle.mutate(true)} loading={toggle.isPending} disabled={!canEnable} data-testid="enable-source" aria-describedby={blockReason ? 'ex-enable-why' : undefined}>Enable source</Button>
        )}
        {!d.state.enabled && blockReason && <p id="ex-enable-why" className="text-xs text-muted-foreground">{blockReason}</p>}
      </div>
      <ConfigureForm d={d} key={`${d.id}:${JSON.stringify(d.config)}:${d.permission.basis ?? ''}`} />
      {d.requirements.accepts_file_upload && <ImportForm d={d} />}
      {me.user && <p className="text-xs text-muted-foreground">Every change is recorded in the audit log as {me.user.email}.</p>}
    </section>
  );
}

interface Row { key: string; value: string; secret: boolean; isSet: boolean; remove: boolean; custom: boolean }

function initialRows(d: ExplorerDetail): Row[] {
  const masked = d.config ?? {};
  return suggestedKeys(d).map((key) => {
    const secret = SECRET_KEY_RE.test(key) || d.requirements.secret_keys_set.includes(key);
    const cur = masked[key];
    // Secrets are write-only: never prefill (the API only returns a mask anyway).
    const value = secret || cur === undefined || cur === null || cur === MASK ? '' : Array.isArray(cur) ? cur.join(', ') : String(cur);
    return { key, value, secret, isSet: key in masked, remove: false, custom: false };
  });
}

function ConfigureForm({ d }: { d: ExplorerDetail }) {
  const [rows, setRows] = React.useState<Row[]>(() => initialRows(d));
  const [basis, setBasis] = React.useState(d.permission.basis ?? '');
  const [note, setNote] = React.useState(d.permission.note ?? '');
  const [err, setErr] = React.useState<string>();
  const [saved, setSaved] = React.useState<string[] | null>(null);
  const baseline = React.useMemo(() => initialRows(d), [d]);
  const save = useApiMutation({
    mutationFn: (body: Schema<'ConfigureIn'>) => api.post<ExplorerDetail>(`/api/source-explorer/${encodeURIComponent(d.id)}/configure`, body),
    successMessage: 'Configuration saved',
    errorMessage: false,
    onError: (e) => setErr(e.message),
    invalidate: [KEY, qk.all('sources')],
  });

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    setErr(undefined); setSaved(null);
    const config: Record<string, unknown> = {};
    const remove: string[] = [];
    for (const r of rows) {
      if (!r.key) continue;
      if (!CONFIG_KEY_RE.test(r.key)) { setErr(`'${r.key}' is not a valid setting name (lower-case letters, digits and underscores, starting with a letter).`); return; }
      if (r.remove) { if (r.isSet) remove.push(r.key); continue; }
      const before = baseline.find((b) => b.key === r.key && !r.custom);
      if (r.secret) { if (r.value) config[r.key] = r.value; continue; } // only send a secret when a new value was typed
      if (r.value.trim() === '') { if (r.isSet) remove.push(r.key); continue; }
      if (!before || before.value !== r.value) config[r.key] = parseConfigValue(r.value);
    }
    const body: Schema<'ConfigureIn'> = { config, remove_keys: remove };
    if (basis && basis !== (d.permission.basis ?? '')) body.permission_basis = basis;
    if (note !== (d.permission.note ?? '')) body.permission_note = note;
    if (!Object.keys(config).length && !remove.length && body.permission_basis === undefined && body.permission_note === undefined) { setErr('Nothing to save – change a setting first.'); return; }
    save.mutate(body, {
      onSuccess: () => {
        setSaved([...Object.keys(config), ...remove.map((k) => `${k} (removed)`)]);
        setRows((rs) => rs.map((r) => (r.secret ? { ...r, value: '', isSet: !r.remove && (r.isSet || !!config[r.key]) } : r)));
      },
    });
  };
  const update = (i: number, patch: Partial<Row>) => setRows((rs) => rs.map((r, j) => (j === i ? { ...r, ...patch } : r)));

  return (
    <form className="space-y-3 rounded-lg border p-3" onSubmit={submit} aria-labelledby="ex-conf" data-testid="configure-form" autoComplete="off">
      <div>
        <h4 id="ex-conf" className="text-sm font-semibold">Configure permitted access</h4>
        <p className="text-xs text-muted-foreground">Settings suggested from this source’s requirements. Secret values (keys, tokens, passwords) are write-only: they are encrypted at rest and never displayed – leave blank to keep the stored value.</p>
      </div>
      {rows.length === 0 && <p className="text-xs text-muted-foreground">No documented settings. Add one below if the source’s documentation requires it.</p>}
      <ul className="space-y-2">
        {rows.map((r, i) => {
          const id = `cfg-${d.id}-${i}`;
          return (
            <li key={i} className="flex flex-wrap items-end gap-2">
              {r.custom ? (
                <FormField label="Setting name" htmlFor={`${id}-k`} className="w-40"><Input id={`${id}-k`} value={r.key} onChange={(e) => update(i, { key: e.target.value.trim(), secret: SECRET_KEY_RE.test(e.target.value) })} placeholder="e.g. base_url" /></FormField>
              ) : null}
              <FormField className="min-w-0 flex-1" htmlFor={id}
                label={<span className="inline-flex items-center gap-1">{r.secret && <KeyRound className="size-3" aria-hidden />}{r.custom ? 'Value' : r.key}{r.secret && <span className="font-normal text-muted-foreground">(secret)</span>}</span>}
                hint={r.secret ? (r.isSet ? 'A value is stored (hidden). Type a new value to replace it.' : 'Not set.') : r.isSet ? undefined : 'Not set.'}>
                <Input id={id} type={r.secret ? 'password' : 'text'} autoComplete={r.secret ? 'new-password' : 'off'} spellCheck={false} value={r.value} disabled={r.remove}
                  placeholder={r.secret && r.isSet ? '•••••••• (stored)' : ''} onChange={(e) => update(i, { value: e.target.value })} aria-describedby={r.secret || !r.isSet ? `${id}-hint` : undefined} data-testid={`cfg-input-${r.key || i}`} />
              </FormField>
              {r.isSet && (
                <label className="flex h-9 items-center gap-1.5 text-xs"><input type="checkbox" checked={r.remove} onChange={(e) => update(i, { remove: e.target.checked })} className="size-4 accent-[hsl(var(--destructive))]" /> Remove</label>
              )}
              {r.custom && <Button type="button" variant="ghost" size="icon" aria-label="Remove this new setting" onClick={() => setRows((rs) => rs.filter((_, j) => j !== i))}><Trash2 /></Button>}
            </li>
          );
        })}
      </ul>
      <Button type="button" size="sm" variant="ghost" onClick={() => setRows((rs) => [...rs, { key: '', value: '', secret: false, isSet: false, remove: false, custom: true }])}><Plus /> Add a setting</Button>
      <div className="grid gap-3 sm:grid-cols-2">
        <FormField label="Permission basis" htmlFor={`basis-${d.id}`} hint="How you are permitted to collect from this source (needed for paid / credentialed sources).">
          <SimpleSelect id={`basis-${d.id}`} aria-label="Permission basis" value={basis || '__none'} onValueChange={(v) => setBasis(v === '__none' ? '' : v)}
            options={[{ value: '__none', label: d.permission.basis ? 'Keep current' : 'Not declared' }, ...d.requirements.permitted_permission_bases.map((b) => ({ value: b, label: BASIS_LABEL[b] ?? b }))]} />
        </FormField>
        <FormField label="Permission note" htmlFor={`note-${d.id}`} hint="e.g. licence reference or who granted permission (max 500).">
          <Textarea id={`note-${d.id}`} rows={2} maxLength={500} value={note} onChange={(e) => setNote(e.target.value)} />
        </FormField>
      </div>
      {err && <p role="alert" className="text-xs font-medium text-destructive" data-testid="configure-error">{err}</p>}
      {saved && <p role="status" className="text-xs text-success-foreground" data-testid="configure-saved">Saved: {saved.length ? saved.join(', ') : 'permission details'}. Secret values are not shown again.</p>}
      <Button type="submit" loading={save.isPending}>Save configuration</Button>
    </form>
  );
}

const MAX_MB = 200;
function ImportForm({ d }: { d: ExplorerDetail }) {
  const [file, setFile] = React.useState<File | null>(null);
  const [err, setErr] = React.useState<string>();
  const [result, setResult] = React.useState<ImportOut | null>(null);
  const input = React.useRef<HTMLInputElement>(null);
  const upload = useApiMutation({
    mutationFn: (f: File) => { const fd = new FormData(); fd.append('file', f); return api.post<ImportOut>(`/api/source-explorer/${encodeURIComponent(d.id)}/import`, fd); },
    errorMessage: false,
    successMessage: 'Dataset imported',
    onMutate: () => { setErr(undefined); setResult(null); },
    onSuccess: (r) => { setResult(r); setFile(null); if (input.current) input.current.value = ''; },
    onError: (e) => setErr(e.message),
    invalidate: [KEY],
  });
  const id = `import-${d.id}`;
  return (
    <form className="space-y-2 rounded-lg border p-3" aria-labelledby={`${id}-t`} onSubmit={(e) => { e.preventDefault(); if (file) upload.mutate(file); }} data-testid="import-form">
      <h4 id={`${id}-t`} className="text-sm font-semibold">Import dataset</h4>
      <p className="text-xs text-muted-foreground">Upload the official bulk file (zip, gz, bz2, xlsx or UTF-8 CSV/JSON/XML) you downloaded under the source’s licence. The server checks the real content, stores it privately and points this source’s <code>path</code> setting at it.</p>
      <label htmlFor={id} className="text-sm font-medium">Dataset file</label>
      <input ref={input} id={id} type="file" onChange={(e) => {
        const f = e.target.files?.[0] ?? null; setFile(f); setErr(undefined); setResult(null);
        if (f && f.size > MAX_MB * 1024 * 1024) setErr(`The file is larger than ${MAX_MB} MB.`);
      }} className="block w-full cursor-pointer rounded-md border border-input bg-background text-sm file:mr-3 file:cursor-pointer file:border-0 file:bg-secondary file:px-3 file:py-2 file:text-sm file:font-medium" />
      {err && <p role="alert" className="text-xs font-medium text-destructive" data-testid="import-error">{err}</p>}
      {result && <p role="status" className="text-xs text-success-foreground" data-testid="import-result">Imported {result.kind.toUpperCase()} ({formatBytes(result.size)}); the source now reads this file.</p>}
      <Button type="submit" variant="outline" loading={upload.isPending} disabled={!file || !!err}><FileUp /> Upload dataset</Button>
    </form>
  );
}

