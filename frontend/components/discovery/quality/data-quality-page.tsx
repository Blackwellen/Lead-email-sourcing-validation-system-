'use client';
import * as React from 'react';
import Link from 'next/link';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { CheckCircle2, ClipboardCheck, History, RefreshCw, ShieldCheck } from 'lucide-react';
import { api, type ApiError, type Page, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { listFetcher } from '@/hooks/use-paged-query';
import { useUrlFilters } from '@/hooks/use-url-filters';
import { useApiMutation } from '@/hooks/use-api-mutation';
import { useMe } from '@/lib/auth';
import { formatDate, formatDateTime, formatNumber, titleCase } from '@/lib/format';
import { PageHeader } from '@/components/composites/page-header';
import { DataTable, createColumnHelper, type DataTableColumn } from '@/components/composites/data-table';
import { FilterSelect } from '@/components/composites/filter-bar';
import { EmptyState } from '@/components/composites/empty-state';
import { ErrorState } from '@/components/composites/error-state';
import { LoadingState } from '@/components/composites/loading-state';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { ConfirmDialog } from '@/components/ui/alert-dialog';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Sheet, SheetContent, SheetHeader, SheetTitle } from '@/components/ui/sheet';
import { CompletenessPanel } from '../completeness-panel';
import { TriStateBadge } from '../shared';

type Issue = Schema<'IssueRow'>;
type Summary = Schema<'SummaryOut'>;
type ActionOut = Schema<'ActionOut'>;
type ActionName = Schema<'ActionIn'>['action'];
type Subject = 'review_link' | 'domain' | 'organisation' | 'email';

export const ISSUE_TABS: { id: string; label: string; description: string; actions: ActionName[] }[] = [
  { id: 'duplicate_companies', label: 'Duplicate companies', description: 'Two records that look like the same company. Never merged automatically: confirm, reject, or (admins) merge.', actions: ['confirm_duplicate', 'reject_duplicate', 'merge'] },
  { id: 'unconfirmed_domains', label: 'Unconfirmed domains', description: 'Candidate websites awaiting a decision, or checked without a conclusive result.', actions: ['confirm_domain', 'reject_domain', 'queue_recrawl'] },
  { id: 'missing_websites', label: 'Missing websites', description: 'Businesses with no confirmed website – split into research not finished vs. genuinely none found.', actions: ['queue_recrawl', 'mark_reviewed'] },
  { id: 'stale_emails', label: 'Stale e-mails', description: 'Published addresses not seen on a page for a long time.', actions: ['queue_revalidate', 'queue_recrawl', 'suppress'] },
  { id: 'conflicting_evidence', label: 'Conflicting evidence', description: 'Several confirmed websites or company numbers for one business, or an address origin that contradicts its evidence.', actions: ['mark_reviewed', 'queue_recrawl'] },
  { id: 'incomplete_contacts', label: 'Incomplete contacts', description: 'Named contacts without any e-mail address.', actions: ['queue_recrawl', 'mark_reviewed'] },
  { id: 'catchall_or_unknown', label: 'Catch-all / unknown', description: 'Addresses whose deliverability cannot be established (catch-all domain or inconclusive evidence).', actions: ['queue_revalidate', 'suppress'] },
  { id: 'unresolved_identities', label: 'Unresolved identities', description: 'Source records that match a business only ambiguously.', actions: ['reject_duplicate', 'merge'] },
];

