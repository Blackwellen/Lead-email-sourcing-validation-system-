import { LoadingState } from '@/components/composites/loading-state';

export default function Loading() {
  return <main className="mx-auto max-w-3xl p-8"><LoadingState rows={5} /></main>;
}
