import { PageHeader } from '@/components/composites/page-header';
import { EmptyState } from '@/components/composites/empty-state';
import { Construction } from 'lucide-react';

export const metadata = { title: 'Verification' };

export default function Page() {
  return (
    <>
      <PageHeader title="Verification" description="Email verification batches and history." />
      <EmptyState icon={Construction} title="Under construction" description="This page is being built." />
    </>
  );
}
