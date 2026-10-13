'use client';
import * as React from 'react';
import Link from 'next/link';
import { useQuery } from '@tanstack/react-query';
import { Bar, BarChart, CartesianGrid, Legend, Tooltip as RTooltip, XAxis, YAxis } from 'recharts';
import { Building2, Globe, Inbox, Lightbulb, Mail, Network, RefreshCw, ShieldAlert, UserSearch } from 'lucide-react';
import { api, type ApiError, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { useUrlFilters } from '@/hooks/use-url-filters';
import { formatDateTime, formatNumber, formatRatio } from '@/lib/format';
import { PageHeader } from '@/components/composites/page-header';
import { StatCard } from '@/components/composites/stat-card';
import { ChartCard, chartAxisProps, chartColor, chartGridProps, chartTooltipStyle } from '@/components/composites/chart-card';
import { EmptyState } from '@/components/composites/empty-state';
import { ErrorState } from '@/components/composites/error-state';
import { LoadingState } from '@/components/composites/loading-state';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { ScrollRegion, tdClass, thClass } from '@/components/system/scroll-region';
import { NotEnoughData, OPPORTUNITY_LABELS, TRI, TriStateBadge, flagLabel, type TriState } from '../shared';
import { EffectivenessSection, SchedulerSection } from './effectiveness';
import { MonitorsSection } from './monitors';
import { SimilarFinder } from './similar';

type Overview = Schema<'OverviewOut'>;
const TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'effectiveness', label: 'Source effectiveness' },
  { id: 'scheduler', label: 'Scheduler' },
  { id: 'monitors', label: 'Monitors' },
  { id: 'similar', label: 'Similar businesses' },
] as const;

export function DiscoveryPage() {
  const { filters, set } = useUrlFilters({ tab: 'overview' });
  const active = TABS.some((t) => t.id === filters.tab) ? filters.tab : 'overview';
  const q = useQuery<Overview, ApiError>({ queryKey: qk.custom('discovery', 'overview'), queryFn: ({ signal }) => api.get<Overview>('/api/discovery/overview', { signal }), refetchInterval: 60_000 });
  return (
    <>
      <PageHeader
        title="Discovery Intelligence"
        description="What discovery has found, where it is incomplete, which sources pay off, and what runs next. Every ratio is measured; when the sample is too small it says so instead of guessing."
        meta={q.data && <span className="text-xs text-muted-foreground">Generated {formatDateTime(q.data.generated_at)}</span>}
        actions={<Button variant="outline" onClick={() => q.refetch()} loading={q.isFetching && !q.isPending}><RefreshCw /> Refresh</Button>}
      />
      <Tabs value={active} onValueChange={(v) => set({ tab: v === 'overview' ? undefined : v })}>
        <TabsList aria-label="Discovery sections">
          {TABS.map((t) => <TabsTrigger key={t.id} value={t.id}>{t.label}</TabsTrigger>)}
        </TabsList>
        <TabsContent value="overview">{active === 'overview' && <OverviewTab q={q} />}</TabsContent>
        <TabsContent value="effectiveness">{active === 'effectiveness' && <EffectivenessSection />}</TabsContent>
        <TabsContent value="scheduler">{active === 'scheduler' && <SchedulerSection />}</TabsContent>
        <TabsContent value="monitors">{active === 'monitors' && <MonitorsSection />}</TabsContent>
        <TabsContent value="similar">{active === 'similar' && <SimilarFinder />}</TabsContent>
      </Tabs>
    </>
  );
}

function OverviewTab({ q }: { q: ReturnType<typeof useQuery<Overview, ApiError>> }) {
  if (q.isPending) return <LoadingState variant="cards" rows={8} label="Loading discovery overview" />;
  if (q.isError) return <ErrorState error={q.error} onRetry={() => q.refetch()} />;
  const d = q.data;
  const t = d.totals;
  return (
    <div className="space-y-6">
      <section aria-label="Totals" className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard label="Businesses" value={t.businesses ?? 0} icon={<Building2 />} href="/businesses" hint={`${formatNumber(d.organisations_without_assessment)} not yet assessed for completeness`} />
        <StatCard label="With a confirmed website" value={t.with_confirmed_website ?? 0} icon={<Globe />} hint={t.businesses ? `${formatRatio((t.with_confirmed_website ?? 0) / t.businesses)} of businesses` : undefined} />
        <StatCard label="With a published e-mail" value={t.with_published_email ?? 0} icon={<Mail />} hint={t.businesses ? `${formatRatio((t.with_published_email ?? 0) / t.businesses)} of businesses` : undefined} />
        <StatCard label="Signals · relationships" value={`${formatNumber(t.signals ?? 0)} · ${formatNumber(t.relationships ?? 0)}`} icon={<Network />} hint={<><Link className="underline-offset-2 hover:underline" href="/opportunities">Signals</Link> · <Link className="underline-offset-2 hover:underline" href="/discovery/network">Network</Link> · {formatNumber(t.pending_reviews ?? 0)} identity reviews pending</>} />
      </section>

      <div className="grid gap-4 xl:grid-cols-2">
        <BySourceChart d={d} />
        <ScoreDistribution d={d} />
      </div>

      <Opportunities d={d} />
      <CompletenessFlags d={d} />

      <div className="grid gap-4 xl:grid-cols-2">
        <GeoCoverage d={d} />
        <Industries d={d} />
      </div>
      <Restricted d={d} />
    </div>
  );
}

