'use client';
import * as React from 'react';
import { Bookmark, Search, X } from 'lucide-react';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/dropdown-menu';
import { SimpleSelect, type SimpleSelectOption } from '@/components/ui/select';
import { useDebounce } from '@/hooks/use-debounce';
import type { SavedView } from '@/hooks/use-saved-views';
import { cn } from '@/lib/utils';

export interface FilterChip { id: string; label: string; onRemove: () => void }

export interface FilterBarProps<F = Record<string, unknown>> {
  search?: { value: string; onChange: (v: string) => void; placeholder?: string; debounceMs?: number; label?: string };
  /** Active filters rendered as removable chips under the controls. */
  chips?: FilterChip[];
  onClearAll?: () => void;
  /** Saved views hook point (see useSavedViews). */
  views?: { items: SavedView<F>[]; onApply: (v: SavedView<F>) => void; onSave?: () => void; onRemove?: (id: string) => void };
  /** Filter controls (FilterSelect, MultiSelect, Popover…). */
  children?: React.ReactNode;
  /** Right-aligned extras (e.g. export button). */
  actions?: React.ReactNode;
  className?: string;
}

export function FilterBar<F = Record<string, unknown>>({ search, chips, onClearAll, views, children, actions, className }: FilterBarProps<F>) {
  const [text, setText] = React.useState(search?.value ?? '');
  const debounced = useDebounce(text, search?.debounceMs ?? 300);
  const onChangeRef = React.useRef(search?.onChange);
  React.useEffect(() => { onChangeRef.current = search?.onChange; });
  const lastSent = React.useRef(search?.value ?? '');
  // push debounced value up (only when it differs from what the parent already has)
  React.useEffect(() => {
    if (!search) return;
    if (debounced !== lastSent.current) { lastSent.current = debounced; onChangeRef.current?.(debounced); }
  }, [debounced, search]);
  // sync external resets (e.g. "Clear all")
  React.useEffect(() => {
    if (search && search.value !== lastSent.current) { lastSent.current = search.value; setText(search.value); }
  }, [search]);

  return (
    <div className={cn('mb-3 space-y-2', className)}>
      <div className="flex flex-wrap items-center gap-2" role="search">
        {search && (
          <div className="w-full sm:w-72">
            <Input
              type="search"
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder={search.placeholder ?? 'Search…'}
              aria-label={search.label ?? search.placeholder ?? 'Search'}
              startAdornment={<Search />}
              endAdornment={text ? (
                <button type="button" aria-label="Clear search" onClick={() => setText('')} className="rounded p-0.5 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"><X /></button>
              ) : undefined}
            />
          </div>
        )}
        {children}
        {views && (
          <DropdownMenu>
            <DropdownMenuTrigger asChild><Button variant="outline" size="sm"><Bookmark /> Views</Button></DropdownMenuTrigger>
            <DropdownMenuContent align="start">
              <DropdownMenuLabel>Saved views</DropdownMenuLabel>
              {views.items.length === 0 && <p className="px-2 py-1.5 text-xs text-muted-foreground">No saved views yet.</p>}
              {views.items.map((v) => (
                <DropdownMenuItem key={v.id} onSelect={() => views.onApply(v)}>
                  <span className="flex-1 truncate">{v.name}</span>
                  {views.onRemove && (
                    <button type="button" aria-label={`Delete view ${v.name}`} className="rounded p-0.5 hover:text-destructive" onClick={(e) => { e.stopPropagation(); views.onRemove?.(v.id); }}><X /></button>
                  )}
                </DropdownMenuItem>
              ))}
              {views.onSave && (<><DropdownMenuSeparator /><DropdownMenuItem onSelect={() => views.onSave?.()}>Save current filters…</DropdownMenuItem></>)}
            </DropdownMenuContent>
          </DropdownMenu>
        )}
        {actions && <div className="ml-auto flex items-center gap-2">{actions}</div>}
      </div>
      {chips && chips.length > 0 && (
        <ul className="flex flex-wrap items-center gap-1.5" aria-label="Active filters">
          {chips.map((c) => (
            <li key={c.id}>
              <Badge variant="outline" className="gap-1 py-0.5 pr-1 text-xs">
                {c.label}
                <button type="button" aria-label={`Remove filter ${c.label}`} onClick={c.onRemove} className="rounded-full p-0.5 hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"><X /></button>
              </Badge>
            </li>
          ))}
          {onClearAll && <li><Button variant="link" size="sm" className="h-auto text-xs" onClick={onClearAll}>Clear all</Button></li>}
        </ul>
      )}
    </div>
  );
}

/** Select with an "All" option. `value` of undefined/'' shows "All". `onChange(undefined)` means no filter. */
export function FilterSelect({ label, value, onChange, options, allLabel = 'All', className }: {
  label: string; value?: string; onChange: (v: string | undefined) => void; options: SimpleSelectOption[]; allLabel?: string; className?: string;
}) {
  return (
    <SimpleSelect
      aria-label={label}
      className={cn('w-auto min-w-36', className)}
      value={value || '__all'}
      onValueChange={(v) => onChange(v === '__all' ? undefined : v)}
      options={[{ value: '__all', label: `${label}: ${allLabel}` }, ...options]}
    />
  );
}
