'use client';
import * as React from 'react';
import { useQuery } from '@tanstack/react-query';
import { AtSign, ShieldQuestion, ShieldCheck, ShieldX } from 'lucide-react';
import { api, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { Badge, type BadgeVariant } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { FormField } from '@/components/ui/label';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import type { ComboboxOption } from '@/components/ui/combobox';
import { titleCase } from '@/lib/format';

/* ------------------------------------------------------------------ constants */
export const COUNTRY_OPTIONS: ComboboxOption[] = [
  ['GB', 'United Kingdom'], ['IE', 'Ireland'], ['DE', 'Germany'], ['FR', 'France'], ['NL', 'Netherlands'], ['BE', 'Belgium'], ['ES', 'Spain'], ['PT', 'Portugal'],
  ['IT', 'Italy'], ['AT', 'Austria'], ['CH', 'Switzerland'], ['DK', 'Denmark'], ['SE', 'Sweden'], ['NO', 'Norway'], ['FI', 'Finland'], ['PL', 'Poland'],
  ['CZ', 'Czechia'], ['US', 'United States'], ['CA', 'Canada'], ['AU', 'Australia'], ['NZ', 'New Zealand'],
].map(([value, label]) => ({ value, label: `${label} (${value})` }));

export const REACHABILITY_OPTIONS = [
  { value: 'safe', label: 'Safe (no adverse evidence)' }, { value: 'risky', label: 'Risky' }, { value: 'invalid', label: 'Invalid' }, { value: 'unknown', label: 'Unknown' },
];
export const SYNTAX_OPTIONS = [{ value: 'valid', label: 'Syntax OK' }, { value: 'invalid', label: 'Bad syntax' }, { value: 'unknown', label: 'Unchecked' }];
export const MAIL_OPTIONS = [
  { value: 'accepts_mail', label: 'Accepts mail' }, { value: 'no_mail_service', label: 'No mail service' }, { value: 'null_mx', label: 'Null MX' },
  { value: 'temporary_dns_failure', label: 'DNS failure' }, { value: 'unknown', label: 'Unknown' },
];
export const RECIPIENT_OPTIONS = [
  { value: 'untested', label: 'Not tested' }, { value: 'smtp_recipient_accepted', label: 'Mailbox accepted (diagnostic)' },
  { value: 'smtp_recipient_rejected', label: 'Mailbox rejected' }, { value: 'inconclusive', label: 'Inconclusive' },
];
export const CATCHALL_OPTIONS = [
  { value: 'confirmed', label: 'Catch-all' }, { value: 'suspected', label: 'Catch-all suspected' }, { value: 'rejected_unknown_recipient', label: 'Rejects unknown' },
  { value: 'unknown', label: 'Unknown' }, { value: 'untested', label: 'Untested' },
];
export const MARKETING_OPTIONS = [
  { value: 'eligible', label: 'Eligible' }, { value: 'needs_review', label: 'Needs review' }, { value: 'consent_required', label: 'Consent required' },
  { value: 'opted_out', label: 'Opted out' }, { value: 'suppressed', label: 'Suppressed' }, { value: 'not_permitted', label: 'Not permitted' },
];
export const ORIGIN_OPTIONS = [
  { value: 'published', label: 'Published (incl. corroborated)' }, { value: 'inferred', label: 'Inferred (pattern / role guess)' },
  { value: 'corroborated', label: 'Corroborated only' }, { value: 'pattern_inferred', label: 'Pattern inferred only' }, { value: 'role_speculative', label: 'Role speculative only' },
];
export const SUPPRESS_REASONS = [
  { value: 'manual', label: 'Manual decision' }, { value: 'opt_out', label: 'Opt-out request' }, { value: 'objection', label: 'Objection' },
  { value: 'bounce', label: 'Bounce' }, { value: 'complaint', label: 'Complaint' }, { value: 'legal', label: 'Legal' },
];

/* ------------------------------------------------------------------ status bits */
const RECIPIENT: Record<string, { label: string; variant: BadgeVariant; Icon: typeof AtSign; hint: string }> = {
  untested: { label: 'Not tested', variant: 'neutral', Icon: ShieldQuestion, hint: 'The mailbox itself has not been tested' },
  smtp_recipient_accepted: { label: 'Mailbox accepted', variant: 'info', Icon: ShieldCheck, hint: 'The mail server accepted the recipient during diagnostics. Evidence only; not a delivery guarantee.' },
  smtp_recipient_rejected: { label: 'Mailbox rejected', variant: 'danger', Icon: ShieldX, hint: 'The mail server rejected the recipient during diagnostics' },
  inconclusive: { label: 'Inconclusive', variant: 'warning', Icon: ShieldQuestion, hint: 'The server gave no reliable answer' },
};
/** Recipient (mailbox) evidence. Deliberately never green: acceptance at SMTP time is a diagnostic, not proof of delivery. */
export function RecipientBadge({ status }: { status: string | null | undefined }) {
  const m = RECIPIENT[status ?? 'untested'] ?? { label: titleCase(status ?? 'unknown'), variant: 'neutral' as BadgeVariant, Icon: ShieldQuestion, hint: '' };
  return <Badge variant={m.variant} title={m.hint}><m.Icon aria-hidden /><span>{m.label}</span></Badge>;
}

/** Link only for http(s) URLs; everything else is rendered as inert plain text. */
export function safeHref(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const u = new URL(url);
    return u.protocol === 'http:' || u.protocol === 'https:' ? u.toString() : null;
  } catch { return null; }
}
export function SafeLink({ url, label, className }: { url: string | null | undefined; label?: string; className?: string }) {
  if (!url) return <span className="text-muted-foreground">—</span>;
  const href = safeHref(url);
  const text = label ?? url;
  if (!href) return <span className={className} title="Not an http(s) URL, shown as text only">{text}</span>;
  return <a href={href} target="_blank" rel="noopener noreferrer" className={`break-all text-primary underline-offset-2 hover:underline ${className ?? ''}`}>{text}</a>;
}

