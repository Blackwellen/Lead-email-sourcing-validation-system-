'use client';
import * as React from 'react';
import * as LabelPrimitive from '@radix-ui/react-label';
import { cn } from '@/lib/utils';

export const Label = React.forwardRef<
  React.ComponentRef<typeof LabelPrimitive.Root>,
  React.ComponentPropsWithoutRef<typeof LabelPrimitive.Root> & { required?: boolean }
>(({ className, required, children, ...props }, ref) => (
  <LabelPrimitive.Root ref={ref} className={cn('text-sm font-medium leading-none peer-disabled:cursor-not-allowed peer-disabled:opacity-70', className)} {...props}>
    {children}
    {required && <span className="ml-0.5 text-destructive" aria-hidden>*</span>}
  </LabelPrimitive.Root>
));
Label.displayName = 'Label';

/** Label + control + hint + error wiring. Pass the control as children; it receives id via the render prop form. */
export function FormField({
  label, htmlFor, hint, error, required, className, children,
}: {
  label: React.ReactNode; htmlFor: string; hint?: React.ReactNode; error?: string; required?: boolean; className?: string; children: React.ReactNode;
}) {
  return (
    <div className={cn('space-y-1.5', className)}>
      <Label htmlFor={htmlFor} required={required}>{label}</Label>
      {children}
      {hint && !error && <p id={`${htmlFor}-hint`} className="text-xs text-muted-foreground">{hint}</p>}
      {error && <p id={`${htmlFor}-error`} role="alert" className="text-xs font-medium text-destructive">{error}</p>}
    </div>
  );
}
