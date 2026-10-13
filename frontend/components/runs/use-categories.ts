'use client';
import * as React from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';

export type CategoryNode = Schema<'CategoryNode'>;
export interface FlatCategory { id: number; name: string; level: string; parentId: number | null; path: string; ancestors: number[] }

/** Enabled category tree (Category -> Subcategory -> Specialism) plus an id -> path lookup. */
export function useCategoryTree() {
  const q = useQuery({
    queryKey: qk.custom('categories', 'tree', { enabledOnly: true }),
    queryFn: ({ signal }) => api.get<CategoryNode[]>('/api/categories', { query: { include_disabled: false }, signal }),
    staleTime: 5 * 60_000,
  });
  const flat = React.useMemo(() => {
    const m = new Map<number, FlatCategory>();
    const walk = (nodes: CategoryNode[], parentPath: string, anc: number[]) => {
      for (const n of nodes) {
        const path = parentPath ? `${parentPath} › ${n.name}` : n.name;
        m.set(n.id, { id: n.id, name: n.name, level: n.level, parentId: n.parent_id, path, ancestors: anc });
        walk(n.children ?? [], path, [...anc, n.id]);
      }
    };
    walk(q.data ?? [], '', []);
    return m;
  }, [q.data]);
  return { ...q, tree: q.data ?? [], flat };
}
