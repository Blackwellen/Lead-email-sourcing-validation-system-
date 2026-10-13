'use client';
import * as React from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useQuery } from '@tanstack/react-query';
import { AlertTriangle, Ban, Flag, RefreshCw, ShieldOff, Trash2 } from 'lucide-react';
import { api, type ApiError, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { formatDateTime, titleCase } from '@/lib/format';
import { Can } from '@/lib/auth';
import { useApiMutation } from '@/hooks/use-api-mutation';
import { PageHeader } from '@/components/composites/page-header';
import { LoadingState } from '@/components/composites/loading-state';
import { ErrorState } from '@/components/composites/error-state';
import { KeyValueList } from '@/components/composites/key-value-list';
import { StatusBadge } from '@/components/composites/status-badge';
import { RelativeTime } from '@/components/composites/relative-time';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Checkbox } from '@/components/ui/checkbox';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { FormField } from '@/components/ui/label';
import { ConfirmDialog } from '@/components/ui/alert-dialog';
import { SuppressDialog, useContactMutations } from './contact-actions';
import { RecipientBadge, SafeLink } from './shared';
import { ScoreBreakdown, type Breakdown } from './score-breakdown';

type Detail = Schema<'ContactDetail'>;
type Validation = Schema<'ValidationOut'>;
const rec = (v: unknown) => (v ?? {}) as Record<string, unknown>;
const str = (v: unknown) => (typeof v === 'string' ? v : v == null ? null : String(v));

const STAGES: { type: string; label: string; help: string }[] = [
  { type: 'V01_syntax', label: 'Syntax', help: 'Is the address well-formed? A well-formed address is not a verified one.' },
  { type: 'V02_domain', label: 'Domain (DNS)', help: 'Does the domain exist in DNS?' },
  { type: 'V03_mx', label: 'Mail routing (MX)', help: 'Does the domain publish mail servers?' },
  { type: 'V05_flags', label: 'Address flags', help: 'Disposable, free-provider and role-account checks.' },
  { type: 'V06_catchall', label: 'Catch-all test', help: 'Does the domain accept any recipient?' },
  { type: 'V07_smtp', label: 'SMTP diagnostics', help: 'Mailbox probe, only for explicitly authorised domains. No message is ever sent.' },
  { type: 'V10_delivery', label: 'Delivery history', help: 'Imported delivery feedback from sending platforms.' },
];

function DetailJson({ detail }: { detail: Record<string, unknown> }) {
  const entries = Object.entries(detail ?? {});
  if (!entries.length) return null;
  return <span className="block text-xs text-muted-foreground">{entries.map(([k, v]) => `${titleCase(k)}: ${typeof v === 'string' ? v : JSON.stringify(v)}`).join(' · ')}</span>;
}