export const ACTIONS: Record<ActionName, { label: string; subject: Subject; destructive?: boolean; admin?: boolean; explain: string }> = {
  confirm_duplicate: { label: 'Confirm duplicate', subject: 'review_link', explain: 'Marks the pair as the same company. Nothing is merged; an admin can merge afterwards.' },
  reject_duplicate: { label: 'Not a duplicate', subject: 'review_link', explain: 'Records that these are different companies; the suggestion is closed.' },
  merge: { label: 'Merge', subject: 'review_link', destructive: true, admin: true, explain: 'Merges the candidate into the business. Irreversible except through an admin split from the audit trail.' },
  confirm_domain: { label: 'Confirm domain', subject: 'domain', explain: 'Marks the website as the business’s official site (manual override).' },
  reject_domain: { label: 'Reject domain', subject: 'domain', explain: 'Marks the website as NOT belonging to the business (manual override).' },
  queue_recrawl: { label: 'Queue re-crawl', subject: 'organisation', explain: 'Queues a refresh of the business (domain resolution and crawl).' },
  queue_revalidate: { label: 'Queue re-validation', subject: 'email', explain: 'Queues DNS/MX re-validation of the addresses.' },
  mark_reviewed: { label: 'Mark reviewed', subject: 'organisation', explain: 'Clears the “needs review” flag of the business.' },
  suppress: { label: 'Suppress', subject: 'email', destructive: true, explain: 'Adds the addresses to the suppression list so they are never exported for marketing.' },
};

/** The id POST /actions expects for this row and action, or null when the action does not apply to the row. */
export function idFor(row: Issue, action: ActionName): number | null {
  const subj = ACTIONS[action].subject;
  if (row.subject_type === subj) return row.subject_id;
  if (subj === 'organisation') return row.organisation_id ?? null;
  return null;
}

const MISSING_CLASS: Record<string, string> = { incomplete_research: 'unknown', genuinely_unavailable: 'no', in_progress: 'in_progress' };
const col = createColumnHelper<Issue>();

function columnsFor(issue: string, openCompleteness: (r: Issue) => void): DataTableColumn<Issue>[] {
  const cols: DataTableColumn<Issue>[] = [
    col.accessor('organisation_name', {
      header: 'Business', enableSorting: false,
      cell: (i) => (i.row.original.organisation_id ? <Link className="text-primary underline-offset-2 hover:underline" href={`/businesses/${i.row.original.organisation_id}`}>{i.getValue() ?? `#${i.row.original.organisation_id}`}</Link> : '—'),
    }),
    col.accessor('summary', { header: 'Issue', enableSorting: false, cell: (i) => <span className="block min-w-[16rem] max-w-xl whitespace-normal">{i.getValue()}</span> }),
  ];
  if (issue === 'missing_websites') {
    cols.push(col.accessor('classification', { header: 'Website research', enableSorting: false, cell: (i) => <TriStateBadge state={MISSING_CLASS[i.getValue() ?? ''] ?? 'unknown'} /> }));
  } else if (issue !== 'duplicate_companies' && issue !== 'unresolved_identities' && issue !== 'incomplete_contacts' && issue !== 'stale_emails') {
    cols.push(col.accessor('classification', { header: 'Kind', enableSorting: false, meta: { hideBelow: 'md' }, cell: (i) => (i.getValue() ? <Badge variant="neutral">{titleCase(String(i.getValue()))}</Badge> : '—') }));
  }
  if (issue === 'duplicate_companies') {
    cols.push(col.display({ id: 'candidate', header: 'Candidate', cell: (i) => {
      const d = i.row.original.details ?? {};
      const cid = d.candidate_org_id as number | null;
      return <span className="text-sm">{cid ? <Link className="text-primary underline-offset-2 hover:underline" href={`/businesses/${cid}`}>{String(d.candidate_name ?? `#${cid}`)}</Link> : '—'}{d.confirmed_duplicate ? <Badge variant="info" className="ml-1.5">Confirmed duplicate</Badge> : null}</span>;
    } }));
  }
  cols.push(col.accessor('detected_at', { header: 'Detected', enableSorting: false, meta: { hideBelow: 'lg', className: 'whitespace-nowrap' }, cell: (i) => (i.getValue() ? formatDate(i.getValue()) : '—') }));
  cols.push(col.display({ id: 'completeness', header: 'Completeness', meta: { hideBelow: 'sm' }, cell: (i) => (i.row.original.organisation_id ? (
    <Button size="sm" variant="ghost" onClick={() => openCompleteness(i.row.original)} aria-label={`Research completeness of ${i.row.original.organisation_name ?? 'this business'}`}><ClipboardCheck /> Flags</Button>
  ) : null) }));
  return cols;
}

