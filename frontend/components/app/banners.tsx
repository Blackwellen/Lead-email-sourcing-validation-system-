'use client';
import * as React from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Construction, WifiOff } from 'lucide-react';
import { api, type Schema } from '@/lib/api';
import { setApiStatus, useApiStatus } from '@/lib/api-status';
import { qk } from '@/lib/query-keys';
import { Button } from '@/components/ui/button';

/** Maintenance (from /api/system/maintenance + any 503 maintenance_mode response) and API-unreachable banners. */
export function StatusBanners() {
  const status = useApiStatus();
  const qc = useQueryClient();
  const maint = useQuery({
    queryKey: qk.maintenance,
    queryFn: () => api.get<Schema<'MaintenanceOut'>>('/api/system/maintenance'),
    refetchInterval: 60_000,
    retry: false,
  });

  // While unreachable, probe /healthz so the banner clears itself when the backend returns.
  React.useEffect(() => {
    if (!status.unreachable) return;
    const t = setInterval(() => { api.get('/healthz').then(() => qc.invalidateQueries()).catch(() => undefined); }, 8000);
    return () => clearInterval(t);
  }, [status.unreachable, qc]);

  const maintenanceOn = maint.data?.enabled || !!status.maintenance;
  const message = maint.data?.enabled ? maint.data.message : status.maintenance?.message;
  React.useEffect(() => {
    if (maint.data && !maint.data.enabled && status.maintenance) setApiStatus({ maintenance: null });
  }, [maint.data, status.maintenance]);

  return (
    <div className="space-y-0">
      {status.unreachable && (
        <div role="alert" data-testid="unreachable-banner" className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-danger-border bg-danger-soft px-4 py-2 text-sm text-danger-foreground">
          <WifiOff className="size-4 shrink-0" aria-hidden />
          <span className="font-medium">Cannot reach the API.</span>
          <span className="text-danger-foreground/80">Data shown may be stale. Retrying automatically.</span>
          <Button variant="outline" size="sm" className="ml-auto h-7 border-danger-border bg-transparent" onClick={() => qc.invalidateQueries()}>Retry now</Button>
        </div>
      )}
      {maintenanceOn && (
        <div role="status" data-testid="maintenance-banner" className="flex items-center gap-3 border-b border-warning-border bg-warning-soft px-4 py-2 text-sm text-warning-foreground">
          <Construction className="size-4 shrink-0" aria-hidden />
          <span><span className="font-medium">Maintenance mode.</span> {message || 'Changes are temporarily disabled; data is read-only.'}</span>
        </div>
      )}
    </div>
  );
}
