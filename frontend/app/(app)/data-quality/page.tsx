import { Suspense } from 'react';
import { LoadingState } from '@/components/composites/loading-state';
import { DataQualityPage } from '@/components/discovery/quality/data-quality-page';

export const metadata = { title: 'Data Quality' };

export default function Page() {
  return (
    <Suspense fallback={<LoadingState label="Loading data quality…" />}>
      <DataQualityPage />
    </Suspense>
  );
}
