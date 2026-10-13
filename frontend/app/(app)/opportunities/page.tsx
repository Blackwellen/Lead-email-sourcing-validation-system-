import { PageHeader } from '@/components/composites/page-header';
import { EmptyState } from '@/components/composites/empty-state';
import { Construction } from 'lucide-react';

export const metadata = { title: 'Opportunities' };

export default function Page() {
  return (
    <>
      <PageHeader title="Opportunities" description="Suggested next actions to grow coverage." />
      <EmptyState icon={Construction} title="Under construction" description="This page is being built." />
    </>
  );
}
