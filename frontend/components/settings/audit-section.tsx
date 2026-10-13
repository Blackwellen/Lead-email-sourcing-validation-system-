'use client';
import * as React from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { listFetcher, usePagedQuery } from '@/hooks/use-paged-query';
import { qk } from '@/lib/query-keys';
import { type Schema } from '@/lib/api';
import { formatDateTime } from '@/lib/format';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { FormField } from '@/components/ui/label';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { EmptyState } from '@/components/composites/empty-state';
import { ErrorState } from '@/components/composites/error-state';
import { LoadingState } from '@/components/composites/loading-state';
import { CodeBlock } from '@/components/composites/code-block';
import { ScrollRegion, tdClass, thClass } from '@/components/system/scroll-region';

type Row = Schema<'AuditRow'>;
interface Filters { action: string; object_type: string; user_id: string; since: string; until: string }
const EMPTY: Filters = { action: '', object_type: '', user_id: '', since: '', until: '' };

export function AuditSection() {
  const [draft, setDraft] = React.useState<Filters>(EMPTY);
  const [applied, setApplied] = React.useState<Filters>(EMPTY);
  const [open, setOpen] = React.useState<number | null>(null);
  const [fieldError, setFieldError] = React.useState<string>();
  const q = usePagedQuery<Row, Record<string, unknown>>({
    queryKey: qk.list('audit'), fetchPage: listFetcher<Row>('/api/audit'), limit: 50,
    filters: { action: applied.action || undefined, object_type: applied.object_type || undefined, user_id: applied.user_id || undefined, since: applied.since ? new Date(applied.since).toISOString() : undefined, until: applied.until ? new Date(`${applied.until}T23:59:59`).toISOString() : undefined },
  });
  const set = (k: keyof Filters) => (e: React.ChangeEvent<HTMLInputElement>) => setDraft((d) => ({ ...d, [k]: e.target.value }));
  return (
    <Card>
      <CardHeader><CardTitle>Audit log</CardTitle><CardDescription>Who changed what. Newest first. Entries cannot be edited or removed here.</CardDescription></CardHeader>
      <CardContent className="space-y-4">
        <form className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-5" aria-label="Audit filters" onSubmit={(e) => {
          e.preventDefault();
          if (draft.user_id && !/^\d+$/.test(draft.user_id)) { setFieldError('User id must be a number'); return; }
          setFieldError(undefined); setApplied(draft);
        }}>
          <FormField label="Action" htmlFor="au-action" hint="e.g. settings.update or run.*"><Input id="au-action" value={draft.action} onChange={set('action')} placeholder="settings.*" /></FormField>
          <FormField label="Object type" htmlFor="au-type"><Input id="au-type" value={draft.object_type} onChange={set('object_type')} placeholder="settings" /></FormField>
          <FormField label="User id" htmlFor="au-user" error={fieldError}><Input id="au-user" inputMode="numeric" value={draft.user_id} onChange={set('user_id')} invalid={!!fieldError} /></FormField>
          <FormField label="From" htmlFor="au-since"><Input id="au-since" type="date" value={draft.since} onChange={set('since')} /></FormField>
          <FormField label="To" htmlFor="au-until"><Input id="au-until" type="date" value={draft.until} onChange={set('until')} /></FormField>
          <div className="flex gap-2 sm:col-span-2 lg:col-span-5"><Button type="submit">Apply filters</Button><Button type="button" variant="ghost" onClick={() => { setDraft(EMPTY); setApplied(EMPTY); setFieldError(undefined); }}>Clear</Button></div>
        </form>

        {q.isPending ? <LoadingState variant="skeleton" rows={5} label="Loading audit log" /> : q.isError ? <ErrorState error={q.error} onRetry={() => q.refetch()} /> : q.items.length === 0 ? (
          <EmptyState compact title="No audit entries match" description="Try clearing the filters." />
        ) : (
          <>
            <ScrollRegion label="Audit log table">
              <table className="w-full min-w-[40rem] border-collapse" data-testid="audit-table">
                <caption className="sr-only">Audit events with time, user, action and object</caption>
                <thead><tr><th scope="col" className={thClass}><span className="sr-only">Details</span></th><th scope="col" className={thClass}>When</th><th scope="col" className={thClass}>User</th><th scope="col" className={thClass}>Action</th><th scope="col" className={thClass}>Object</th></tr></thead>
                <tbody>
                  {q.items.map((a) => (
                    <React.Fragment key={a.id}>
                      <tr data-testid={`audit-row-${a.id}`}>
                        <td className={tdClass}>
                          <Button variant="ghost" size="icon-sm" aria-expanded={open === a.id} aria-label={`${open === a.id ? 'Hide' : 'Show'} details for event ${a.id}`} onClick={() => setOpen(open === a.id ? null : a.id)}>{open === a.id ? <ChevronDown /> : <ChevronRight />}</Button>
                        </td>
                        <td className={`${tdClass} whitespace-nowrap tabular`}>{formatDateTime(a.at)}</td>
                        <td className={tdClass}>{a.user_email ?? (a.user_id ? `User #${a.user_id}` : <span className="text-muted-foreground">System / anonymous</span>)}</td>
                        <td className={`${tdClass} font-mono text-xs`}>{a.action}</td>
                        <td className={tdClass}>{a.object_type ? `${a.object_type}${a.object_id ? ` · ${a.object_id}` : ''}` : '—'}</td>
                      </tr>
                      {open === a.id && <tr><td colSpan={5} className="border-b bg-muted/30 p-3"><CodeBlock code={JSON.stringify(a.detail ?? {}, null, 2)} language="json" /></td></tr>}
                    </React.Fragment>
                  ))}
                </tbody>
              </table>
            </ScrollRegion>
            <div className="flex items-center justify-between text-sm text-muted-foreground">
              <span>{q.items.length} loaded</span>
              {q.hasNextPage && <Button variant="outline" onClick={() => q.fetchNextPage()} loading={q.isFetchingNextPage}>Load older entries</Button>}
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}
