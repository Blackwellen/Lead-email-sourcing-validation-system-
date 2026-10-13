'use client';
import * as React from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import type { Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { listFetcher } from '@/hooks/use-paged-query';
import { formatDate } from '@/lib/format';
import { DataTable, createColumnHelper } from '@/components/composites/data-table';
import { FilterBar, FilterSelect, type FilterChip } from '@/components/composites/filter-bar';
import { ConfidenceMeter } from '@/components/composites/confidence-meter';
import { Badge } from '@/components/ui/badge';
import { SafeLink } from '@/components/contacts/shared';
import { DebouncedTextFilter, RELATIONS, relationMeta } from '../shared';

type Rel = Schema<'RelationshipOut'>;
const col = createColumnHelper<Rel>();
const fetchPage = listFetcher<Rel>('/api/network/relationships');

export function RelationBadge({ relation }: { relation: string }) {
  const m = relationMeta(relation);
  return (
    <Badge variant="outline" className="gap-1 whitespace-nowrap">
      <svg width="16" height="6" aria-hidden><line x1="0" y1="3" x2="16" y2="3" stroke={m.color} strokeWidth="2.5" strokeDasharray={m.dash} /></svg>
      {m.label}
    </Badge>
  );
}

const columns = [
  col.accessor('from_name', { header: 'Business', enableSorting: false, cell: (i) => <Link className="text-primary underline-offset-2 hover:underline" href={`/businesses/${i.row.original.from_org_id}`}>{i.getValue() ?? `#${i.row.original.from_org_id}`}</Link> }),
  col.accessor('relation', { header: 'Relationship', enableSorting: false, cell: (i) => <RelationBadge relation={i.getValue()} /> }),
  col.accessor('to_name', { header: 'Related business', enableSorting: false, cell: (i) => <Link className="text-primary underline-offset-2 hover:underline" href={`/businesses/${i.row.original.to_org_id}`}>{i.getValue() ?? `#${i.row.original.to_org_id}`}</Link> }),
  col.accessor('confidence', { header: 'Confidence', enableSorting: false, cell: (i) => <ConfidenceMeter value={i.getValue() * 100} showLabel={false} /> }),
  col.accessor('depth', { header: 'Depth', enableSorting: false, meta: { hideBelow: 'md', className: 'tabular' } }),
  col.accessor('source_adapter', { header: 'Source', enableSorting: false, meta: { hideBelow: 'md' }, cell: (i) => <span className="text-xs">{i.getValue()}{i.row.original.source_url && <> · <SafeLink url={i.row.original.source_url} label="link" /></>}</span> }),
  col.accessor('observed_at', { header: 'Observed', enableSorting: false, meta: { hideBelow: 'lg' }, cell: (i) => formatDate(i.getValue()) }),
];

export function RelationshipsTable({ orgId }: { orgId: number | null }) {
  const router = useRouter();
  const [relation, setRelation] = React.useState<string | undefined>();
  const [adapter, setAdapter] = React.useState('');
  const [minConf, setMinConf] = React.useState<string | undefined>();
  const filters = React.useMemo(() => ({ org_id: orgId ?? undefined, relation, source_adapter: adapter || undefined, min_confidence: minConf }), [orgId, relation, adapter, minConf]);
  const chips: FilterChip[] = [];
  if (relation) chips.push({ id: 'rel', label: `Relationship: ${relationMeta(relation).label}`, onRemove: () => setRelation(undefined) });
  if (adapter) chips.push({ id: 'src', label: `Source: ${adapter}`, onRemove: () => setAdapter('') });
  if (minConf) chips.push({ id: 'conf', label: `Confidence ≥ ${Math.round(Number(minConf) * 100)}`, onRemove: () => setMinConf(undefined) });
  return (
    <>
      <FilterBar chips={chips} onClearAll={() => { setRelation(undefined); setAdapter(''); setMinConf(undefined); }}>
        <FilterSelect label="Relationship" value={relation} onChange={setRelation} options={RELATIONS.map((r) => ({ value: r, label: relationMeta(r).label }))} />
        <FilterSelect label="Min confidence" value={minConf} onChange={setMinConf} allLabel="Any" options={[{ value: '0.5', label: '50+' }, { value: '0.7', label: '70+' }, { value: '0.9', label: '90+' }]} />
        <DebouncedTextFilter label="Source adapter" placeholder="Source adapter (exact)" value={adapter} onChange={setAdapter} />
      </FilterBar>
      <DataTable caption="Business relationships" tableId="network-relationships" columns={columns} queryKey={qk.list('network-relationships')} fetchPage={fetchPage} filters={filters}
        defaultPageSize={50} onRowOpen={(r) => router.push(`/businesses/${r.from_org_id}`)} maxHeightClass="max-h-[60vh]" />
    </>
  );
}
