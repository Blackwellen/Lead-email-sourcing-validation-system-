'use client';
import * as React from 'react';
import { ChevronRight, Search, X } from 'lucide-react';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';
import { LoadingState } from '@/components/composites/loading-state';
import { ErrorState } from '@/components/composites/error-state';
import { EmptyState } from '@/components/composites/empty-state';
import { useCategoryTree, type CategoryNode } from '../use-categories';

const LEVEL_LABEL: Record<string, string> = { category: 'Category', subcategory: 'Subcategory', specialism: 'Specialism' };

/**
 * Hierarchical Category -> Subcategory -> Specialism multi-select with search.
 * Selecting a node includes all of its descendants in the run (the backend expands descendants), so descendants of a selected
 * node are shown as "included" rather than individually selectable.
 */
export function CategoryTreeSelect({ value, onChange, invalid }: { value: number[]; onChange: (v: number[]) => void; invalid?: boolean }) {
  const { tree, flat, isPending, isError, error, refetch } = useCategoryTree();
  const [query, setQuery] = React.useState('');
  const [expanded, setExpanded] = React.useState<Set<number>>(new Set());
  const selected = React.useMemo(() => new Set(value), [value]);
  const q = query.trim().toLowerCase();

  // ids that match the query or have a matching descendant
  const visible = React.useMemo(() => {
    if (!q) return null;
    const keep = new Set<number>();
    const walk = (n: CategoryNode): boolean => {
      let any = n.name.toLowerCase().includes(q) || (n.synonyms ?? []).some((s) => s.toLowerCase().includes(q)) || (n.keywords ?? []).some((s) => s.toLowerCase().includes(q));
      for (const c of n.children ?? []) if (walk(c)) any = true;
      if (any) keep.add(n.id);
      return any;
    };
    tree.forEach(walk);
    return keep;
  }, [q, tree]);

  const toggle = (id: number, on: boolean) => onChange(on ? [...value, id] : value.filter((v) => v !== id));
  const coveredBy = (id: number) => (flat.get(id)?.ancestors ?? []).find((a) => selected.has(a));

  const renderNode = (n: CategoryNode, depth: number): React.ReactNode => {
    if (visible && !visible.has(n.id)) return null;
    const kids = (n.children ?? []).filter((c) => !visible || visible.has(c.id));
    const open = !!visible || expanded.has(n.id);
    const cover = coveredBy(n.id);
    const isSel = selected.has(n.id);
    const cid = `cat-${n.id}`;
    return (
      <li key={n.id}>
        <div className={cn('flex items-center gap-1.5 rounded px-1 py-1 hover:bg-muted/60', isSel && 'bg-accent/50')} style={{ paddingLeft: `${depth * 18 + 4}px` }}>
          {kids.length > 0 ? (
            <button type="button" aria-expanded={open} aria-label={`${open ? 'Collapse' : 'Expand'} ${n.name}`} onClick={() => setExpanded((s) => { const x = new Set(s); if (x.has(n.id)) x.delete(n.id); else x.add(n.id); return x; })}
              className="flex size-6 shrink-0 items-center justify-center rounded text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
              <ChevronRight className={cn('size-4 transition-transform', open && 'rotate-90')} aria-hidden />
            </button>
          ) : <span className="size-6 shrink-0" aria-hidden />}
          <Checkbox id={cid} checked={isSel || cover !== undefined} disabled={cover !== undefined} onCheckedChange={(v) => toggle(n.id, v === true)} aria-describedby={cover !== undefined ? `${cid}-inc` : undefined} />
          <label htmlFor={cid} className="min-w-0 flex-1 cursor-pointer text-sm">
            <span className={cn(cover !== undefined && 'text-muted-foreground')}>{n.name}</span>
            <span className="ml-2 text-xs text-muted-foreground">{LEVEL_LABEL[n.level] ?? n.level}</span>
            {cover !== undefined && <span id={`${cid}-inc`} className="ml-2 text-xs italic text-muted-foreground">included via {flat.get(cover)?.name}</span>}
          </label>
        </div>
        {open && kids.length > 0 && <ul>{kids.map((c) => renderNode(c, depth + 1))}</ul>}
      </li>
    );
  };

  return (
    <div className={cn('rounded-md border', invalid && 'border-destructive')}>
      <div className="space-y-2 border-b p-2">
        <Input type="search" aria-label="Search categories" placeholder="Search categories, synonyms and keywords…" value={query} onChange={(e) => setQuery(e.target.value)} startAdornment={<Search />}
          endAdornment={query ? <button type="button" aria-label="Clear category search" onClick={() => setQuery('')} className="rounded p-0.5 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"><X /></button> : undefined} />
        {value.length > 0 && (
          <ul className="flex flex-wrap gap-1.5" aria-label="Selected categories">
            {value.map((id) => (
              <li key={id}>
                <Badge variant="primary" className="gap-1 pr-1">
                  {flat.get(id)?.path ?? `#${id}`}
                  <button type="button" aria-label={`Remove ${flat.get(id)?.name ?? id}`} onClick={() => toggle(id, false)} className="rounded p-0.5 hover:bg-foreground/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"><X className="size-3" /></button>
                </Badge>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div className="max-h-72 overflow-y-auto p-1" role="group" aria-label="Category tree">
        {isPending ? <LoadingState label="Loading categories…" rows={4} /> : isError ? <ErrorState compact error={error} onRetry={() => refetch()} /> : tree.length === 0 ? (
          <EmptyState compact title="No categories yet" description="Import or create categories on the Categories page. You can still use keywords." />
        ) : visible && visible.size === 0 ? (
          <p className="px-3 py-6 text-center text-sm text-muted-foreground">No categories match “{query}”.</p>
        ) : <ul>{tree.map((n) => renderNode(n, 0))}</ul>}
      </div>
    </div>
  );
}
