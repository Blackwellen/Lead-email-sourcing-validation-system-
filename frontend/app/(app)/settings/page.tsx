import { PageHeader } from '@/components/composites/page-header';
import { EmptyState } from '@/components/composites/empty-state';
import { RequireRole } from '@/lib/auth';
import { Construction } from 'lucide-react';

export const metadata = { title: 'Settings' };

export default function Page() {
  return (
    <RequireRole role="admin">
      <PageHeader title="Settings" description="Platform configuration." />
      <EmptyState icon={Construction} title="Under construction" description="This page is being built." />
    </RequireRole>
  );
}
