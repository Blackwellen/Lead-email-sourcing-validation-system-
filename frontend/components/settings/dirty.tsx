'use client';
import * as React from 'react';

interface DirtyCtx { setDirty: (key: string, dirty: boolean) => void; anyDirty: () => boolean }
const Ctx = React.createContext<DirtyCtx | null>(null);

/**
 * Tracks which settings forms have unsaved edits. Guards: browser close/reload (beforeunload) and in-app link clicks
 * (capture-phase click on internal anchors). Tab switches inside the page are handled by SettingsPage via `anyDirty`.
 */
export function DirtyProvider({ children }: { children: React.ReactNode }) {
  const keys = React.useRef(new Set<string>());
  const setDirty = React.useCallback((key: string, dirty: boolean) => { if (dirty) keys.current.add(key); else keys.current.delete(key); }, []);
  const anyDirty = React.useCallback(() => keys.current.size > 0, []);
  React.useEffect(() => {
    const beforeUnload = (e: BeforeUnloadEvent) => { if (keys.current.size) { e.preventDefault(); e.returnValue = ''; } };
    const onClick = (e: MouseEvent) => {
      if (!keys.current.size || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey) return;
      const a = (e.target as Element | null)?.closest?.('a[href]') as HTMLAnchorElement | null;
      if (!a || a.target === '_blank' || a.origin !== window.location.origin) return;
      if (a.pathname === window.location.pathname && a.search === window.location.search) return;
      if (!window.confirm('You have unsaved changes on this page. Leave without saving?')) { e.preventDefault(); e.stopPropagation(); }
    };
    window.addEventListener('beforeunload', beforeUnload);
    document.addEventListener('click', onClick, true);
    return () => { window.removeEventListener('beforeunload', beforeUnload); document.removeEventListener('click', onClick, true); };
  }, []);
  const value = React.useMemo(() => ({ setDirty, anyDirty }), [setDirty, anyDirty]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useDirtyRegistry() {
  const c = React.useContext(Ctx);
  if (!c) throw new Error('DirtyProvider missing');
  return c;
}

/** Report a form's dirty state; clears on unmount. */
export function useReportDirty(key: string, dirty: boolean) {
  const { setDirty } = useDirtyRegistry();
  React.useEffect(() => { setDirty(key, dirty); return () => setDirty(key, false); }, [key, dirty, setDirty]);
}
