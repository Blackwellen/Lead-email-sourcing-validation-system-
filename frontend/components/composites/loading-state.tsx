import * as React from 'react';
import { Loader2 } from 'lucide-react';
import { Skeleton } from '@/components/ui/skeleton';
import { cn } from '@/lib/utils';

/** `variant="spinner"` for small regions, `"skeleton"` (default) for blocks of lines, `"cards"` for stat grids. */
export function LoadingState({ label = 'Loading…', variant = 'skeleton', rows = 4, className }: {
  label?: string; variant?: 'spinner' | 'skeleton' | 'cards'; rows?: number; className?: string;
}) {
  if (variant === 'spinner') {
    return (
      <div role="status" className={cn('flex items-center justify-center gap-2 py-10 text-sm text-muted-foreground', className)}>
        <Loader2 className="size-4 animate-spin" aria-hidden /> {label}
      </div>
    );
  }
  if (variant === 'cards') {
    return (
      <div role="status" aria-label={label} className={cn('grid gap-4 sm:grid-cols-2 xl:grid-cols-4', className)}>
        {Array.from({ length: rows }).map((_, i) => <Skeleton key={i} className="h-28" />)}
        <span className="sr-only">{label}</span>
      </div>
    );
  }
  return (
    <div role="status" aria-label={label} className={cn('space-y-3', className)}>
      {Array.from({ length: rows }).map((_, i) => <Skeleton key={i} className={cn('h-5', i % 3 === 2 ? 'w-2/3' : 'w-full')} />)}
      <span className="sr-only">{label}</span>
    </div>
  );
}
