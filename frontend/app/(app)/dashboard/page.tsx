import { PageHeader } from '@/components/composites/page-header';
import { EmptyState } from '@/components/composites/empty-state';
import { Construction } from 'lucide-react';

export const metadata = { title: 'Dashboard' };

export default function Page() {
  return (
    <>
      <PageHeader title="Dashboard" description="Operational overview of runs, yield and data health." />
      <EmptyState icon={Construction} title="Under construction" description="This page is being built." />
    </>
  );
}
