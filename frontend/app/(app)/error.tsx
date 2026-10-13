'use client';
import { useEffect } from 'react';
import { ErrorState } from '@/components/composites/error-state';

export default function SectionError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => { console.error(error); }, [error]);
  return <ErrorState title="This page hit an error" message={error.message} onRetry={reset} />;
}
