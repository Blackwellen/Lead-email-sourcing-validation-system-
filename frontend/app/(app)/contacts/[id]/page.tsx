import { ContactDetailPage } from '@/components/contacts/contact-detail';

export const metadata = { title: 'Contact' };

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <ContactDetailPage id={id} />;
}
