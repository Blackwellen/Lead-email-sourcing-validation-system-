'use client';
import * as React from 'react';
import Link from 'next/link';
import { ChevronDown, ChevronRight, Download, FolderPlus, MoreHorizontal, Upload } from 'lucide-react';
import { api, downloadBlob } from '@/lib/api';
import { formatNumber } from '@/lib/format';
import { Can } from '@/lib/auth';
import { toast } from '@/lib/toast';
import { PageHeader } from '@/components/composites/page-header';
import { LoadingState } from '@/components/composites/loading-state';
import { ErrorState } from '@/components/composites/error-state';
import { EmptyState } from '@/components/composites/empty-state';
import { RelativeTime } from '@/components/composites/relative-time';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { ConfirmDialog } from '@/components/ui/alert-dialog';
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/dropdown-menu';
import { Search } from 'lucide-react';
import { useCategoryTree, type CategoryNode } from '@/components/contacts/shared';
import { CategoryFormDialog, ImportTaxonomyDialog, MergeCategoryDialog, SynonymsDialog } from './category-dialogs';
import { useCategoryMutations } from './category-actions';

interface FlatRow { node: CategoryNode; depth: number; hasKids: boolean }

function flatten(nodes: CategoryNode[], collapsed: Set<number>, q: string, depth = 0): FlatRow[] {
  const out: FlatRow[] = [];
  const needle = q.trim().toLowerCase();
  const matches = (n: CategoryNode): boolean => n.name.toLowerCase().includes(needle) || n.slug.includes(needle) || (n.children ?? []).some(matches);
  for (const n of nodes) {
    if (needle && !matches(n)) continue;
    out.push({ node: n, depth, hasKids: (n.children ?? []).length > 0 });
    if (!collapsed.has(n.id) || needle) out.push(...flatten(n.children ?? [], collapsed, q, depth + 1));
  }
  return out;
}

export function CategoryRowMenu({ node, onEdit, onSynonyms, onMerge, onAddChild }: {
  node: CategoryNode; onEdit: () => void; onSynonyms: () => void; onMerge: () => void; onAddChild: () => void;
}) {
  const { setEnabled, remove } = useCategoryMutations();
  const [confirm, setConfirm] = React.useState<'delete' | 'disable' | null>(null);
  return (
    <>
      <Can role="operator">
        <DropdownMenu>
          <DropdownMenuTrigger asChild><Button size="icon-sm" variant="ghost" aria-label={`Actions for ${node.name}`}><MoreHorizontal /></Button></DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onSelect={onEdit}>Edit…</DropdownMenuItem>
            <DropdownMenuItem onSelect={onSynonyms}>Edit synonyms…</DropdownMenuItem>
            <DropdownMenuItem onSelect={onAddChild}>Add sub-category…</DropdownMenuItem>
            <Can role="admin">
              <DropdownMenuSeparator />
              <DropdownMenuItem onSelect={onMerge}>Merge into…</DropdownMenuItem>
              {node.enabled ? <DropdownMenuItem onSelect={() => setConfirm('disable')}>Disable…</DropdownMenuItem> : <DropdownMenuItem onSelect={() => setEnabled.mutate({ id: node.id, enabled: true })}>Enable</DropdownMenuItem>}
              <DropdownMenuItem destructive onSelect={() => setConfirm('delete')}>Delete…</DropdownMenuItem>
            </Can>
          </DropdownMenuContent>
        </DropdownMenu>
      </Can>
      <ConfirmDialog open={confirm === 'delete'} onOpenChange={(o) => !o && setConfirm(null)} destructive title={`Delete “${node.name}”?`} confirmLabel="Delete category"
        description="Only an empty category can be deleted. If it still has businesses or sub-categories the server refuses; merge or disable it instead." onConfirm={() => remove.mutateAsync(node.id)} />
      <ConfirmDialog open={confirm === 'disable'} onOpenChange={(o) => !o && setConfirm(null)} destructive title={`Disable “${node.name}”?`} confirmLabel="Disable"
        description="This category and all of its sub-categories stop being offered for new runs. Existing businesses keep their category." onConfirm={() => setEnabled.mutateAsync({ id: node.id, enabled: false })} />
    </>
  );
}

