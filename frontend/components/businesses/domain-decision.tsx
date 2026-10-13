'use client';
import * as React from 'react';
import { api, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { useApiMutation } from '@/hooks/use-api-mutation';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { FormField } from '@/components/ui/label';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';

type Domain = Schema<'DomainOut'>;
export type Decision = 'confirm' | 'reject' | 'set';

/** Manual domain correction. A reason is required; the backend appends it to the domain's evidence and writes an audit entry. */
export function DomainDecisionDialog({ open, onOpenChange, businessId, domain, initial }: {
  open: boolean; onOpenChange: (o: boolean) => void; businessId: number; domain: Domain | null; initial: Decision;
}) {
  const [decision, setDecision] = React.useState<Decision>(initial);
  const [reason, setReason] = React.useState('');
  const [newDomain, setNewDomain] = React.useState('');
  const [website, setWebsite] = React.useState('');
  const [err, setErr] = React.useState<Record<string, string>>({});
  React.useEffect(() => { if (open) { setDecision(initial); setReason(''); setNewDomain(''); setWebsite(''); setErr({}); } }, [open, initial]);

  const run = useApiMutation<Domain, void>({
    mutationFn: () => api.post(`/api/businesses/${businessId}/domains/${domain?.id ?? 0}/decision`, {
      decision, note: reason.trim(), ...(decision === 'set' ? { domain: newDomain.trim(), website_url: website.trim() || undefined } : {}),
    }),
    successMessage: 'Website decision saved and recorded in the audit log',
    errorMessage: false,
    invalidate: [qk.detail('businesses', businessId), qk.all('businesses')],
    onError: (e) => setErr(Object.keys(e.fields).length ? e.fields : { form: e.message }),
  });
  const submit = () => {
    const next: Record<string, string> = {};
    if (reason.trim().length < 5) next.note = 'Explain the reason (at least 5 characters). It is stored with the evidence and the audit log.';
    if (decision === 'set' && !newDomain.trim()) next.domain = 'Enter the correct domain, e.g. example.com';
    setErr(next);
    if (Object.keys(next).length) return;
    run.mutateAsync().then(() => onOpenChange(false)).catch(() => undefined);
  };
  return (
    <Dialog open={open} onOpenChange={(o) => !run.isPending && onOpenChange(o)}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Correct website match</DialogTitle>
          <DialogDescription>{domain ? <>Current website: <span className="font-mono">{domain.domain}</span>.</> : 'This business has no website yet.'} Manual decisions override automatic matching and are audited.</DialogDescription>
        </DialogHeader>
        <RadioGroup value={decision} onValueChange={(v) => setDecision(v as Decision)} aria-label="Decision" className="gap-2">
          {([
            ['confirm', 'Confirm', 'This website belongs to the business.', !domain],
            ['reject', 'Reject', 'This website does not belong to the business (marks it as a mismatch).', !domain],
            ['set', 'Set a different domain', 'Use another domain as the confirmed website.', false],
          ] as const).map(([v, label, hint, disabled]) => (
            <label key={v} className="flex items-start gap-3 rounded-md border p-2.5 text-sm has-[[data-state=checked]]:border-primary">
              <RadioGroupItem value={v} disabled={disabled} className="mt-0.5" aria-label={label} />
              <span><span className="block font-medium">{label}</span><span className="block text-xs text-muted-foreground">{hint}</span></span>
            </label>
          ))}
        </RadioGroup>
        {decision === 'set' && (
          <div className="grid gap-3">
            <FormField label="Domain" htmlFor="dd-domain" required error={err.domain}><Input id="dd-domain" value={newDomain} onChange={(e) => setNewDomain(e.target.value)} placeholder="example.com" invalid={!!err.domain} aria-describedby={err.domain ? 'dd-domain-error' : undefined} /></FormField>
            <FormField label="Website URL (optional)" htmlFor="dd-url" error={err.website_url}><Input id="dd-url" value={website} onChange={(e) => setWebsite(e.target.value)} placeholder="https://example.com" /></FormField>
          </div>
        )}
        <FormField label="Reason" htmlFor="dd-reason" required error={err.note} hint="Required. Stored with the domain evidence and the audit log.">
          <Textarea id="dd-reason" rows={3} maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} aria-invalid={!!err.note} aria-describedby={err.note ? 'dd-reason-error' : 'dd-reason-hint'} />
        </FormField>
        {err.form && <p role="alert" className="text-sm font-medium text-destructive">{err.form}</p>}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={run.isPending}>Cancel</Button>
          <Button loading={run.isPending} onClick={submit}>Save decision</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
