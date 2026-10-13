import { Suspense } from 'react';
import { LoadingState } from '@/components/composites/loading-state';
import { ExplorerPage } from '@/components/discovery/explorer/explorer-page';

export const metadata = { title: 'Source Explorer' };

export default function Page() {
  return (
    <Suspense fallback={<LoadingState label="Loading source explorer…" />}>
      <ExplorerPage />
    </Suspense>
  );
}
