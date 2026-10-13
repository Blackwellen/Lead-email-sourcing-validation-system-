import { Suspense } from 'react';
import { SettingsPage } from '@/components/settings/settings-page';
import { LoadingState } from '@/components/composites/loading-state';

export const metadata = { title: 'Settings' };

export default function Page() {
  return (
    <Suspense fallback={<LoadingState variant="skeleton" rows={6} label="Loading settings" />}>
      <SettingsPage />
    </Suspense>
  );
}
