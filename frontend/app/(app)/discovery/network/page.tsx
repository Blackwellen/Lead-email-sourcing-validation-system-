import { PageHeader } from '@/components/composites/page-header';
import { EmptyState } from '@/components/composites/empty-state';
import { Construction } from 'lucide-react';

export const metadata = { title: 'Business Network' };

export default function Page() {
  return (
    <>
      <PageHeader title="Business Network" description="Relationships between businesses and domains." />
      <EmptyState icon={Construction} title="Under construction" description="This page is being built." />
    </>
  );
}