function StageTable({ latest }: { latest: Validation[] }) {
  const by = new Map(latest.map((v) => [v.check_type, v]));
  return (
    <div className="relative overflow-x-auto">
      <table className="w-full min-w-[640px] text-sm">
        <caption className="sr-only">Validation evidence by stage</caption>
        <thead><tr className="border-b text-left text-xs text-muted-foreground"><th scope="col" className="py-2 pr-3 font-medium">Stage</th><th scope="col" className="py-2 pr-3 font-medium">Result</th><th scope="col" className="py-2 pr-3 font-medium">Evidence</th><th scope="col" className="py-2 font-medium">Observed</th></tr></thead>
        <tbody>
          {STAGES.map((s) => {
            const v = by.get(s.type);
            const smtp = s.type === 'V07_smtp';
            const notTested = !v || v.result === 'untested';
            return (
              <tr key={s.type} className="border-b align-top last:border-0" data-testid={`stage-${s.type}`}>
                <th scope="row" className="py-2.5 pr-3 text-left font-medium">{s.label}<span className="block text-xs font-normal text-muted-foreground">{s.help}</span></th>
                <td className="py-2.5 pr-3">
                  {!v ? <Badge variant="neutral">{smtp ? 'Not tested' : 'Not recorded'}</Badge>
                    : notTested ? <Badge variant="neutral">Not tested</Badge>
                    : s.type === 'V01_syntax' ? <StatusBadge kind="syntax" status={v.result} />
                    : s.type === 'V03_mx' ? <StatusBadge kind="mail" status={v.result} />
                    : s.type === 'V06_catchall' ? <StatusBadge kind="catchall" status={v.result} />
                    : <Badge variant={['fail', 'invalid', 'rejected', 'error', 'smtp_recipient_rejected'].includes(v.result) ? 'danger' : ['flagged', 'inconclusive', 'suspected'].includes(v.result) ? 'warning' : 'neutral'}>{titleCase(v.result)}</Badge>}
                  {v?.expired && <Badge variant="warning" className="ml-1">Expired</Badge>}
                </td>
                <td className="py-2.5 pr-3 text-xs">
                  {!v && smtp && <span className="text-muted-foreground">No SMTP diagnostic has been recorded: the mailbox was <strong>not tested</strong>.</span>}
                  {!v && !smtp && <span className="text-muted-foreground">This stage has not run for this address.</span>}
                  {v && (
                    <>
                      {v.reason_code && <span className="block">Reason: {titleCase(v.reason_code)}</span>}
                      {smtp && notTested && <span className="block text-muted-foreground">No SMTP connection was made{v.reason_code === 'not_authorised_for_probe' ? ' because this domain is not authorised for probing' : ''}.</span>}
                      {(v.response_code != null || v.enhanced_status_code) && <span className="block tabular">SMTP reply: {v.response_code ?? '—'} {v.enhanced_status_code ?? ''}</span>}
                      {v.provider && <span className="block">Provider: {v.provider}</span>}
                      {v.permission_context && <span className="block">Authorisation: {v.permission_context}</span>}
                      <DetailJson detail={v.detail ?? {}} />
                      <span className="block text-muted-foreground">Verifier {v.verifier_version}{v.expires_at ? ` · expires ${formatDateTime(v.expires_at)}` : ''}</span>
                    </>
                  )}
                </td>
                <td className="py-2.5 text-xs">{v ? <RelativeTime value={v.observed_at} /> : '—'}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function DeleteDialog({ open, onOpenChange, email, id, onDeleted }: { open: boolean; onOpenChange: (o: boolean) => void; email: string; id: number; onDeleted: () => void }) {
  const [typed, setTyped] = React.useState('');
  const [keep, setKeep] = React.useState(true);
  React.useEffect(() => { if (open) { setTyped(''); setKeep(true); } }, [open]);
  const del = useApiMutation<Schema<'DeleteOut'>, void>({
    mutationFn: () => api.delete(`/api/contacts/${id}`, { query: { keep_suppression: keep } }),
    successMessage: (d) => `Address deleted${d.suppression_kept ? '; suppression kept so it is not re-collected' : ''}`,
    invalidate: [qk.all('contacts'), qk.all('businesses'), qk.all('verification'), qk.all('categories')],
    onSuccess: onDeleted,
  });
  return (
    <Dialog open={open} onOpenChange={(o) => !del.isPending && onOpenChange(o)}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Permanently delete this address?</DialogTitle>
          <DialogDescription>This erases the address, its evidence links and validation history (right to erasure). It cannot be undone.</DialogDescription>
        </DialogHeader>
        <FormField label={<>Type <span className="font-mono">{email}</span> to confirm</>} htmlFor="del-confirm" required>
          <Input id="del-confirm" value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" />
        </FormField>
        <label className="flex items-start gap-2 text-sm"><Checkbox aria-label="Keep a suppression record" checked={keep} onCheckedChange={(c) => setKeep(c === true)} className="mt-0.5" />
          <span>Keep a suppression record so this address is not collected and marketed to again (recommended).</span></label>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={del.isPending}>Cancel</Button>
          <Button variant="destructive" loading={del.isPending} disabled={typed.trim().toLowerCase() !== email.toLowerCase()} onClick={() => del.mutate()}>Delete permanently</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function ContactDetailPage({ id }: { id: string }) {
  const router = useRouter();
  const q = useQuery<Detail, ApiError>({ queryKey: qk.detail('contacts', id), queryFn: ({ signal }) => api.get<Detail>(`/api/contacts/${id}`, { signal }) });
  const m = useContactMutations();
  const [suppressOpen, setSuppressOpen] = React.useState(false);
  const [deleteOpen, setDeleteOpen] = React.useState(false);

  if (q.isPending) return <LoadingState label="Loading contact…" variant="cards" />;
  if (q.isError) return <ErrorState error={q.error} onRetry={() => q.refetch()} title={q.error.status === 404 ? 'Contact not found' : undefined} />;
  const d = q.data;
  const cid = d.id;
  const flags = rec(d.flags);
  const org = rec(d.organisation);
  const profile = d.contact_profile ? rec(d.contact_profile) : null;
  const fresh = rec(d.freshness);
  const mkt = rec(d.marketing);
  const catchAll = rec(d.catch_all);
  const needsReview = flags.needs_review === true;
  const suppressed = mkt.suppressed === true;
  const breakdown = d.confidence_breakdown as Breakdown;
  const st = d.statuses;
  const delivery = d.delivery_counts;

  return (
    <>
      <PageHeader
        breadcrumbs={[{ label: 'Contacts', href: '/contacts' }, { label: d.email }]}
        title={<span className="break-all">{d.email}</span>}
        meta={<><StatusBadge kind="origin" status={d.origin} /><Badge variant={d.published_or_inferred === 'Published' ? 'success' : 'info'}>{d.published_or_inferred}</Badge></>}
        description={d.email_original !== d.email ? `Originally recorded as ${d.email_original}` : undefined}
        actions={
          <Can role="operator">
            <Button variant="outline" size="sm" loading={m.revalidate.isPending} onClick={() => m.revalidate.mutate({ id: cid })}><RefreshCw /> Re-validate</Button>
            <Button variant="outline" size="sm" onClick={() => m.needsReview.mutate({ id: cid, value: !needsReview })}><Flag /> {needsReview ? 'Clear needs review' : 'Mark needs review'}</Button>
            {!suppressed && <Button variant="outline" size="sm" onClick={() => setSuppressOpen(true)}><Ban /> Suppress…</Button>}
            <Can role="admin"><Button variant="destructive" size="sm" onClick={() => setDeleteOpen(true)}><Trash2 /> Delete…</Button></Can>
          </Can>
        }
      />

      <div role="note" className="mb-4 flex gap-2 rounded-lg border border-info-border bg-info-soft p-3 text-sm text-info-foreground">
        <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
        <p><strong>“Safe” is not a delivery guarantee.</strong> It means no adverse evidence was found for a published or corroborated address on a domain that accepts mail. No check can promise inbox delivery, and SMTP acceptance is a diagnostic, not proof a mailbox exists.</p>
      </div>
      {(needsReview || suppressed) && (
        <div className="mb-4 flex flex-wrap gap-2">
          {needsReview && <Badge variant="warning">Flagged: needs review</Badge>}
          {suppressed && <Badge variant="danger">Suppressed: do not use for marketing</Badge>}
        </div>
      )}

      <section aria-labelledby="statuses-h" className="mb-6">
        <h2 id="statuses-h" className="mb-2 text-sm font-semibold">Five independent statuses</h2>
        <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
          {[
            ['Syntax', <StatusBadge key="s" kind="syntax" status={st.syntax} />, 'Is the address well-formed?'],
            ['Mail routing', <StatusBadge key="m" kind="mail" status={st.domain_mail} />, 'Can the domain receive mail?'],
            ['Mailbox evidence', <RecipientBadge key="r" status={st.recipient_evidence} />, 'Did the server accept this recipient in diagnostics?'],
            ['Reachability', <StatusBadge key="o" kind="reachability" status={st.overall_reachability} />, 'Overall technical triage'],
            ['Marketing', <StatusBadge key="k" kind="marketing" status={st.marketing} />, 'Policy triage, independent of validity'],
          ].map(([label, badge, hint]) => (
            <li key={label as string} className="rounded-lg border bg-card p-3 shadow-card">
              <p className="text-xs font-medium text-muted-foreground">{label}</p>
              <div className="mt-1.5">{badge}</div>
              <p className="mt-1.5 text-xs text-muted-foreground">{hint}</p>
            </li>
          ))}
        </ul>
      </section>

      <Card className="mb-6">
        <CardHeader><CardTitle>Why this classification</CardTitle><CardDescription>Generated only from stored evidence.</CardDescription></CardHeader>
        <CardContent>
          <ul className="list-disc space-y-1.5 pl-5 text-sm" data-testid="classification-explanation">
            {d.classification_explanation.map((s, i) => <li key={i}>{s}</li>)}
          </ul>
        </CardContent>
      </Card>

      <div className="mb-6 grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader><CardTitle>Verification score</CardTitle><CardDescription>How the confidence score was built, with caps and penalties.</CardDescription></CardHeader>
          <CardContent><ScoreBreakdown confidence={d.confidence} breakdown={breakdown} /></CardContent>
        </Card>
        <div className="grid content-start gap-4">
          <Card>
            <CardHeader><CardTitle>Contact and organisation</CardTitle></CardHeader>
            <CardContent>
              <KeyValueList columns={2} items={[
                { label: 'Contact person', value: profile ? str(profile.full_name) : <span className="text-muted-foreground">{flags.role_type ? `Role mailbox (${str(flags.role_type)})` : 'No person linked'}</span> },
                { label: 'Position', value: profile ? str(profile.job_title) : null },
                { label: 'Department', value: profile ? str(profile.department) : null },
                { label: 'Identity confidence', value: profile ? `${Math.round(Number(profile.identity_confidence) * 100)}%` : null },
                { label: 'Business', value: <Link className="text-primary underline-offset-2 hover:underline" href={`/businesses/${org.id}`}>{str(org.legal_name) ?? str(org.trading_name) ?? `Business #${org.id}`}</Link> },
                { label: 'Category', value: org.category_id ? <Link className="text-primary underline-offset-2 hover:underline" href={`/categories/${org.category_id}`}>{str(org.category_name)}</Link> : null },
                { label: 'Country', value: str(org.country_code) },
                { label: 'Location', value: [org.city, org.region].filter(Boolean).join(', ') || null },
                { label: 'Discovery source', value: str(org.discovery_source) },
                { label: 'First / last seen', value: <>{formatDateTime(d.first_seen_at)} / {formatDateTime(d.last_seen_at)}</> },
              ]} />
              {d.domains.length > 0 && (
                <div className="mt-3 border-t pt-3">
                  <p className="mb-1 text-xs font-medium text-muted-foreground">Business websites</p>
                  <ul className="space-y-1 text-sm">
                    {d.domains.map((dm) => { const r = rec(dm); return (
                      <li key={String(r.id)} className="flex flex-wrap items-center gap-2"><SafeLink url={str(r.website_url) ?? `https://${r.domain}`} label={str(r.domain) ?? ''} /><StatusBadge kind="domain" status={str(r.status)} />{r.parked === true && <Badge variant="warning">Parked</Badge>}</li>
                    ); })}
                  </ul>
                </div>
              )}
            </CardContent>
          </Card>
          <Card>
            <CardHeader><CardTitle>Marketing eligibility and suppression</CardTitle><CardDescription>{str(mkt.note)}</CardDescription></CardHeader>
            <CardContent className="space-y-3">
              <div className="flex flex-wrap items-center gap-2">
                <StatusBadge kind="marketing" status={str(mkt.status)} />
                <Badge variant={mkt.eligible === true ? 'success' : 'neutral'}>{mkt.eligible === true ? 'Eligible under current policy' : 'Not eligible'}</Badge>
                {suppressed && <Badge variant="danger">Suppressed{mkt.suppressed_at ? ` ${formatDateTime(String(mkt.suppressed_at))}` : ''}</Badge>}
              </div>
              {d.suppressions.length === 0 ? <p className="text-sm text-muted-foreground">No suppression entries match this address, its domain or its business.</p> : (
                <ul className="space-y-2" aria-label="Suppression entries">
                  {d.suppressions.map((s) => (
                    <li key={s.id} className="flex flex-wrap items-center justify-between gap-2 rounded-md border p-2 text-sm">
                      <span><Badge variant="danger">{titleCase(s.kind)}</Badge> <span className="font-mono text-xs">{s.kind === 'email' ? s.value : s.value}</span> · {titleCase(s.reason)}{s.note ? ` · ${s.note}` : ''}<span className="block text-xs text-muted-foreground">Since {formatDateTime(s.created_at)}</span></span>
                      <Can role="operator">
                        <ConfirmDialog
                          trigger={<Button size="sm" variant="outline"><ShieldOff /> Unsuppress</Button>}
                          title={`Remove the ${s.kind}-level suppression?`}
                          description={s.kind === 'email' ? 'This address will no longer be suppressed.' : `This releases every address covered by the ${s.kind}-level entry, not only this one.`}
                          confirmLabel="Unsuppress"
                          onConfirm={() => m.unsuppress.mutateAsync({ id: cid, scope: s.kind as 'email' | 'domain' | 'company' })}
                        />
                      </Can>
                    </li>
                  ))}
                </ul>
              )}
              {suppressed && d.suppressions.length === 0 && (
                <Can role="operator"><Button size="sm" variant="outline" onClick={() => m.unsuppress.mutate({ id: cid, scope: 'email' })}><ShieldOff /> Unsuppress address</Button></Can>
              )}
            </CardContent>
          </Card>
        </div>
      </div>

      <Card className="mb-6">
        <CardHeader><CardTitle>Validation evidence by stage</CardTitle><CardDescription>Latest result per stage. Stages that were not run, or not authorised, are shown as such rather than hidden.</CardDescription></CardHeader>
        <CardContent><StageTable latest={d.validation_latest} /></CardContent>
      </Card>

      <div className="mb-6 grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader><CardTitle>Catch-all status</CardTitle></CardHeader>
          <CardContent className="space-y-2 text-sm">
            <StatusBadge kind="catchall" status={str(catchAll.status)} />
            <p className="text-muted-foreground">{({
              confirmed: `${str(catchAll.domain)} accepts any recipient, so acceptance says nothing about this mailbox.`,
              suspected: `${str(catchAll.domain)} looks like a catch-all from passive evidence; mailbox existence cannot be proven.`,
              rejected_unknown_recipient: `${str(catchAll.domain)} rejected a made-up recipient, so it does not behave as a catch-all.`,
            } as Record<string, string>)[str(catchAll.status) ?? ''] ?? 'Catch-all behaviour has not been tested for this domain.'}</p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader><CardTitle>Freshness and delivery evidence</CardTitle></CardHeader>
          <CardContent className="space-y-3">
            <KeyValueList columns={2} items={[
              { label: 'Last validated', value: fresh.last_validated_at ? <RelativeTime value={String(fresh.last_validated_at)} /> : 'Never' },
              { label: 'Freshness', value: <span className="flex flex-wrap items-center gap-1.5"><Badge variant={fresh.stale === true ? 'warning' : 'success'}>{fresh.stale === true ? 'Stale' : 'Fresh'}</Badge><span className="text-xs text-muted-foreground">fresh for {str(fresh.validity_days)} days</span></span> },
              { label: 'Next result expiry', value: fresh.next_expiry ? formatDateTime(String(fresh.next_expiry)) : null },
              { label: 'Previously bounced', value: flags.previously_bounced === true ? 'Yes' : 'No' },
            ]} />
            <div>
              <p className="mb-1 text-xs font-medium text-muted-foreground">Historical delivery events (imported feedback)</p>
              {d.delivery_history.length === 0 ? <p className="text-sm text-muted-foreground">No delivery feedback has been imported for this address.</p> : (
                <>
                  <p className="mb-1 flex flex-wrap gap-1.5">{Object.entries(delivery).map(([k, n]) => <Badge key={k} variant={k.includes('bounce') || k === 'complaint' ? 'danger' : 'neutral'}>{titleCase(k)}: {n}</Badge>)}</p>
                  <ul className="space-y-1 text-xs">{d.delivery_history.slice(0, 10).map((e) => <li key={e.id}>{formatDateTime(e.occurred_at)} · {titleCase(e.event_type)}{e.campaign ? ` · ${e.campaign}` : ''}{e.detail ? ` · ${e.detail}` : ''}</li>)}</ul>
                </>
              )}
            </div>
          </CardContent>
        </Card>
      </div>

      <Card className="mb-6">
        <CardHeader><CardTitle>Sources</CardTitle><CardDescription>Exact pages and datasets where this address was observed. Only http(s) links are clickable.</CardDescription></CardHeader>
        <CardContent>
          {d.sources.length === 0 ? <p className="text-sm text-muted-foreground">No source evidence is recorded. {d.origin === 'pattern_inferred' || d.origin === 'role_speculative' ? 'This is expected for an inferred address: nothing publishes it.' : ''}</p> : (
            <ul className="divide-y" aria-label="Evidence sources" data-testid="sources-list">
              {d.sources.map((s) => (
                <li key={s.evidence_id} className="grid gap-1 py-2.5 text-sm">
                  <SafeLink url={s.source_url ?? s.source_document} />
                  <span className="text-xs text-muted-foreground">{s.source_adapter} · {titleCase(s.evidence_type)}{s.extraction_method ? ` · extracted by ${titleCase(s.extraction_method)}` : ''} · observed {formatDateTime(s.observed_at)} · reliability {Math.round(s.source_reliability * 100)}%{s.licence ? ` · licence: ${s.licence}` : ''}</span>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      <details className="mb-6 rounded-lg border bg-card p-4 shadow-card">
        <summary className="cursor-pointer text-sm font-semibold">Validation history ({d.validation_history.length})</summary>
        <div className="relative mt-3 overflow-x-auto">
          <table className="w-full min-w-[560px] text-xs">
            <caption className="sr-only">Validation history</caption>
            <thead><tr className="border-b text-left text-muted-foreground"><th scope="col" className="py-1.5 pr-3 font-medium">When</th><th scope="col" className="py-1.5 pr-3 font-medium">Stage</th><th scope="col" className="py-1.5 pr-3 font-medium">Result</th><th scope="col" className="py-1.5 font-medium">Reason</th></tr></thead>
            <tbody>{d.validation_history.map((v) => <tr key={v.id} className="border-b last:border-0"><td className="py-1.5 pr-3">{formatDateTime(v.observed_at)}</td><td className="py-1.5 pr-3">{v.check_type}</td><td className="py-1.5 pr-3">{titleCase(v.result)}</td><td className="py-1.5">{v.reason_code ? titleCase(v.reason_code) : '—'}</td></tr>)}</tbody>
          </table>
        </div>
      </details>

      <SuppressDialog open={suppressOpen} onOpenChange={setSuppressOpen} target={{ ids: [cid], label: d.email }} />
      <DeleteDialog open={deleteOpen} onOpenChange={setDeleteOpen} email={d.email} id={cid} onDeleted={() => router.replace('/contacts')} />
    </>
  );
}
