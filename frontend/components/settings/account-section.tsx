'use client';
import * as React from 'react';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import { zodResolver } from '@hookform/resolvers/zod';
import { useQueryClient } from '@tanstack/react-query';
import { CheckCircle2, KeyRound, ShieldCheck, ShieldOff } from 'lucide-react';
import { api, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { applyFieldErrors, useApiMutation } from '@/hooks/use-api-mutation';
import { useMe } from '@/lib/auth';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { FormField } from '@/components/ui/label';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { CopyButton } from '@/components/composites/copy-button';
import { KeyValueList } from '@/components/composites/key-value-list';
import { QrCode } from './qr-code';
import { useReportDirty } from './dirty';

export function AccountSection() {
  const me = useMe();
  if (!me.user) return null;
  return (
    <div className="space-y-6">
      <Card>
        <CardHeader><CardTitle>Your account</CardTitle></CardHeader>
        <CardContent>
          <KeyValueList columns={3} items={[
            { label: 'Email', value: me.user.email, mono: true },
            { label: 'Role', value: <span className="capitalize">{me.user.role}</span> },
            { label: 'Two-step verification', value: <Badge variant={me.user.totp_enabled ? 'success' : 'warning'}>{me.user.totp_enabled ? 'On' : 'Off'}</Badge> },
          ]} />
        </CardContent>
      </Card>
      <PasswordCard />
      <MfaCard />
    </div>
  );
}

const pwSchema = z.object({
  current_password: z.string().min(1, 'Enter your current password'),
  new_password: z.string().min(12, 'Use at least 12 characters').max(256, 'At most 256 characters'),
  confirm: z.string(),
}).refine((v) => v.new_password === v.confirm, { path: ['confirm'], message: 'The passwords do not match' })
  .refine((v) => v.new_password !== v.current_password, { path: ['new_password'], message: 'The new password must differ from the current one' });
type PwValues = z.infer<typeof pwSchema>;

function PasswordCard() {
  const form = useForm<PwValues>({ resolver: zodResolver(pwSchema), defaultValues: { current_password: '', new_password: '', confirm: '' }, mode: 'onChange' });
  const { register, handleSubmit, reset, setError, formState: { errors, isDirty } } = form;
  useReportDirty('settings:password', isDirty);
  const change = useApiMutation({
    mutationFn: (v: PwValues) => api.post<{ message: string }>('/api/auth/change-password', { current_password: v.current_password, new_password: v.new_password }),
    errorMessage: false,
    successMessage: 'Password changed. Other sessions were signed out.',
    onSuccess: () => reset(),
    onError: (e) => {
      if (e.code === 'wrong_password') setError('current_password', { message: e.message });
      else if (e.code === 'weak_password') setError('new_password', { message: e.message });
      else if (!applyFieldErrors(e, setError as never)) setError('root', { message: e.message });
    },
  });
  return (
    <Card>
      <CardHeader><CardTitle className="flex items-center gap-2"><KeyRound className="size-4" aria-hidden /> Change password</CardTitle><CardDescription>At least 12 characters, not a common password and not containing your email name. Your other signed-in sessions will be signed out.</CardDescription></CardHeader>
      <CardContent>
        <form noValidate onSubmit={handleSubmit((v) => change.mutate(v))} className="max-w-md space-y-4" aria-label="Change password" data-testid="form-password">
          <FormField label="Current password" htmlFor="pw-current" error={errors.current_password?.message}><Input id="pw-current" type="password" autoComplete="current-password" {...register('current_password')} invalid={!!errors.current_password} aria-describedby={errors.current_password ? 'pw-current-error' : undefined} /></FormField>
          <FormField label="New password" htmlFor="pw-new" error={errors.new_password?.message}><Input id="pw-new" type="password" autoComplete="new-password" {...register('new_password')} invalid={!!errors.new_password} aria-describedby={errors.new_password ? 'pw-new-error' : undefined} /></FormField>
          <FormField label="Confirm new password" htmlFor="pw-confirm" error={errors.confirm?.message}><Input id="pw-confirm" type="password" autoComplete="new-password" {...register('confirm')} invalid={!!errors.confirm} aria-describedby={errors.confirm ? 'pw-confirm-error' : undefined} /></FormField>
          {errors.root?.message && <p role="alert" className="text-sm font-medium text-destructive">{errors.root.message}</p>}
          <Button type="submit" loading={change.isPending} disabled={!isDirty}>Change password</Button>
        </form>
      </CardContent>
    </Card>
  );
}

const codeSchema = z.object({ code: z.string().trim().regex(/^\d{6,10}$/, 'Enter the 6-digit code from your authenticator app') });
const disableSchema = codeSchema.extend({ password: z.string().min(1, 'Enter your password') });

function MfaCard() {
  const me = useMe();
  const qc = useQueryClient();
  const enabled = !!me.user?.totp_enabled;
  const [setup, setSetup] = React.useState<Schema<'MfaSetupOut'> | null>(null);
  const refreshMe = () => qc.invalidateQueries({ queryKey: qk.me });

  const start = useApiMutation({ mutationFn: () => api.post<Schema<'MfaSetupOut'>>('/api/auth/mfa/setup'), onSuccess: (s) => setSetup(s) });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">{enabled ? <ShieldCheck className="size-4" aria-hidden /> : <ShieldOff className="size-4" aria-hidden />} Two-step verification (TOTP)</CardTitle>
        <CardDescription>Adds a 6-digit code from an authenticator app (such as Aegis, 1Password or Google Authenticator) to sign-in.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {enabled ? <DisableMfa onDone={() => { setSetup(null); refreshMe(); }} /> : setup ? (
          <ConfirmMfa setup={setup} onCancel={() => setSetup(null)} onDone={() => { setSetup(null); refreshMe(); }} />
        ) : (
          <>
            <p className="text-sm text-muted-foreground">Two-step verification is <strong>off</strong>. Turn it on to protect this account if your password is leaked.</p>
            <Button onClick={() => start.mutate()} loading={start.isPending} data-testid="mfa-start">Set up two-step verification</Button>
          </>
        )}
      </CardContent>
    </Card>
  );
}

