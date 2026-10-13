'use client';
import * as React from 'react';
import { useForm, type DefaultValues, type FieldValues, type Path, type Resolver, type UseFormRegister, type UseFormReturn } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { useQuery } from '@tanstack/react-query';
import type { ZodType } from 'zod';
import { Lock } from 'lucide-react';
import { api } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { applyFieldErrors, useApiMutation } from '@/hooks/use-api-mutation';
import { useMe } from '@/lib/auth';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { LoadingState } from '@/components/composites/loading-state';
import { ErrorState } from '@/components/composites/error-state';
import { useReportDirty } from './dirty';

/** Number field registration: empty -> null when `nullable`, otherwise NaN (rejected by z.number). */
export function num<V extends FieldValues>(register: UseFormRegister<V>, name: Path<V>, nullable = false) {
  return register(name, { setValueAs: (v) => (v === '' || v === null || v === undefined ? (nullable ? null : Number.NaN) : Number(v)) });
}

export interface SectionConfig<V extends FieldValues, A> {
  /** Backend section name (`/api/settings/{name}`). */
  name: string;
  title: string;
  description: React.ReactNode;
  schema: ZodType<V>;
  /** API payload -> form values */
  toForm: (api: A) => V;
  /** form values -> API payload */
  toApi: (v: V) => A;
  /** Render the inputs. */
  children: (form: UseFormReturn<V>, readOnly: boolean) => React.ReactNode;
  /** Extra content under the form (e.g. "run retention now"). */
  footer?: React.ReactNode;
  /** Non-admins can read but not change this section. Advanced sections are not even readable by them. */
  readOnlyForNonAdmin?: boolean;
}

/**
 * One admin-editable settings section: loads the saved values, validates with zod, saves with PUT (never optimistic),
 * maps server field errors, reports unsaved edits to the page guard, and re-syncs from the server response.
 */
export function SectionForm<V extends FieldValues, A>(cfg: SectionConfig<V, A>) {
  const me = useMe();
  const readOnly = !me.can('admin');
  const q = useQuery({ queryKey: qk.custom('settings', cfg.name), queryFn: ({ signal }) => api.get<A>(`/api/settings/${cfg.name}`, { signal }) });
  if (q.isPending) return <LoadingState variant="skeleton" rows={5} label={`Loading ${cfg.title}`} />;
  if (q.isError) return <ErrorState error={q.error} onRetry={() => q.refetch()} />;
  return <Loaded cfg={cfg} data={q.data} readOnly={readOnly} />;
}

function Loaded<V extends FieldValues, A>({ cfg, data, readOnly }: { cfg: SectionConfig<V, A>; data: A; readOnly: boolean }) {
  const initial = React.useMemo(() => cfg.toForm(data), [cfg, data]);
  const form = useForm<V>({ resolver: zodResolver(cfg.schema as never) as unknown as Resolver<V>, defaultValues: initial as DefaultValues<V>, mode: 'onChange' });
  const { handleSubmit, reset, setError, formState: { isDirty, errors } } = form;
  useReportDirty(`settings:${cfg.name}`, isDirty && !readOnly);
  const save = useApiMutation({
    mutationFn: (body: A) => api.put<A>(`/api/settings/${cfg.name}`, body),
    successMessage: `${cfg.title} saved`,
    errorMessage: false,
    invalidate: [qk.custom('settings', cfg.name), qk.custom('settings', 'all')],
    onSuccess: (saved) => reset(cfg.toForm(saved)),
    onError: (err) => { if (!applyFieldErrors(err, setError as never)) setError('root', { message: err.message }); },
  });
  const rootError = (errors as { root?: { message?: string } }).root?.message;

  return (
    <Card>
      <CardHeader>
        <CardTitle>{cfg.title}</CardTitle>
        <CardDescription>{cfg.description}</CardDescription>
      </CardHeader>
      <CardContent>
        <form noValidate aria-label={cfg.title} data-testid={`form-${cfg.name}`} onSubmit={handleSubmit((v) => save.mutate(cfg.toApi(v)))} className="space-y-5">
          {readOnly && (
            <p role="note" className="flex items-center gap-2 rounded-md border bg-muted/40 p-3 text-sm text-muted-foreground"><Lock className="size-4 shrink-0" aria-hidden /> Read-only: only administrators can change these settings.</p>
          )}
          <fieldset disabled={readOnly || save.isPending} className="min-w-0 space-y-5 border-0 p-0">{cfg.children(form, readOnly)}</fieldset>
          {rootError && <p role="alert" data-testid="form-error" className="text-sm font-medium text-destructive">{rootError}</p>}
          {!readOnly && (
            <div className="flex flex-wrap items-center gap-2">
              <Button type="submit" loading={save.isPending} disabled={!isDirty}>Save changes</Button>
              <Button type="button" variant="ghost" disabled={!isDirty || save.isPending} onClick={() => reset(initial)}>Discard changes</Button>
              {isDirty && <span className="text-xs text-warning-foreground" role="status">Unsaved changes</span>}
            </div>
          )}
        </form>
        {cfg.footer}
      </CardContent>
    </Card>
  );
}

export function FieldGrid({ children }: { children: React.ReactNode }) {
  return <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">{children}</div>;
}