function BySourceChart({ d }: { d: Overview }) {
  const keys = Array.from(new Set([...d.businesses_by_source, ...d.websites_by_source, ...d.emails_by_source].map((r) => r.key)));
  const get = (rows: { key: string; count: number }[], k: string) => rows.find((r) => r.key === k)?.count ?? 0;
  const rows = keys.map((k) => ({ source: k, businesses: get(d.businesses_by_source, k), websites: get(d.websites_by_source, k), emails: get(d.emails_by_source, k) })).slice(0, 12);
  return (
    <ChartCard title="Businesses, websites and e-mails by source" description="Discovery source of each business; websites are confirmed matches, e-mails are published or corroborated addresses."
      table={{ columns: ['Source', 'Businesses', 'Confirmed websites', 'Published e-mails'], rows: rows.map((r) => [r.source, r.businesses, r.websites, r.emails]) }}
      empty={rows.length === 0 ? <EmptyState compact icon={Inbox} title="Nothing discovered yet" description="Run a discovery to see yield per source." /> : undefined}>
      <BarChart data={rows} margin={{ left: 0, right: 8, top: 8 }}>
        <CartesianGrid {...chartGridProps} />
        <XAxis dataKey="source" {...chartAxisProps} interval={0} tickFormatter={(v: string) => (v.length > 14 ? `${v.slice(0, 13)}…` : v)} />
        <YAxis {...chartAxisProps} allowDecimals={false} width={44} />
        <RTooltip {...chartTooltipStyle} />
        <Legend />
        <Bar dataKey="businesses" name="Businesses" fill={chartColor(0)} />
        <Bar dataKey="websites" name="Confirmed websites" fill={chartColor(1)} />
        <Bar dataKey="emails" name="Published e-mails" fill={chartColor(2)} />
      </BarChart>
    </ChartCard>
  );
}

function ScoreDistribution({ d }: { d: Overview }) {
  const buckets = ['0-19', '20-39', '40-59', '60-79', '80-100'];
  const rows = buckets.map((b) => ({ bucket: b, count: d.completeness_score_distribution.find((r) => r.key === b)?.count ?? 0 }));
  const total = rows.reduce((a, r) => a + r.count, 0);
  return (
    <ChartCard title="Completeness score distribution" description={`Research completeness (0–100) of ${formatNumber(total)} assessed businesses; ${formatNumber(d.organisations_without_assessment)} have no assessment yet.`}
      table={{ columns: ['Score band', 'Businesses'], rows: rows.map((r) => [r.bucket, r.count]) }}
      empty={total === 0 ? <EmptyState compact icon={Inbox} title="No completeness assessments yet" description="Scores appear after businesses are processed by the worker." /> : undefined}>
      <BarChart data={rows} margin={{ left: 0, right: 8, top: 8 }}>
        <CartesianGrid {...chartGridProps} />
        <XAxis dataKey="bucket" {...chartAxisProps} />
        <YAxis {...chartAxisProps} allowDecimals={false} width={44} />
        <RTooltip {...chartTooltipStyle} />
        <Bar dataKey="count" name="Businesses" fill={chartColor(5)} />
      </BarChart>
    </ChartCard>
  );
}

