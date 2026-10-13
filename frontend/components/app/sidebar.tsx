'use client';
import * as React from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { PanelLeftClose, PanelLeftOpen, Radar } from 'lucide-react';
import { NAV, activeNavHref } from '@/lib/nav';
import { roleAtLeast, useMe } from '@/lib/auth';
import { cn } from '@/lib/utils';
import { Tooltip } from '@/components/ui/tooltip';

export function Brand({ collapsed }: { collapsed?: boolean }) {
  return (
    <Link href="/dashboard" className="flex items-center gap-2.5 rounded-md px-1 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
      <span className="flex size-8 shrink-0 items-center justify-center rounded-md bg-primary text-primary-foreground"><Radar className="size-4" aria-hidden /></span>
      {!collapsed && (
        <span className="leading-tight">
          <span className="block text-sm font-semibold tracking-tight">Lead Intelligence</span>
          <span className="block text-2xs uppercase tracking-wider text-muted-foreground">Operations console</span>
        </span>
      )}
    </Link>
  );
}

export function NavList({ collapsed, onNavigate }: { collapsed?: boolean; onNavigate?: () => void }) {
  const pathname = usePathname();
  const { role } = useMe();
  const active = activeNavHref(pathname);
  return (
    <nav aria-label="Primary" className="flex-1 overflow-y-auto px-2 py-3 scrollbar-thin">
      {NAV.map((group) => {
        const items = group.items.filter((i) => !i.minRole || roleAtLeast(role, i.minRole));
        if (!items.length) return null;
        return (
          <div key={group.label} className="mb-4">
            {collapsed ? <div className="mx-3 mb-1 h-px bg-sidebar-border" aria-hidden /> : <p className="mb-1 px-3 text-2xs font-semibold uppercase tracking-wider text-muted-foreground">{group.label}</p>}
            <ul className="space-y-0.5">
              {items.map((it) => {
                const isActive = active === it.href;
                const link = (
                  <Link
                    href={it.href}
                    onClick={onNavigate}
                    aria-current={isActive ? 'page' : undefined}
                    aria-label={collapsed ? it.label : undefined}
                    className={cn(
                      'group flex items-center gap-2.5 rounded-md px-3 py-1.5 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                      collapsed && 'justify-center px-0',
                      isActive ? 'bg-sidebar-accent text-accent-foreground' : 'text-sidebar-foreground hover:bg-muted hover:text-foreground',
                    )}
                  >
                    <it.icon className={cn('size-4 shrink-0', isActive ? 'text-primary' : 'text-muted-foreground group-hover:text-foreground')} aria-hidden />
                    {!collapsed && <span className="truncate">{it.label}</span>}
                  </Link>
                );
                return <li key={it.href}>{collapsed ? <Tooltip content={it.label} side="right">{link}</Tooltip> : link}</li>;
              })}
            </ul>
          </div>
        );
      })}
    </nav>
  );
}

export function Sidebar({ collapsed, onToggle }: { collapsed: boolean; onToggle: () => void }) {
  return (
    <aside className={cn('hidden shrink-0 flex-col border-r border-sidebar-border bg-sidebar transition-[width] duration-200 lg:flex', collapsed ? 'w-16' : 'w-60')} aria-label="Sidebar">
      <div className={cn('flex h-14 items-center border-b border-sidebar-border px-3', collapsed && 'justify-center px-0')}><Brand collapsed={collapsed} /></div>
      <NavList collapsed={collapsed} />
      <div className="border-t border-sidebar-border p-2">
        <button
          type="button"
          onClick={onToggle}
          aria-pressed={collapsed}
          aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          className={cn('flex w-full items-center gap-2 rounded-md px-3 py-1.5 text-xs font-medium text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring', collapsed && 'justify-center px-0')}
        >
          {collapsed ? <PanelLeftOpen className="size-4" /> : <><PanelLeftClose className="size-4" /> Collapse</>}
        </button>
      </div>
    </aside>
  );
}
