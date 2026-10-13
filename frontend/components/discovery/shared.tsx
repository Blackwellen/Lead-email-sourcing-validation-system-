'use client';
import * as React from 'react';
import { useQuery } from '@tanstack/react-query';
import { CircleCheck, CircleDashed, CircleHelp, CircleMinus, Loader2, type LucideIcon } from 'lucide-react';
import { api, type Page, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { useDebounce } from '@/hooks/use-debounce';
import { Badge, type BadgeVariant } from '@/components/ui/badge';
import { Combobox, type ComboboxOption } from '@/components/ui/combobox';
import { Tooltip } from '@/components/ui/tooltip';
import { formatNumber, titleCase } from '@/lib/format';
import { cn } from '@/lib/utils';

/* ------------------------------------------------------------------ "not enough data" */
/** Rendered wherever the API returns `null` for a measured ratio. We never substitute an estimate. */
export function NotEnoughData({ minSample, className }: { minSample?: number; className?: string }) {
  const tip = minSample ? `Fewer than ${minSample} samples have been measured, so this ratio is not computed.` : 'Not enough measured data to compute this value.';
  return (
    <Tooltip content={tip}>
      <span className={cn('inline-flex items-center gap-1 whitespace-nowrap text-xs text-muted-foreground', className)} data-testid="not-enough-data" tabIndex={0}>
        <CircleDashed className="size-3" aria-hidden />—&nbsp;not enough data
      </span>
    </Tooltip>
  );
}

/** Number or "— not enough data" (for null). */
export function Measured({ value, digits = 1, minSample, suffix }: { value: number | null | undefined; digits?: number; minSample?: number; suffix?: string }) {
  if (value === null || value === undefined || Number.isNaN(value)) return <NotEnoughData minSample={minSample} />;
  const shown = Number.isInteger(value) ? formatNumber(value) : value.toLocaleString('en-US', { maximumFractionDigits: digits });
  return <span className="tabular">{shown}{suffix}</span>;
}

/* ------------------------------------------------------------------ completeness tri-state */
export type TriState = 'yes' | 'no' | 'unknown' | 'in_progress';
interface TriMeta { label: string; variant: BadgeVariant; Icon: LucideIcon; spin?: boolean; help: string }
/**
 * Four DISTINCT states. "unknown" (research not performed) is deliberately neutral – it must never look like "no"
 * (research performed, genuinely unavailable).
 */
export const TRI: Record<TriState, TriMeta> = {
  yes: { label: 'Yes', variant: 'success', Icon: CircleCheck, help: 'Established from stored evidence' },
  no: { label: 'No – researched', variant: 'warning', Icon: CircleMinus, help: 'The research step WAS performed and this is genuinely unavailable' },
  unknown: { label: 'Not researched yet', variant: 'neutral', Icon: CircleHelp, help: 'The research step has not been performed (or produced nothing conclusive)' },
  in_progress: { label: 'In progress', variant: 'info', Icon: Loader2, spin: true, help: 'Work is queued or running, or a candidate awaits review' },
};
export function triMeta(state: string | null | undefined): TriMeta {
  return TRI[(state ?? 'unknown') as TriState] ?? { ...TRI.unknown, label: titleCase(state ?? 'unknown') };
}
export function TriStateBadge({ state, className }: { state: string | null | undefined; className?: string }) {
  const m = triMeta(state);
  return (
    <Badge variant={m.variant} className={className} title={m.help} data-state={state ?? 'unknown'} data-testid="tri-state">
      <m.Icon aria-hidden className={cn(m.spin && 'animate-spin motion-reduce:animate-none')} />
      <span>{m.label}</span>
    </Badge>
  );
}

export const FLAG_LABELS: Record<string, string> = {
  official_website: 'Official website',
  contact_page_inspected: 'Contact page inspected',
  published_email: 'Published e-mail',
  contact_person_corroborated: 'Named contact corroborated',
  email_infrastructure_checked: 'E-mail infrastructure checked',
  recipient_evidence: 'Recipient evidence',
  campaign_eligibility: 'Campaign eligibility',
  recently_checked: 'Recently checked',
};
export const flagLabel = (k: string) => FLAG_LABELS[k] ?? titleCase(k);

export const OPPORTUNITY_LABELS: Record<string, { label: string; variant: BadgeVariant }> = {
  incomplete_research: { label: 'Incomplete research', variant: 'info' },
  genuinely_unavailable: { label: 'Genuinely unavailable', variant: 'neutral' },
  in_progress: { label: 'In progress', variant: 'info' },
  stale: { label: 'Stale', variant: 'warning' },
};

/* ------------------------------------------------------------------ relations (network) */
/** Colour + dash pattern + short code per relation type, so the legend never relies on colour alone. */
export const RELATION_META: Record<string, { label: string; color: string; dash?: string; code: string }> = {
  branch_of: { label: 'Branch of', color: 'hsl(var(--chart-1))', code: 'BR' },
  brand_of: { label: 'Brand of', color: 'hsl(var(--chart-2))', dash: '6 3', code: 'BD' },
  subsidiary_of: { label: 'Subsidiary of', color: 'hsl(var(--chart-3))', code: 'SU' },
  supplier_of: { label: 'Supplier of', color: 'hsl(var(--chart-4))', dash: '2 3', code: 'SP' },
  partner_of: { label: 'Partner of', color: 'hsl(var(--chart-5))', dash: '8 3 2 3', code: 'PA' },
  franchise_of: { label: 'Franchise of', color: 'hsl(var(--chart-6))', dash: '4 4', code: 'FR' },
  member_of_group: { label: 'Member of group', color: 'hsl(var(--chart-7))', code: 'MG' },
  exhibits_with: { label: 'Exhibits with', color: 'hsl(var(--chart-8))', dash: '1 3', code: 'EX' },
  awarded_by: { label: 'Awarded by', color: 'hsl(var(--chart-1))', dash: '10 4', code: 'AW' },
  same_group: { label: 'Same group', color: 'hsl(var(--chart-3))', dash: '3 2 1 2', code: 'SG' },
};
export const RELATIONS = Object.keys(RELATION_META);
export const relationMeta = (r: string) => RELATION_META[r] ?? { label: titleCase(r), color: 'hsl(var(--muted-foreground))', code: '??' };

/* ------------------------------------------------------------------ signal types */
export const SIGNAL_TYPES = ['new_location', 'new_service', 'expansion', 'hiring', 'contract_award', 'supplier_partnership', 'event', 'accreditation',
  'new_registration', 'exhibitor', 'sponsor', 'registry_change'] as const;

/* ------------------------------------------------------------------ organisation search */
type BizRow = Schema<'BusinessRow'>;
export const orgName = (r: { legal_name?: string | null; trading_name?: string | null; id: number }) => r.legal_name ?? r.trading_name ?? `Business #${r.id}`;

/** Server-side searchable organisation combobox (prefix search on name / domain via /api/businesses?q=). */
export function OrgCombobox({ value, onChange, id, 'aria-label': ariaLabel, placeholder = 'Search businesses…', selectedLabel, className }: {
  value: number | null; onChange: (id: number | null, label?: string) => void; id?: string; 'aria-label'?: string; placeholder?: string; selectedLabel?: string | null; className?: string;
}) {
  const [search, setSearch] = React.useState('');
  const q = useDebounce(search, 250);
  const res = useQuery({
    queryKey: qk.custom('businesses', 'org-search', { q }),
    queryFn: ({ signal }) => api.get<Page<BizRow>>('/api/businesses', { query: { q: q || undefined, limit: 20 }, signal }),
    staleTime: 30_000,
  });
  const options: ComboboxOption[] = (res.data?.items ?? []).map((r) => ({ value: String(r.id), label: orgName(r), description: [r.domain, r.city, r.country_code].filter(Boolean).join(' · ') }));
  if (value && !options.some((o) => o.value === String(value))) options.unshift({ value: String(value), label: selectedLabel ?? `Business #${value}` });
  return (
    <Combobox id={id} aria-label={ariaLabel} className={className} placeholder={placeholder} searchPlaceholder="Type a company name or domain…" emptyText="No matching businesses"
      options={options} loading={res.isFetching} onSearchChange={setSearch} value={value ? String(value) : null} clearable
      onChange={(v, o) => onChange(v ? Number(v) : null, o?.label)} />
  );
}

/** Small labelled section heading used across the discovery pages. */
export function SectionTitle({ id, children, description }: { id: string; children: React.ReactNode; description?: React.ReactNode }) {
  return (
    <div className="mb-3 space-y-0.5">
      <h2 id={id} className="text-base font-semibold">{children}</h2>
      {description && <p className="text-sm text-muted-foreground">{description}</p>}
    </div>
  );
}

/** Text filter that debounces before reporting (for secondary text filters next to FilterBar's main search). */
export function DebouncedTextFilter({ label, value, onChange, placeholder, className, id }: {
  label: string; value: string; onChange: (v: string) => void; placeholder?: string; className?: string; id?: string;
}) {
  const [text, setText] = React.useState(value);
  const debounced = useDebounce(text, 300);
  const cb = React.useRef(onChange);
  React.useEffect(() => { cb.current = onChange; });
  const last = React.useRef(value);
  React.useEffect(() => { if (debounced !== last.current) { last.current = debounced; cb.current(debounced); } }, [debounced]);
  React.useEffect(() => { if (value !== last.current) { last.current = value; setText(value); } }, [value]);
  return (
    <input id={id} type="text" aria-label={label} placeholder={placeholder ?? label} value={text} onChange={(e) => setText(e.target.value)}
      className={cn('flex h-9 w-full min-w-0 rounded-md border border-input bg-background px-3 text-sm shadow-card placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring sm:w-44', className)} />
  );
}
