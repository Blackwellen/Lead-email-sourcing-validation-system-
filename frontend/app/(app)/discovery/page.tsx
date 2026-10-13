import { Suspense } from 'react';
import { LoadingState } from '@/components/composites/loading-state';
import { DiscoveryPage } from '@/components/discovery/overview/discovery-page';

export const metadata = { title: 'Discovery Intelligence' };

export default function Page() {
  return (
    <Suspense fallback={<LoadingState label="Loading discovery…" />}>
      <DiscoveryPage />
    </Suspense>
  );
}
