'use client';
import * as React from 'react';
import { LogOut, Menu, Search, UserRound } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Kbd } from '@/components/ui/kbd';
import { Sheet, SheetContent, SheetDescription, SheetTitle, SheetTrigger } from '@/components/ui/sheet';
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/dropdown-menu';
import { GlobalSearchDialog } from './global-search';
import { ROLE_LABEL, useLogout, useMe } from '@/lib/auth';
import { Brand, NavList } from './sidebar';
import { ThemeToggle } from './theme-toggle';
import type { Role } from '@/lib/api';

const roleVariant = { admin: 'primary', operator: 'info', viewer: 'neutral' } as const;

function GlobalSearch() {
  const [open, setOpen] = React.useState(false);
  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (e.key === '/' && !(t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable))) { e.preventDefault(); setOpen(true); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="hidden h-9 w-full max-w-sm items-center gap-2 rounded-md border border-input bg-muted/50 px-3 text-sm text-muted-foreground transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring sm:flex"
      >
        <Search className="size-4" aria-hidden />
        <span className="flex-1 text-left">Search businesses, contacts, runs…</span>
        <Kbd aria-hidden>/</Kbd>
        <span className="sr-only">Open search (shortcut: slash)</span>
      </button>
      <Button variant="ghost" size="icon" className="sm:hidden" aria-label="Search" onClick={() => setOpen(true)}><Search /></Button>
      <GlobalSearchDialog open={open} onOpenChange={setOpen} />
    </>
  );
}

export function Topbar() {
  const { user, role } = useMe();
  const logout = useLogout();
  const [navOpen, setNavOpen] = React.useState(false);
  return (
    <header className="sticky top-0 z-30 flex h-14 shrink-0 items-center gap-2 border-b bg-background/90 px-3 backdrop-blur sm:px-4">
      <Sheet open={navOpen} onOpenChange={setNavOpen}>
        <SheetTrigger asChild>
          <Button variant="ghost" size="icon" className="lg:hidden" aria-label="Open navigation"><Menu /></Button>
        </SheetTrigger>
        <SheetContent side="left" className="w-72 max-w-[85vw] gap-0 bg-sidebar p-0">
          <SheetTitle className="sr-only">Navigation</SheetTitle>
          <SheetDescription className="sr-only">Primary navigation</SheetDescription>
          <div className="flex h-14 items-center border-b border-sidebar-border px-3"><Brand /></div>
          <NavList onNavigate={() => setNavOpen(false)} />
        </SheetContent>
      </Sheet>
      <div className="flex min-w-0 flex-1 items-center"><GlobalSearch /></div>
      <ThemeToggle />
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="ghost" className="h-9 gap-2 px-2" aria-label="User menu" data-testid="user-menu">
            <span className="flex size-7 items-center justify-center rounded-full bg-accent text-xs font-semibold text-accent-foreground">
              {user ? user.email.slice(0, 1).toUpperCase() : <UserRound className="size-4" />}
            </span>
            <span className="hidden max-w-40 truncate text-sm sm:inline">{user?.email ?? '…'}</span>
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-60">
          <DropdownMenuLabel className="space-y-1">
            <span className="block truncate text-sm font-medium text-foreground">{user?.email}</span>
            {role && <Badge variant={roleVariant[role as Role] ?? 'neutral'}>{ROLE_LABEL[role as Role] ?? role}</Badge>}
          </DropdownMenuLabel>
          <DropdownMenuSeparator />
          <DropdownMenuItem onSelect={() => logout.mutate()}><LogOut /> Sign out</DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </header>
  );
}
