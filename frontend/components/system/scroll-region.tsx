import * as React from 'react';
import { cn } from '@/lib/utils';

/** Horizontally scrollable wrapper for wide tables: keyboard focusable + labelled (axe `scrollable-region-focusable`). */
export function ScrollRegion({ label, className, children }: { label: string; className?: string; children: React.ReactNode }) {
  return (
    <div role="region" aria-label={label} tabIndex={0} className={cn('relative max-w-full overflow-x-auto rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring', className)}>
      {children}
    </div>
  );
}

export const thClass = 'whitespace-nowrap border-b bg-muted/40 px-3 py-2 text-left text-xs font-medium text-muted-foreground';
export const tdClass = 'border-b px-3 py-2 align-middle text-sm';
