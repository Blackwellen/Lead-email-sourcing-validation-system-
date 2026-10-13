'use client';
import { useCallback } from 'react';
import { useLocalStorage } from './use-local-storage';

export interface SavedView<F = Record<string, unknown>> { id: string; name: string; filters: F }

/**
 * Saved filter views, stored per browser (hook point – swap the storage for an API later without changing callers).
 *   const { views, save, remove } = useSavedViews<MyFilters>('contacts');
 */
export function useSavedViews<F = Record<string, unknown>>(scope: string) {
  const [views, setViews] = useLocalStorage<SavedView<F>[]>(`lead-views:${scope}`, []);
  const save = useCallback((name: string, filters: F) => {
    const id = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
    setViews((v) => [...v, { id, name, filters }]);
    return id;
  }, [setViews]);
  const remove = useCallback((id: string) => setViews((v) => v.filter((x) => x.id !== id)), [setViews]);
  return { views, save, remove };
}
