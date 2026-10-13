import { PageHeader } from '@/components/composites/page-header';
import { EmptyState } from '@/components/composites/empty-state';
import { Construction } from 'lucide-react';

export const metadata = { title: 'Contacts' };

export default function Page() {
  return (
    <>
      <PageHeader title="Contacts" description="Email contacts with reachability and evidence." />
      <EmptyState icon={Construction} title="Under construction" description="This page is being built." />
    </>
  );
}
