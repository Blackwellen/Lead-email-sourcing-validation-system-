'use client';
import * as React from 'react';
import Link from 'next/link';
import { useQuery } from '@tanstack/react-query';
import { Check, Globe, Pencil, X } from 'lucide-react';
import { api, type ApiError, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { formatDateTime, titleCase } from '@/lib/format';
import { Can } from '@/lib/auth';
import { PageHeader } from '@/components/composites/page-header';
import { LoadingState } from '@/components/composites/loading-state';
import { ErrorState } from '@/components/composites/error-state';
import { EmptyState } from '@/components/composites/empty-state';
import { KeyValueList } from '@/components/composites/key-value-list';
import { StatusBadge } from '@/components/composites/status-badge';
import { ConfidenceMeter } from '@/components/composites/confidence-meter';
import { RelativeTime } from '@/components/composites/relative-time';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { SafeLink } from '@/components/contacts/shared';
import { DomainBadge } from './domain-badge';
import { DomainDecisionDialog, type Decision } from './domain-decision';

type Detail = Schema<'BusinessDetail'>;
type Domain = Schema<'DomainOut'>;
const s = (v: unknown) => (v == null || v === '' ? null : String(v));

function EvidenceList({ items }: { items: Record<string, unknown>[] }) {
  if (!items.length) return <p className="text-sm text-muted-foreground">No evidence recorded for this website.</p>;
  const sorted = [...items].sort((a, b) => String(b.at ?? b.observed_at ?? '').localeCompare(String(a.at ?? a.observed_at ?? '')));
  return (
    <ul className="space-y-2" aria-label="Domain evidence">
      {sorted.map((e, i) => {
        const when = s(e.at ?? e.observed_at);
        const { type, at: _at, observed_at: _o, decision, note, by_user, ...rest } = e;
        void _at; void _o;
        return (
          <li key={i} className="rounded-md border p-2 text-xs" data-testid={type === 'manual' ? 'manual-evidence' : undefined}>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="font-medium">{type === 'manual' ? `Manual decision: ${titleCase(String(decision ?? ''))}` : titleCase(String(type ?? 'evidence'))}{by_user != null && ` (user #${String(by_user)})`}</span>
              {when && <time dateTime={when} className="text-muted-foreground">{formatDateTime(when)}</time>}
            </div>
            {note ? <p className="mt-1 whitespace-pre-wrap break-words">Reason: {String(note)}</p> : null}
            {Object.keys(rest).length > 0 && <p className="mt-1 break-words text-muted-foreground">{Object.entries(rest).map(([k, v]) => `${titleCase(k)}: ${typeof v === 'object' ? JSON.stringify(v) : String(v)}`).join(' · ')}</p>}
          </li>
        );
      })}
    </ul>
  );
}

export function BusinessDetailPage({ id }: { id: string }) {
  const q = useQuery<Detail, ApiError>({ queryKey: qk.detail('businesses', id), queryFn: ({ signal }) => api.get<Detail>(`/api/businesses/${id}`, { signal }) });
  const [decision, setDecision] = React.useState<{ domain: Domain | null; kind: Decision } | null>(null);

  if (q.isPending) return <LoadingState label="Loading business…" variant="cards" />;
  if (q.isError) return <ErrorState error={q.error} onRetry={() => q.refetch()} title={q.error.status === 404 ? 'Business not found' : undefined} />;
  const d = q.data;
  const o = d.organisation as Record<string, unknown>;
  const name = s(o.legal_name) ?? s(o.trading_name) ?? `Business #${id}`;
  const best = d.domains[0];

  return (
    <>
      <PageHeader
        breadcrumbs={[{ label: 'Businesses', href: '/businesses' }, { label: name }]}
        title={<span className="break-words">{name}</span>}
        meta={<>{best ? <DomainBadge status={best.status} /> : <Badge variant="neutral">No website</Badge>}{d.duplicates_of && <Badge variant="neutral">Merged into <Link className="underline" href={`/businesses/${d.duplicates_of}`}>#{d.duplicates_of}</Link></Badge>}</>}
        description={s(o.trading_name) && s(o.legal_name) ? `Trading as ${s(o.trading_name)}` : undefined}
      />
      <Tabs defaultValue="overview">
        <TabsList aria-label="Business sections">
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="contact-info">Contact information</TabsTrigger>
          <TabsTrigger value="domains" count={d.domains.length}>Domains / websites</TabsTrigger>
          <TabsTrigger value="contacts" count={d.contacts.length}>Contacts</TabsTrigger>
          <TabsTrigger value="emails" count={d.emails.length}>Emails</TabsTrigger>
          <TabsTrigger value="sources" count={d.evidence_sources.length}>Sources / evidence</TabsTrigger>
          <TabsTrigger value="history" count={d.validation_history.length}>Verification history</TabsTrigger>
          <TabsTrigger value="runs" count={d.runs.length}>Linked runs</TabsTrigger>
        </TabsList>

        <TabsContent value="overview">
          <Card><CardContent className="pt-6">
            <KeyValueList columns={3} items={[
              { label: 'Legal name', value: s(o.legal_name) }, { label: 'Trading name', value: s(o.trading_name) }, { label: 'Company number', value: s(o.company_number), mono: true },
              { label: 'Company type', value: s(o.legal_type) }, { label: 'Status', value: s(o.status) }, { label: 'Industry', value: s(o.industry) },
              { label: 'Category', value: o.category_id ? <Link className="text-primary underline-offset-2 hover:underline" href={`/categories/${o.category_id}`}>{d.category_name}</Link> : null },
              { label: 'Country', value: s(o.country_code) }, { label: 'Discovery source', value: s(o.discovery_source) },
              { label: 'First discovered', value: o.first_discovered_at ? formatDateTime(String(o.first_discovered_at)) : null }, { label: 'Last checked', value: o.last_checked_at ? <RelativeTime value={String(o.last_checked_at)} /> : 'Never' },
              { label: 'Best website', value: best ? <span className="flex flex-wrap items-center gap-2">{best.domain} <DomainBadge status={best.status} /></span> : null },
              { label: 'E-mail addresses', value: d.emails.length }, { label: 'Published addresses', value: d.emails.filter((e) => e.origin === 'published' || e.origin === 'corroborated').length },
              { label: 'Best confidence', value: d.emails.length ? <ConfidenceMeter value={Math.max(...d.emails.map((e) => e.confidence))} /> : null },
            ]} />
          </CardContent></Card>
        </TabsContent>

        <TabsContent value="contact-info">
          <Card><CardContent className="pt-6">
            <KeyValueList columns={2} items={[
              { label: 'Address', value: s(o.address) }, { label: 'City', value: s(o.city) }, { label: 'Region', value: s(o.region) },
              { label: 'Postal code', value: s(o.postal_code) }, { label: 'Country', value: s(o.country_code) }, { label: 'Phone', value: s(o.phone), copy: s(o.phone) ?? undefined },
              { label: 'Websites', value: d.domains.length ? <ul className="space-y-0.5">{d.domains.map((x) => <li key={x.id}><SafeLink url={x.website_url ?? `https://${x.domain}`} label={x.domain} /></li>)}</ul> : null },
            ]} />
          </CardContent></Card>
        </TabsContent>

        <TabsContent value="domains" className="space-y-4">
          <div className="flex items-center justify-between gap-2">
            <p className="text-sm text-muted-foreground">Website match status is automatic until corrected here. Corrections need a reason and are audited.</p>
            <Can role="operator"><Button size="sm" variant="outline" onClick={() => setDecision({ domain: best ?? null, kind: 'set' })}><Pencil /> Set a different domain…</Button></Can>
          </div>
          {d.domains.length === 0 && <EmptyState icon={Globe} title="No website found yet" description="No domain has been matched to this business." compact />}
          {d.domains.map((x) => (
            <Card key={x.id} data-testid={`domain-${x.domain}`}>
              <CardHeader className="flex-row flex-wrap items-center justify-between gap-2 space-y-0">
                <CardTitle className="flex flex-wrap items-center gap-2 text-base"><SafeLink url={x.website_url ?? `https://${x.domain}`} label={x.domain} /><DomainBadge status={x.status} />
                  {x.parked && <Badge variant="warning">Parked</Badge>}{x.manual_override && <Badge variant="info">Manually decided</Badge>}</CardTitle>
                <Can role="operator">
                  <div className="flex gap-2">
                    <Button size="sm" variant="outline" onClick={() => setDecision({ domain: x, kind: 'confirm' })}><Check /> Confirm</Button>
                    <Button size="sm" variant="outline" onClick={() => setDecision({ domain: x, kind: 'reject' })}><X /> Reject</Button>
                  </div>
                </Can>
              </CardHeader>
              <CardContent className="space-y-3">
                <KeyValueList columns={3} items={[
                  { label: 'Match confidence', value: <ConfidenceMeter value={x.confidence * 100} /> }, { label: 'Last checked', value: x.last_checked_at ? formatDateTime(x.last_checked_at) : 'Never' }, { label: 'Last crawled', value: x.last_crawled_at ? formatDateTime(x.last_crawled_at) : 'Never' },
                ]} />
                <div><h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Evidence</h3><EvidenceList items={x.evidence} /></div>
              </CardContent>
            </Card>
          ))}
        </TabsContent>

        <TabsContent value="contacts">
          {d.contacts.length === 0 ? <EmptyState title="No named contacts" description="Only role mailboxes or no addresses have been found." compact /> : (
            <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {d.contacts.map((c) => (
                <li key={c.id} className="rounded-lg border bg-card p-3 shadow-card">
                  <p className="font-medium">{c.full_name ?? 'Unnamed contact'}</p>
                  <p className="text-sm text-muted-foreground">{[c.job_title, c.department].filter(Boolean).join(' · ') || 'No position recorded'}</p>
                  <div className="mt-2 text-xs"><span className="text-muted-foreground">Identity confidence </span><ConfidenceMeter value={c.identity_confidence * 100} showLabel={false} /></div>
                  <ul className="mt-2 space-y-0.5 text-xs">{d.emails.filter((e) => e.contact_id === c.id).map((e) => <li key={e.id}><Link className="text-primary underline-offset-2 hover:underline" href={`/contacts/${e.id}`}>{e.email}</Link></li>)}</ul>
                </li>
              ))}
            </ul>
          )}
        </TabsContent>

        <TabsContent value="emails">
          {d.emails.length === 0 ? <EmptyState title="No e-mail addresses found" description="No address has been discovered for this business yet." compact /> : (
            <div className="relative overflow-x-auto rounded-lg border">
              <table className="w-full min-w-[640px] text-sm">
                <caption className="sr-only">E-mail addresses of this business</caption>
                <thead><tr className="border-b bg-muted/40 text-left text-xs text-muted-foreground"><th scope="col" className="p-2 font-medium">Email</th><th scope="col" className="p-2 font-medium">Origin</th><th scope="col" className="p-2 font-medium">Confidence</th><th scope="col" className="p-2 font-medium">Reachability</th><th scope="col" className="p-2 font-medium">Marketing</th><th scope="col" className="p-2 font-medium">Validated</th></tr></thead>
                <tbody>{d.emails.map((e) => (
                  <tr key={e.id} className="border-b last:border-0">
                    <td className="p-2"><Link className="break-all font-medium text-primary underline-offset-2 hover:underline" href={`/contacts/${e.id}`}>{e.email}</Link>{e.role_type && <span className="ml-1 text-xs text-muted-foreground">({e.role_type})</span>}</td>
                    <td className="p-2"><StatusBadge kind="origin" status={e.origin} /></td>
                    <td className="p-2"><ConfidenceMeter value={e.confidence} /></td>
                    <td className="p-2"><StatusBadge kind="reachability" status={e.overall_reachability} /></td>
                    <td className="p-2"><StatusBadge kind="marketing" status={e.suppressed ? 'suppressed' : e.marketing_status} /></td>
                    <td className="p-2 text-xs">{e.last_validated_at ? <RelativeTime value={e.last_validated_at} /> : 'Never'}</td>
                  </tr>))}
                </tbody>
              </table>
            </div>
          )}
        </TabsContent>

        <TabsContent value="sources">
          {d.evidence_sources.length === 0 ? <EmptyState title="No source evidence" description="No pages or datasets are linked to this business yet." compact /> : (
            <ul className="divide-y rounded-lg border bg-card" aria-label="Evidence sources">
              {d.evidence_sources.map((e, i) => (
                <li key={`${e.evidence_id}-${i}`} className="grid gap-1 p-3 text-sm">
                  <SafeLink url={e.source_url ?? e.source_document} />
                  <span className="text-xs text-muted-foreground">{e.source_adapter} · {titleCase(e.evidence_type)}{e.extraction_method ? ` · ${titleCase(e.extraction_method)}` : ''} · observed {formatDateTime(e.observed_at)} · reliability {Math.round(e.source_reliability * 100)}%{e.licence ? ` · ${e.licence}` : ''}</span>
                </li>
              ))}
            </ul>
          )}
        </TabsContent>

        <TabsContent value="history">
          {d.validation_history.length === 0 ? <EmptyState title="No verification history" description="None of this business’s addresses has been validated yet." compact /> : (
            <div className="relative overflow-x-auto rounded-lg border">
              <table className="w-full min-w-[560px] text-xs">
                <caption className="sr-only">Verification history for all addresses of this business</caption>
                <thead><tr className="border-b bg-muted/40 text-left text-muted-foreground"><th scope="col" className="p-2 font-medium">When</th><th scope="col" className="p-2 font-medium">Stage</th><th scope="col" className="p-2 font-medium">Result</th><th scope="col" className="p-2 font-medium">Reason</th><th scope="col" className="p-2 font-medium">Verifier</th></tr></thead>
                <tbody>{d.validation_history.map((v) => <tr key={v.id} className="border-b last:border-0"><td className="p-2">{formatDateTime(v.observed_at)}</td><td className="p-2">{v.check_type}</td><td className="p-2">{titleCase(v.result)}</td><td className="p-2">{v.reason_code ? titleCase(v.reason_code) : '—'}</td><td className="p-2">{v.verifier_version}</td></tr>)}</tbody>
              </table>
            </div>
          )}
        </TabsContent>

        <TabsContent value="runs">
          {d.runs.length === 0 ? <EmptyState title="Not linked to a run" description="This business was not discovered by a run." compact /> : (
            <ul className="divide-y rounded-lg border bg-card">
              {d.runs.map((r) => (
                <li key={r.run_id} className="flex flex-wrap items-center justify-between gap-2 p-3 text-sm">
                  <Link className="font-medium text-primary underline-offset-2 hover:underline" href={`/runs/${r.run_id}`}>{r.name}</Link>
                  <span className="flex items-center gap-2"><StatusBadge kind="run" status={r.status} /><Badge variant="outline">Stage: {titleCase(r.stage)}</Badge></span>
                </li>
              ))}
            </ul>
          )}
        </TabsContent>
      </Tabs>
      <DomainDecisionDialog open={!!decision} onOpenChange={(o) => !o && setDecision(null)} businessId={Number(id)} domain={decision?.domain ?? null} initial={decision?.kind ?? 'confirm'} />
    </>
  );
}
