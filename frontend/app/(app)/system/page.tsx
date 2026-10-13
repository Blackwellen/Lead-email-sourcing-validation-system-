import { PageHeader } from '@/components/composites/page-header';
import { EmptyState } from '@/components/composites/empty-state';
import { RequireRole } from '@/lib/auth';
import { Construction } from 'lucide-react';

export const metadata = { title: 'System' };

export default function Page() {
  return (
    <RequireRole role="admin">
      <PageHeader title="System" description="Workers, queues, health and maintenance." />
      <EmptyState icon={Construction} title="Under construction" description="This page is being built." />
    </RequireRole>
  );
}
