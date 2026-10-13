'use client';
import * as React from 'react';
import type { QueryKey } from '@tanstack/react-query';
import { flexRender, type RowData } from '@tanstack/react-table';
import {
  getCoreRowModel, legacyCreateColumnHelper, useLegacyTable,
  type LegacyColumnDef, type LegacyRow,
} from '@tanstack/react-table/legacy';
import { ArrowDown, ArrowUp, ArrowUpDown, Columns3, MoreHorizontal, Rows3 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { SimpleSelect } from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import {
  DropdownMenu, DropdownMenuCheckboxItem, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuRadioGroup,
  DropdownMenuRadioItem, DropdownMenuSeparator, DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { EmptyState } from './empty-state';
import { ErrorState } from './error-state';
import { usePagedQuery, type FetchPage, type SortState } from '@/hooks/use-paged-query';
import { useLocalStorage } from '@/hooks/use-local-storage';
import { formatNumber } from '@/lib/format';
import { cn } from '@/lib/utils';

/** Column definition type (TanStack Table; v8-compatible API through `@tanstack/react-table/legacy`). */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type DataTableColumn<T extends RowData> = LegacyColumnDef<T, any>;
/** `const col = createColumnHelper<RunSummary>(); col.accessor('name', { header: 'Name', cell: i => … })` */
export const createColumnHelper = legacyCreateColumnHelper;

/** Optional per-column layout hints, set through `meta`. */
export interface DataTableColumnMeta {
  /** Extra classes on both th and td (e.g. 'w-40', 'text-right tabular'). */
  className?: string;
  /** Hide below this breakpoint to keep mobile readable. */
  hideBelow?: 'sm' | 'md' | 'lg';
  /** Column label used in the visibility menu when `header` is not a string. */
  label?: string;
}

export type RowAction<T extends RowData> =
  | { label: string; icon?: React.ReactNode; onSelect: (row: T) => void; destructive?: boolean; disabled?: boolean; hidden?: boolean }
  | 'separator';

export interface BulkContext<T extends RowData> { rows: T[]; ids: string[]; clear: () => void }

export interface DataTableProps<T extends RowData, F = Record<string, unknown>> {
  /** Accessible table name (visually hidden caption). */
  caption: string;
  columns: DataTableColumn<T>[];
  /** Base key for the infinite query, e.g. qk.list('runs'). Filters/limit/sort are appended automatically. */
  queryKey: QueryKey;
  fetchPage: FetchPage<T, F>;
  filters?: F;
  getRowId?: (row: T) => string;
  /** Persist density + column visibility under this id. */
  tableId?: string;
  rowActions?: (row: T) => RowAction<T>[];
  /** Click / Enter on a row. Interactive children (links, buttons) keep their own behaviour. */
  onRowOpen?: (row: T) => void;
  selectable?: boolean;
  bulkActions?: (ctx: BulkContext<T>) => React.ReactNode;
  defaultPageSize?: number;
  pageSizes?: number[];
  defaultSort?: SortState | null;
  /** Rendered left of the columns/density buttons. */
  toolbar?: React.ReactNode;
  emptyState?: React.ReactNode;
  refetchInterval?: number | false;
  /** Tailwind max-height class for the scroll area (header stays sticky). */
  maxHeightClass?: string;
  className?: string;
}

const BP = { sm: 'max-sm:hidden', md: 'max-md:hidden', lg: 'max-lg:hidden' } as const;
const metaOf = (c: DataTableColumn<never>): DataTableColumnMeta => ((c as { meta?: DataTableColumnMeta }).meta ?? {});

export function DataTable<T extends RowData, F = Record<string, unknown>>({
  caption, columns, queryKey, fetchPage, filters, getRowId, tableId, rowActions, onRowOpen, selectable, bulkActions,
  defaultPageSize = 50, pageSizes = [25, 50, 100, 200], defaultSort = null, toolbar, emptyState, refetchInterval,
  maxHeightClass = 'max-h-[70vh]', className,
}: DataTableProps<T, F>) {
  const persistKey = tableId ?? '__ephemeral';
  const [density, setDensity] = useLocalStorage<'compact' | 'comfortable'>(`lead-table:${persistKey}:density`, 'compact');
  const [hidden, setHidden] = useLocalStorage<Record<string, boolean>>(`lead-table:${persistKey}:cols`, {});
  const [limit, setLimit] = React.useState(defaultPageSize);
  const [sort, setSort] = React.useState<SortState | null>(defaultSort);
  const [selection, setSelection] = React.useState<Record<string, true>>({});

  const q = usePagedQuery<T, F>({ queryKey, fetchPage, filters, limit, sort, refetchInterval });
  const items = q.items;

  const filtersKey = JSON.stringify(filters ?? {});
  React.useEffect(() => { setSelection({}); }, [filtersKey]);

  const idOf = React.useCallback((row: T, index: number) => (getRowId ? getRowId(row) : String((row as { id?: string | number }).id ?? index)), [getRowId]);

  const table = useLegacyTable<T>({
    data: items,
    columns: columns as LegacyColumnDef<T>[],
    getCoreRowModel: getCoreRowModel(),
    getRowId: idOf,
    manualSorting: true,
    enableRowSelection: !!selectable,
    state: {
      sorting: sort ? [{ id: sort.id, desc: sort.desc }] : [],
      columnVisibility: hidden,
      rowSelection: selection,
    },
    onSortingChange: (u) => {
      const cur = sort ? [{ id: sort.id, desc: sort.desc }] : [];
      const next = typeof u === 'function' ? u(cur) : u;
      setSort(next[0] ? { id: next[0].id, desc: next[0].desc } : null);
    },
    onColumnVisibilityChange: (u) => setHidden(typeof u === 'function' ? u(hidden) : u),
    onRowSelectionChange: (u) => setSelection((cur) => (typeof u === 'function' ? u(cur) : u)),
  });

  const toggleRow = (id: string, on: boolean) => setSelection((s) => {
    const n = { ...s };
    if (on) n[id] = true; else delete n[id];
    return n;
  });
  const rows = table.getRowModel().rows as LegacyRow<T>[];
  const selectedRows = rows.filter((r) => selection[r.id]).map((r) => r.original);
  const selectedIds = rows.filter((r) => selection[r.id]).map((r) => r.id);
  const allSelected = rows.length > 0 && selectedIds.length === rows.length;
  const someSelected = selectedIds.length > 0 && !allSelected;

  /* keyboard row navigation (roving tabindex) */
  const bodyRef = React.useRef<HTMLTableSectionElement>(null);
  const [activeRow, setActiveRow] = React.useState(0);
  const focusRow = (i: number) => {
    const n = Math.max(0, Math.min(rows.length - 1, i));
    setActiveRow(n);
    requestAnimationFrame(() => bodyRef.current?.querySelectorAll<HTMLTableRowElement>('tr[data-row]')[n]?.focus());
  };
  const onRowKeyDown = (e: React.KeyboardEvent<HTMLTableRowElement>, i: number, row: LegacyRow<T>) => {
    if (e.target !== e.currentTarget) return; // ignore keys from inner controls
    if (e.key === 'ArrowDown') { e.preventDefault(); focusRow(i + 1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); focusRow(i - 1); }
    else if (e.key === 'Home') { e.preventDefault(); focusRow(0); }
    else if (e.key === 'End') { e.preventDefault(); focusRow(rows.length - 1); }
    else if (e.key === 'Enter' && onRowOpen) { e.preventDefault(); onRowOpen(row.original); }
    else if (e.key === ' ' && selectable) { e.preventDefault(); toggleRow(row.id, !selection[row.id]); }
  };

  const visibleLeaf = table.getVisibleLeafColumns();
  const colCount = visibleLeaf.length + (selectable ? 1 : 0) + (rowActions ? 1 : 0);
  const pad = density === 'compact' ? 'px-3 py-1.5' : 'px-3 py-3';
  const loadingInitial = q.isPending;
  const hideableCols = table.getAllLeafColumns().filter((c) => c.getCanHide());
  const labelOf = (c: (typeof hideableCols)[number]) => {
    const h = c.columnDef.header;
    return metaOf(c.columnDef as DataTableColumn<never>).label ?? (typeof h === 'string' ? h : c.id);
  };

  return (
    <div className={cn('space-y-2', className)}>
      {/* toolbar */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0 text-xs text-muted-foreground" aria-live="polite">
          {q.isSuccess && (
            <span className="tabular">
              {formatNumber(items.length)} loaded{q.total !== null && <> of {q.approximate ? '~' : ''}{formatNumber(q.total)}</>}
              {q.approximate && <span title="Approximate count"> (approx.)</span>}
            </span>
          )}
        </div>
        <div className="flex items-center gap-1.5">
          {toolbar}
          <DropdownMenu>
            <DropdownMenuTrigger asChild><Button variant="outline" size="sm" aria-label="Choose columns"><Columns3 /> <span className="max-sm:sr-only">Columns</span></Button></DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="max-h-80 overflow-y-auto">
              <DropdownMenuLabel>Visible columns</DropdownMenuLabel>
              {hideableCols.map((c) => (
                <DropdownMenuCheckboxItem key={c.id} checked={c.getIsVisible()} onCheckedChange={(v) => c.toggleVisibility(!!v)} onSelect={(e) => e.preventDefault()}>
                  {labelOf(c)}
                </DropdownMenuCheckboxItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
          <DropdownMenu>
            <DropdownMenuTrigger asChild><Button variant="outline" size="sm" aria-label="Row density"><Rows3 /> <span className="max-sm:sr-only">Density</span></Button></DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuLabel>Row density</DropdownMenuLabel>
              <DropdownMenuRadioGroup value={density} onValueChange={(v) => setDensity(v as 'compact' | 'comfortable')}>
                <DropdownMenuRadioItem value="compact">Compact</DropdownMenuRadioItem>
                <DropdownMenuRadioItem value="comfortable">Comfortable</DropdownMenuRadioItem>
              </DropdownMenuRadioGroup>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>

      {/* bulk bar */}
      {selectable && selectedIds.length > 0 && (
        <div role="region" aria-label="Bulk actions" className="flex flex-wrap items-center gap-2 rounded-lg border border-primary/30 bg-accent px-3 py-2 text-sm">
          <span className="font-medium tabular">{selectedIds.length} selected</span>
          <span className="mx-1 h-4 w-px bg-border" aria-hidden />
          {bulkActions?.({ rows: selectedRows, ids: selectedIds, clear: () => setSelection({}) })}
          <Button variant="ghost" size="sm" className="ml-auto" onClick={() => setSelection({})}>Clear selection</Button>
        </div>
      )}

      {/* table */}
      {q.isError && items.length === 0 ? (
        <ErrorState error={q.error} onRetry={() => q.refetch()} retrying={q.isFetching} />
      ) : !loadingInitial && items.length === 0 ? (
        emptyState ?? <EmptyState title="Nothing here yet" description="No records match the current filters." />
      ) : (
        <div className="relative overflow-hidden rounded-lg border bg-card shadow-card">
          {q.isFetching && !q.isFetchingNextPage && !loadingInitial && <div className="absolute inset-x-0 top-0 z-20 h-0.5 animate-pulse bg-primary" aria-hidden />}
          <div className={cn('overflow-auto scrollbar-thin', maxHeightClass)}>
            <table className="w-full min-w-max border-separate border-spacing-0 text-left text-sm">
              <caption className="sr-only">{caption}</caption>
              <thead>
                {table.getHeaderGroups().map((hg) => (
                  <tr key={hg.id}>
                    {selectable && (
                      <th scope="col" className="sticky top-0 z-10 w-10 border-b bg-muted px-3 py-2">
                        <Checkbox aria-label="Select all loaded rows" checked={allSelected ? true : someSelected ? 'indeterminate' : false}
                          onCheckedChange={(v) => setSelection(v ? Object.fromEntries(rows.map((r) => [r.id, true as const])) : {})} />
                      </th>
                    )}
                    {hg.headers.map((h) => {
                      const meta = metaOf(h.column.columnDef as DataTableColumn<never>);
                      const canSort = h.column.getCanSort();
                      const dir = h.column.getIsSorted();
                      return (
                        <th key={h.id} scope="col" aria-sort={dir === 'asc' ? 'ascending' : dir === 'desc' ? 'descending' : canSort ? 'none' : undefined}
                          className={cn('sticky top-0 z-10 whitespace-nowrap border-b bg-muted px-3 py-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground', meta.hideBelow && BP[meta.hideBelow], meta.className)}>
                          {h.isPlaceholder ? null : canSort ? (
                            <button type="button" onClick={h.column.getToggleSortingHandler()} className="-mx-1 inline-flex items-center gap-1 rounded px-1 uppercase hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                              {flexRender(h.column.columnDef.header, h.getContext())}
                              {dir === 'asc' ? <ArrowUp className="size-3" aria-hidden /> : dir === 'desc' ? <ArrowDown className="size-3" aria-hidden /> : <ArrowUpDown className="size-3 opacity-50" aria-hidden />}
                              <span className="sr-only">{dir ? `sorted ${dir === 'asc' ? 'ascending' : 'descending'}` : 'sortable'}</span>
                            </button>
                          ) : flexRender(h.column.columnDef.header, h.getContext())}
                        </th>
                      );
                    })}
                    {rowActions && <th scope="col" className="sticky right-0 top-0 z-10 w-10 border-b bg-muted px-2 py-2"><span className="sr-only">Actions</span></th>}
                  </tr>
                ))}
              </thead>
              <tbody ref={bodyRef}>
                {loadingInitial && Array.from({ length: 8 }).map((_, i) => (
                  <tr key={`sk${i}`} aria-hidden>
                    <td colSpan={colCount} className={cn('border-b', pad)}><Skeleton className="h-4 w-full" /></td>
                  </tr>
                ))}
                {rows.map((row, i) => {
                  const selected = !!selection[row.id];
                  return (
                    <tr
                      key={row.id}
                      data-row
                      tabIndex={i === activeRow ? 0 : -1}
                      aria-selected={selectable ? selected : undefined}
                      onFocus={(e) => { if (e.target === e.currentTarget) setActiveRow(i); }}
                      onKeyDown={(e) => onRowKeyDown(e, i, row)}
                      onClick={(e) => {
                        if (!onRowOpen) return;
                        if ((e.target as HTMLElement).closest('a,button,input,[role=checkbox],[role=menuitem]')) return;
                        onRowOpen(row.original);
                      }}
                      className={cn('group outline-none focus-visible:bg-accent focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring hover:bg-muted/60', selected && 'bg-accent/70', onRowOpen && 'cursor-pointer')}
                    >
                      {selectable && (
                        <td className={cn('w-10 border-b', pad)}>
                          <Checkbox aria-label={`Select row ${i + 1}`} checked={selected} onCheckedChange={(v) => toggleRow(row.id, !!v)} />
                        </td>
                      )}
                      {row.getVisibleCells().map((cell) => {
                        const meta = metaOf(cell.column.columnDef as DataTableColumn<never>);
                        return (
                          <td key={cell.id} className={cn('border-b align-middle', pad, meta.hideBelow && BP[meta.hideBelow], meta.className)}>
                            {flexRender(cell.column.columnDef.cell, cell.getContext())}
                          </td>
                        );
                      })}
                      {rowActions && (
                        <td className={cn('sticky right-0 w-10 border-b bg-card px-2 group-hover:bg-muted', pad, selected && 'bg-accent')}>
                          <RowMenu row={row.original} actions={rowActions(row.original)} />
                        </td>
                      )}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {/* footer */}
          <div className="flex flex-wrap items-center justify-between gap-2 border-t bg-card px-3 py-2">
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <span id={`${caption}-ps`} className="max-sm:sr-only">Rows per load</span>
              <SimpleSelect aria-label="Rows per load" className="h-8 w-20 text-xs" value={String(limit)} onValueChange={(v) => setLimit(Number(v))} options={pageSizes.map((n) => ({ value: String(n), label: String(n) }))} />
            </div>
            <div className="flex items-center gap-3">
              {q.isError && items.length > 0 && <span role="alert" className="text-xs text-danger">Could not load more. </span>}
              {q.hasNextPage ? (
                <Button variant="outline" size="sm" onClick={() => q.fetchNextPage()} loading={q.isFetchingNextPage}>Load more</Button>
              ) : (
                items.length > 0 && <span className="text-xs text-muted-foreground">End of results</span>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function RowMenu<T extends RowData>({ row, actions }: { row: T; actions: RowAction<T>[] }) {
  const visible = actions.filter((a) => a === 'separator' || !a.hidden);
  if (visible.length === 0) return null;
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon-sm" aria-label="Row actions"><MoreHorizontal /></Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {visible.map((a, i) => a === 'separator'
          ? <DropdownMenuSeparator key={i} />
          : <DropdownMenuItem key={i} destructive={a.destructive} disabled={a.disabled} onSelect={() => a.onSelect(row)}>{a.icon}{a.label}</DropdownMenuItem>)}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
