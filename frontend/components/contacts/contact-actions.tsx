'use client';
import * as React from 'react';
import { api, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { useApiMutation } from '@/hooks/use-api-mutation';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { FormField } from '@/components/ui/label';
import { SimpleSelect } from '@/components/ui/select';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { SUPPRESS_REASONS } from './shared';

type Reason = 'opt_out' | 'objection' | 'bounce' | 'complaint' | 'manual' | 'legal';
type Scope = 'email' | 'domain' | 'company';

const INVALIDATE = [qk.all('contacts'), qk.all('verification'), qk.all('businesses'), qk.all('categories')];

export function useContactMutations() {
  const revalidate = useApiMutation<Schema<'Ok'>, { id: number; mode?: 'fast' | 'deep' }>({
    mutationFn: ({ id, mode = 'fast' }) => api.post(`/api/contacts/${id}/revalidate`, { mode }),
    successMessage: (d) => d.message ?? 'Re-validation queued',
    invalidate: INVALIDATE,
  });
  const needsReview = useApiMutation<Schema<'Ok'>, { id: number; value: boolean }>({
    mutationFn: ({ id, value }) => api.post(`/api/contacts/${id}/needs-review`, { value }),
    successMessage: (_d, v) => (v.value ? 'Marked as needs review' : 'Needs-review flag cleared'),
    invalidate: INVALIDATE,
  });
  const suppress = useApiMutation<Schema<'SuppressionResult'>, { id: number; scope: Scope; reason: Reason; note?: string }>({
    mutationFn: ({ id, ...body }) => api.post(`/api/contacts/${id}/suppress`, body),
    successMessage: (d) => `Suppressed (${d.scope}); ${d.emails_affected} address${d.emails_affected === 1 ? '' : 'es'} affected`,
    invalidate: INVALIDATE,
  });
  const unsuppress = useApiMutation<Schema<'SuppressionResult'>, { id: number; scope: Scope }>({
    mutationFn: ({ id, scope }) => api.post(`/api/contacts/${id}/unsuppress`, { scope }),
    successMessage: (d) => `Suppression removed; ${d.emails_affected} address${d.emails_affected === 1 ? '' : 'es'} released`,
    invalidate: INVALIDATE,
  });
  return { revalidate, needsReview, suppress, unsuppress };
}

/** Suppress confirmation: reason is required, scope defaults to the single address. `ids` => bulk (address scope only). */
export function SuppressDialog({ open, onOpenChange, target, onDone }: {
  open: boolean; onOpenChange: (o: boolean) => void;
  target: { ids: number[]; label: string } | null;
  onDone?: () => void;
}) {
  const [reason, setReason] = React.useState<Reason>('manual');
  const [scope, setScope] = React.useState<Scope>('email');
  const [note, setNote] = React.useState('');
  const single = target?.ids.length === 1;
  React.useEffect(() => { if (open) { setReason('manual'); setScope('email'); setNote(''); } }, [open]);
  const { suppress } = useContactMutations();
  const bulk = useApiMutation<Schema<'ContactBulkOut'>, void>({
    mutationFn: () => api.post('/api/contacts/bulk', { action: 'suppress', ids: target?.ids ?? [], reason, note: note || undefined }),
    successMessage: (d) => `${d.affected} address${d.affected === 1 ? '' : 'es'} suppressed`,
    invalidate: INVALIDATE,
  });
  const busy = suppress.isPending || bulk.isPending;
  const submit = async () => {
    if (!target) return;
    if (single) await suppress.mutateAsync({ id: target.ids[0], scope, reason, note: note || undefined });
    else await bulk.mutateAsync();
    onOpenChange(false);
    onDone?.();
  };
  return (
    <Dialog open={open} onOpenChange={(o) => !busy && onOpenChange(o)}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Suppress {target ? target.label : ''}?</DialogTitle>
          <DialogDescription>Suppressed addresses are excluded from marketing exports. This is recorded in the audit log and can be reversed with “Unsuppress”.</DialogDescription>
        </DialogHeader>
        <div className="grid gap-3">
          {single && (
            <FormField label="Scope" htmlFor="sup-scope" hint={scope === 'email' ? 'Only this address.' : scope === 'domain' ? 'Every address on this registrable domain.' : 'Every address of this business.'}>
              <SimpleSelect id="sup-scope" aria-label="Scope" value={scope} onValueChange={(v) => setScope(v as Scope)}
                options={[{ value: 'email', label: 'This address only' }, { value: 'domain', label: 'Whole domain' }, { value: 'company', label: 'Whole business' }]} />
            </FormField>
          )}
          <FormField label="Reason" htmlFor="sup-reason" required>
            <SimpleSelect id="sup-reason" aria-label="Reason" value={reason} onValueChange={(v) => setReason(v as Reason)} options={SUPPRESS_REASONS} />
          </FormField>
          <FormField label="Note" htmlFor="sup-note" hint="Optional, kept with the suppression entry.">
            <Textarea id="sup-note" value={note} onChange={(e) => setNote(e.target.value)} maxLength={1000} rows={3} />
          </FormField>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={busy}>Cancel</Button>
          <Button variant="destructive" loading={busy} onClick={() => submit().catch(() => undefined)}>Suppress</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
