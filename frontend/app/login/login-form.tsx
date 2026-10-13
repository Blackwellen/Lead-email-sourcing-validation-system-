'use client';
import * as React from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import { zodResolver } from '@hookform/resolvers/zod';
import { useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, Eye, EyeOff, KeyRound, Lock, Radar, ShieldCheck, Terminal, WifiOff } from 'lucide-react';
import { api, ApiError, setCsrfToken, type Session } from '@/lib/api';
import { useApiStatus } from '@/lib/api-status';
import { qk } from '@/lib/query-keys';
import { safeNext } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { FormField } from '@/components/ui/label';
import { CodeBlock } from '@/components/composites/code-block';
import { ThemeToggle } from '@/components/app/theme-toggle';

const schema = z.object({
  email: z.string().trim().min(1, 'Enter your email address').email('Enter a valid email address'),
  password: z.string().min(1, 'Enter your password'),
  totp_code: z.string().trim().optional(),
});
type Values = z.infer<typeof schema>;

type Problem =
  | { kind: 'credentials'; message: string }
  | { kind: 'rate'; message: string; seconds: number }
  | { kind: 'network'; message: string }
  | { kind: 'other'; message: string }
  | null;

export function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const qc = useQueryClient();
  const apiStatus = useApiStatus();
  const [step, setStep] = React.useState<'credentials' | 'totp'>('credentials');
  const [pending, setPending] = React.useState(false);
  const [problem, setProblem] = React.useState<Problem>(null);
  const [failures, setFailures] = React.useState(0);
  const [showPw, setShowPw] = React.useState(false);
  const [countdown, setCountdown] = React.useState(0);

  const form = useForm<Values>({ resolver: zodResolver(schema), defaultValues: { email: '', password: '', totp_code: '' } });
  const { register, handleSubmit, formState: { errors }, setError, setFocus } = form;

  React.useEffect(() => {
    if (countdown <= 0) return;
    const t = setTimeout(() => setCountdown((c) => c - 1), 1000);
    return () => clearTimeout(t);
  }, [countdown]);

  // Already signed in? Skip the form.
  React.useEffect(() => {
    let cancelled = false;
    api.get<Session>('/api/me', { skipAuthRedirect: true }).then((s) => {
      if (cancelled) return;
      setCsrfToken(s.csrf_token);
      qc.setQueryData(qk.me, s);
      router.replace(safeNext(params.get('next')));
    }).catch(() => undefined);
    return () => { cancelled = true; };
  }, [qc, router, params]);

  const onSubmit = handleSubmit(async (v) => {
    setProblem(null);
    if (step === 'totp' && !v.totp_code) { setError('totp_code', { message: 'Enter the 6-digit code from your authenticator app' }); return; }
    setPending(true);
    try {
      const s = await api.post<Session>('/api/auth/login', { email: v.email.trim(), password: v.password, totp_code: step === 'totp' ? v.totp_code?.replace(/\s/g, '') : undefined }, { skipAuthRedirect: true });
      setCsrfToken(s.csrf_token);
      qc.setQueryData(qk.me, s);
      router.replace(safeNext(params.get('next')));
    } catch (e) {
      const err = e instanceof ApiError ? e : null;
      if (err?.code === 'mfa_required') {
        setStep('totp');
        setTimeout(() => setFocus('totp_code'), 50);
      } else if (err?.status === 429) {
        const s = err.retryAfter ?? 60;
        setCountdown(s);
        setProblem({ kind: 'rate', message: err.message, seconds: s });
      } else if (err?.isNetwork || err?.code === 'unreachable' || err?.status === 502 || err?.status === 504) {
        setProblem({ kind: 'network', message: 'We could not reach the server. Check that the API is running, then try again.' });
      } else if (err?.status === 401 || err?.code === 'invalid_credentials') {
        setFailures((n) => n + 1);
        setProblem({ kind: 'credentials', message: step === 'totp' ? 'That code or password was not accepted.' : err?.message ?? 'Invalid email or password.' });
        if (step === 'totp') { form.setValue('totp_code', ''); setTimeout(() => setFocus('totp_code'), 50); }
      } else if (err?.isValidation) {
        Object.entries(err.fields).forEach(([f, m]) => { if (f === 'email' || f === 'password' || f === 'totp_code') setError(f, { message: m }); });
        setProblem({ kind: 'other', message: err.message });
      } else {
        setProblem({ kind: 'other', message: err?.message ?? 'Sign-in failed. Please try again.' });
      }
    } finally {
      setPending(false);
    }
  });

  const blocked = countdown > 0;
  const unreachable = apiStatus.unreachable && problem?.kind !== 'network';

  return (
    <main className="relative grid min-h-dvh lg:grid-cols-[minmax(0,1fr)_minmax(0,28rem)]">
      <div className="absolute right-3 top-3 z-10"><ThemeToggle /></div>

      {/* brand panel */}
      <section aria-hidden className="hidden flex-col justify-between border-r bg-sidebar p-12 lg:flex">
        <div className="flex items-center gap-2.5">
          <span className="flex size-9 items-center justify-center rounded-md bg-primary text-primary-foreground"><Radar className="size-5" /></span>
          <span className="text-base font-semibold tracking-tight">Lead Intelligence</span>
        </div>
        <div className="max-w-md space-y-4">
          <h2 className="text-balance text-3xl font-semibold leading-tight tracking-tight">Know which addresses are real, and why.</h2>
          <p className="text-muted-foreground">Every contact carries its evidence: where it was published, how the domain was matched and what verification actually proved. Nothing is marked safe on syntax alone.</p>
        </div>
        <p className="text-xs text-muted-foreground">Sessions are cookie-based and expire automatically.</p>
      </section>

      {/* form */}
      <section className="flex items-center justify-center p-6 sm:p-10">
        <div className="w-full max-w-sm">
          <div className="mb-8 flex items-center gap-2.5 lg:hidden">
            <span className="flex size-9 items-center justify-center rounded-md bg-primary text-primary-foreground"><Radar className="size-5" aria-hidden /></span>
            <span className="text-base font-semibold tracking-tight">Lead Intelligence</span>
          </div>
          <h1 className="text-2xl font-semibold tracking-tight">{step === 'totp' ? 'Two-step verification' : 'Sign in'}</h1>
          <p className="mt-1.5 text-sm text-muted-foreground">
            {step === 'totp' ? 'Enter the 6-digit code from your authenticator app to finish signing in.' : 'Use your operator account to continue.'}
          </p>

          <div className="mt-5 space-y-3" aria-live="polite">
            {unreachable && (
              <div role="alert" className="flex gap-2 rounded-md border border-danger-border bg-danger-soft p-3 text-sm text-danger-foreground">
                <WifiOff className="mt-0.5 size-4 shrink-0" aria-hidden /> <span>The API is currently unreachable. Sign-in will not work until it is back.</span>
              </div>
            )}
            {problem && (
              <div role="alert" data-testid="login-error" className="flex gap-2 rounded-md border border-danger-border bg-danger-soft p-3 text-sm text-danger-foreground">
                {problem.kind === 'rate' ? <Lock className="mt-0.5 size-4 shrink-0" aria-hidden /> : problem.kind === 'network' ? <WifiOff className="mt-0.5 size-4 shrink-0" aria-hidden /> : <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />}
                <div className="space-y-1">
                  <p>{problem.message}</p>
                  {problem.kind === 'rate' && <p className="text-xs">{countdown > 0 ? `You can try again in ${countdown}s.` : 'You can try again now.'}</p>}
                  {problem.kind === 'credentials' && failures >= 2 && (
                    <p className="text-xs">After 5 failed attempts an account is locked for 15 minutes, even if you then enter the right password. If you are locked out, wait and retry or ask an administrator.</p>
                  )}
                </div>
              </div>
            )}
          </div>

          <form onSubmit={onSubmit} noValidate className="mt-5 space-y-4">
            <div className={step === 'totp' ? 'hidden' : 'space-y-4'}>
              <FormField label="Email" htmlFor="email" error={errors.email?.message}>
                <Input id="email" type="email" autoComplete="username" inputMode="email" autoFocus invalid={!!errors.email} aria-describedby={errors.email ? 'email-error' : undefined} {...register('email')} />
              </FormField>
              <FormField label="Password" htmlFor="password" error={errors.password?.message}>
                <Input id="password" type={showPw ? 'text' : 'password'} autoComplete="current-password" invalid={!!errors.password} aria-describedby={errors.password ? 'password-error' : undefined}
                  endAdornment={
                    <button type="button" onClick={() => setShowPw((s) => !s)} aria-label={showPw ? 'Hide password' : 'Show password'} aria-pressed={showPw} className="rounded p-1 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                      {showPw ? <EyeOff /> : <Eye />}
                    </button>
                  }
                  {...register('password')} />
              </FormField>
            </div>
            {step === 'totp' && (
              <FormField label="Authentication code" htmlFor="totp_code" error={errors.totp_code?.message} hint="Open your authenticator app and enter the current code. Codes refresh every 30 seconds.">
                <Input id="totp_code" inputMode="numeric" autoComplete="one-time-code" maxLength={10} placeholder="123456" className="tabular tracking-widest" startAdornment={<KeyRound />} invalid={!!errors.totp_code} {...register('totp_code')} />
              </FormField>
            )}
            <Button type="submit" className="w-full" size="lg" loading={pending} disabled={blocked}>
              {blocked ? `Try again in ${countdown}s` : step === 'totp' ? 'Verify and sign in' : 'Sign in'}
            </Button>
            {step === 'totp' && (
              <Button type="button" variant="ghost" className="w-full" onClick={() => { setStep('credentials'); setProblem(null); form.setValue('totp_code', ''); }}>Back</Button>
            )}
          </form>

          <details className="group mt-8 rounded-lg border bg-card text-sm">
            <summary className="flex cursor-pointer list-none items-center gap-2 rounded-lg px-3 py-2.5 font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
              <Terminal className="size-4 text-muted-foreground" aria-hidden /> First time here? No account yet?
            </summary>
            <div className="space-y-2 border-t px-3 py-3 text-muted-foreground">
              <p>There is no public sign-up. The first administrator is created on the server with the CLI:</p>
              <CodeBlock language="shell" code={'python -m app.cli create-admin --email you@example.com'} wrap />
              <p>Run it in the backend environment (for Docker: inside the <code className="font-mono text-xs">api</code> container). You will be asked for a password. Further users are added by an admin under Settings.</p>
            </div>
          </details>
          <p className="mt-6 flex items-center gap-1.5 text-xs text-muted-foreground"><ShieldCheck className="size-3.5" aria-hidden /> Sessions use a secure, httpOnly cookie.</p>
        </div>
      </section>
    </main>
  );
}
