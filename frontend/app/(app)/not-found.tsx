import Link from 'next/link';
import { Compass } from 'lucide-react';
import { EmptyState } from '@/components/composites/empty-state';
import { Button } from '@/components/ui/button';

export default function NotFound() {
  return <EmptyState icon={Compass} title="Not found" description="That record or page does not exist (or was removed)." action={<Button asChild variant="outline"><Link href="/dashboard">Back to dashboard</Link></Button>} />;
}