export function DataQualityPage() {
  const me = useMe();
  const qc = useQueryClient();
  const { filters, set } = useUrlFilters({ tab: 'duplicate_companies', classification: '', stale_days: '' });
  const tab = ISSUE_TABS.find((t) => t.id === filters.tab) ?? ISSUE_TABS[0];
  const staleDays = filters.stale_days && /^\d+$/.test(filters.stale_days) ? filters.stale_days : '90';
  const summary = useQuery<Summary, ApiError>({ queryKey: qk.custom('data-quality', 'summary', { staleDays }), queryFn: ({ signal }) => api.get<Summary>('/api/data-quality/summary', { query: { stale_days: staleDays }, signal }) });
  const [compl, setCompl] = React.useState<Issue | null>(null);
  const [result, setResult] = React.useState<{ out: ActionOut; reason: string; notApplicable: number } | null>(null);
  React.useEffect(() => { setResult(null); }, [tab.id]);

  const listFilters = React.useMemo(() => ({
    classification: tab.id === 'missing_websites' ? filters.classification || undefined : undefined,
    stale_days: tab.id === 'stale_emails' ? staleDays : undefined,
  }), [tab.id, filters.classification, staleDays]);
  const fetchPage = React.useMemo(() => listFetcher<Issue>(`/api/data-quality/issues/${tab.id}`), [tab.id]);
  const columns = React.useMemo(() => columnsFor(tab.id, setCompl), [tab.id]);
  const counts = new Map((summary.data?.issues ?? []).map((i) => [i.issue_type, i]));
  const isOperator = me.can('operator');

  return (
    <>
      <PageHeader
        title="Data Quality"
        description="Review what the pipeline could not decide on its own. Every action needs a reason and is written to the audit log with the before and after state."
        actions={<Button variant="outline" onClick={() => { void summary.refetch(); void qc.invalidateQueries({ queryKey: qk.all('data-quality') }); }} loading={summary.isFetching && !summary.isPending}><RefreshCw /> Refresh</Button>}
      />
      {summary.isError ? <ErrorState error={summary.error} onRetry={() => summary.refetch()} className="mb-4" /> : (
        <section aria-label="Issue summary" className="mb-4 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm" data-testid="dq-summary">
          {summary.isPending ? <span className="text-muted-foreground">Counting issues…</span> : (
            <>
              <span><strong className="tabular">{formatNumber(summary.data.total)}</strong> open issues</span>
              <span className="text-xs text-muted-foreground">counted {formatDateTime(summary.data.generated_at)}</span>
            </>
          )}
        </section>
      )}
      <Tabs value={tab.id} onValueChange={(v) => set({ tab: v === 'duplicate_companies' ? undefined : v, classification: undefined })}>
        <TabsList aria-label="Issue types" className="mb-3">
          {ISSUE_TABS.map((t) => {
            const c = counts.get(t.id);
            return <TabsTrigger key={t.id} value={t.id} count={c ? `${c.capped ? '≥' : ''}${formatNumber(c.count ?? 0)}` : undefined} data-testid={`dq-tab-${t.id}`}>{t.label}</TabsTrigger>;
          })}
        </TabsList>
        {ISSUE_TABS.map((t) => (
          <TabsContent key={t.id} value={t.id}>{t.id === tab.id && (
      <div className="space-y-3">
        <p className="text-sm text-muted-foreground">{tab.description}</p>
        {!isOperator && <p role="note" className="rounded-md border bg-muted/40 p-2 text-xs text-muted-foreground">Read-only: review actions need the Operator role.</p>}
        {(tab.id === 'missing_websites' || tab.id === 'stale_emails') && (
          <div className="flex flex-wrap gap-2">
            {tab.id === 'missing_websites' && (
              <FilterSelect label="Research" value={filters.classification} onChange={(v) => set({ classification: v ?? '' })} allLabel="All"
                options={[{ value: 'incomplete_research', label: 'Incomplete research' }, { value: 'genuinely_unavailable', label: 'Genuinely unavailable' }, { value: 'in_progress', label: 'In progress' }]} />
            )}
            {tab.id === 'stale_emails' && (
              <FilterSelect label="Not seen for" value={filters.stale_days} onChange={(v) => set({ stale_days: v ?? '' })} allLabel="90 days"
                options={[{ value: '30', label: '30 days' }, { value: '180', label: '180 days' }, { value: '365', label: '365 days' }]} />
            )}
          </div>
        )}
        {result && <ResultSummary result={result} onClose={() => setResult(null)} isAdmin={me.can('admin')} />}
        <DataTable key={tab.id} caption={`${tab.label} issues`} tableId={`dq-${tab.id}`} columns={columns} queryKey={qk.list('data-quality', { issue: tab.id })} fetchPage={fetchPage}
          filters={listFilters} selectable={isOperator} defaultPageSize={50}
          emptyState={<EmptyState icon={ShieldCheck} title="Nothing to review" description={`No ${tab.label.toLowerCase()} right now.`} />}
          bulkActions={({ rows, clear }) => (
            <div className="flex flex-wrap gap-2">
              {tab.actions.filter((a) => !ACTIONS[a].admin || me.can('admin')).map((a) => (
                <ActionButton key={a} action={a} rows={rows} onDone={(out, reason, notApplicable) => { setResult({ out, reason, notApplicable }); clear(); }} />
              ))}
              {tab.actions.some((a) => ACTIONS[a].admin) && !me.can('admin') && <span className="self-center text-xs text-muted-foreground">Merging needs the Admin role.</span>}
            </div>
          )} />
      </div>
          )}</TabsContent>
        ))}
      </Tabs>
      <Sheet open={!!compl} onOpenChange={(o) => { if (!o) setCompl(null); }}>
        <SheetContent size="lg" aria-describedby={undefined}>
          <SheetHeader><SheetTitle>{compl?.organisation_name ?? 'Business'}</SheetTitle></SheetHeader>
          <div className="min-h-0 flex-1 overflow-y-auto pr-1">
            {compl?.organisation_id && <CompletenessPanel orgId={compl.organisation_id} headingLevel={3} />}
            {compl?.organisation_id && <p className="mt-4 text-sm"><Link className="text-primary underline-offset-2 hover:underline" href={`/businesses/${compl.organisation_id}`}>Open business</Link></p>}
          </div>
        </SheetContent>
      </Sheet>
      {me.can('admin') && <RecentAudit />}
    </>
  );
}