function Opportunities({ d }: { d: Overview }) {
  const groups = d.missing_contact_opportunities;
  const order = ['incomplete_research', 'genuinely_unavailable', 'in_progress'];
  const sorted = [...groups].sort((a, b) => order.indexOf(a.classification) - order.indexOf(b.classification));
  return (
    <section aria-labelledby="opp-h" className="space-y-3" data-testid="missing-contact-opportunities">
      <div>
        <h2 id="opp-h" className="text-base font-semibold">Missing-contact opportunities</h2>
        <p className="text-sm text-muted-foreground">Businesses without a published e-mail, split by <em>why</em>: research that has not been done yet is worth running; research that was done and found nothing is genuinely unavailable.</p>
      </div>
      <div className="grid gap-3 lg:grid-cols-3">
        {sorted.map((g) => {
          const meta = OPPORTUNITY_LABELS[g.classification] ?? { label: g.classification, variant: 'neutral' as const };
          const Icon = g.classification === 'incomplete_research' ? UserSearch : g.classification === 'genuinely_unavailable' ? ShieldAlert : Lightbulb;
          return (
            <Card key={g.classification} data-testid={`opp-${g.classification}`}>
              <CardHeader className="space-y-1">
                <CardTitle className="flex items-center justify-between gap-2 text-sm"><span className="inline-flex items-center gap-1.5"><Icon className="size-4" aria-hidden />{meta.label}</span><span className="text-xl tabular">{formatNumber(g.count)}</span></CardTitle>
                <CardDescription>{g.explanation}</CardDescription>
              </CardHeader>
              <CardContent>
                {g.samples.length === 0 ? <p className="text-xs text-muted-foreground">No businesses in this group.</p> : (
                  <ul className="space-y-1 text-sm" aria-label={`${meta.label}: highest-scoring examples`}>
                    {g.samples.slice(0, 5).map((s) => (
                      <li key={s.organisation_id} className="flex items-center justify-between gap-2">
                        <Link href={`/businesses/${s.organisation_id}`} className="min-w-0 truncate text-primary underline-offset-2 hover:underline">{s.name ?? `Business #${s.organisation_id}`}</Link>
                        <span className="shrink-0 text-xs text-muted-foreground">{s.country_code} · score {s.score}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </CardContent>
            </Card>
          );
        })}
      </div>
    </section>
  );
}

function CompletenessFlags({ d }: { d: Overview }) {
  const flags = Object.entries(d.completeness_flags);
  const states: TriState[] = ['yes', 'no', 'in_progress', 'unknown'];
  return (
    <section aria-labelledby="flags-h" className="space-y-2">
      <h2 id="flags-h" className="text-base font-semibold">Research completeness by step</h2>
      <p className="text-sm text-muted-foreground">Assessed businesses per research step and state. <strong>Not researched yet</strong> is not the same as <strong>No – researched</strong>.</p>
      <ScrollRegion label="Completeness flags table" className="rounded-lg border bg-card">
        <table className="w-full min-w-[36rem] border-collapse text-left text-sm">
          <caption className="sr-only">Businesses per completeness step and state</caption>
          <thead><tr><th scope="col" className={thClass}>Research step</th>{states.map((s) => <th key={s} scope="col" className={`${thClass} text-right`}><TriStateBadge state={s} /></th>)}</tr></thead>
          <tbody>
            {flags.map(([f, counts]) => (
              <tr key={f}>
                <th scope="row" className={`${tdClass} font-medium`}>{flagLabel(f)}</th>
                {states.map((s) => <td key={s} className={`${tdClass} text-right tabular`}><span className="sr-only">{TRI[s].label}: </span>{formatNumber(counts[s] ?? 0)}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </ScrollRegion>
    </section>
  );
}

function GeoCoverage({ d }: { d: Overview }) {
  return (
    <section aria-labelledby="geo-h" className="space-y-2">
      <h2 id="geo-h" className="text-base font-semibold">Geographic coverage</h2>
      {d.geographic_coverage.length === 0 ? <EmptyState compact title="No businesses yet" /> : (
        <ScrollRegion label="Geographic coverage table" className="max-h-96 rounded-lg border bg-card">
          <table className="w-full min-w-[28rem] border-collapse text-left text-sm">
            <caption className="sr-only">Businesses, confirmed websites and published e-mails per country</caption>
            <thead><tr><th scope="col" className={thClass}>Country</th><th scope="col" className={`${thClass} text-right`}>Businesses</th><th scope="col" className={`${thClass} text-right`}>Confirmed websites</th><th scope="col" className={`${thClass} text-right`}>Published e-mails</th></tr></thead>
            <tbody>
              {d.geographic_coverage.map((r) => (
                <tr key={r.country_code}>
                  <th scope="row" className={`${tdClass} font-medium`}>{r.country_code}</th>
                  <td className={`${tdClass} text-right tabular`}>{formatNumber(r.businesses)}</td>
                  <td className={`${tdClass} text-right tabular`}>{formatNumber(r.with_confirmed_website)} <span className="text-xs text-muted-foreground">({formatRatio(r.businesses ? r.with_confirmed_website / r.businesses : 0, 0)})</span></td>
                  <td className={`${tdClass} text-right tabular`}>{formatNumber(r.with_published_email)} <span className="text-xs text-muted-foreground">({formatRatio(r.businesses ? r.with_published_email / r.businesses : 0, 0)})</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </ScrollRegion>
      )}
    </section>
  );
}

function Industries({ d }: { d: Overview }) {
  return (
    <section aria-labelledby="ind-h" className="space-y-2">
      <h2 id="ind-h" className="text-base font-semibold">High-performing industries</h2>
      <p className="text-xs text-muted-foreground">Ranked by the share of businesses with a published e-mail. Industries with fewer than {d.high_performing_industries[0]?.min_sample ?? 10} businesses get no rate.</p>
      {d.high_performing_industries.length === 0 ? <EmptyState compact title="No industries yet" /> : (
        <ScrollRegion label="Industries table" className="max-h-96 rounded-lg border bg-card">
          <table className="w-full min-w-[28rem] border-collapse text-left text-sm" data-testid="industries-table">
            <caption className="sr-only">Industries by published e-mail rate</caption>
            <thead><tr><th scope="col" className={thClass}>Industry</th><th scope="col" className={`${thClass} text-right`}>Businesses</th><th scope="col" className={`${thClass} text-right`}>With e-mail</th><th scope="col" className={`${thClass} text-right`}>Rate</th></tr></thead>
            <tbody>
              {d.high_performing_industries.map((r) => (
                <tr key={`${r.category_id}-${r.name}`}>
                  <th scope="row" className={`${tdClass} font-medium`}>{r.category_id ? <Link className="text-primary underline-offset-2 hover:underline" href={`/categories/${r.category_id}`}>{r.name}</Link> : r.name}</th>
                  <td className={`${tdClass} text-right tabular`}>{formatNumber(r.businesses)}</td>
                  <td className={`${tdClass} text-right tabular`}>{formatNumber(r.with_published_email)}</td>
                  <td className={`${tdClass} text-right`}>{r.published_email_rate === null || r.published_email_rate === undefined ? <NotEnoughData minSample={r.min_sample} /> : <span className="tabular">{formatRatio(r.published_email_rate)}</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </ScrollRegion>
      )}
    </section>
  );
}

function Restricted({ d }: { d: Overview }) {
  const [all, setAll] = React.useState(false);
  const shown = all ? d.restricted_sources : d.restricted_sources.slice(0, 8);
  return (
    <section aria-labelledby="restr-h" className="space-y-2" data-testid="restricted-sources">
      <h2 id="restr-h" className="text-base font-semibold">Source restrictions and unavailable datasets <Badge variant="neutral" className="ml-1 align-middle">{d.restricted_sources.length}</Badge></h2>
      <p className="text-sm text-muted-foreground">Catalogued sources that cannot be collected automatically (terms, licence, or no adapter). Use the <Link className="text-primary underline-offset-2 hover:underline" href="/sources/explore">Source Explorer</Link> to import licensed datasets.</p>
      <ul className="divide-y rounded-lg border bg-card">
        {shown.map((r) => (
          <li key={r.id} className="flex flex-wrap items-start justify-between gap-2 px-3 py-2 text-sm">
            <div className="min-w-0">
              <Link href={`/sources/explore?open=${encodeURIComponent(r.id)}`} className="font-medium text-primary underline-offset-2 hover:underline">{r.name}</Link>
              <p className="break-words text-xs text-muted-foreground">{r.licence || 'Licence not recorded'}</p>
            </div>
            <span className="flex shrink-0 flex-wrap items-center gap-1.5"><Badge variant={r.status === 'restricted' ? 'danger' : 'neutral'}>{r.status === 'restricted' ? 'Restricted' : r.status === 'info_only' ? 'Catalogue only' : r.status === 'not_implemented' ? 'Not implemented' : r.status}</Badge><span className="text-xs text-muted-foreground">{r.reason.replaceAll('_', ' ')}</span></span>
          </li>
        ))}
      </ul>
      {d.restricted_sources.length > 8 && <Button variant="link" size="sm" onClick={() => setAll((v) => !v)} aria-expanded={all}>{all ? 'Show fewer' : `Show all ${d.restricted_sources.length}`}</Button>}
    </section>
  );
}
