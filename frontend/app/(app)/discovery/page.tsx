import { PageHeader } from '@/components/composites/page-header';
import { EmptyState } from '@/components/composites/empty-state';
import { Construction } from 'lucide-react';

export const metadata = { title: 'Discovery' };

export default function Page() {
  return (
    <>
      <PageHeader title="Discovery" description="Discovery pipeline coverage and gaps." />
      <EmptyState icon={Construction} title="Under construction" description="This page is being built." />
    </>
  );
}
