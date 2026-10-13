'use client';
import * as React from 'react';
import { Check, ChevronsUpDown, Loader2, Plus, Search, X } from 'lucide-react';
import { cn } from '@/lib/utils';
import { Popover, PopoverContent, PopoverTrigger } from './popover';
import { inputClass } from './input';

export interface ComboboxOption { value: string; label: string; description?: string; disabled?: boolean }

interface BaseProps {
  options: ComboboxOption[];
  placeholder?: string;
  searchPlaceholder?: string;
  emptyText?: string;
  /** Controlled search text (for server-side search). When set, built-in filtering is skipped. */
  onSearchChange?: (q: string) => void;
  loading?: boolean;
  disabled?: boolean;
  id?: string;
  className?: string;
  'aria-label'?: string;
  invalid?: boolean;
  /** Allow adding values not in `options` (shown as "Add “text”"). */
  allowCreate?: boolean;
}

function useListState(options: ComboboxOption[], onSearchChange: BaseProps['onSearchChange'], open: boolean) {
  const [query, setQuery] = React.useState('');
  const [active, setActive] = React.useState(0);
  React.useEffect(() => { if (!open) { setQuery(''); setActive(0); } }, [open]);
  const filtered = React.useMemo(() => {
    if (onSearchChange) return options;
    const q = query.trim().toLowerCase();
    return q ? options.filter((o) => o.label.toLowerCase().includes(q) || o.value.toLowerCase().includes(q) || o.description?.toLowerCase().includes(q)) : options;
  }, [options, query, onSearchChange]);
  const update = (q: string) => { setQuery(q); setActive(0); onSearchChange?.(q); };
  return { query, update, active, setActive, filtered };
}

