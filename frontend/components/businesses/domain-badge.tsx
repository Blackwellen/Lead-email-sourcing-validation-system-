import { StatusBadge } from '@/components/composites/status-badge';

/** Domain-match status. The backend uses confirmed | high | review | mismatch | unknown. */
export function DomainBadge({ status }: { status: string | null | undefined }) {
  if (!status) return <span className="text-xs text-muted-foreground">No website</span>;
  if (status === 'review') return <StatusBadge kind="domain" status="probable" label="Needs review" />;
  return <StatusBadge kind="domain" status={status} />;
}
export const DOMAIN_STATUS_OPTIONS = [
  { value: 'confirmed', label: 'Confirmed' }, { value: 'high', label: 'High match' }, { value: 'review', label: 'Needs review' },
  { value: 'mismatch', label: 'Mismatch' }, { value: 'unknown', label: 'Unverified' }, { value: 'none', label: 'No website' },
];
