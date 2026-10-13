'use client';
import * as React from 'react';
import Link from 'next/link';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import { useQuery } from '@tanstack/react-query';
import { Download, FileUp, Pause, Play, Search, Truck, Upload, XCircle } from 'lucide-react';
import { api, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { formatNumber } from '@/lib/format';
import { Can } from '@/lib/auth';
import { useApiMutation } from '@/hooks/use-api-mutation';
import { listFetcher } from '@/hooks/use-paged-query';
import { PageHeader } from '@/components/composites/page-header';
import { StatCard } from '@/components/composites/stat-card';
import { StatusBadge } from '@/components/composites/status-badge';
import { RelativeTime } from '@/components/composites/relative-time';
import { LoadingState } from '@/components/composites/loading-state';
import { ErrorState } from '@/components/composites/error-state';
import { EmptyState } from '@/components/composites/empty-state';
import { DataTable, createColumnHelper } from '@/components/composites/data-table';
import { Button } from '@/components/ui/button';
import { Progress } from '@/components/ui/progress';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet';
import { ConfirmDialog } from '@/components/ui/alert-dialog';
import { ContactFilterBar, contactQuery, useContactFilters } from '@/components/contacts/contact-filters';
import { ContactsTable } from '@/components/contacts/contacts-table';
import { BatchDialog, type BatchScope } from './batch-dialog';
import { DeliveryImportDialog } from './delivery-import-dialog';
import { SmtpPanel } from './smtp-panel';

type Overview = Schema<'VerificationOverview'>;
type Batch = Schema<'BatchOut'>;
type Hist = Schema<'HistoryRow'>;
type TabKey = 'overview' | 'batches' | 'invalid' | 'inconclusive' | 'catch_all' | 'needs_recheck' | 'history';
const EMAIL_TABS = ['invalid', 'inconclusive', 'catch_all', 'needs_recheck'] as const;
type EmailTab = (typeof EMAIL_TABS)[number];

const TAB_COPY: Record<EmailTab, { title: string; help: string; spec: Record<string, unknown> }> = {
  invalid: { title: 'Invalid', help: 'Addresses with evidence they will not accept mail: bad syntax, a domain with no mail service, a previous hard bounce or a rejected recipient.', spec: { reachability: ['invalid'] } },
  inconclusive: { title: 'Inconclusive', help: 'Mailbox tests that gave no reliable answer (greylisting, timeouts, blocked port 25). These are neither good nor bad; review or retry.', spec: { recipient_evidence_status: ['inconclusive'] } },
  catch_all: { title: 'Catch-all', help: 'Domains that accept any recipient, or look like they do. Acceptance proves nothing about the mailbox, so confidence is capped.', spec: { catch_all_status: ['confirmed', 'suspected'] } },
  needs_recheck: { title: 'Needs recheck', help: 'Never validated, or last validated outside the freshness window.', spec: { freshness: 'stale' } },
};

/* ----------------------------------------------------------------- overview */
function OverviewTab({ ov, onTab }: { ov: Overview; onTab: (t: TabKey) => void }) {
  const a = ov.approximate;
  const total = ov.reachability.reduce((n, b) => n + b.count, 0) || 1;
  return (
    <div className="space-y-6">
      {a && <p role="note" className="rounded-md border border-info-border bg-info-soft p-2.5 text-sm text-info-foreground">These counts are estimated from a sample of the table, so they are shown as approximate.</p>}
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard label="Addresses" value={ov.total_emails} approximate={a} />
        <StatCard label="Never validated" value={ov.never_validated} approximate={a} />
        <StatCard label="Mailbox not tested" value={ov.untested_recipient} approximate={a} hint="No SMTP evidence for the mailbox itself" />
        <StatCard label="Active batches" value={ov.active_batches} />
        <button type="button" onClick={() => onTab('invalid')} className="text-left"><StatCard label="Invalid" value={ov.invalid} approximate={a} hint="View list" /></button>
        <button type="button" onClick={() => onTab('inconclusive')} className="text-left"><StatCard label="Inconclusive" value={ov.inconclusive} approximate={a} hint="View list" /></button>
        <button type="button" onClick={() => onTab('catch_all')} className="text-left"><StatCard label="Catch-all domains" value={ov.catch_all} approximate={a} hint="View list" /></button>
        <button type="button" onClick={() => onTab('needs_recheck')} className="text-left"><StatCard label="Needs recheck" value={ov.needs_recheck} approximate={a} hint={`Older than ${ov.validity_days} days or never`} /></button>
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <section aria-labelledby="reach-h" className="rounded-lg border bg-card p-4 shadow-card">
          <h2 id="reach-h" className="mb-3 text-sm font-semibold">Reachability distribution</h2>
          <ul className="space-y-2.5">
            {ov.reachability.map((b) => (
              <li key={b.key} className="grid gap-1">
                <div className="flex items-center justify-between text-sm"><StatusBadge kind="reachability" status={b.key} /><span className="tabular text-xs text-muted-foreground">{formatNumber(b.count, { approximate: a })} · {Math.round((b.count / total) * 100)}%</span></div>
                <Progress value={(b.count / total) * 100} tone={b.key === 'safe' ? 'success' : b.key === 'invalid' ? 'danger' : b.key === 'risky' ? 'warning' : 'primary'} aria-label={`${b.key}: ${Math.round((b.count / total) * 100)} percent`} />
              </li>
            ))}
          </ul>
          <p className="mt-3 text-xs text-muted-foreground">“Safe” means no adverse evidence for a published address. It is not a delivery guarantee.</p>
        </section>
        <section aria-labelledby="queue-h" className="rounded-lg border bg-card p-4 shadow-card">
          <h2 id="queue-h" className="mb-3 text-sm font-semibold">Verification queue</h2>
          {Object.keys(ov.verify_queue).length === 0 ? <p className="text-sm text-muted-foreground">The verification queue is empty.</p> : (
            <dl className="grid grid-cols-2 gap-2 text-sm">{Object.entries(ov.verify_queue).map(([k, n]) => <div key={k} className="rounded-md border p-2"><dt className="text-xs capitalize text-muted-foreground">{k}</dt><dd className="tabular font-semibold">{formatNumber(n)}</dd></div>)}</dl>
          )}
          <p className="mt-3 text-xs text-muted-foreground">SMTP diagnostics are {ov.smtp_enabled ? 'enabled for authorised domains only' : 'disabled; only DNS/MX evidence is collected'}.</p>
        </section>
      </div>
      <SmtpPanel />
    </div>
  );
}

/* ----------------------------------------------------------------- batches */
const bcol = createColumnHelper<Batch>();
const fetchBatches = listFetcher<Batch>('/api/verification/batches');

function FailuresSheet({ batch, onClose }: { batch: Batch | null; onClose: () => void }) {
  const q = useQuery({
    queryKey: qk.custom('verification', 'failures', batch?.id ?? 0), enabled: !!batch?.run_id,
    queryFn: ({ signal }) => api.get<{ items: Schema<'JobRow'>[] }>(`/api/runs/${batch!.run_id}/jobs`, { query: { state: 'failed,dead', limit: 50 }, signal }),
  });
  return (
    <Sheet open={!!batch} onOpenChange={(o) => !o && onClose()}>
      <SheetContent size="lg">
        <SheetHeader>
          <SheetTitle>Failures: {batch?.name}</SheetTitle>
          <SheetDescription>Addresses whose verification job failed. Failures are not classifications; retry them with a new batch.</SheetDescription>
        </SheetHeader>
        {q.isPending ? <LoadingState rows={3} /> : q.isError && (q.error as { status?: number }).status === 404 ? (
          <div role="note" data-testid="failures-unavailable" className="rounded-md border border-warning-border bg-warning-soft p-3 text-sm text-warning-foreground">
            <p className="font-medium">{batch?.failed} job{batch?.failed === 1 ? '' : 's'} failed in this batch.</p>
            <p className="mt-1">The failure details cannot be shown: the API hides the internal run behind a verification batch, so its failed jobs are not listed anywhere. Failed addresses are not classified; start a new batch for them to retry.</p>
          </div>
        ) : q.isError ? <ErrorState error={q.error} onRetry={() => q.refetch()} compact /> : q.data.items.length === 0 ? <EmptyState title="No failed jobs" description="Nothing has failed in this batch." compact /> : (
          <ul className="divide-y text-sm" aria-label="Failed jobs">
            {q.data.items.map((j) => {
              const eid = (j.payload as { email_id?: number }).email_id;
              return (
                <li key={j.id} className="grid gap-0.5 py-2">
                  <span className="flex flex-wrap items-center gap-2"><StatusBadge kind="job" status={j.state === 'dead' ? 'failed' : j.state} />{eid ? <Link className="text-primary underline-offset-2 hover:underline" href={`/contacts/${eid}`}>Address #{eid}</Link> : <span>Job #{j.id}</span>}<span className="text-xs text-muted-foreground">{j.attempts} attempt{j.attempts === 1 ? '' : 's'}</span></span>
                  <span className="text-xs text-muted-foreground">{j.error_code ?? 'error'}{j.error_detail ? `: ${j.error_detail}` : ''}</span>
                </li>
              );
            })}
          </ul>
        )}
      </SheetContent>
    </Sheet>
  );
}

function BatchesTab({ onCsv }: { onCsv: () => void }) {
  const [failures, setFailures] = React.useState<Batch | null>(null);
  const act = useApiMutation<Batch, { id: number; verb: 'pause' | 'resume' | 'cancel' }>({
    mutationFn: ({ id, verb }) => api.post(`/api/verification/batches/${id}/${verb}`),
    successMessage: (b) => `Batch “${b.name}” is now ${b.status}`, invalidate: [qk.all('verification')],
  });
  const columns = React.useMemo(() => [
    bcol.accessor('name', { header: 'Batch', enableSorting: false, meta: { className: 'min-w-44' }, cell: (i) => <span className="font-medium">{i.getValue()}<span className="block text-xs font-normal text-muted-foreground">#{i.row.original.id} · {i.row.original.mode}</span></span> }),
    bcol.accessor('status', { header: 'Status', enableSorting: false, cell: (i) => <StatusBadge kind="job" status={i.getValue() === 'cancelled' ? 'cancelled' : i.getValue()} /> }),
    bcol.accessor('percent', {
      header: 'Progress', enableSorting: false, meta: { className: 'min-w-44' },
      cell: (i) => { const b = i.row.original; return (
        <div className="grid gap-1"><Progress value={b.percent} tone={b.failed ? 'warning' : 'primary'} aria-label={`Batch ${b.name} ${b.percent}% complete`} />
          <span className="tabular text-xs text-muted-foreground">{formatNumber(b.done)} / {formatNumber(b.total)} done · {formatNumber(b.queued)} queued{b.failed ? ` · ${formatNumber(b.failed)} failed` : ''}</span></div>
      ); },
    }),
    bcol.accessor('created_at', { header: 'Created', enableSorting: false, meta: { hideBelow: 'md' }, cell: (i) => <RelativeTime value={i.getValue()} /> }),
    bcol.display({
      id: 'actions', header: 'Actions', enableSorting: false,
      cell: (i) => { const b = i.row.original; return (
        <div className="flex flex-wrap gap-1.5">
          <Can role="operator">
            {(b.status === 'queued' || b.status === 'running') && <Button size="sm" variant="outline" onClick={() => act.mutate({ id: b.id, verb: 'pause' })}><Pause /> Pause</Button>}
            {b.status === 'paused' && <Button size="sm" variant="outline" onClick={() => act.mutate({ id: b.id, verb: 'resume' })}><Play /> Resume</Button>}
            {['queued', 'running', 'paused'].includes(b.status) && (
              <ConfirmDialog destructive title={`Cancel “${b.name}”?`} description="Queued addresses are not verified. Results already recorded are kept." confirmLabel="Cancel batch"
                trigger={<Button size="sm" variant="outline"><XCircle /> Cancel</Button>} onConfirm={() => act.mutateAsync({ id: b.id, verb: 'cancel' })} />
            )}
          </Can>
          {b.failed > 0 && <Button size="sm" variant="ghost" onClick={() => setFailures(b)}><Search /> Inspect {b.failed} failure{b.failed === 1 ? '' : 's'}</Button>}
        </div>
      ); },
    }),
  ], [act]);
  return (
    <>
      <DataTable<Batch> caption="Verification batches" tableId="verification-batches" columns={columns} queryKey={qk.list('verification', { t: 'batches' })} fetchPage={fetchBatches} refetchInterval={5000}
        emptyState={<EmptyState title="No verification batches yet" description="Import a CSV of addresses, or start a batch from one of the list tabs." action={<Can role="operator"><Button size="sm" onClick={onCsv}><Upload /> Import CSV</Button></Can>} />} />
      <FailuresSheet batch={failures} onClose={() => setFailures(null)} />
    </>
  );
}

/* ----------------------------------------------------------------- email tabs */
function toExportFilters(q: Record<string, string | string[]>, spec: Record<string, unknown>) {
  const out: Record<string, unknown> = { ...q, ...spec };
  if (typeof q.category_id === 'string') out.category_id = [Number(q.category_id)];
  for (const k of ['min_confidence', 'max_confidence']) if (typeof out[k] === 'string') out[k] = Number(out[k]);
  for (const k of ['has_name', 'suppressed', 'needs_review', 'free_provider', 'previously_bounced', 'exclude_catch_all', 'exclude_unknown', 'include_subcategories']) if (typeof out[k] === 'string') out[k] = out[k] === 'true';
  return out;
}

function EmailTab({ tab, onBatch }: { tab: EmailTab; onBatch: (s: BatchScope) => void }) {
  const { filters, set, reset } = useContactFilters();
  const query = React.useMemo(() => contactQuery(filters), [filters]);
  const copy = TAB_COPY[tab];
  const filterSpec = React.useMemo(() => toExportFilters(query, copy.spec), [query, copy]);
  const exportResults = useApiMutation<Schema<'ExportOut'>, void>({
    mutationFn: () => api.post('/api/exports', { preset: 'validation_audit', format: 'csv', filters: filterSpec, wait: false }),
    successMessage: (d) => `Export #${d.id} created. Find it on the Exports page.`, invalidate: [qk.all('exports')],
  });
  return (
    <>
      <p className="mb-3 max-w-3xl text-sm text-muted-foreground">{copy.help}</p>
      <ContactFilterBar filters={filters} set={set} reset={reset} scope={`verification-${tab}`}
        actions={<Can role="operator">
          <Button size="sm" variant="outline" onClick={() => onBatch({ kind: 'filters', label: `${copy.title} addresses${Object.keys(query).length ? ' (with current filters)' : ''}`, filters: filterSpec })}><Play /> Re-verify these…</Button>
          <Button size="sm" variant="outline" loading={exportResults.isPending} onClick={() => exportResults.mutate()}><Download /> Export results</Button>
        </Can>} />
      {tab === 'needs_recheck' && <p className="mb-2 text-xs text-muted-foreground">Re-verify and export use the “stale” freshness filter; addresses that were never validated are listed here but need the “Never validated” freshness filter to be selected separately.</p>}
      <ContactsTable caption={`${copy.title} addresses`} tableId={`verification-${tab}`} queryKey={qk.list('verification', { tab })} endpoint="/api/verification/emails" filters={query} extraParams={{ tab }} emptyFiltered={Object.keys(query).length > 0} />
    </>
  );
}

/* ----------------------------------------------------------------- history */
const hcol = createColumnHelper<Hist>();
const fetchHistory = listFetcher<Hist>('/api/verification/history');
function HistoryTab() {
  const [checkType, setCheckType] = React.useState('');
  const [result, setResult] = React.useState('');
  const columns = React.useMemo(() => [
    hcol.accessor('observed_at', { header: 'When', enableSorting: false, cell: (i) => <RelativeTime value={i.getValue()} /> }),
    hcol.accessor('email_id', { header: 'Address', enableSorting: false, cell: (i) => <Link className="text-primary underline-offset-2 hover:underline" href={`/contacts/${i.getValue()}`}>Address #{i.getValue()}</Link> }),
    hcol.accessor('check_type', { header: 'Stage', enableSorting: false }),
    hcol.accessor('result', { header: 'Result', enableSorting: false }),
    hcol.accessor('reason_code', { header: 'Reason', enableSorting: false, meta: { hideBelow: 'md' }, cell: (i) => i.getValue() ?? '—' }),
    hcol.accessor('provider', { header: 'Provider', enableSorting: false, meta: { hideBelow: 'lg' }, cell: (i) => i.getValue() ?? '—' }),
    hcol.accessor('verifier_version', { header: 'Verifier', enableSorting: false, meta: { hideBelow: 'lg' } }),
  ], []);
  const filters = React.useMemo(() => ({ check_type: checkType || undefined, result: result || undefined }), [checkType, result]);
  return (
    <>
      <div className="mb-3 flex flex-wrap gap-2" role="search">
        <label className="sr-only" htmlFor="h-stage">Stage</label>
        <select id="h-stage" className="h-9 rounded-md border border-input bg-background px-2 text-sm" value={checkType} onChange={(e) => setCheckType(e.target.value)}>
          <option value="">Stage: all</option>
          {['V01_syntax', 'V02_domain', 'V03_mx', 'V05_flags', 'V06_catchall', 'V07_smtp', 'V10_delivery'].map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
        <label className="sr-only" htmlFor="h-result">Result</label>
        <input id="h-result" className="h-9 w-44 rounded-md border border-input bg-background px-2 text-sm" placeholder="Result (exact), e.g. valid" value={result} onChange={(e) => setResult(e.target.value.trim())} aria-label="Result" />
      </div>
      <DataTable<Hist> caption="Verification history" tableId="verification-history" columns={columns} queryKey={qk.list('verification', { t: 'history' })} fetchPage={fetchHistory} filters={filters}
        emptyState={<EmptyState title="No verification history yet" description="Results appear here as addresses are validated." />} />
    </>
  );
}

/* ----------------------------------------------------------------- page */
export function VerificationPage() {
  const router = useRouter();
  const pathname = usePathname();
  const sp = useSearchParams();
  const raw = sp.get('tab') as TabKey | null;
  const tab: TabKey = raw && ['overview', 'batches', 'invalid', 'inconclusive', 'catch_all', 'needs_recheck', 'history'].includes(raw) ? raw : 'overview';
  const setTab = React.useCallback((t: TabKey) => {
    const p = new URLSearchParams(sp.toString());
    if (t === 'overview') p.delete('tab'); else p.set('tab', t);
    router.replace(p.toString() ? `${pathname}?${p}` : pathname, { scroll: false });
  }, [router, pathname, sp]);
  const ov = useQuery({ queryKey: qk.custom('verification', 'overview'), queryFn: ({ signal }) => api.get<Overview>('/api/verification/overview', { signal }), refetchInterval: 15_000 });
  const [batchScope, setBatchScope] = React.useState<BatchScope | null>(null);
  const [deliveryOpen, setDeliveryOpen] = React.useState(false);
  const a = ov.data?.approximate;
  const c = (n: number | undefined) => (n === undefined ? undefined : formatNumber(n, { approximate: a, compact: true }));

  return (
    <>
      <PageHeader
        title="Verification"
        description="Evidence-based checks on discovered addresses. Results are diagnostics, not delivery guarantees."
        actions={<Can role="operator">
          <Button size="sm" onClick={() => setBatchScope({ kind: 'csv' })}><FileUp /> Import CSV to verify</Button>
          <Button size="sm" variant="outline" onClick={() => setDeliveryOpen(true)}><Truck /> Import delivery events</Button>
        </Can>}
      />
      <Tabs value={tab} onValueChange={(v) => setTab(v as TabKey)}>
        <TabsList aria-label="Verification sections">
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="batches" count={ov.data?.active_batches ? ov.data.active_batches : undefined}>Batch Jobs</TabsTrigger>
          <TabsTrigger value="invalid" count={c(ov.data?.invalid)}>Invalid</TabsTrigger>
          <TabsTrigger value="inconclusive" count={c(ov.data?.inconclusive)}>Inconclusive</TabsTrigger>
          <TabsTrigger value="catch_all" count={c(ov.data?.catch_all)}>Catch-all</TabsTrigger>
          <TabsTrigger value="needs_recheck" count={c(ov.data?.needs_recheck)}>Needs Recheck</TabsTrigger>
          <TabsTrigger value="history">Verification History</TabsTrigger>
        </TabsList>
        <TabsContent value="overview">
          {ov.isPending ? <LoadingState variant="cards" label="Loading overview…" /> : ov.isError ? <ErrorState error={ov.error} onRetry={() => ov.refetch()} /> : <OverviewTab ov={ov.data} onTab={setTab} />}
        </TabsContent>
        <TabsContent value="batches"><BatchesTab onCsv={() => setBatchScope({ kind: 'csv' })} /></TabsContent>
        {EMAIL_TABS.map((t) => <TabsContent key={t} value={t}><EmailTab tab={t} onBatch={setBatchScope} /></TabsContent>)}
        <TabsContent value="history"><HistoryTab /></TabsContent>
      </Tabs>
      {batchScope && <BatchDialog open onOpenChange={(o) => { if (!o) { setBatchScope(null); setTab('batches'); } }} scope={batchScope} />}
      <DeliveryImportDialog open={deliveryOpen} onOpenChange={setDeliveryOpen} />
    </>
  );
}
