'use client';
import { useEffect } from 'react';
import { ErrorState } from '@/components/composites/error-state';

export default function GlobalError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => { console.error(error); }, [error]);
  return (
    <main className="mx-auto flex min-h-screen max-w-lg items-center p-6">
      <ErrorState className="w-full" title="This page failed to load" message={error.digest ? `${error.message} (ref ${error.digest})` : error.message} onRetry={reset} />
    </main>
  );
}