/* ------------------------------------------------------------------ data helpers */
export type CategoryNode = Schema<'CategoryNode'>;
export function flattenCategories(nodes: CategoryNode[], depth = 0, prefix = ''): { node: CategoryNode; depth: number; path: string }[] {
  return nodes.flatMap((n) => {
    const path = prefix ? `${prefix} › ${n.name}` : n.name;
    return [{ node: n, depth, path }, ...flattenCategories(n.children ?? [], depth + 1, path)];
  });
}
export function useCategoryTree() {
  return useQuery({ queryKey: qk.custom('categories', 'tree'), queryFn: ({ signal }) => api.get<CategoryNode[]>('/api/categories', { signal }), staleTime: 30_000 });
}
export function useCategoryOptions(opts: { enabledOnly?: boolean } = {}) {
  const q = useCategoryTree();
  const options = React.useMemo<ComboboxOption[]>(
    () => flattenCategories(q.data ?? []).filter((f) => !opts.enabledOnly || f.node.enabled).map((f) => ({ value: String(f.node.id), label: f.path })),
    [q.data, opts.enabledOnly],
  );
  return { ...q, options };
}
export function useSourceOptions() {
  const q = useQuery({ queryKey: qk.custom('sources', 'options'), queryFn: ({ signal }) => api.get<{ adapter_id: string; name: string }[]>('/api/sources', { signal }), staleTime: 300_000 });
  const options = React.useMemo<ComboboxOption[]>(() => (q.data ?? []).map((s) => ({ value: s.adapter_id, label: s.adapter_id, description: s.name })), [q.data]);
  return { ...q, options };
}

/* ------------------------------------------------------------------ saved view dialog */
export function SaveViewDialog({ open, onOpenChange, onSave }: { open: boolean; onOpenChange: (o: boolean) => void; onSave: (name: string) => void }) {
  const [name, setName] = React.useState('');
  React.useEffect(() => { if (open) setName(''); }, [open]);
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <form onSubmit={(e) => { e.preventDefault(); if (name.trim()) { onSave(name.trim()); onOpenChange(false); } }} className="grid gap-4">
          <DialogHeader>
            <DialogTitle>Save current filters as a view</DialogTitle>
            <DialogDescription>Views are stored in this browser only.</DialogDescription>
          </DialogHeader>
          <FormField label="View name" htmlFor="view-name" required>
            <Input id="view-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={60} autoFocus />
          </FormField>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" disabled={!name.trim()}>Save view</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** Drop empty values and the reserved UI-only keys so the object can be sent as query params / saved. */
export function activeFilters<T extends Record<string, string | string[] | undefined>>(f: T): Partial<T> {
  const out: Record<string, string | string[]> = {};
  for (const [k, v] of Object.entries(f)) {
    if (v === undefined || v === '' || (Array.isArray(v) && v.length === 0)) continue;
    out[k] = v;
  }
  return out as Partial<T>;
}

/** Text/number/date input that commits on blur or Enter and re-syncs when the committed value changes (e.g. "Clear all"). */
export function CommitInput({ id, value, onCommit, ...rest }: { id: string; value: string; onCommit: (v: string) => void } & Omit<React.InputHTMLAttributes<HTMLInputElement>, 'value' | 'onChange' | 'id'>) {
  const [v, setV] = React.useState(value);
  React.useEffect(() => setV(value), [value]);
  const commit = () => { if (v.trim() !== value) onCommit(v.trim()); };
  return <Input id={id} value={v} onChange={(e) => setV(e.target.value)} onBlur={commit} onKeyDown={(e) => { if (e.key === 'Enter') commit(); }} {...rest} />;
}
