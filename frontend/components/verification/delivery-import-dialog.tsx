'use client';
import * as React from 'react';
import { Upload } from 'lucide-react';
import { api, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { formatNumber } from '@/lib/format';
import { useApiMutation } from '@/hooks/use-api-mutation';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';

export function DeliveryImportDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const [file, setFile] = React.useState<File | null>(null);
  const [res, setRes] = React.useState<Schema<'DeliveryImportOut'> | null>(null);
  const ref = React.useRef<HTMLInputElement>(null);
  React.useEffect(() => { if (open) { setFile(null); setRes(null); } }, [open]);
  const run = useApiMutation<Schema<'DeliveryImportOut'>, void>({
    mutationFn: () => { const fd = new FormData(); fd.append('file', file!); return api.post('/api/verification/delivery-events', fd); },
    invalidate: [qk.all('verification'), qk.all('contacts')], errorMessage: 'Import failed',
  });
  return (
    <Dialog open={open} onOpenChange={(o) => !run.isPending && onOpenChange(o)}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Import delivery events</DialogTitle>
          <DialogDescription>Feedback exported from your sending platform. CSV columns: <code>email,event_type,campaign,occurred_at,detail</code> (or a JSON list). Hard bounces and complaints suppress or downgrade the address.</DialogDescription>
        </DialogHeader>
        {res ? (
          <dl className="grid grid-cols-2 gap-2 text-sm" data-testid="delivery-summary">
            {([['Received', res.received], ['Inserted', res.inserted], ['Duplicates', res.duplicates], ['Unknown address (skipped)', res.unknown_email], ['Invalid rows', res.invalid], ['Suppressions added', res.suppressions_added], ['Addresses affected', res.emails_affected]] as const).map(([k, v]) => (
              <div key={k} className="rounded-md border p-2"><dt className="text-xs text-muted-foreground">{k}</dt><dd className="tabular font-semibold">{formatNumber(v)}</dd></div>
            ))}
            {res.errors.length > 0 && <div className="col-span-2 text-xs text-destructive"><p className="font-medium">First errors</p><ul className="list-disc pl-5">{res.errors.slice(0, 5).map((e) => <li key={e}>{e}</li>)}</ul></div>}
          </dl>
        ) : (
          <div className="flex flex-wrap items-center gap-2">
            <input ref={ref} type="file" accept=".csv,.json,text/csv,application/json,text/plain" className="sr-only" aria-label="Delivery events file" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
            <Button type="button" variant="outline" size="sm" onClick={() => ref.current?.click()}><Upload /> Choose file…</Button>
            <span className="text-sm text-muted-foreground">{file ? file.name : 'No file chosen'}</span>
          </div>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={run.isPending}>{res ? 'Close' : 'Cancel'}</Button>
          {!res && <Button loading={run.isPending} disabled={!file} onClick={() => run.mutateAsync().then(setRes).catch(() => undefined)}>Import events</Button>}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
