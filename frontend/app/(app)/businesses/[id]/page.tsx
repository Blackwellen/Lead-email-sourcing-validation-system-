import { BusinessDetailPage } from '@/components/businesses/business-detail';

export const metadata = { title: 'Business' };

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <BusinessDetailPage id={id} />;
}
