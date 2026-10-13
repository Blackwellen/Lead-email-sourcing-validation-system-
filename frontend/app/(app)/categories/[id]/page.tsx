import { Suspense } from 'react';
import { LoadingState } from '@/components/composites/loading-state';
import { CategoryDetailPage } from '@/components/categories/category-detail';

export const metadata = { title: 'Category' };

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return (
    <Suspense fallback={<LoadingState label="Loading category…" />}>
      <CategoryDetailPage id={id} />
    </Suspense>
  );
}
