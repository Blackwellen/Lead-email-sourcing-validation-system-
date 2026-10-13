'use client';
import * as React from 'react';
import { useRouter } from 'next/navigation';
import { useMe } from '@/lib/auth';
import { useLocalStorage } from '@/hooks/use-local-storage';
import { Sidebar } from './sidebar';
import { Topbar } from './topbar';
import { StatusBanners } from './banners';
import { LoadingState } from '@/components/composites/loading-state';
import { ErrorState } from '@/components/composites/error-state';
import { ApiError } from '@/lib/api';

/** Authenticated frame: guards the session, renders nav, banners and the main landmark. */
export function AppShell({ children }: { children: React.ReactNode }) {
  const me = useMe();
  const router = useRouter();
  const [collapsed, setCollapsed] = useLocalStorage('lead-sidebar-collapsed', false);

  React.useEffect(() => {
    if (me.isUnauthenticated) router.replace(`/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`);
  }, [me.isUnauthenticated, router]);

  // API down on first load (not a 401): keep the shell usable and explain.
  const apiDown = me.isError && !me.isUnauthenticated;

  return (
    <div className="flex h-dvh overflow-hidden bg-background">
      <a href="#main" className="sr-only z-[200] rounded-md bg-primary px-3 py-2 text-primary-foreground focus:not-sr-only focus:fixed focus:left-3 focus:top-3">Skip to content</a>
      <Sidebar collapsed={collapsed} onToggle={() => setCollapsed((c) => !c)} />
      <div className="flex min-w-0 flex-1 flex-col">
        <Topbar />
        <StatusBanners />
        <main id="main" tabIndex={-1} className="flex-1 overflow-y-auto focus-visible:outline-none">
          <div className="mx-auto w-full max-w-[1600px] px-4 py-6 sm:px-6 lg:px-8">
            {me.isLoading || me.isUnauthenticated ? (
              <LoadingState label="Loading your session…" rows={6} />
            ) : apiDown ? (
              <ErrorState error={me.error instanceof ApiError ? me.error : undefined} title="Could not load your session" message="The API did not respond as expected. You can retry, or check the System page once it is back." onRetry={() => me.refetch()} retrying={me.isFetching} />
            ) : (
              children
            )}
          </div>
        </main>
      </div>
    </div>
  );
}
