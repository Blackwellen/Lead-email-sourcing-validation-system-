'use client';
import * as React from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useQuery } from '@tanstack/react-query';
import { AlertTriangle, ArrowRight, Crosshair, ExternalLink, Network, Share2, X } from 'lucide-react';
import { api, type ApiError, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { useUrlFilters } from '@/hooks/use-url-filters';
import { useApiMutation } from '@/hooks/use-api-mutation';
import { Can } from '@/lib/auth';
import { formatDate, formatNumber } from '@/lib/format';
import { PageHeader } from '@/components/composites/page-header';
import { EmptyState } from '@/components/composites/empty-state';
import { ErrorState } from '@/components/composites/error-state';
import { LoadingState } from '@/components/composites/loading-state';
import { ConfidenceMeter } from '@/components/composites/confidence-meter';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { FormField } from '@/components/ui/label';
import { SimpleSelect } from '@/components/ui/select';
import { MultiSelect } from '@/components/ui/combobox';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog';
import { SafeLink } from '@/components/contacts/shared';
import { OrgCombobox, RELATIONS, relationMeta } from '../shared';
import { GraphView } from './graph-view';
import { RelationshipsTable } from './relationships-table';

type Graph = Schema<'GraphOut'>;
const DEFAULTS = { org: '', depth: '1', limit: '150', relations: [] as string[], name: '' };

export function NetworkPage() {
  const { filters, set } = useUrlFilters(DEFAULTS, { arrayKeys: ['relations'] });
  const orgId = filters.org && /^\d+$/.test(filters.org) ? Number(filters.org) : null;
  const depth = ['1', '2', '3'].includes(filters.depth) ? Number(filters.depth) : 1;
  const limit = ['50', '100', '150', '300'].includes(filters.limit) ? Number(filters.limit) : 150;
  const relations = filters.relations as string[];
  const [selected, setSelected] = React.useState<number | null>(null);
  React.useEffect(() => { setSelected(null); }, [orgId]);

  const q = useQuery<Graph, ApiError>({
    queryKey: qk.custom('network', 'graph', { orgId, depth, limit, relations }),
    queryFn: ({ signal }) => api.get<Graph>(`/api/network/organisations/${orgId}/graph`, { query: { depth, limit, relations }, signal }),
    enabled: orgId !== null,
  });
  const centerName = q.data?.nodes.find((n) => n.id === orgId)?.name ?? (filters.name || null);

  return (
    <>
      <PageHeader
        title="Business Network"
        description="Branches, brands, subsidiaries, suppliers and partners discovered between businesses. Choose a business to see its neighbourhood; the relationships table below holds the same data in an accessible, filterable form."
        actions={orgId !== null && <Can role="operator"><ExpandDialog orgId={orgId} name={centerName} /></Can>}
      />
      <section aria-label="Graph controls" className="mb-4 grid gap-3 rounded-lg border bg-card p-3 shadow-card sm:grid-cols-2 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)_minmax(0,1fr)_minmax(0,2fr)]">
        <FormField label="Business" htmlFor="net-org">
          <OrgCombobox id="net-org" aria-label="Business" value={orgId} selectedLabel={centerName} onChange={(id, label) => set({ org: id ? String(id) : '', name: label ?? '' })} />
        </FormField>
        <FormField label="Depth" htmlFor="net-depth" hint="Hops from the business (1–3)">
          <SimpleSelect id="net-depth" aria-label="Depth" value={String(depth)} onValueChange={(v) => set({ depth: v === '1' ? '' : v })} options={[{ value: '1', label: '1 hop' }, { value: '2', label: '2 hops' }, { value: '3', label: '3 hops' }]} />
        </FormField>
        <FormField label="Maximum businesses" htmlFor="net-limit" hint="Hard cap 300">
          <SimpleSelect id="net-limit" aria-label="Maximum businesses" value={String(limit)} onValueChange={(v) => set({ limit: v === '150' ? '' : v })} options={['50', '100', '150', '300'].map((v) => ({ value: v, label: v }))} />
        </FormField>
        <FormField label="Relationship types" htmlFor="net-rel" hint="All when empty">
          <MultiSelect id="net-rel" aria-label="Relationship types" value={relations} onChange={(v) => set({ relations: v })} options={RELATIONS.map((r) => ({ value: r, label: relationMeta(r).label }))} />
        </FormField>
      </section>

      {orgId === null ? (
        <EmptyState icon={Share2} title="Choose a business" description="Search for a business above to draw its network. Or browse every recorded relationship in the table below." />
      ) : q.isPending ? <LoadingState rows={8} label="Loading network" /> : q.isError ? (
        <ErrorState error={q.error} onRetry={() => q.refetch()} title={q.error.status === 404 ? 'Business not found' : undefined} />
      ) : q.data.nodes.length <= 1 ? (
        <EmptyState icon={Network} title="No relationships recorded" description={`${centerName ?? 'This business'} has no recorded relationships${relations.length ? ' of the selected types' : ''}. Operators can run “Expand network” to look for some.`} />
      ) : (
        <div className="space-y-3">
          {q.data.truncated && (
            <div role="status" className="flex gap-2 rounded-md border border-warning-border bg-warning-soft p-3 text-sm text-warning-foreground" data-testid="truncated-notice">
              <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
              <p><strong>Truncated:</strong> this neighbourhood has more than {formatNumber(q.data.limit_applied)} businesses, so only the {formatNumber(q.data.nodes.length)} strongest connections are drawn (cap {formatNumber(q.data.node_cap)}). Narrow the relationship types or depth, or use the table below for the complete list.</p>
            </div>
          )}
          <p className="text-xs text-muted-foreground" aria-live="polite" data-testid="graph-summary">{formatNumber(q.data.nodes.length)} businesses · {formatNumber(q.data.edges.length)} relationships · depth {q.data.depth_applied}</p>
          <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_20rem]">
            <GraphView graph={q.data} selected={selected} onSelect={setSelected} />
            <NodePanel graph={q.data} id={selected} onClose={() => setSelected(null)} onRecenter={(id, name) => set({ org: String(id), name: name ?? '' })} />
          </div>
        </div>
      )}

      <section aria-labelledby="rel-h" className="mt-8 space-y-2">
        <h2 id="rel-h" className="text-base font-semibold">Relationships{orgId !== null && centerName ? <span className="font-normal text-muted-foreground"> involving {centerName}</span> : null}</h2>
        <RelationshipsTable orgId={orgId} />
      </section>
    </>
  );
}

