'use client';
import * as React from 'react';
import { useQuery } from '@tanstack/react-query';
import { Trash2 } from 'lucide-react';
import { api, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { formatDateTime } from '@/lib/format';
import { Can } from '@/lib/auth';
import { useApiMutation } from '@/hooks/use-api-mutation';
import { LoadingState } from '@/components/composites/loading-state';
import { ErrorState } from '@/components/composites/error-state';
import { StatusBadge } from '@/components/composites/status-badge';
import { KeyValueList } from '@/components/composites/key-value-list';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { FormField } from '@/components/ui/label';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { ConfirmDialog } from '@/components/ui/alert-dialog';

export function SmtpPanel() {
  const st = useQuery({ queryKey: qk.custom('verification', 'smtp'), queryFn: ({ signal }) => api.get<Schema<'SmtpStatus'>>('/api/verification/smtp', { signal }) });
  const perms = useQuery({ queryKey: qk.custom('verification', 'permissions'), queryFn: ({ signal }) => api.get<Schema<'app__api__verification__PermissionOut'>[]>('/api/verification/domain-permissions', { signal }) });
  const [domain, setDomain] = React.useState('');
  const [basis, setBasis] = React.useState('');
  const [err, setErr] = React.useState<Record<string, string>>({});
  const save = useApiMutation<Schema<'app__api__verification__PermissionOut'>, void>({
    mutationFn: () => api.put(`/api/verification/domain-permissions/${encodeURIComponent(domain.trim().toLowerCase())}`, { smtp_probe_allowed: true, basis: basis.trim() }),
    successMessage: 'Domain authorised for SMTP diagnostics (audited)', errorMessage: false,
    invalidate: [qk.custom('verification', 'permissions'), qk.custom('verification', 'smtp')],
    onError: (e) => setErr(Object.keys(e.fields).length ? e.fields : { form: e.message }),
  });
  const remove = useApiMutation<unknown, string>({
    mutationFn: (d) => api.delete(`/api/verification/domain-permissions/${encodeURIComponent(d)}`),
    successMessage: 'Permission removed', invalidate: [qk.custom('verification', 'permissions'), qk.custom('verification', 'smtp')],
  });

  if (st.isPending) return <LoadingState label="Loading SMTP status…" rows={2} />;
  if (st.isError) return <ErrorState error={st.error} onRetry={() => st.refetch()} compact />;
  const s = st.data;
  const submit = () => {
    const next: Record<string, string> = {};
    if (!domain.trim()) next.domain = 'Enter a domain you own or have written permission to test.';
    if (basis.trim().length < 10) next.basis = 'Describe the authorisation basis (at least 10 characters).';
    setErr(next);
    if (!Object.keys(next).length) save.mutateAsync().then(() => { setDomain(''); setBasis(''); }).catch(() => undefined);
  };
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">SMTP diagnostics <StatusBadge kind="health" status={s.enabled ? (s.ready ? 'healthy' : 'degraded') : 'disabled'} label={s.enabled ? (s.ready ? 'Enabled and ready' : 'Enabled, needs attention') : 'Disabled'} /></CardTitle>
        <CardDescription>Mailbox probes are only ever made to explicitly authorised domains, and no message is sent. Everything else gets DNS/MX evidence only.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <KeyValueList columns={3} items={[
          { label: 'Enabled', value: s.enabled ? 'Yes' : 'No' }, { label: 'HELO host', value: s.helo_host, mono: true }, { label: 'Envelope sender domain', value: s.mail_from_domain, mono: true },
          { label: 'Authorised in configuration', value: s.configured_authorised_domains.length ? s.configured_authorised_domains.join(', ') : null },
          { label: 'Authorised in database', value: `${s.permission_rows} domain${s.permission_rows === 1 ? '' : 's'}` },
          { label: 'Port 25 egress check', value: s.egress ? `${String((s.egress as Record<string, unknown>).status ?? 'unknown')}` : 'Not checked' },
        ]} />
        {s.problems.length > 0 && <ul className="list-disc space-y-1 pl-5 text-sm text-warning-foreground" aria-label="SMTP problems">{s.problems.map((p) => <li key={p}>{p}</li>)}</ul>}
        <div>
          <h3 className="mb-1.5 text-sm font-semibold">Authorised domains</h3>
          {perms.isPending ? <LoadingState rows={2} /> : perms.isError ? <ErrorState error={perms.error} compact /> : perms.data.length === 0 ? <p className="text-sm text-muted-foreground">No domain permissions recorded. SMTP diagnostics will not run against any domain.</p> : (
            <div className="relative overflow-x-auto rounded-lg border">
              <table className="w-full min-w-[520px] text-sm"><caption className="sr-only">Domain permissions for SMTP diagnostics</caption>
                <thead><tr className="border-b bg-muted/40 text-left text-xs text-muted-foreground"><th scope="col" className="p-2 font-medium">Domain</th><th scope="col" className="p-2 font-medium">Probe allowed</th><th scope="col" className="p-2 font-medium">Basis</th><th scope="col" className="p-2 font-medium">Granted</th><th scope="col" className="p-2"><span className="sr-only">Actions</span></th></tr></thead>
                <tbody>{perms.data.map((p) => (
                  <tr key={p.domain} className="border-b last:border-0">
                    <td className="p-2 font-mono text-xs">{p.domain}</td><td className="p-2">{p.smtp_probe_allowed ? 'Yes' : 'No'}</td><td className="p-2 text-xs">{p.basis}</td><td className="p-2 text-xs">{formatDateTime(p.granted_at)}</td>
                    <td className="p-2 text-right"><Can role="admin"><ConfirmDialog trigger={<Button size="icon-sm" variant="ghost" aria-label={`Remove permission for ${p.domain}`}><Trash2 /></Button>} title={`Remove permission for ${p.domain}?`} description="SMTP diagnostics will stop for this domain." destructive confirmLabel="Remove" onConfirm={() => remove.mutateAsync(p.domain)} /></Can></td>
                  </tr>))}
                </tbody>
              </table>
            </div>
          )}
        </div>
        <Can role="admin">
          <form onSubmit={(e) => { e.preventDefault(); submit(); }} className="grid gap-3 rounded-lg border p-3 sm:grid-cols-[1fr_2fr_auto] sm:items-start" aria-label="Authorise a domain">
            <FormField label="Domain" htmlFor="perm-domain" error={err.domain}><Input id="perm-domain" value={domain} onChange={(e) => setDomain(e.target.value)} placeholder="example.com" invalid={!!err.domain} /></FormField>
            <FormField label="Authorisation basis" htmlFor="perm-basis" error={err.basis} hint="e.g. “Domain owned by us” or “Written consent from the domain owner”"><Input id="perm-basis" value={basis} onChange={(e) => setBasis(e.target.value)} maxLength={300} invalid={!!err.basis} /></FormField>
            <div className="sm:pt-[22px]"><Button type="submit" loading={save.isPending}>Authorise</Button></div>
            {err.form && <p role="alert" className="text-sm font-medium text-destructive sm:col-span-3">{err.form}</p>}
          </form>
        </Can>
      </CardContent>
    </Card>
  );
}
