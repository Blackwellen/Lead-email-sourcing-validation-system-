'use client';
import * as React from 'react';
import { AlertTriangle, WifiOff } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { ApiError } from '@/lib/api';
import { cn } from '@/lib/utils';

/** Inline error panel with retry. Pass the thrown error (ApiError aware) or an explicit message. */
export function ErrorState({ error, title, message, onRetry, retrying, className, compact }: {
  error?: unknown;
  title?: string;
  message?: React.ReactNode;
  onRetry?: () => void;
  retrying?: boolean;
  className?: string;
  compact?: boolean;
}) {
  const api = error instanceof ApiError ? error : null;
  const offline = api?.isNetwork;
  const Icon = offline ? WifiOff : AlertTriangle;
  const heading = title ?? (offline ? 'Cannot reach the API' : api?.isForbidden ? 'Not allowed' : 'Something went wrong');
  const body = message ?? (error instanceof Error ? error.message : 'An unexpected error occurred.');
  return (
    <div role="alert" className={cn('flex flex-col items-center justify-center rounded-lg border border-danger-border bg-danger-soft/50 px-6 text-center', compact ? 'py-8' : 'py-12', className)}>
      <span className="mb-3 flex size-10 items-center justify-center rounded-full bg-danger-soft text-danger"><Icon className="size-5" aria-hidden /></span>
      <h2 className="text-sm font-semibold">{heading}</h2>
      <p className="mt-1 max-w-md text-sm text-muted-foreground">{body}</p>
      {api?.requestId && <p className="mt-1 font-mono text-2xs text-muted-foreground">Request ID: {api.requestId}</p>}
      {onRetry && <Button className="mt-4" variant="outline" size="sm" onClick={onRetry} loading={retrying}>Try again</Button>}
    </div>
  );
}
