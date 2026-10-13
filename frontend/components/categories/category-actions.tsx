'use client';
import { api, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { useApiMutation } from '@/hooks/use-api-mutation';

const INVALIDATE = [qk.all('categories'), qk.all('businesses'), qk.all('contacts')];
export function useCategoryMutations() {
  const setEnabled = useApiMutation<Schema<'CategoryOut'>, { id: number; enabled: boolean }>({
    mutationFn: ({ id, enabled }) => api.post(`/api/categories/${id}/${enabled ? 'enable' : 'disable'}`),
    successMessage: (c) => `“${c.name}” ${c.enabled ? 'enabled' : 'disabled'}`, invalidate: INVALIDATE,
  });
  const remove = useApiMutation<Schema<'Ok'>, number>({
    mutationFn: (id) => api.delete(`/api/categories/${id}`), successMessage: 'Category deleted', invalidate: INVALIDATE,
  });
  return { setEnabled, remove };
}
