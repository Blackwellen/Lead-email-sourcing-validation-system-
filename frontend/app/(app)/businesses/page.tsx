import { Suspense } from 'react';
import { PageHeader } from '@/components/composites/page-header';
import { LoadingState } from '@/components/composites/loading-state';
import { BusinessesPage } from '@/components/businesses/businesses-page';

export const metadata = { title: 'Businesses' };

export default function Page() {
  return (
    <>
      <PageHeader title="Businesses" description="Organisations discovered across runs, with the status of their website match and the e-mail addresses found." />
      <Suspense fallback={<LoadingState label="Loading businesses…" />}>
        <BusinessesPage />
      </Suspense>
    </>
  );
}