function NodePanel({ graph, id, onClose, onRecenter }: { graph: Graph; id: number | null; onClose: () => void; onRecenter: (id: number, name?: string | null) => void }) {
  const router = useRouter();
  const node = id !== null ? graph.nodes.find((n) => n.id === id) : null;
  if (!node) {
    return (
      <aside aria-label="Selected business" className="rounded-lg border border-dashed bg-card/50 p-4 text-sm text-muted-foreground">
        Select a business in the graph (click, or focus the graph and press Enter) to see its relationships here.
      </aside>
    );
  }
  const edges = graph.edges.filter((e) => e.from === node.id || e.to === node.id);
  const nameOf = (nid: number) => graph.nodes.find((n) => n.id === nid)?.name ?? `#${nid}`;
  return (
    <aside aria-labelledby="node-h" className="space-y-3 rounded-lg border bg-card p-4 shadow-card" data-testid="node-panel">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 id="node-h" className="break-words font-semibold">{node.name ?? `Business #${node.id}`}</h3>
          <p className="text-xs text-muted-foreground">{[node.city, node.country_code].filter(Boolean).join(', ') || 'Location unknown'} · {node.distance === 0 ? 'centre' : `${node.distance} hop${node.distance === 1 ? '' : 's'} away`}</p>
        </div>
        <Button size="icon-sm" variant="ghost" aria-label="Close details" onClick={onClose}><X /></Button>
      </div>
      <div className="flex flex-wrap gap-1.5">
        {node.has_confirmed_domain ? <Badge variant="success">Confirmed website</Badge> : <Badge variant="neutral">Website not confirmed</Badge>}
        {node.merged_into_id && <Badge variant="neutral">Merged into #{node.merged_into_id}</Badge>}
      </div>
      <div className="flex flex-wrap gap-2">
        <Button size="sm" onClick={() => router.push(`/businesses/${node.id}`)} data-testid="open-business"><ExternalLink /> Open business</Button>
        {node.id !== graph.center && <Button size="sm" variant="outline" onClick={() => onRecenter(node.id, node.name)}><Crosshair /> Centre here</Button>}
      </div>
      <div>
        <h4 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Relationships in view ({edges.length})</h4>
        <ul className="max-h-80 space-y-2 overflow-y-auto pr-1 text-sm">
          {edges.map((e) => {
            const m = relationMeta(e.relation);
            return (
              <li key={e.id} className="rounded-md border p-2">
                <p className="flex flex-wrap items-center gap-1 break-words">
                  <span className={e.from === node.id ? 'font-medium' : ''}>{nameOf(e.from)}</span>
                  <ArrowRight className="size-3 shrink-0" aria-hidden />
                  <Badge variant="outline" className="gap-1"><svg width="16" height="6" aria-hidden><line x1="0" y1="3" x2="16" y2="3" stroke={m.color} strokeWidth="2.5" strokeDasharray={m.dash} /></svg>{m.label}</Badge>
                  <ArrowRight className="size-3 shrink-0" aria-hidden />
                  <span className={e.to === node.id ? 'font-medium' : ''}>{nameOf(e.to)}</span>
                </p>
                <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
                  <ConfidenceMeter value={e.confidence * 100} showLabel={false} />
                  <span>{e.source_adapter}</span>
                  <span>observed {formatDate(e.observed_at)}</span>
                  {e.source_url && <SafeLink url={e.source_url} label="source" />}
                </div>
              </li>
            );
          })}
        </ul>
      </div>
      <Link href={`/businesses/${node.id}`} className="sr-only">Go to {node.name ?? `business ${node.id}`}</Link>
    </aside>
  );
}

function ExpandDialog({ orgId, name }: { orgId: number; name: string | null }) {
  const [open, setOpen] = React.useState(false);
  const [depth, setDepth] = React.useState('1');
  const [budget, setBudget] = React.useState('30');
  const [err, setErr] = React.useState<string>();
  const expand = useApiMutation({
    mutationFn: (b: Schema<'ExpandIn'>) => api.post<Schema<'ExpandOut'>>(`/api/network/organisations/${orgId}/expand`, b),
    successMessage: (r) => (r.queued ? `Network expansion queued (job #${r.job_id}, depth ${r.max_depth}, budget ${r.budget})` : 'An identical expansion was queued in the last minute'),
    errorMessage: false,
    onError: (e) => setErr(e.message),
    onSuccess: () => setOpen(false),
  });
  return (
    <Dialog open={open} onOpenChange={(o) => { setOpen(o); setErr(undefined); }}>
      <DialogTrigger asChild><Button data-testid="expand-network"><Share2 /> Expand network</Button></DialogTrigger>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Expand the network of {name ?? `business #${orgId}`}?</DialogTitle>
          <DialogDescription>Queues a bounded background job that follows links on the business’s confirmed website. Newly found companies are candidates only – their websites stay unverified until identity checks confirm them.</DialogDescription>
        </DialogHeader>
        <form className="space-y-3" onSubmit={(e) => {
          e.preventDefault();
          const d = Number(depth); const b = Number(budget);
          if (!Number.isInteger(b) || b < 1 || b > 200) { setErr('Budget must be a whole number between 1 and 200 pages.'); return; }
          setErr(undefined);
          expand.mutate({ max_depth: d, budget: b });
        }}>
          <FormField label="Maximum depth" htmlFor="exp-depth"><SimpleSelect id="exp-depth" aria-label="Maximum depth" value={depth} onValueChange={setDepth} options={[{ value: '1', label: '1 hop' }, { value: '2', label: '2 hops' }, { value: '3', label: '3 hops' }]} /></FormField>
          <FormField label="Page budget" htmlFor="exp-budget" hint="Maximum page fetches for this expansion (1–200)."><Input id="exp-budget" inputMode="numeric" value={budget} onChange={(e) => setBudget(e.target.value)} aria-describedby="exp-budget-hint" /></FormField>
          {err && <p role="alert" className="text-sm font-medium text-destructive" data-testid="expand-error">{err}</p>}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setOpen(false)}>Cancel</Button>
            <Button type="submit" loading={expand.isPending}>Queue expansion</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
