import {
  Activity, BadgeCheck, BarChart3, Building2, Compass, Database, Download, FolderTree, Gauge, Lightbulb, Network, Play, Search, Settings, Users, type LucideIcon,
} from 'lucide-react';
import type { Role } from '@/lib/api';

export interface NavItem { href: string; label: string; icon: LucideIcon; minRole?: Role }
export interface NavGroup { label: string; items: NavItem[] }

/** Single source of truth for sidebar navigation. `minRole` hides items below that role. */
export const NAV: NavGroup[] = [
  {
    label: 'Overview',
    items: [
      { href: '/dashboard', label: 'Dashboard', icon: Gauge },
      { href: '/runs', label: 'Runs', icon: Play },
    ],
  },
  {
    label: 'Data',
    items: [
      { href: '/businesses', label: 'Businesses', icon: Building2 },
      { href: '/contacts', label: 'Contacts', icon: Users },
      { href: '/verification', label: 'Verification', icon: BadgeCheck },
      { href: '/categories', label: 'Categories', icon: FolderTree },
    ],
  },
  {
    label: 'Discovery',
    items: [
      { href: '/sources', label: 'Sources', icon: Database },
      { href: '/sources/explore', label: 'Source Explorer', icon: Search },
      { href: '/discovery', label: 'Discovery', icon: Compass },
      { href: '/discovery/network', label: 'Business Network', icon: Network },
      { href: '/opportunities', label: 'Opportunities', icon: Lightbulb },
    ],
  },
  {
    label: 'Operations',
    items: [
      { href: '/data-quality', label: 'Data Quality', icon: BarChart3 },
      { href: '/exports', label: 'Exports', icon: Download },
      { href: '/system', label: 'System', icon: Activity, minRole: 'admin' },
      // Visible to every role: Account & security (password, MFA) lives here; the page itself limits other tabs to admins.
      { href: '/settings', label: 'Settings', icon: Settings },
    ],
  },
];

export const ALL_NAV_ITEMS = NAV.flatMap((g) => g.items);

/** Longest-prefix match so /sources/explore highlights "Source Explorer", not "Sources". */
export function activeNavHref(pathname: string): string | null {
  let best: string | null = null;
  for (const it of ALL_NAV_ITEMS) {
    if ((pathname === it.href || pathname.startsWith(`${it.href}/`)) && (!best || it.href.length > best.length)) best = it.href;
  }
  return best;
}
