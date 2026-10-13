'use client';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Eye } from 'lucide-react';
import { api, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { Can, useMe } from '@/lib/auth';
import { toast } from '@/lib/toast';
import { PageHeader } from '@/components/composites/page-header';
import { LoadingState } from '@/components/composites/loading-state';
import { ErrorState } from '@/components/composites/error-state';
import { ExportBuilder } from './export-builder';
import { ExportHistory } from './export-history';

export function ExportsPage() {
  const me = useMe();
  const qc = useQueryClient();
  const presets = useQuery({ queryKey: qk.custom('exports', 'presets'), queryFn: ({ signal }) => api.get<Schema<'PresetList'>>('/api/exports/presets', { signal }), staleTime: 10 * 60_000 });
  return (
    <>
      <PageHeader title="Exports" description="Build a filtered contact file, then download it. Files are generated on the server and downloads require your signed-in session." />
      {presets.isPending ? <LoadingState variant="skeleton" rows={6} label="Loading export presets" /> : presets.isError ? <ErrorState error={presets.error} onRetry={() => presets.refetch()} /> : (
        <>
          <Can role="operator" fallback={me.isPending ? null : (
            <div role="note" className="mb-6 flex gap-2 rounded-md border bg-muted/40 p-3 text-sm text-muted-foreground">
              <Eye className="mt-0.5 size-4 shrink-0" aria-hidden />
              <p>Your role is read-only. Operators and administrators can create and delete exports; you can download exports you created.</p>
            </div>
          )}>
            <ExportBuilder presets={presets.data} onCreated={(e) => {
              qc.invalidateQueries({ queryKey: qk.all('exports') });
              if (e.status === 'done') toast.success('Export ready', `${e.row_count} rows · you can download it from the history below.`);
              else if (e.status === 'failed') toast.error('Export failed', e.error ?? undefined);
              else toast.info('Export queued', 'It is running in the background; the history updates automatically.');
            }} />
          </Can>
          <ExportHistory presets={presets.data.presets} />
        </>
      )}
    </>
  );
}
