import { Suspense } from 'react';
import { LoadingState } from '@/components/composites/loading-state';
import { RunsList } from '@/components/runs/runs-list';

export const metadata = { title: 'Runs' };

export default function Page() {
  return (
    <Suspense fallback={<LoadingState label="Loading runs…" />}>
      <RunsList />
    </Suspense>
  );
}
