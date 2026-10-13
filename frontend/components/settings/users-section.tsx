'use client';
import * as React from 'react';
import { useQuery } from '@tanstack/react-query';
import { Controller, useForm } from 'react-hook-form';
import { z } from 'zod';
import { zodResolver } from '@hookform/resolvers/zod';
import { KeyRound, Lock, Plus, UserCheck, UserX } from 'lucide-react';
import { api, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { applyFieldErrors, useApiMutation } from '@/hooks/use-api-mutation';
import { useMe } from '@/lib/auth';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { FormField } from '@/components/ui/label';
import { SimpleSelect } from '@/components/ui/select';
import { ConfirmDialog } from '@/components/ui/alert-dialog';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { ErrorState } from '@/components/composites/error-state';
import { LoadingState } from '@/components/composites/loading-state';
import { RelativeTime } from '@/components/composites/relative-time';
import { CodeBlock } from '@/components/composites/code-block';
import { ScrollRegion, tdClass, thClass } from '@/components/system/scroll-region';

type User = Schema<'UserOut'>;
const ROLES = [{ value: 'admin', label: 'Admin' }, { value: 'operator', label: 'Operator' }, { value: 'viewer', label: 'Viewer' }];
const ROLE_HELP = 'Viewer: read-only. Operator: create runs and exports, retry jobs. Admin: everything, including settings, users and system controls.';

export function UsersSection() {
  const me = useMe();
  const q = useQuery({ queryKey: qk.list('users'), queryFn: ({ signal }) => api.get<{ items: User[]; total_estimate?: number | null }>('/api/users', { query: { limit: 200 }, signal }) });
  const [creating, setCreating] = React.useState(false);
  const [reset, setReset] = React.useState<User | null>(null);
  const [roleChange, setRoleChange] = React.useState<{ user: User; role: string } | null>(null);
  const [now] = React.useState(() => Date.now());

  const update = useApiMutation({
    mutationFn: ({ id, body }: { id: number; body: Record<string, unknown> }) => api.patch<User>(`/api/users/${id}`, body),
    successMessage: 'User updated', invalidate: [qk.all('users')],
  });

  return (
    <Card>
      <CardHeader className="flex-row items-start justify-between gap-3 space-y-0">
        <div className="space-y-1.5"><CardTitle>Users</CardTitle><CardDescription>Accounts that can sign in. {ROLE_HELP}</CardDescription></div>
        <Button onClick={() => setCreating(true)} data-testid="create-user"><Plus /> Add user</Button>
      </CardHeader>
      <CardContent>
        {q.isPending ? <LoadingState variant="skeleton" rows={4} label="Loading users" /> : q.isError ? <ErrorState error={q.error} onRetry={() => q.refetch()} /> : (
          <ScrollRegion label="Users table">
            <table className="w-full min-w-[44rem] border-collapse" data-testid="users-table">
              <caption className="sr-only">User accounts with role, status and two-step verification</caption>
              <thead><tr>
                <th scope="col" className={thClass}>Email</th><th scope="col" className={thClass}>Role</th><th scope="col" className={thClass}>Status</th>
                <th scope="col" className={thClass}>2-step</th><th scope="col" className={`${thClass} max-md:hidden`}>Created</th><th scope="col" className={thClass}><span className="sr-only">Actions</span></th>
              </tr></thead>
              <tbody>
                {q.data.items.map((u) => {
                  const self = u.id === me.user?.id;
                  const locked = !!u.locked_until && new Date(u.locked_until).getTime() > now;
                  return (
                    <tr key={u.id} data-testid={`user-row-${u.email}`}>
                      <th scope="row" className={`${tdClass} text-left font-medium`}>{u.email}{self && <Badge variant="outline" className="ml-2">You</Badge>}</th>
                      <td className={tdClass}>
                        <SimpleSelect aria-label={`Role for ${u.email}`} className="w-32" value={u.role} disabled={self} onValueChange={(r) => { if (r !== u.role) setRoleChange({ user: u, role: r }); }} options={ROLES} />
                      </td>
                      <td className={tdClass}>
                        {u.is_active ? <Badge variant="success">Active</Badge> : <Badge variant="neutral">Deactivated</Badge>}
                        {locked && <Badge variant="warning" className="ml-1"><Lock className="mr-1 size-3" aria-hidden />Locked</Badge>}
                      </td>
                      <td className={tdClass}>{u.totp_enabled ? <Badge variant="success">On</Badge> : <Badge variant="outline">Off</Badge>}</td>
                      <td className={`${tdClass} max-md:hidden`}><RelativeTime value={u.created_at} /></td>
                      <td className={`${tdClass} whitespace-nowrap text-right`}>
                        {locked && <Button size="sm" variant="ghost" onClick={() => update.mutate({ id: u.id, body: { unlock: true } })}>Unlock</Button>}
                        <Button size="sm" variant="ghost" onClick={() => setReset(u)} aria-label={`Reset password for ${u.email}`}><KeyRound /> Reset password</Button>
                        {u.is_active ? (
                          <ConfirmDialog
                            trigger={<Button size="sm" variant="ghost" disabled={self} aria-label={`Deactivate ${u.email}`}><UserX /> Deactivate</Button>}
                            title={`Deactivate ${u.email}?`} destructive confirmLabel="Deactivate"
                            description="They are signed out immediately and cannot sign in again until reactivated. Their exports and audit history are kept."
                            onConfirm={() => update.mutateAsync({ id: u.id, body: { is_active: false } })}
                          />
                        ) : (
                          <Button size="sm" variant="ghost" onClick={() => update.mutate({ id: u.id, body: { is_active: true } })} aria-label={`Reactivate ${u.email}`}><UserCheck /> Reactivate</Button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </ScrollRegion>
        )}
      </CardContent>

      <ConfirmDialog
        open={!!roleChange} onOpenChange={(o) => { if (!o) setRoleChange(null); }}
        title={`Change role to ${roleChange?.role}?`}
        description={<p>{roleChange?.user.email} will be changed from <strong>{roleChange?.user.role}</strong> to <strong>{roleChange?.role}</strong> and will be signed out of every session, so the new permissions apply on their next sign-in.</p>}
        confirmLabel="Change role"
        onConfirm={async () => { if (roleChange) await update.mutateAsync({ id: roleChange.user.id, body: { role: roleChange.role } }); }}
      />
      <CreateUserDialog open={creating} onOpenChange={setCreating} />
      <ResetPasswordDialog user={reset} onClose={() => setReset(null)} />
    </Card>
  );
}

const createSchema = z.object({
  email: z.string().trim().min(1, 'Enter an email address').email('Enter a valid email address'),
  password: z.string().min(12, 'Use at least 12 characters').max(256),
  role: z.enum(['admin', 'operator', 'viewer']),
});
function CreateUserDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const form = useForm<z.infer<typeof createSchema>>({ resolver: zodResolver(createSchema), defaultValues: { email: '', password: '', role: 'operator' } });
  const { register, handleSubmit, setError, reset, control, formState: { errors } } = form;
  const create = useApiMutation({
    mutationFn: (v: z.infer<typeof createSchema>) => api.post<User>('/api/users', v),
    errorMessage: false, successMessage: 'User created', invalidate: [qk.all('users')],
    onSuccess: () => { reset(); onOpenChange(false); },
    onError: (e) => {
      if (e.code === 'email_in_use') setError('email', { message: e.message });
      else if (e.code === 'weak_password') setError('password', { message: e.message });
      else if (!applyFieldErrors(e, setError as never)) setError('root', { message: e.message });
    },
  });
  return (
    <Dialog open={open} onOpenChange={(o) => { onOpenChange(o); if (!o) reset(); }}>
      <DialogContent>
        <DialogHeader><DialogTitle>Add user</DialogTitle><DialogDescription>Give them the password through a secure channel; they can change it after signing in.</DialogDescription></DialogHeader>
        <form noValidate onSubmit={handleSubmit((v) => create.mutate(v))} className="space-y-4" aria-label="Add user">
          <FormField label="Email" htmlFor="nu-email" error={errors.email?.message}><Input id="nu-email" type="email" autoComplete="off" {...register('email')} invalid={!!errors.email} /></FormField>
          <FormField label="Initial password" htmlFor="nu-pw" error={errors.password?.message} hint="At least 12 characters"><Input id="nu-pw" type="password" autoComplete="new-password" {...register('password')} invalid={!!errors.password} /></FormField>
          <FormField label="Role" htmlFor="nu-role" hint={ROLE_HELP}><Controller control={control} name="role" render={({ field }) => <SimpleSelect id="nu-role" aria-label="Role" value={field.value} onValueChange={field.onChange} options={ROLES} />} /></FormField>
          {errors.root?.message && <p role="alert" className="text-sm font-medium text-destructive">{errors.root.message}</p>}
          <DialogFooter><Button type="button" variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button><Button type="submit" loading={create.isPending}>Create user</Button></DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function ResetPasswordDialog({ user, onClose }: { user: User | null; onClose: () => void }) {
  const [pw, setPw] = React.useState('');
  const [err, setErr] = React.useState<string>();
  const [result, setResult] = React.useState<string | null>(null);
  const done = () => { setPw(''); setErr(undefined); setResult(null); onClose(); };
  const reset = useApiMutation({
    mutationFn: () => api.post<Schema<'PasswordResetOut'>>(`/api/users/${user!.id}/reset-password`, pw ? { new_password: pw } : {}),
    errorMessage: false,
    onSuccess: (r) => { if (r.temporary_password) setResult(r.temporary_password); else { toastDone(); done(); } },
    onError: (e) => setErr(e.message),
  });
  const toastDone = () => import('@/lib/toast').then((m) => m.toast.success('Password reset', 'Their sessions were signed out.'));
  return (
    <Dialog open={!!user} onOpenChange={(o) => { if (!o) done(); }}>
      <DialogContent>
        <DialogHeader><DialogTitle>Reset password for {user?.email}</DialogTitle><DialogDescription>Their current sessions are signed out and any lockout is cleared.</DialogDescription></DialogHeader>
        {result ? (
          <div className="space-y-3" data-testid="temp-password">
            <p className="text-sm">Temporary password (shown once). Share it securely and ask them to change it after signing in.</p>
            <CodeBlock code={result} />
            <DialogFooter><Button onClick={done}>Done</Button></DialogFooter>
          </div>
        ) : (
          <form noValidate onSubmit={(e) => { e.preventDefault(); setErr(undefined); if (pw && pw.length < 12) { setErr('Use at least 12 characters, or leave empty to generate one.'); return; } reset.mutate(); }} className="space-y-4" aria-label="Reset password">
            <FormField label="New password (optional)" htmlFor="rp-pw" error={err} hint="Leave empty to generate a random one-time password.">
              <Input id="rp-pw" type="password" autoComplete="new-password" value={pw} onChange={(e) => setPw(e.target.value)} invalid={!!err} aria-describedby={err ? 'rp-pw-error' : 'rp-pw-hint'} />
            </FormField>
            <DialogFooter><Button type="button" variant="outline" onClick={done}>Cancel</Button><Button type="submit" variant="destructive" loading={reset.isPending}>Reset password</Button></DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}
