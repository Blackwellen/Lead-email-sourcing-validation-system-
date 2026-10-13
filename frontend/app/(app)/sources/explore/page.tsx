import { PageHeader } from '@/components/composites/page-header';
import { EmptyState } from '@/components/composites/empty-state';
import { Construction } from 'lucide-react';

export const metadata = { title: 'Source Explorer' };

export default function Page() {
  return (
    <>
      <PageHeader title="Source Explorer" description="Inspect and test individual sources." />
      <EmptyState icon={Construction} title="Under construction" description="This page is being built." />
    </>
  );
}
