import * as React from 'react';
import { cn } from '@/lib/utils';

export const inputClass =
  'flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 text-sm shadow-card transition-colors placeholder:text-muted-foreground/80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-0 focus-visible:border-ring disabled:cursor-not-allowed disabled:opacity-50 aria-[invalid=true]:border-destructive aria-[invalid=true]:focus-visible:ring-destructive';

export interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  /** Adornment rendered inside the left edge (icon). */
  startAdornment?: React.ReactNode;
  endAdornment?: React.ReactNode;
  invalid?: boolean;
}

export const Input = React.forwardRef<HTMLInputElement, InputProps>(
  ({ className, type = 'text', startAdornment, endAdornment, invalid, ...props }, ref) => {
    if (!startAdornment && !endAdornment) {
      return <input ref={ref} type={type} aria-invalid={invalid || props['aria-invalid']} className={cn(inputClass, className)} {...props} />;
    }
    return (
      <div className={cn('relative flex w-full items-center', className)}>
        {startAdornment && <span className="pointer-events-none absolute left-3 flex items-center text-muted-foreground [&_svg]:size-4">{startAdornment}</span>}
        <input ref={ref} type={type} aria-invalid={invalid || props['aria-invalid']} className={cn(inputClass, startAdornment && 'pl-9', endAdornment && 'pr-9')} {...props} />
        {endAdornment && <span className="absolute right-2 flex items-center text-muted-foreground [&_svg]:size-4">{endAdornment}</span>}
      </div>
    );
  },
);
Input.displayName = 'Input';
