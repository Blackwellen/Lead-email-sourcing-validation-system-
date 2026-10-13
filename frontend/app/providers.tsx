'use client';
import * as React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ApiError } from '@/lib/api';
import { TooltipProvider } from '@/components/ui/tooltip';
import { Toaster } from '@/components/ui/toast';
import { z } from 'zod';

// The CSP has no 'unsafe-eval' (next.config.ts). Zod 4 probes `new Function` to JIT-compile object parsers; jitless mode skips the
// probe (and the CSP violation report it triggers) and uses the regular interpreter.
z.config({ jitless: true });

function makeClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 30_000,
        refetchOnWindowFocus: false,
        retry: (count, err) => {
          if (err instanceof ApiError) {
            if (err.status >= 400 && err.status < 500) return false; // client errors won't fix themselves
            if (err.status === 0 || err.status >= 500) return count < 1;
          }
          return count < 1;
        },
      },
    },
  });
}

export function Providers({ children }: { children: React.ReactNode }) {
  const [client] = React.useState(makeClient);
  return (
    <QueryClientProvider client={client}>
      <TooltipProvider delayDuration={250}>
        {children}
        <Toaster />
      </TooltipProvider>
    </QueryClientProvider>
  );
}
