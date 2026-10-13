import { PageHeader } from '@/components/composites/page-header';
import { EmptyState } from '@/components/composites/empty-state';
import { Construction } from 'lucide-react';

export const metadata = { title: 'Sources' };

export default function Page() {
  return (
    <>
      <PageHeader title="Sources" description="Data source adapters and their health." />
      <EmptyState icon={Construction} title="Under construction" description="This page is being built." />
    </>
  );
}
