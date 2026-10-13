import { PageHeader } from '@/components/composites/page-header';
import { EmptyState } from '@/components/composites/empty-state';
import { Construction } from 'lucide-react';

export const metadata = { title: 'Data Quality' };

export default function Page() {
  return (
    <>
      <PageHeader title="Data Quality" description="Completeness and quality metrics." />
      <EmptyState icon={Construction} title="Under construction" description="This page is being built." />
    </>
  );
}
