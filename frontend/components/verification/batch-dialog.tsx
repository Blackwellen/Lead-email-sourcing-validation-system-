'use client';
import * as React from 'react';
import { useQuery } from '@tanstack/react-query';
import { Upload } from 'lucide-react';
import { api, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { formatNumber } from '@/lib/format';
import { useDebounce } from '@/hooks/use-debounce';
import { useApiMutation } from '@/hooks/use-api-mutation';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { FormField } from '@/components/ui/label';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Skeleton } from '@/components/ui/skeleton';

type Mode = 'fast' | 'deep' | 'custom';
export type BatchScope = { kind: 'csv' } | { kind: 'filters'; label: string; filters: Record<string, unknown> };

const CUSTOM_OPTIONS: { key: string; label: string; hint: string }[] = [
  { key: 'syntax', label: 'Syntax', hint: 'Local, instant' }, { key: 'dns', label: 'DNS / MX', hint: 'Cached per domain' }, { key: 'flags', label: 'Disposable / free / role flags', hint: 'Local lists' },
  { key: 'provider', label: 'Mail provider classification', hint: 'Recorded on the MX check' }, { key: 'dns_diagnostics', label: 'SPF / DMARC presence', hint: 'Extra DNS queries; never affects existence' },
  { key: 'catch_all', label: 'Catch-all test', hint: 'SMTP; authorised domains only' }, { key: 'smtp', label: 'SMTP mailbox diagnostics', hint: 'SMTP; authorised domains only' },
  { key: 'delivery', label: 'Delivery history', hint: 'Uses imported feedback' }, { key: 'force', label: 'Ignore fresh stored results', hint: 'Re-runs checks even if still valid' },
];

/** Count addresses in a CSV client-side for the estimate (the server tells us how many actually exist after upload). */
async function countCsvAddresses(file: File): Promise<number> {
  const text = await file.text();
  return new Set(text.split(/\r?\n/).flatMap((l) => l.split(',')).map((c) => c.trim().toLowerCase()).filter((c) => c.includes('@'))).size;
}

