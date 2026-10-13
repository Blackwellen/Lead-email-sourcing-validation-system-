import { Suspense } from 'react';
import { LoadingState } from '@/components/composites/loading-state';
import { RunWizard } from '@/components/runs/wizard/wizard';

export const metadata = { title: 'New sourcing run' };

export default function Page() {
  return (
    <Suspense fallback={<LoadingState label="Loading wizard…" />}>
      <RunWizard />
    </Suspense>
  );
}
