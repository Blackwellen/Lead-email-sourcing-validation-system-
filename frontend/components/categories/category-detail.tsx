'use client';
import * as React from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useQuery } from '@tanstack/react-query';
import { api, type ApiError } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { Can } from '@/lib/auth';
import { PageHeader } from '@/components/composites/page-header';
import { StatCard } from '@/components/composites/stat-card';
import { LoadingState } from '@/components/composites/loading-state';
import { ErrorState } from '@/components/composites/error-state';
import { RelativeTime } from '@/components/composites/relative-time';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { ContactFilterBar, contactQuery, useContactFilters } from '@/components/contacts/contact-filters';
import { ContactsTable } from '@/components/contacts/contacts-table';
import type { CategoryNode } from '@/components/contacts/shared';
import { CategoryFormDialog, MergeCategoryDialog, SynonymsDialog } from './category-dialogs';
import { CategoryRowMenu } from './categories-page';

function Chips({ items, empty }: { items: string[]; empty: string }) {
  if (!items.length) return <p className="text-sm text-muted-foreground">{empty}</p>;
  return <ul className="flex flex-wrap gap-1.5">{items.map((i) => <li key={i}><Badge variant="outline">{i}</Badge></li>)}</ul>;
}

export function CategoryDetailPage({ id }: { id: string }) {
  const router = useRouter();
  const q = useQuery<CategoryNode, ApiError>({ queryKey: qk.detail('categories', id), queryFn: ({ signal }) => api.get<CategoryNode>(`/api/categories/${id}`, { signal }) });
  const { filters, set, reset } = useContactFilters();
  const query = React.useMemo(() => contactQuery(filters), [filters]);
  const [edit, setEdit] = React.useState(false);
  const [syn, setSyn] = React.useState(false);
  const [merge, setMerge] = React.useState(false);
  const [child, setChild] = React.useState(false);

  if (q.isPending) return <LoadingState variant="cards" label="Loading category…" />;
  if (q.isError) return <ErrorState error={q.error} onRetry={() => q.refetch()} title={q.error.status === 404 ? 'Category not found' : undefined} />;
  const c = q.data;
  return (
    <>
      <PageHeader
        breadcrumbs={[{ label: 'Categories', href: '/categories' }, { label: c.name }]}
        title={c.name}
        meta={<><Badge variant="outline">{c.level}</Badge>{!c.enabled && <Badge variant="neutral">{c.merged_into_id ? 'Merged' : 'Disabled'}</Badge>}</>}
        description={`Slug: ${c.slug}`}
        actions={<><Can role="operator"><Button size="sm" variant="outline" onClick={() => setEdit(true)}>Edit…</Button></Can><CategoryRowMenu node={c} onEdit={() => setEdit(true)} onSynonyms={() => setSyn(true)} onMerge={() => setMerge(true)} onAddChild={() => setChild(true)} /></>}
      />
      <div className="mb-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard label="Businesses" value={c.business_count} /><StatCard label="E-mail addresses" value={c.email_count} />
        <StatCard label="Published contacts" value={c.published_count} /><StatCard label="Marketing-eligible" value={c.eligible_count} hint="Eligible and not suppressed" />
      </div>
      <div className="mb-6 grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader><CardTitle className="text-base">Definition</CardTitle></CardHeader>
          <CardContent className="space-y-3 text-sm">
            <div><h3 className="mb-1 text-xs font-medium text-muted-foreground">Keywords</h3><Chips items={c.keywords} empty="None" /></div>
            <div><h3 className="mb-1 text-xs font-medium text-muted-foreground">Synonyms</h3><Chips items={c.synonyms} empty="None" /></div>
            <div><h3 className="mb-1 text-xs font-medium text-muted-foreground">Excluded keywords</h3><Chips items={c.excluded_keywords} empty="None" /></div>
            <div><h3 className="mb-1 text-xs font-medium text-muted-foreground">Default source strategies</h3><Chips items={c.default_sources} empty="No defaults; runs choose their own sources" /></div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader><CardTitle className="text-base">Coverage</CardTitle></CardHeader>
          <CardContent className="space-y-3 text-sm">
            <div><h3 className="mb-1 text-xs font-medium text-muted-foreground">Countries</h3><Chips items={c.countries ?? []} empty="No businesses yet" /></div>
            <div><h3 className="mb-1 text-xs font-medium text-muted-foreground">Last run</h3>{c.last_run_at ? <RelativeTime value={c.last_run_at} /> : <span className="text-muted-foreground">No run yet</span>}</div>
            <div><h3 className="mb-1 text-xs font-medium text-muted-foreground">Sub-categories</h3>
              {(c.children ?? []).length === 0 ? <p className="text-muted-foreground">None</p> : <ul className="flex flex-wrap gap-1.5">{(c.children ?? []).map((k) => <li key={k.id}><Link className="text-primary underline-offset-2 hover:underline" href={`/categories/${k.id}`}>{k.name}</Link></li>)}</ul>}</div>
          </CardContent>
        </Card>
      </div>
      <h2 className="mb-2 text-base font-semibold">Contacts in this category</h2>
      <ContactFilterBar filters={filters} set={set} reset={reset} scope="category-contacts" hide={['category_id']} />
      <ContactsTable caption={`Contacts in ${c.name}`} tableId="category-contacts" queryKey={qk.list('categories', { id, kind: 'contacts' })} endpoint={`/api/categories/${id}/contacts`} filters={query} emptyFiltered={Object.keys(query).length > 0} />
      <CategoryFormDialog open={edit} onOpenChange={setEdit} category={c} />
      <CategoryFormDialog open={child} onOpenChange={setChild} category={null} defaultParentId={c.id} />
      <SynonymsDialog open={syn} onOpenChange={setSyn} category={c} />
      <MergeCategoryDialog open={merge} onOpenChange={setMerge} category={c} onDone={() => router.push('/categories')} />
    </>
  );
}
