'use client';
import * as React from 'react';
import { Trash2 } from 'lucide-react';
import { api, type Schema } from '@/lib/api';
import { useApiMutation } from '@/hooks/use-api-mutation';
import { Can } from '@/lib/auth';
import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/alert-dialog';
import { Separator } from '@/components/ui/separator';
import { KeyValueList } from '@/components/composites/key-value-list';

export function RetentionRunner() {
  const run = useApiMutation({
    mutationFn: () => api.post<Schema<'RetentionResult'>>('/api/settings/retention/run'),
    successMessage: 'Retention job finished',
  });
  const counts = (run.data?.counts ?? {}) as Record<string, unknown>;
  return (
    <Can role="admin">
      <Separator className="my-6" />
      <section aria-labelledby="ret-run" className="space-y-3">
        <h3 id="ret-run" className="text-sm font-semibold">Run retention now</h3>
        <p className="text-sm text-muted-foreground">Retention normally runs on its own schedule. Running it now applies the <strong>saved</strong> settings above (save first if you changed them) and permanently deletes everything older than the limits.</p>
        <ConfirmDialog
          trigger={<Button variant="destructive" loading={run.isPending} data-testid="run-retention"><Trash2 /> Run retention now</Button>}
          title="Run the retention job now?" destructive confirmLabel="Delete expired data"
          description={<p>Data older than the saved retention limits (evidence, validation history, export files, audit entries) will be <strong>permanently deleted</strong>. This cannot be undone and is not covered by an automatic backup, so make sure you have a recent backup if the limits are new or shorter than before.</p>}
          onConfirm={() => run.mutateAsync()}
        />
        {run.data && (
          <div data-testid="retention-result" aria-live="polite">
            <p className="mb-2 text-sm font-medium">Deleted in this run</p>
            <KeyValueList columns={3} items={Object.entries(counts).map(([k, v]) => ({ label: k.replaceAll('_', ' '), value: typeof v === 'object' ? JSON.stringify(v) : String(v) }))} />
            {Object.keys(counts).length === 0 && <p className="text-sm text-muted-foreground">Nothing to delete.</p>}
          </div>
        )}
      </section>
    </Can>
  );
}
