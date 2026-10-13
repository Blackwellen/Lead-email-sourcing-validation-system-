'use client';
import { useCallback, useEffect, useSyncExternalStore } from 'react';

export type Theme = 'light' | 'dark';
const KEY = 'lead-theme';

function read(): Theme {
  try { if (localStorage.getItem(KEY) === 'dark') return 'dark'; } catch { /* storage blocked */ }
  return 'light';
}
const listeners = new Set<() => void>();
function apply(t: Theme) {
  document.documentElement.classList.toggle('dark', t === 'dark');
  document.documentElement.style.colorScheme = t;
}

/** Light is the default; choice persisted in localStorage. An inline script in the root layout applies it pre-hydration. */
export function useTheme() {
  const theme = useSyncExternalStore(
    (cb) => { listeners.add(cb); return () => { listeners.delete(cb); }; },
    () => (document.documentElement.classList.contains('dark') ? 'dark' : 'light') as Theme,
    () => 'light' as Theme,
  );
  useEffect(() => { apply(read()); listeners.forEach((l) => l()); }, []);
  const setTheme = useCallback((t: Theme) => {
    try { localStorage.setItem(KEY, t); } catch { /* ignore */ }
    apply(t); listeners.forEach((l) => l());
  }, []);
  const toggle = useCallback(() => setTheme(document.documentElement.classList.contains('dark') ? 'light' : 'dark'), [setTheme]);
  return { theme, setTheme, toggle };
}

export const themeInitScript = `try{if(localStorage.getItem('${KEY}')==='dark'){document.documentElement.classList.add('dark');document.documentElement.style.colorScheme='dark'}}catch(e){}`;
