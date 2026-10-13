import { Suspense } from 'react';
import { LoadingState } from '@/components/composites/loading-state';
import { NetworkPage } from '@/components/discovery/network/network-page';

export const metadata = { title: 'Business Network' };

export default function Page() {
  return (
    <Suspense fallback={<LoadingState label="Loading business network…" />}>
      <NetworkPage />
    </Suspense>
  );
}