function Listbox({ props, selected, onPick, query, update, active, setActive, filtered, listId, multi }: {
  props: BaseProps; selected: string[]; onPick: (o: ComboboxOption) => void; query: string; update: (q: string) => void; active: number; setActive: (n: number) => void; filtered: ComboboxOption[]; listId: string; multi: boolean;
}) {
  const trimmed = query.trim();
  const canCreate = !!props.allowCreate && trimmed.length > 0 && !props.options.some((o) => o.value.toLowerCase() === trimmed.toLowerCase() || o.label.toLowerCase() === trimmed.toLowerCase());
  const rows: ComboboxOption[] = canCreate ? [...filtered, { value: trimmed, label: `Add “${trimmed}”` }] : filtered;
  const refs = React.useRef<(HTMLLIElement | null)[]>([]);
  React.useEffect(() => { refs.current[active]?.scrollIntoView({ block: 'nearest' }); }, [active]);
  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); setActive(Math.min(rows.length - 1, active + 1)); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setActive(Math.max(0, active - 1)); }
    else if (e.key === 'Home') { e.preventDefault(); setActive(0); }
    else if (e.key === 'End') { e.preventDefault(); setActive(rows.length - 1); }
    else if (e.key === 'Enter') { e.preventDefault(); const r = rows[active]; if (r && !r.disabled) onPick(r); }
  };
  return (
    <div>
      <div className="relative border-b">
        <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
        <input
          autoFocus
          role="combobox"
          aria-expanded
          aria-controls={listId}
          aria-activedescendant={rows[active] ? `${listId}-${active}` : undefined}
          aria-autocomplete="list"
          aria-label={props.searchPlaceholder ?? 'Search options'}
          value={query}
          onChange={(e) => update(e.target.value)}
          onKeyDown={onKey}
          placeholder={props.searchPlaceholder ?? 'Search…'}
          className="h-10 w-full bg-transparent pl-9 pr-9 text-sm outline-none placeholder:text-muted-foreground/80"
        />
        {props.loading && <Loader2 className="absolute right-3 top-1/2 size-4 -translate-y-1/2 animate-spin text-muted-foreground" aria-label="Loading" />}
      </div>
      <ul id={listId} role="listbox" aria-multiselectable={multi || undefined} className="max-h-64 overflow-y-auto p-1 scrollbar-thin">
        {rows.length === 0 && <li className="px-3 py-6 text-center text-sm text-muted-foreground">{props.loading ? 'Searching…' : (props.emptyText ?? 'No results found.')}</li>}
        {rows.map((o, i) => {
          const isSel = selected.includes(o.value);
          const create = canCreate && i === rows.length - 1;
          return (
            <li
              key={`${o.value}-${i}`}
              id={`${listId}-${i}`}
              ref={(el) => { refs.current[i] = el; }}
              role="option"
              aria-selected={isSel}
              aria-disabled={o.disabled || undefined}
              onMouseMove={() => setActive(i)}
              onClick={() => !o.disabled && onPick(o)}
              className={cn('flex cursor-pointer items-start gap-2 rounded-sm px-2 py-1.5 text-sm', i === active && 'bg-muted', o.disabled && 'cursor-not-allowed opacity-50')}
            >
              <span className="mt-0.5 flex size-4 shrink-0 items-center justify-center">
                {create ? <Plus className="size-4 text-muted-foreground" aria-hidden /> : isSel ? <Check className="size-4 text-primary" aria-hidden /> : null}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate">{o.label}</span>
                {o.description && <span className="block truncate text-xs text-muted-foreground">{o.description}</span>}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/** Searchable single-select. */
export function Combobox(props: BaseProps & { value?: string | null; onChange: (v: string | null, option?: ComboboxOption) => void; clearable?: boolean }) {
  const [open, setOpen] = React.useState(false);
  const listId = React.useId();
  const { query, update, active, setActive, filtered } = useListState(props.options, props.onSearchChange, open);
  const current = props.options.find((o) => o.value === props.value);
  const label = current?.label ?? (props.value ? props.value : undefined);
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <div className={cn('relative', props.className)}>
        <PopoverTrigger asChild>
          <button type="button" id={props.id} role="combobox" aria-expanded={open} aria-haspopup="listbox" aria-controls={open ? listId : undefined} aria-label={props['aria-label']} aria-invalid={props.invalid} disabled={props.disabled}
            className={cn(inputClass, 'items-center justify-between gap-2 text-left', props.clearable && props.value && 'pr-14')}>
            <span className={cn('truncate', !label && 'text-muted-foreground/80')}>{label ?? props.placeholder ?? 'Select…'}</span>
            <ChevronsUpDown className="size-4 shrink-0 text-muted-foreground" aria-hidden />
          </button>
        </PopoverTrigger>
        {props.clearable && props.value && !props.disabled && (
          <button type="button" aria-label="Clear selection" onClick={() => props.onChange(null)} className="absolute right-8 top-1/2 -translate-y-1/2 rounded p-0.5 text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
            <X className="size-3.5" />
          </button>
        )}
      </div>
      <PopoverContent className="w-[var(--radix-popover-trigger-width)] min-w-64 p-0">
        <Listbox props={props} selected={props.value ? [props.value] : []} multi={false} listId={listId}
          query={query} update={update} active={active} setActive={setActive} filtered={filtered}
          onPick={(o) => { props.onChange(o.value, o); setOpen(false); }} />
      </PopoverContent>
    </Popover>
  );
}

/** Searchable multi-select with removable chips. */
export function MultiSelect(props: BaseProps & { value: string[]; onChange: (v: string[]) => void; max?: number; maxChipsShown?: number }) {
  const [open, setOpen] = React.useState(false);
  const listId = React.useId();
  const { query, update, active, setActive, filtered } = useListState(props.options, props.onSearchChange, open);
  const labelOf = (v: string) => props.options.find((o) => o.value === v)?.label ?? v;
  const atMax = props.max !== undefined && props.value.length >= props.max;
  const toggle = (o: ComboboxOption) => {
    if (props.value.includes(o.value)) props.onChange(props.value.filter((v) => v !== o.value));
    else if (!atMax) props.onChange([...props.value, o.value]);
  };
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <div className={cn('flex min-h-9 w-full flex-wrap items-center gap-1.5 rounded-md border border-input bg-background px-2 py-1 shadow-card focus-within:ring-2 focus-within:ring-ring', props.invalid && 'border-destructive', props.disabled && 'opacity-50', props.className)}>
        <ul className="contents" aria-label="Selected values">
          {props.value.map((v) => (
            <li key={v} className="inline-flex max-w-full items-center gap-1 rounded-md border border-neutral-border bg-neutral-soft py-0.5 pl-2 pr-1 text-xs font-medium text-neutral-foreground">
              <span className="truncate">{labelOf(v)}</span>
              <button type="button" disabled={props.disabled} aria-label={`Remove ${labelOf(v)}`} onClick={() => props.onChange(props.value.filter((x) => x !== v))} className="rounded p-0.5 hover:bg-foreground/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                <X className="size-3" />
              </button>
            </li>
          ))}
        </ul>
        <PopoverTrigger asChild>
          <button type="button" id={props.id} role="combobox" aria-expanded={open} aria-haspopup="listbox" aria-controls={open ? listId : undefined} aria-label={props['aria-label'] ?? props.placeholder ?? 'Add'} disabled={props.disabled}
            className="flex h-6 min-w-24 flex-1 items-center justify-between gap-2 rounded text-left text-sm text-muted-foreground/90 focus-visible:outline-none">
            <span className="truncate">{props.value.length ? 'Add more…' : (props.placeholder ?? 'Select…')}</span>
            <ChevronsUpDown className="size-4 shrink-0" aria-hidden />
          </button>
        </PopoverTrigger>
      </div>
      <PopoverContent className="w-[var(--radix-popover-trigger-width)] min-w-64 p-0">
        <Listbox props={props} selected={props.value} multi listId={listId}
          query={query} update={update} active={active} setActive={setActive} filtered={filtered}
          onPick={(o) => { toggle(o); update(''); }} />
        {atMax && <p className="border-t px-3 py-2 text-xs text-muted-foreground">Maximum of {props.max} selected.</p>}
      </PopoverContent>
    </Popover>
  );
}
