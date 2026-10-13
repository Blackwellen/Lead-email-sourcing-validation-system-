import { PageHeader } from '@/components/composites/page-header';
import { EmptyState } from '@/components/composites/empty-state';
import { Construction } from 'lucide-react';

export const metadata = { title: 'Exports' };

export default function Page() {
  return (
    <>
      <PageHeader title="Exports" description="Export jobs and presets." />
      <EmptyState icon={Construction} title="Under construction" description="This page is being built." />
    </>
  );
}
