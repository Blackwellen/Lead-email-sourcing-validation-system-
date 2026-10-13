'use client';
import * as React from 'react';
import { useRouter } from 'next/navigation';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ShieldX } from 'lucide-react';
import { api, setCsrfToken, type Role, type Session, type User } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { useApiMutation } from '@/hooks/use-api-mutation';
import { LoadingState } from '@/components/composites/loading-state';
import { EmptyState } from '@/components/composites/empty-state';
import { Button } from '@/components/ui/button';

export const ROLE_RANK: Record<Role, number> = { viewer: 1, operator: 2, admin: 3 };
export const ROLE_LABEL: Record<Role, string> = { viewer: 'Viewer', operator: 'Operator', admin: 'Admin' };

/** true when `role` is at least `min` (admin > operator > viewer). Unknown roles rank 0. */
export function roleAtLeast(role: string | null | undefined, min: Role) {
  return (ROLE_RANK[role as Role] ?? 0) >= ROLE_RANK[min];
}

/** Current session. 401 does NOT redirect here – the shell guard does; login page uses this to detect existing sessions. */
export function useMe() {
  const q = useQuery<Session>({
    queryKey: qk.me,
    queryFn: async () => {
      const s = await api.get<Session>('/api/me', { skipAuthRedirect: true });
      setCsrfToken(s.csrf_token);
      return s;
    },
    retry: false,
    staleTime: 60_000,
  });
  const user: User | null = q.data?.user ?? null;
  const role = (user?.role as Role | undefined) ?? null;
  return {
    ...q,
    session: q.data ?? null,
    user,
    role,
    isAuthenticated: !!user,
    isUnauthenticated: q.isError && (q.error as { status?: number })?.status === 401,
    can: (min: Role) => roleAtLeast(role, min),
  };
}

/** Render children only when the current user has at least `role`; otherwise `fallback` (default: nothing). For hiding buttons/controls. */
export function Can({ role, children, fallback = null }: { role: Role; children: React.ReactNode; fallback?: React.ReactNode }) {
  const { role: mine } = useMe();
  return roleAtLeast(mine, role) ? <>{children}</> : <>{fallback}</>;
}

/** Page/section guard: loading state while resolving, redirect to /login if signed out, "no access" panel when role too low. */
export function RequireRole({ role, children, fallback }: { role: Role; children: React.ReactNode; fallback?: React.ReactNode }) {
  const me = useMe();
  const router = useRouter();
  React.useEffect(() => {
    if (me.isUnauthenticated) router.replace(`/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`);
  }, [me.isUnauthenticated, router]);
  if (me.isLoading || me.isUnauthenticated) return <LoadingState label="Checking access…" />;
  if (!roleAtLeast(me.role, role)) {
    return fallback ?? (
      <EmptyState icon={ShieldX} title="You don’t have access to this page" description={`This area requires the ${ROLE_LABEL[role]} role. Your role is ${me.role ? ROLE_LABEL[me.role] : 'unknown'}. Ask an administrator if you need access.`} />
    );
  }
  return <>{children}</>;
}

export function useLogout() {
  const qc = useQueryClient();
  const router = useRouter();
  return useApiMutation<unknown, void>({
    mutationFn: () => api.post('/api/auth/logout', undefined, { skipAuthRedirect: true }),
    errorMessage: false,
    onSettled: () => {
      setCsrfToken(null);
      qc.clear();
      router.replace('/login');
    },
  });
}

export function LogoutButton() {
  const logout = useLogout();
  return <Button variant="outline" size="sm" loading={logout.isPending} onClick={() => logout.mutate()}>Sign out</Button>;
}
