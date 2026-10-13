'use client';
import * as React from 'react';
import { qk } from '@/lib/query-keys';
import { ContactFilterBar, contactQuery, useContactFilters } from './contact-filters';
import { ContactsTable } from './contacts-table';

export function ContactsPage() {
  const { filters, set, reset } = useContactFilters();
  const query = React.useMemo(() => contactQuery(filters), [filters]);
  return (
    <>
      <ContactFilterBar filters={filters} set={set} reset={reset} scope="contacts" />
      <ContactsTable caption="Contacts" tableId="contacts" queryKey={qk.list('contacts')} endpoint="/api/contacts" filters={query} emptyFiltered={Object.keys(query).length > 0} />
    </>
  );
}