export function CategoriesPage() {
  const tree = useCategoryTree();
  const [collapsed, setCollapsed] = React.useState<Set<number>>(new Set());
  const [q, setQ] = React.useState('');
  const [form, setForm] = React.useState<{ category: CategoryNode | null; parent?: number | null } | null>(null);
  const [syn, setSyn] = React.useState<CategoryNode | null>(null);
  const [merge, setMerge] = React.useState<CategoryNode | null>(null);
  const [importOpen, setImportOpen] = React.useState(false);

  const rows = React.useMemo(() => flatten(tree.data ?? [], collapsed, q), [tree.data, collapsed, q]);
  const toggle = (id: number) => setCollapsed((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n; });

  const exportDefs = async (format: 'json' | 'csv') => {
    try {
      const res = await api.get<unknown>('/api/categories/export', { query: { format } });
      const blob = res instanceof Blob ? res : new Blob([JSON.stringify(res, null, 2)], { type: 'application/json' });
      downloadBlob(blob, `taxonomy.${format}`);
    } catch (e) { toast.error('Export failed', (e as Error).message); }
  };

  return (
    <>
      <PageHeader
        title="Categories"
        description="The business taxonomy that runs target. Counts roll up through sub-categories."
        actions={<>
          <Can role="operator"><Button size="sm" onClick={() => setForm({ category: null })}><FolderPlus /> New category</Button></Can>
          <Can role="admin"><Button size="sm" variant="outline" onClick={() => setImportOpen(true)}><Upload /> Import taxonomy</Button></Can>
          <DropdownMenu>
            <DropdownMenuTrigger asChild><Button size="sm" variant="outline"><Download /> Export definitions <ChevronDown /></Button></DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem onSelect={() => exportDefs('json')}>JSON</DropdownMenuItem>
              <DropdownMenuItem onSelect={() => exportDefs('csv')}>CSV</DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </>}
      />
      {tree.isPending ? <LoadingState label="Loading categories…" rows={6} /> : tree.isError ? <ErrorState error={tree.error} onRetry={() => tree.refetch()} /> : (
        <>
          <div className="mb-3 w-full sm:w-72"><Input type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter categories…" aria-label="Filter categories" startAdornment={<Search />} /></div>
          {rows.length === 0 ? <EmptyState title={q ? 'No categories match' : 'No categories yet'} description={q ? 'Try a different search.' : 'Create a category or import a taxonomy.'} /> : (
            <div className="relative overflow-x-auto rounded-lg border bg-card shadow-card">
              <table className="w-full min-w-[820px] text-sm">
                <caption className="sr-only">Business categories</caption>
                <thead>
                  <tr className="border-b bg-muted/40 text-left text-xs text-muted-foreground">
                    <th scope="col" className="p-2.5 font-medium">Category</th><th scope="col" className="p-2.5 text-right font-medium">Businesses</th><th scope="col" className="p-2.5 text-right font-medium">Emails</th>
                    <th scope="col" className="p-2.5 text-right font-medium">Published</th><th scope="col" className="p-2.5 text-right font-medium">Eligible</th><th scope="col" className="p-2.5 font-medium">Countries</th>
                    <th scope="col" className="p-2.5 font-medium">Last run</th><th scope="col" className="p-2.5"><span className="sr-only">Actions</span></th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map(({ node, depth, hasKids }) => (
                    <tr key={node.id} className="border-b last:border-0 hover:bg-muted/30" data-testid={`cat-row-${node.slug}`}>
                      <th scope="row" className="p-2.5 text-left font-normal">
                        <div className="flex items-center gap-1" style={{ paddingLeft: depth * 20 }}>
                          {hasKids ? (
                            <button type="button" onClick={() => toggle(node.id)} aria-expanded={!collapsed.has(node.id)} aria-label={`${collapsed.has(node.id) ? 'Expand' : 'Collapse'} ${node.name}`} className="rounded p-0.5 hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                              {collapsed.has(node.id) ? <ChevronRight className="size-4" /> : <ChevronDown className="size-4" />}
                            </button>
                          ) : <span className="inline-block w-5" aria-hidden />}
                          <Link href={`/categories/${node.id}`} className="font-medium text-primary underline-offset-2 hover:underline">{node.name}</Link>
                          {!node.enabled && <Badge variant="neutral">{node.merged_into_id ? `Merged into #${node.merged_into_id}` : 'Disabled'}</Badge>}
                        </div>
                      </th>
                      <td className="tabular p-2.5 text-right">{formatNumber(node.business_count)}</td><td className="tabular p-2.5 text-right">{formatNumber(node.email_count)}</td>
                      <td className="tabular p-2.5 text-right">{formatNumber(node.published_count)}</td><td className="tabular p-2.5 text-right">{formatNumber(node.eligible_count)}</td>
                      <td className="p-2.5 text-xs">{(node.countries ?? []).length ? (node.countries ?? []).join(', ') : <span className="text-muted-foreground">—</span>}</td>
                      <td className="p-2.5 text-xs">{node.last_run_at ? <RelativeTime value={node.last_run_at} /> : <span className="text-muted-foreground">No run yet</span>}</td>
                      <td className="p-2.5 text-right">
                        <CategoryRowMenu node={node} onEdit={() => setForm({ category: node })} onSynonyms={() => setSyn(node)} onMerge={() => setMerge(node)} onAddChild={() => setForm({ category: null, parent: node.id })} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <p className="mt-2 text-xs text-muted-foreground">“Eligible” counts addresses whose marketing status is eligible and not suppressed. Last updated time is not tracked per category by the API.</p>
        </>
      )}
      <CategoryFormDialog open={!!form} onOpenChange={(o) => !o && setForm(null)} category={form?.category} defaultParentId={form?.parent} />
      <SynonymsDialog open={!!syn} onOpenChange={(o) => !o && setSyn(null)} category={syn} />
      <MergeCategoryDialog open={!!merge} onOpenChange={(o) => !o && setMerge(null)} category={merge} />
      <ImportTaxonomyDialog open={importOpen} onOpenChange={setImportOpen} />
    </>
  );
}
