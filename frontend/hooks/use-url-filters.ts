'use client';
import { useCallback, useMemo } from 'react';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';

export type FilterValue = string | string[] | undefined;
export type UrlFilters = Record<string, FilterValue>;

/**
 * Filters kept in the URL query string (shareable, back-button friendly).
 * `defaults` define the known keys; keys listed in `arrayKeys` are comma-split into string[].
 * `set` merges a patch (undefined/''/[] removes the key) and resets nothing else; `reset` returns to defaults.
 *   const { filters, set, reset } = useUrlFilters({ q: '', status: [] }, { arrayKeys: ['status'] });
 */
export function useUrlFilters<T extends UrlFilters>(defaults: T, opts: { arrayKeys?: (keyof T & string)[] } = {}) {
  const router = useRouter();
  const pathname = usePathname();
  const sp = useSearchParams();
  const arrayKeys = opts.arrayKeys;
  const spStr = sp.toString();
  const arrayKeysKey = arrayKeys?.join('|');

  const filters = useMemo(() => {
    const out: UrlFilters = { ...defaults };
    const params = new URLSearchParams(spStr);
    for (const key of Object.keys(defaults)) {
      const raw = params.get(key);
      if (raw === null) continue;
      out[key] = arrayKeys?.includes(key) ? raw.split(',').filter(Boolean) : raw;
    }
    return out as T;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [spStr, arrayKeysKey]);

  const set = useCallback((patch: Partial<T>) => {
    const params = new URLSearchParams(spStr);
    for (const [k, v] of Object.entries(patch)) {
      const empty = v === undefined || v === '' || (Array.isArray(v) && v.length === 0);
      if (empty) params.delete(k);
      else params.set(k, Array.isArray(v) ? v.join(',') : String(v));
    }
    const qs = params.toString();
    router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
  }, [router, pathname, spStr]);

  const reset = useCallback(() => router.replace(pathname, { scroll: false }), [router, pathname]);
  return { filters, set, reset };
}
