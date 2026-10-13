import { PageHeader } from '@/components/composites/page-header';
import { EmptyState } from '@/components/composites/empty-state';
import { Construction } from 'lucide-react';

export const metadata = { title: 'Runs' };

export default function Page() {
  return (
    <>
      <PageHeader title="Runs" description="Create, monitor and manage sourcing runs." />
      <EmptyState icon={Construction} title="Under construction" description="This page is being built." />
    </>
  );
}