export function BatchDialog({ open, onOpenChange, scope }: { open: boolean; onOpenChange: (o: boolean) => void; scope: BatchScope }) {
  const [name, setName] = React.useState('');
  const [mode, setMode] = React.useState<Mode>('fast');
  const [custom, setCustom] = React.useState<Record<string, boolean>>({ syntax: true, dns: true, flags: true });
  const [file, setFile] = React.useState<File | null>(null);
  const [fileCount, setFileCount] = React.useState<number | null>(null);
  const [fileErr, setFileErr] = React.useState<string | null>(null);
  const [result, setResult] = React.useState<Schema<'BatchUploadOut'> | Schema<'BatchOut'> | null>(null);
  const [confirmAll, setConfirmAll] = React.useState(false);
  const fileRef = React.useRef<HTMLInputElement>(null);

  React.useEffect(() => {
    if (!open) return;
    setName(`Verification ${new Date().toISOString().slice(0, 16).replace('T', ' ')}`);
    setMode('fast'); setFile(null); setFileCount(null); setFileErr(null); setResult(null); setConfirmAll(false);
  }, [open]);

  const csv = scope.kind === 'csv';
  const effectiveMode: Mode = csv && mode === 'custom' ? 'fast' : mode;
  const body = React.useMemo(() => {
    const base: Record<string, unknown> = { mode: effectiveMode };
    if (effectiveMode === 'custom') base.options = custom;
    if (csv) { if (fileCount) base.count = fileCount; }
    else base.filters = scope.filters;
    return base;
  }, [effectiveMode, custom, csv, fileCount, scope]);
  const debounced = useDebounce(body, 400);
  const canEstimate = open && (!csv || !!fileCount);
  const est = useQuery({
    queryKey: qk.custom('verification', 'estimate', debounced),
    queryFn: ({ signal }) => api.post<Schema<'VerificationEstimateOut'>>('/api/verification/estimate', debounced, { signal }),
    enabled: canEstimate, staleTime: 30_000,
  });
  const cost = est.data?.cost as { resources?: string[]; limitations?: string[]; total_note?: string } | undefined;

  const start = useApiMutation<Schema<'BatchUploadOut'> | Schema<'BatchOut'>, void>({
    mutationFn: () => {
      if (csv) {
        const fd = new FormData();
        fd.append('file', file!);
        return api.post<Schema<'BatchUploadOut'>>('/api/verification/batches/upload', fd, { query: { name: name.trim(), mode: effectiveMode as 'fast' | 'deep' } });
      }
      return api.post<Schema<'BatchOut'>>('/api/verification/batches', {
        name: name.trim(), mode: effectiveMode, options: effectiveMode === 'custom' ? custom : {}, filters: scope.kind === 'filters' ? scope.filters : undefined, confirm_all: confirmAll,
      });
    },
    errorMessage: 'Could not start the batch',
    invalidate: [qk.all('verification')],
  });

  const needsFilters = !csv && Object.keys((scope as { filters: object }).filters).length === 0;
  const canStart = !!name.trim() && (!csv || !!file) && (!needsFilters || confirmAll) && !start.isPending;
  const submit = () => start.mutateAsync().then((r) => setResult(r)).catch(() => undefined);
  const upload = result && 'batch' in result ? result : null;

  return (
    <Dialog open={open} onOpenChange={(o) => !start.isPending && onOpenChange(o)}>
      <DialogContent className="max-w-2xl">
        {result ? (
          <>
            <DialogHeader>
              <DialogTitle>Batch queued</DialogTitle>
              <DialogDescription>{'batch' in result ? result.batch.name : result.name} is queued with {formatNumber('batch' in result ? result.batch.total : result.total)} addresses. Progress appears on the Batch jobs tab.</DialogDescription>
            </DialogHeader>
            {upload && (
              <div className="rounded-md border p-3 text-sm" data-testid="upload-summary">
                <p>Rows read: <strong>{formatNumber(upload.rows_read)}</strong> · Matched in the database: <strong>{formatNumber(upload.matched)}</strong> · <span className={upload.unmatched ? 'font-semibold text-warning-foreground' : ''}>Skipped, unknown address: {formatNumber(upload.unmatched)}</span></p>
                <p className="mt-1 text-xs text-muted-foreground">Only addresses already discovered by a run can be verified. Unknown addresses are skipped, never added.</p>
                {upload.unmatched_sample.length > 0 && <p className="mt-2 text-xs">Examples (masked): <span className="font-mono">{upload.unmatched_sample.join(', ')}</span></p>}
              </div>
            )}
            <DialogFooter><Button onClick={() => onOpenChange(false)}>Done</Button></DialogFooter>
          </>
        ) : (
          <form onSubmit={(e) => { e.preventDefault(); if (canStart) submit(); }} className="grid gap-4">
            <DialogHeader>
              <DialogTitle>{csv ? 'Import a CSV to verify' : 'Start a verification batch'}</DialogTitle>
              <DialogDescription>{csv ? 'One e-mail per row (an “email” column, or the first column). Only addresses already in the database are verified.' : `Verifies the addresses matching: ${scope.label}.`}</DialogDescription>
            </DialogHeader>
            <FormField label="Batch name" htmlFor="batch-name" required><Input id="batch-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={200} /></FormField>
            {csv && (
              <FormField label="CSV file" htmlFor="batch-file" required error={fileErr ?? undefined}>
                <div className="flex flex-wrap items-center gap-2">
                  <input ref={fileRef} id="batch-file" type="file" accept=".csv,text/csv,text/plain" className="sr-only" onChange={async (e) => {
                    const f = e.target.files?.[0] ?? null; setFile(f); setFileErr(null); setFileCount(null);
                    if (f) { try { const n = await countCsvAddresses(f); if (!n) setFileErr('No e-mail addresses found in this file.'); setFileCount(n || null); } catch { setFileErr('Could not read the file.'); } }
                  }} />
                  <Button type="button" variant="outline" size="sm" onClick={() => fileRef.current?.click()}><Upload /> Choose file…</Button>
                  <span className="text-sm text-muted-foreground" data-testid="file-name">{file ? `${file.name} · ${formatNumber(fileCount ?? 0)} addresses` : 'No file chosen'}</span>
                </div>
              </FormField>
            )}
            <fieldset className="grid gap-2">
              <legend className="mb-1 text-sm font-medium">Mode</legend>
              <RadioGroup value={effectiveMode} onValueChange={(v) => setMode(v as Mode)} className="gap-2" aria-label="Verification mode">
                {([
                  ['fast', 'Fast', 'Syntax, DNS/MX and flags. No SMTP connections.'],
                  ['deep', 'Deep', 'Adds provider, catch-all, SMTP diagnostics (authorised domains only), delivery history and SPF/DMARC presence.'],
                  ['custom', 'Custom', csv ? 'Available when starting a batch from filters.' : 'Choose individual stages.'],
                ] as const).map(([v, label, hint]) => (
                  <label key={v} className="flex items-start gap-3 rounded-md border p-2.5 text-sm has-[[data-state=checked]]:border-primary">
                    <RadioGroupItem value={v} disabled={v === 'custom' && csv} className="mt-0.5" aria-label={label} />
                    <span><span className="block font-medium">{label}</span><span className="block text-xs text-muted-foreground">{hint}</span></span>
                  </label>
                ))}
              </RadioGroup>
            </fieldset>
            {effectiveMode === 'custom' && (
              <div className="grid gap-2 sm:grid-cols-2">
                {CUSTOM_OPTIONS.map((o) => (
                  <label key={o.key} className="flex items-start gap-2 text-sm"><Checkbox aria-label={o.label} className="mt-0.5" checked={!!custom[o.key]} onCheckedChange={(c) => setCustom((s) => ({ ...s, [o.key]: c === true }))} /><span>{o.label}<span className="block text-xs text-muted-foreground">{o.hint}</span></span></label>
                ))}
              </div>
            )}
            {needsFilters && (
              <label className="flex items-start gap-2 rounded-md border border-warning-border bg-warning-soft p-2.5 text-sm text-warning-foreground"><Checkbox aria-label="Verify every address" className="mt-0.5" checked={confirmAll} onCheckedChange={(c) => setConfirmAll(c === true)} />No filters are set, so this verifies <strong>every address</strong>. Tick to confirm.</label>
            )}
            <section aria-live="polite" aria-label="Cost estimate" className="rounded-md border bg-muted/30 p-3 text-sm" data-testid="estimate">
              <h3 className="mb-1 font-semibold">Cost estimate and limitations</h3>
              {!canEstimate && <p className="text-muted-foreground">Choose a file to see the estimate.</p>}
              {canEstimate && est.isFetching && !est.data && <Skeleton className="h-16 w-full" />}
              {est.isError && <p className="text-destructive">{est.error.message}</p>}
              {est.data && (
                <>
                  <p>About <strong>{est.data.addresses_capped ? '≥ ' : ''}{formatNumber(est.data.addresses)}</strong> address{est.data.addresses === 1 ? '' : 'es'}{csv ? ' in the file (not all may exist in the database)' : ''}. {cost?.total_note}</p>
                  <ul className="mt-1 list-disc space-y-0.5 pl-5 text-xs text-muted-foreground">{(cost?.resources ?? []).map((r) => <li key={r}>{r}</li>)}</ul>
                  {est.data.warnings.map((w) => <p key={w} className="mt-2 text-xs font-medium text-warning-foreground">{w}</p>)}
                  <details className="mt-2"><summary className="cursor-pointer text-xs font-medium">What this cannot tell you</summary>
                    <ul className="mt-1 list-disc space-y-0.5 pl-5 text-xs text-muted-foreground">{(cost?.limitations ?? []).map((r) => <li key={r}>{r}</li>)}</ul></details>
                </>
              )}
            </section>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => onOpenChange(false)} disabled={start.isPending}>Cancel</Button>
              <Button type="submit" loading={start.isPending} disabled={!canStart}>Start batch</Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}
