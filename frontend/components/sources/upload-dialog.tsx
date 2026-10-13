'use client';
import * as React from 'react';
import { AlertTriangle, CheckCircle2, FileUp } from 'lucide-react';
import { api, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { useApiMutation } from '@/hooks/use-api-mutation';
import { Can } from '@/lib/auth';
import { formatBytes, formatNumber } from '@/lib/format';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog';
import { KeyValueList } from '@/components/composites/key-value-list';
import { CodeBlock } from '@/components/composites/code-block';

type UploadOut = Schema<'UploadOut'>;
interface Report {
  rows_read?: number; accepted?: number; rejected?: number; duplicates?: number; formula_cells_dropped?: number;
  reasons?: Record<string, number>; sample_errors?: { row: number; reason: string }[]; columns_mapped?: Record<string, string>; columns_ignored?: string[]; encoding?: string | null;
}
const MAX_MB = 50;

export function UploadSourceDialog({ onApply }: { onApply?: (u: UploadOut) => void }) {
  const [open, setOpen] = React.useState(false);
  const [file, setFile] = React.useState<File | null>(null);
  const [clientError, setClientError] = React.useState<string>();
  const [serverError, setServerError] = React.useState<{ code: string; message: string }>();
  const [result, setResult] = React.useState<UploadOut | null>(null);
  const input = React.useRef<HTMLInputElement>(null);

  const reset = () => { setFile(null); setClientError(undefined); setServerError(undefined); setResult(null); if (input.current) input.current.value = ''; };
  const upload = useApiMutation({
    mutationFn: (f: File) => { const fd = new FormData(); fd.append('file', f); return api.post<UploadOut>('/api/sources/upload', fd); },
    errorMessage: false,
    onMutate: () => { setServerError(undefined); setResult(null); },
    onSuccess: (r) => setResult(r),
    onError: (e) => setServerError({ code: e.code, message: e.message }),
    invalidate: [qk.all('sources')],
  });

  const pick = (f: File | null) => {
    setServerError(undefined); setResult(null); setClientError(undefined); setFile(f);
    if (!f) return;
    if (!/\.(csv|xlsx)$/i.test(f.name)) setClientError('Only .csv and .xlsx files can be uploaded. The server also checks the real content, not just the extension.');
    else if (f.size > MAX_MB * 1024 * 1024) setClientError(`The file is larger than ${MAX_MB} MB.`);
  };
  const report = (result?.report ?? null) as Report | null;

  return (
    <Can role="operator">
      <Dialog open={open} onOpenChange={(o) => { setOpen(o); if (!o) reset(); }}>
        <DialogTrigger asChild><Button variant="outline"><FileUp /> Upload file</Button></DialogTrigger>
        <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-xl">
          <DialogHeader>
            <DialogTitle>Upload a source file</DialogTitle>
            <DialogDescription>
              CSV or XLSX of businesses you are entitled to use (for example a licensed list or a public register you downloaded). Only company details are read; e-mail columns are ignored by design. Formula cells are dropped and the file is never executed or served back.
            </DialogDescription>
          </DialogHeader>
          <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); if (file && !clientError) upload.mutate(file); }}>
            <div className="space-y-1.5">
              <label htmlFor="source-file" className="text-sm font-medium">File (.csv or .xlsx, up to {MAX_MB} MB)</label>
              <input ref={input} id="source-file" data-testid="source-file-input" type="file" accept=".csv,.xlsx,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                onChange={(e) => pick(e.target.files?.[0] ?? null)}
                className="block w-full cursor-pointer rounded-md border border-input bg-background text-sm file:mr-3 file:cursor-pointer file:border-0 file:bg-secondary file:px-3 file:py-2 file:text-sm file:font-medium" />
              {clientError && <p role="alert" className="text-xs font-medium text-destructive">{clientError}</p>}
            </div>
            {serverError && (
              <div role="alert" data-testid="upload-error" className="flex gap-2 rounded-md border border-danger-border bg-danger-soft p-3 text-sm text-danger-foreground">
                <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
                <div><p className="font-medium">The server rejected this file.</p><p>{serverError.message}</p></div>
              </div>
            )}
            <Button type="submit" loading={upload.isPending} disabled={!file || !!clientError}>Upload and validate</Button>
          </form>

          {result && (
            <div className="space-y-3" data-testid="upload-result">
              <p className="flex items-center gap-2 text-sm font-medium text-success-foreground"><CheckCircle2 className="size-4" aria-hidden /> File accepted ({result.kind.toUpperCase()}, {formatBytes(result.size)}).</p>
              {report && (
                <KeyValueList columns={2} items={[
                  { label: 'Rows read', value: formatNumber(report.rows_read ?? 0) },
                  { label: 'Accepted', value: formatNumber(report.accepted ?? 0) },
                  { label: 'Rejected', value: `${formatNumber(report.rejected ?? 0)}${report.reasons && Object.keys(report.reasons).length ? ` (${Object.entries(report.reasons).map(([k, v]) => `${k}: ${v}`).join('; ')})` : ''}` },
                  { label: 'Duplicates', value: formatNumber(report.duplicates ?? 0) },
                  { label: 'Formula cells dropped', value: formatNumber(report.formula_cells_dropped ?? 0) },
                  { label: 'Encoding', value: report.encoding },
                ]} />
              )}
              {report?.columns_mapped && Object.keys(report.columns_mapped).length > 0 && (
                <p className="text-xs text-muted-foreground">Mapped columns: {Object.entries(report.columns_mapped).map(([k, v]) => `${k} → ${v}`).join(', ')}{report.columns_ignored?.length ? `. Ignored: ${report.columns_ignored.join(', ')}` : ''}</p>
              )}
              {report?.sample_errors && report.sample_errors.length > 0 && (
                <div><p className="mb-1 text-xs font-medium">Sample row errors</p><CodeBlock code={report.sample_errors.slice(0, 10).map((r) => `row ${r.row}: ${r.reason}`).join('\n')} /></div>
              )}
              <div>
                <p className="mb-1 text-xs font-medium">Server path (use as the <code>path</code> in a source’s configuration)</p>
                <CodeBlock code={JSON.stringify(result.suggested_source_params, null, 2)} language="json" />
              </div>
              <DialogFooter>
                <Can role="admin">
                  {onApply && <Button onClick={() => { onApply(result); setOpen(false); reset(); }}>Use for “Imported business list” source</Button>}
                </Can>
                <Button variant="outline" onClick={() => { setOpen(false); reset(); }}>Close</Button>
              </DialogFooter>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </Can>
  );
}
