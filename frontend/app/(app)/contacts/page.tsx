import { Suspense } from 'react';
import { PageHeader } from '@/components/composites/page-header';
import { LoadingState } from '@/components/composites/loading-state';
import { ContactsPage } from '@/components/contacts/contacts-page';

export const metadata = { title: 'Contacts' };

export default function Page() {
  return (
    <>
      <PageHeader
        title="Contacts"
        description="Every discovered e-mail address with its evidence. Statuses are independent: syntax, mail routing, mailbox evidence, reachability and marketing eligibility."
      />
      <Suspense fallback={<LoadingState label="Loading contacts…" />}>
        <ContactsPage />
      </Suspense>
    </>
  );
}
