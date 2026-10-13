import { Suspense } from 'react';
import { LoadingState } from '@/components/composites/loading-state';
import { OpportunitiesPage } from '@/components/discovery/signals/opportunities-page';

export const metadata = { title: 'Opportunity Signals' };

export default function Page() {
  return (
    <Suspense fallback={<LoadingState label="Loading signals…" />}>
      <OpportunitiesPage />
    </Suspense>
  );
}
