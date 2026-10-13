'use client';
import * as React from 'react';
import * as ProgressPrimitive from '@radix-ui/react-progress';
import { cn } from '@/lib/utils';

export const Progress = React.forwardRef<
  React.ComponentRef<typeof ProgressPrimitive.Root>,
  React.ComponentPropsWithoutRef<typeof ProgressPrimitive.Root> & { tone?: 'primary' | 'success' | 'warning' | 'danger' }
>(({ className, value, tone = 'primary', ...props }, ref) => (
  <ProgressPrimitive.Root ref={ref} value={value} className={cn('relative h-2 w-full overflow-hidden rounded-full bg-muted', className)} {...props}>
    <ProgressPrimitive.Indicator
      className={cn('h-full w-full flex-1 rounded-full transition-transform duration-300', { primary: 'bg-primary', success: 'bg-success', warning: 'bg-warning', danger: 'bg-danger' }[tone])}
      style={{ transform: `translateX(-${100 - Math.max(0, Math.min(100, value || 0))}%)` }}
    />
  </ProgressPrimitive.Root>
));
Progress.displayName = 'Progress';
