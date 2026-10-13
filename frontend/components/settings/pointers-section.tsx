'use client';
import Link from 'next/link';
import { useQuery } from '@tanstack/react-query';
import { ArrowRight, FolderTree, Globe2, Database } from 'lucide-react';
import { api, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { formatNumber } from '@/lib/format';

export function PointersSection() {
  const countries = useQuery({ queryKey: qk.custom('geo', 'countries-with-data'), queryFn: ({ signal }) => api.get<Schema<'CountryRow'>[]>('/api/geo/countries', { query: { with_data: true }, signal }), staleTime: 60_000 });
  return (
    <div className="space-y-4">
      <p className="text-sm text-muted-foreground">Countries and categories are managed where you use them, not as settings. These shortcuts take you to the right place.</p>
      <ul className="grid grid-cols-1 gap-4 md:grid-cols-3">
        <Pointer href="/categories" icon={<FolderTree />} title="Categories" body="The business taxonomy: add, rename, merge, disable categories, edit keywords and synonyms, import or export as CSV." />
        <Pointer href="/sources/explore" icon={<Globe2 />} title="Countries and coverage" body={countries.data ? `${formatNumber(countries.data.length)} countries have data. Explore which sources cover which country and what is missing.` : 'Explore which sources cover which country and what is missing.'} />
        <Pointer href="/sources" icon={<Database />} title="Sources" body="Enable or disable data sources, set their priority and configuration, and upload permitted custom files." />
      </ul>
    </div>
  );
}

function Pointer({ href, icon, title, body }: { href: string; icon: React.ReactNode; title: string; body: string }) {
  return (
    <li>
      <Card className="h-full transition-colors hover:border-primary/40">
        <CardHeader><CardTitle className="flex items-center gap-2 text-base [&_svg]:size-4"><span aria-hidden>{icon}</span>{title}</CardTitle></CardHeader>
        <CardContent className="space-y-3"><CardDescription>{body}</CardDescription>
          <Link href={href} className="inline-flex items-center gap-1 rounded text-sm font-medium text-primary underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">Open {title.toLowerCase()} <ArrowRight className="size-3.5" aria-hidden /></Link>
        </CardContent>
      </Card>
    </li>
  );
}