function ActionButton({ action, rows, onDone }: { action: ActionName; rows: Issue[]; onDone: (out: ActionOut, reason: string, notApplicable: number) => void }) {
  const meta = ACTIONS[action];
  const [open, setOpen] = React.useState(false);
  const [reason, setReason] = React.useState('');
  const [err, setErr] = React.useState<string>();
  const ids = Array.from(new Set(rows.map((r) => idFor(r, action)).filter((x): x is number => x !== null)));
  const notApplicable = rows.length - rows.filter((r) => idFor(r, action) !== null).length;
  const run = useApiMutation({
    mutationFn: () => api.post<ActionOut>('/api/data-quality/actions', { action, ids, reason: reason.trim() }),
    successMessage: (o) => `${meta.label}: ${o.ok} done${o.skipped ? `, ${o.skipped} skipped` : ''}${o.errors ? `, ${o.errors} failed` : ''}`,
    invalidate: [qk.all('data-quality'), qk.all('businesses'), qk.all('contacts')],
  });
  const id = `reason-${action}`;
  return (
    <ConfirmDialog
      open={open}
      onOpenChange={(o) => { setOpen(o); if (o) { setReason(''); setErr(undefined); } }}
      trigger={<Button size="sm" variant={meta.destructive ? 'destructive' : 'outline'} disabled={ids.length === 0} data-testid={`dq-action-${action}`}>{meta.label}</Button>}
      title={`${meta.label}: ${ids.length} item${ids.length === 1 ? '' : 's'}?`}
      destructive={meta.destructive}
      confirmLabel={meta.label}
      description={
        <div className="space-y-3 text-left">
          <p>{meta.explain}</p>
          {notApplicable > 0 && <p className="text-xs">{notApplicable} selected row{notApplicable === 1 ? ' does' : 's do'} not support this action and will be left unchanged.</p>}
          <div className="space-y-1.5">
            <label htmlFor={id} className="text-sm font-medium text-foreground">Reason <span className="text-destructive" aria-hidden>*</span><span className="sr-only">(required)</span></label>
            <Textarea id={id} rows={3} maxLength={500} value={reason} onChange={(e) => { setReason(e.target.value); setErr(undefined); }} aria-invalid={!!err} aria-describedby={`${id}-help`} data-testid="dq-reason" />
            <p id={`${id}-help`} className="text-xs">Required (3–500 characters). Stored in the audit log with the before and after state of every item.</p>
            {err && <p role="alert" className="text-xs font-medium text-destructive" data-testid="dq-reason-error">{err}</p>}
          </div>
        </div>
      }
      onConfirm={async () => {
        if (reason.trim().length < 3) { setErr('Enter a reason of at least 3 characters.'); throw new Error('reason'); }
        const out = await run.mutateAsync();
        onDone(out, reason.trim(), notApplicable);
      }}
    />
  );
}

