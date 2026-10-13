'use client';
import * as React from 'react';
import { AlertTriangle, CheckCircle2, Info, X, XCircle } from 'lucide-react';
import { toast, useToasts, type ToastItem, type ToastKind } from '@/lib/toast';
import { cn } from '@/lib/utils';

export { toast };

const tone: Record<ToastKind, { cls: string; Icon: React.ComponentType<{ className?: string }> }> = {
  success: { cls: 'border-success-border', Icon: CheckCircle2 },
  error: { cls: 'border-danger-border', Icon: XCircle },
  warning: { cls: 'border-warning-border', Icon: AlertTriangle },
  info: { cls: 'border-info-border', Icon: Info },
};
const iconTone: Record<ToastKind, string> = { success: 'text-success', error: 'text-danger', warning: 'text-warning', info: 'text-info' };

function ToastView({ t }: { t: ToastItem }) {
  React.useEffect(() => {
    const h = setTimeout(() => toast.dismiss(t.id), t.duration);
    return () => clearTimeout(h);
  }, [t.id, t.duration]);
  const { cls, Icon } = tone[t.kind];
  return (
    <li className={cn('pointer-events-auto flex w-full items-start gap-3 rounded-lg border bg-popover p-3.5 shadow-pop animate-in fade-in-0 slide-in-from-bottom-2', cls)}>
      <Icon className={cn('mt-0.5 size-4 shrink-0', iconTone[t.kind])} aria-hidden />
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium leading-5">{t.title}</p>
        {t.description && <p className="mt-0.5 break-words text-xs text-muted-foreground">{t.description}</p>}
        {t.action && (
          <button className="mt-1.5 text-xs font-medium text-primary underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" onClick={() => { t.action?.onClick(); toast.dismiss(t.id); }}>
            {t.action.label}
          </button>
        )}
      </div>
      <button aria-label="Dismiss notification" className="rounded p-0.5 text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" onClick={() => toast.dismiss(t.id)}>
        <X className="size-4" />
      </button>
    </li>
  );
}

/** Mount once (done in Providers). Polite live region for info/success, assertive for errors. */
export function Toaster() {
  const items = useToasts();
  const urgent = items.filter((i) => i.kind === 'error' || i.kind === 'warning');
  const calm = items.filter((i) => i.kind !== 'error' && i.kind !== 'warning');
  const wrap = 'pointer-events-none flex flex-col gap-2';
  return (
    <div className="pointer-events-none fixed bottom-4 right-4 z-[100] flex w-[calc(100vw-2rem)] max-w-sm flex-col gap-2">
      <ul role="status" aria-live="polite" aria-atomic="false" className={wrap}>{calm.map((t) => <ToastView key={t.id} t={t} />)}</ul>
      <ul role="alert" aria-live="assertive" aria-atomic="false" className={wrap}>{urgent.map((t) => <ToastView key={t.id} t={t} />)}</ul>
    </div>
  );
}