function ConfirmMfa({ setup, onCancel, onDone }: { setup: Schema<'MfaSetupOut'>; onCancel: () => void; onDone: () => void }) {
  const form = useForm<z.infer<typeof codeSchema>>({ resolver: zodResolver(codeSchema), defaultValues: { code: '' } });
  const confirm = useApiMutation({
    mutationFn: (v: { code: string }) => api.post('/api/auth/mfa/confirm', { code: v.code.replace(/\s/g, '') }),
    errorMessage: false, successMessage: 'Two-step verification is on',
    onSuccess: onDone,
    onError: (e) => form.setError('code', { message: e.code === 'invalid_code' ? e.message : e.message }),
  });
  return (
    <div className="grid grid-cols-[minmax(0,1fr)] gap-6 md:grid-cols-[auto_minmax(0,1fr)]" data-testid="mfa-setup">
      <div className="space-y-2">
        <QrCode value={setup.provisioning_uri} label="QR code to scan with your authenticator app" />
        <p className="max-w-[12rem] text-xs text-muted-foreground">Scan with your authenticator app. Drawn in your browser; nothing is sent to a third party.</p>
      </div>
      <div className="min-w-0 space-y-4">
        <ol className="list-decimal space-y-1 pl-5 text-sm text-muted-foreground">
          <li>Scan the QR code, or enter the secret key by hand.</li>
          <li>Type the 6-digit code your app shows to confirm.</li>
        </ol>
        <div className="space-y-2">
          <p className="text-xs font-medium">Secret key</p>
          <div className="flex items-center gap-1"><code data-testid="mfa-secret" className="break-all rounded bg-muted px-2 py-1 font-mono text-xs">{setup.secret}</code><CopyButton value={setup.secret} label="Copy secret key" /></div>
          <p className="text-xs font-medium">Provisioning URI (for apps that import a link)</p>
          <div className="flex items-start gap-1"><code data-testid="mfa-uri" className="break-all rounded bg-muted px-2 py-1 font-mono text-xs">{setup.provisioning_uri}</code><CopyButton value={setup.provisioning_uri} label="Copy provisioning URI" /></div>
        </div>
        <form noValidate onSubmit={form.handleSubmit((v) => confirm.mutate(v))} className="flex flex-wrap items-end gap-2" aria-label="Confirm two-step verification">
          <FormField label="Verification code" htmlFor="mfa-code" error={form.formState.errors.code?.message} className="w-48">
            <Input id="mfa-code" inputMode="numeric" autoComplete="one-time-code" maxLength={10} {...form.register('code')} invalid={!!form.formState.errors.code} aria-describedby={form.formState.errors.code ? 'mfa-code-error' : undefined} />
          </FormField>
          <Button type="submit" loading={confirm.isPending}><CheckCircle2 /> Confirm and turn on</Button>
          <Button type="button" variant="ghost" onClick={onCancel}>Cancel</Button>
        </form>
      </div>
    </div>
  );
}

function DisableMfa({ onDone }: { onDone: () => void }) {
  const form = useForm<z.infer<typeof disableSchema>>({ resolver: zodResolver(disableSchema), defaultValues: { password: '', code: '' } });
  const [open, setOpen] = React.useState(false);
  const disable = useApiMutation({
    mutationFn: (v: z.infer<typeof disableSchema>) => api.post('/api/auth/mfa/disable', { password: v.password, code: v.code.replace(/\s/g, '') }),
    errorMessage: false, successMessage: 'Two-step verification turned off',
    onSuccess: () => { form.reset(); setOpen(false); onDone(); },
    onError: (e) => form.setError('root', { message: e.message }),
  });
  return (
    <div className="space-y-3">
      <p className="text-sm">Two-step verification is <strong>on</strong>. You will be asked for a code each time you sign in.</p>
      {!open ? <Button variant="outline" onClick={() => setOpen(true)} data-testid="mfa-disable-open">Turn off…</Button> : (
        <form noValidate onSubmit={form.handleSubmit((v) => disable.mutate(v))} className="max-w-md space-y-3 rounded-md border p-4" aria-label="Turn off two-step verification">
          <p className="text-sm text-muted-foreground">Turning it off makes your account easier to take over. Confirm with your password and a current code.</p>
          <FormField label="Password" htmlFor="mfa-off-pw" error={form.formState.errors.password?.message}><Input id="mfa-off-pw" type="password" autoComplete="current-password" {...form.register('password')} invalid={!!form.formState.errors.password} /></FormField>
          <FormField label="Verification code" htmlFor="mfa-off-code" error={form.formState.errors.code?.message}><Input id="mfa-off-code" inputMode="numeric" autoComplete="one-time-code" {...form.register('code')} invalid={!!form.formState.errors.code} /></FormField>
          {form.formState.errors.root?.message && <p role="alert" className="text-sm font-medium text-destructive">{form.formState.errors.root.message}</p>}
          <div className="flex gap-2"><Button type="submit" variant="destructive" loading={disable.isPending}>Turn off two-step verification</Button><Button type="button" variant="ghost" onClick={() => setOpen(false)}>Cancel</Button></div>
        </form>
      )}
    </div>
  );
}
