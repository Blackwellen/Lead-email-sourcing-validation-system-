import Link from 'next/link';
import { Compass } from 'lucide-react';
import { EmptyState } from '@/components/composites/empty-state';
import { Button } from '@/components/ui/button';

export default function NotFound() {
  return (
    <main className="mx-auto flex min-h-screen max-w-lg items-center p-6">
      <EmptyState className="w-full" icon={Compass} title="Page not found" description="The page you are looking for does not exist or has moved."
        action={<Button asChild><Link href="/dashboard">Go to dashboard</Link></Button>} />
    </main>
  );
}
