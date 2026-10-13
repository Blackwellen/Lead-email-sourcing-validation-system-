import { Suspense } from 'react';
import { LoadingState } from '@/components/composites/loading-state';
import { VerificationPage } from '@/components/verification/verification-page';

export const metadata = { title: 'Verification' };

export default function Page() {
  return (
    <Suspense fallback={<LoadingState label="Loading verification…" />}>
      <VerificationPage />
    </Suspense>
  );
}
