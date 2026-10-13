'use client';
import * as React from 'react';
import { useRouter } from 'next/navigation';
import { useQuery } from '@tanstack/react-query';
import { Building2, Loader2, Mail, PlayCircle, Search } from 'lucide-react';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { api, type Schema } from '@/lib/api';
import { useDebounce } from '@/hooks/use-debounce';
import { cn } from '@/lib/utils';

type Hit = { key: string; href: string; label: string; detail?: string | null; group: 'Businesses' | 'Contacts' | 'Runs' };
const ICON = { Businesses: Building2, Contacts: Mail, Runs: PlayCircle } as const;

/** Cross-entity search: businesses (name, company number, domain), contacts (email prefix) and runs. Keyboard: arrows + Enter. */
export function GlobalSearchDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const router = useRouter();
  const [q, setQ] = React.useState('');
  const [active, setActive] = React.useState(0);
  const term = useDebounce(q.trim(), 200);
  const { data, isFetching, isError } = useQuery({
    queryKey: ['search', term],
    queryFn: () => api.get<Schema<'SearchOut'>>('/api/search', { query: { q: term, limit: 6 } }),
    enabled: open && term.length >= 2,
    staleTime: 15_000,
  });
  const hits: Hit[] = React.useMemo(() => {
    if (!data) return [];
    return [
      ...data.businesses.map((b) => ({ key: `b${b.id}`, href: `/businesses/${b.id}`, label: b.name, detail: b.detail, group: 'Businesses' as const })),
      ...data.contacts.map((c) => ({ key: `c${c.id}`, href: `/contacts/${c.id}`, label: c.email, detail: c.organisation, group: 'Contacts' as const })),
      ...data.runs.map((r) => ({ key: `r${r.id}`, href: `/runs/${r.id}`, label: r.name, detail: r.status, group: 'Runs' as const })),
    ];
  }, [data]);
  React.useEffect(() => setActive(0), [term]);
  React.useEffect(() => { if (!open) setQ(''); }, [open]);

  const go = (h: Hit | undefined) => { if (!h) return; onOpenChange(false); router.push(h.href); };
  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); setActive((a) => Math.min(a + 1, Math.max(0, hits.length - 1))); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setActive((a) => Math.max(0, a - 1)); }
    else if (e.key === 'Enter') { e.preventDefault(); go(hits[active]); }
  };
  const listId = 'global-search-results';
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl gap-3 p-4">
        <DialogHeader>
          <DialogTitle>Search</DialogTitle>
          <DialogDescription>Businesses by name, company number or domain · contacts by e-mail · runs by name.</DialogDescription>
        </DialogHeader>
        <Input
          autoFocus value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={onKey} placeholder="Type at least 2 characters…"
          startAdornment={<Search className="size-4" aria-hidden />}
          endAdornment={isFetching ? <Loader2 className="size-4 animate-spin" aria-label="Searching" /> : undefined}
          role="combobox" aria-expanded={hits.length > 0} aria-controls={listId} aria-activedescendant={hits[active] ? `gs-${hits[active].key}` : undefined}
          aria-label="Search businesses, contacts and runs"
        />
        <div id={listId} role="listbox" aria-label="Search results" className="max-h-[50vh] overflow-y-auto">
          {term.length < 2 ? null : isError ? (
            <p className="px-2 py-6 text-center text-sm text-muted-foreground">Search failed. Try again.</p>
          ) : data && hits.length === 0 ? (
            <p className="px-2 py-6 text-center text-sm text-muted-foreground">No matches for “{term}”.</p>
          ) : (
            (['Businesses', 'Contacts', 'Runs'] as const).map((g) => {
              const items = hits.filter((h) => h.group === g);
              if (!items.length) return null;
              const Icon = ICON[g];
              return (
                <div key={g} role="group" aria-label={g} className="py-1">
                  <p className="px-2 pb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">{g}</p>
                  {items.map((h) => {
                    const idx = hits.indexOf(h);
                    return (
                      <div
                        key={h.key} id={`gs-${h.key}`} role="option" aria-selected={idx === active}
                        onMouseEnter={() => setActive(idx)} onClick={() => go(h)}
                        className={cn('flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm', idx === active && 'bg-accent')}
                      >
                        <Icon className="size-4 shrink-0 text-muted-foreground" aria-hidden />
                        <span className="min-w-0 flex-1 truncate">{h.label}</span>
                        {h.detail && <span className="max-w-[45%] truncate text-xs text-muted-foreground">{h.detail}</span>}
                      </div>
                    );
                  })}
                </div>
              );
            })
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