function ResultSummary({ result, onClose, isAdmin }: { result: { out: ActionOut; reason: string; notApplicable: number }; onClose: () => void; isAdmin: boolean }) {
  const { out } = result;
  const problems = out.results.filter((r) => r.status !== 'ok');
  const label = ACTIONS[out.action as ActionName]?.label ?? titleCase(out.action);
  return (
    <section role="status" aria-label="Action result" className="space-y-2 rounded-lg border border-success-border bg-success-soft/60 p-3 text-sm" data-testid="dq-result">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <p className="flex items-center gap-2 font-medium"><CheckCircle2 className="size-4 text-success" aria-hidden />{label}: {out.ok} of {out.requested} done{out.skipped ? `, ${out.skipped} skipped` : ''}{out.errors ? `, ${out.errors} failed` : ''}{result.notApplicable ? ` (${result.notApplicable} not applicable)` : ''}.</p>
        <Button size="sm" variant="ghost" onClick={onClose}>Dismiss</Button>
      </div>
      {problems.length > 0 && (
        <ul className="list-disc space-y-0.5 pl-5 text-xs">
          {problems.slice(0, 20).map((r) => <li key={r.id}><span className="font-medium">#{r.id}</span> {r.status}: {r.message}</li>)}
        </ul>
      )}
      <p className="text-xs text-muted-foreground" data-testid="dq-audit-note">
        <History className="mr-1 inline size-3" aria-hidden />Audit: each change was recorded as <code>data_quality.{out.action}</code> with your reason “{result.reason}” and the before/after state.
        {isAdmin && <> See the <Link className="underline" href="/settings?tab=audit">audit log</Link>.</>}
      </p>
    </section>
  );
}

type AuditRow = Schema<'AuditRow'>;
function RecentAudit() {
  const q = useQuery({ queryKey: qk.custom('data-quality', 'audit'), queryFn: ({ signal }) => api.get<Page<AuditRow>>('/api/audit', { query: { action: 'data_quality.*', limit: 10 }, signal }) });
  return (
    <section aria-labelledby="dq-audit-h" className="mt-8 space-y-2">
      <h2 id="dq-audit-h" className="text-base font-semibold">Recent review actions</h2>
      {q.isPending ? <LoadingState rows={3} label="Loading audit entries" /> : q.isError ? <ErrorState compact error={q.error} onRetry={() => q.refetch()} /> : q.data.items.length === 0 ? (
        <p className="text-sm text-muted-foreground">No data-quality actions recorded yet.</p>
      ) : (
        <ul className="divide-y rounded-lg border bg-card text-sm" data-testid="dq-audit-list">
          {q.data.items.map((a) => (
            <li key={a.id} className="flex flex-wrap items-baseline justify-between gap-2 px-3 py-2">
              <span><code className="text-xs">{a.action}</code> on {a.object_type} #{a.object_id}{typeof a.detail?.reason === 'string' ? <> – “{String(a.detail.reason)}”</> : null}</span>
              <span className="text-xs text-muted-foreground">{a.user_email ?? `user #${a.user_id}`} · {formatDateTime(a.at)}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
