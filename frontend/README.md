# Lead Intelligence – frontend foundation

Next.js 16 (App Router) · React 19 · TypeScript strict · Tailwind 3.4 · Radix primitives · TanStack Query/Table · react-hook-form + zod · Recharts.

```bash
npm run dev          # http://localhost:3000 (proxies /api/* and /healthz to BACKEND_URL, default http://localhost:8000)
npm run typecheck && npm run lint && npm run build
npm run gen:api      # regenerate lib/api-types.ts from ../backend/openapi.json
npm run test:e2e     # Playwright (starts/reuses `npm run dev`; backend NOT required)
```

Dev-only visual QA page: **/design** (every primitive, 404 in production builds).
Optional auth e2e: `E2E_ADMIN_EMAIL=… E2E_ADMIN_PASSWORD=… npx playwright test e2e/auth.spec.ts` (needs backend + admin created with `python -m app.cli create-admin`).

## Layout

| Path | Purpose |
|---|---|
| `app/(app)/**` | Authenticated pages, wrapped by the shell (`app/(app)/layout.tsx` -> `components/app/app-shell.tsx`) |
| `app/login`, `app/design` | Public pages (excluded from the auth gate in `proxy.ts`) |
| `proxy.ts` | Redirects to `/login?next=` when the `lead_session` cookie is absent (Next 16 name for middleware). Real validation is the API's 401 |
| `lib/api.ts` | Fetch client, `ApiError`, CSRF, types helpers |
| `lib/api-types.ts` | Generated (do not edit) |
| `lib/auth.tsx` | `useMe`, `Can`, `RequireRole`, `useLogout`, `roleAtLeast` |
| `lib/nav.ts` | Sidebar items (single source of truth; `minRole` hides items) |
| `lib/query-keys.ts`, `lib/format.ts`, `lib/toast.ts`, `lib/utils.ts` | keys, number/date formatters, imperative toasts, `cn` |
| `hooks/` | `useApiMutation`, `useSSE`, `usePagedQuery`/`listFetcher`, `useUrlFilters`, `useSavedViews`, `useDebounce`, `useLocalStorage`, `useTheme`, `useMediaQuery` |
| `components/ui/` | Primitives (Radix/shadcn style) |
| `components/composites/` | PageHeader, DataTable, StatusBadge, StatCard, ChartCard, FilterBar, … |
| `components/app/` | Shell parts (sidebar, topbar, banners, theme toggle) |

## Adding a page

1. Create `app/(app)/<route>/page.tsx` (server component by default; add `'use client'` in a child component when you need hooks). The placeholder pages already exist for every nav route – replace their body.
2. Start with `<PageHeader title description actions />`; it renders the page's only `<h1>`.
3. Restrict by role: wrap in `<RequireRole role="operator">…</RequireRole>` (page) and `<Can role="operator">…</Can>` (buttons). The backend enforces roles regardless; this is UX only. Viewers are read-only: hide/disable mutating actions.
4. New nav entry: add to `lib/nav.ts`.
5. Detail routes: `app/(app)/runs/[id]/page.tsx` (`params` is a Promise in Next 16: `const { id } = await params` in server components, or `useParams()` in client ones).

## Data fetching conventions

```tsx
'use client';
import { useQuery } from '@tanstack/react-query';
import { api, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';

const q = useQuery({ queryKey: qk.detail('runs', id), queryFn: ({ signal }) => api.get<Schema<'RunDetail'>>(`/api/runs/${id}`, { signal }) });
if (q.isPending) return <LoadingState />;
if (q.isError) return <ErrorState error={q.error} onRetry={() => q.refetch()} />;
```

* Always call the API through `api.get/post/put/patch/delete` (cookies, CSRF header, error normalisation, 401 redirect, maintenance/unreachable banners are handled for you). Paths are same-origin `/api/...`.
* Types: `Schema<'RunSummary'>` = `components['schemas']['RunSummary']`; `Page<T>` is the list envelope. Prefer generated types over hand-written ones.
* Query keys: `qk.list('runs', filters)`, `qk.detail('runs', id)`, `qk.custom('runs','tab-counts')`; invalidate with `qk.all('runs')`.
* Query params: arrays are comma-joined (backend CSV filters); `null/undefined/''` are dropped.
* Errors are `ApiError { status, code, message, fields, detail, retryAfter, requestId }`. `err.fields` maps field path -> message for 422.
* Live data: `useSSE<RunSnapshot>(`/api/runs/${id}/events`, { events: ['snapshot'], onEvent })` – reconnects with backoff, closes on unmount. Use it to `setQueryData` or to invalidate, and fall back to `refetchInterval` while `status !== 'open'`.
* Lists: `usePagedQuery({ queryKey, fetchPage: listFetcher<Row>('/api/contacts'), filters })` returns `items`, `total`, `approximate`, `fetchNextPage`, `hasNextPage`. When `approximate` is true render counts as `~1,234` (`formatNumber(n, {approximate})`). Never claim an exact total the API did not give.
* Keep filters in the URL with `useUrlFilters(defaults, { arrayKeys })` so views are shareable.

## Mutations

```tsx
const stop = useApiMutation({
  mutationFn: () => api.post(`/api/runs/${id}/stop`),
  successMessage: 'Run stopping',
  invalidate: [qk.all('runs')],
});
<ConfirmDialog trigger={<Button variant="destructive">Stop</Button>} title="Stop this run?" destructive onConfirm={() => stop.mutateAsync()} />
```

`useApiMutation` toasts errors (title + server message; 503 maintenance gets its own wording) unless `errorMessage: false`. For forms, pass `errorMessage: false` and call `applyFieldErrors(err, form.setError)` in `onError`.

## Forms

react-hook-form + zod (`zodResolver`). Use `FormField` (label, hint, error with `role=alert`) around inputs and pass `invalid` / `aria-describedby={`${id}-error`}`. Controlled Radix widgets (`SimpleSelect`, `Combobox`, `MultiSelect`, `Switch`, `Checkbox`, `RadioGroup`) go through `Controller`/`watch`+`setValue`. Radix `Select` forbids `''` item values – use a sentinel (`FilterSelect` does). Put rarely-used options in `<AdvancedPanel>`. See `app/login/login-form.tsx` for a complete example (server errors, lockout/rate-limit messaging, TOTP step).

## Tables

```tsx
const col = createColumnHelper<Schema<'ContactRow'>>();
const columns = [
  col.accessor('email', { header: 'Email', enableSorting: true }),
  col.accessor('overall_reachability', { header: 'Reachability', enableSorting: false, cell: (i) => <StatusBadge kind="reachability" status={i.getValue()} /> }),
  col.accessor('last_seen_at', { header: 'Last seen', meta: { hideBelow: 'md' }, cell: (i) => <RelativeTime value={i.getValue()} /> }),
];
<DataTable caption="Contacts" tableId="contacts" columns={columns} queryKey={qk.list('contacts')} fetchPage={listFetcher('/api/contacts')} filters={filters}
  selectable bulkActions={({ ids, clear }) => …} rowActions={(r) => [{ label: 'Open', onSelect: … }, 'separator', { label: 'Suppress', destructive: true, onSelect: … }]}
  onRowOpen={(r) => router.push(`/contacts/${r.id}`)} />
```

* Pagination is server-side cursor + "Load more"; the page-size select changes `limit`. Sorting is server-side: clicking a sortable header sends `sort=<columnId>&direction=asc|desc` (via `listFetcher`; write your own `fetchPage` for other conventions). Disable sorting on columns the backend cannot sort (`enableSorting: false`).
* TanStack Table v9 is installed; `DataTable` uses its v8-compatible `@tanstack/react-table/legacy` layer, so column definitions use the familiar v8 API (`createColumnHelper`, `accessorKey`, `cell`, `header`, `meta`).
* `tableId` persists density and column visibility per browser. Selection resets when `filters` change and is limited to loaded rows.
* Keyboard: rows are a roving tab stop – Up/Down/Home/End move, Enter opens (`onRowOpen`), Space toggles selection. Clicks on links/buttons inside cells do not trigger row open.
* Use `FilterBar` (+`FilterSelect`, `MultiSelect`) above the table; its search debounces (300ms) before calling `onChange`. `views` is the hook point for saved views (`useSavedViews(scope)` stores per browser today).

## Status vocabulary (never colour-only)

`<StatusBadge kind status />` – kinds: `run job reachability domain origin marketing mail catchall syntax health`. Each status has its own label + icon + colour. **Syntax validity is deliberately neutral (`kind="syntax"`, "Syntax OK", @ icon) – there is no green check for a well-formed address.** `safe` means "no adverse evidence for a published/corroborated address", not "deliverable guaranteed". Unknown statuses fall back to a neutral humanised badge. Use `ConfidenceMeter` (0–100, text band + number) for confidence.

## Component reference (props summary)

**Primitives (`components/ui/*`)** – Button `variant: default|secondary|outline|ghost|destructive|link`, `size: sm|md|lg|icon|icon-sm`, `loading`, `asChild` · Input `startAdornment endAdornment invalid` · Textarea · Label/`FormField {label htmlFor hint error required}` · SimpleSelect `{value onValueChange options placeholder}` (+ full Radix `Select*`) · Combobox `{options value onChange onSearchChange loading clearable allowCreate}` · MultiSelect `{options value:string[] onChange max allowCreate onSearchChange}` · Checkbox (supports `'indeterminate'`) · Switch · RadioGroup/RadioGroupItem · Tabs/TabsList/TabsTrigger `count`/TabsContent · Dialog* · ConfirmDialog `{trigger|open,onOpenChange,title,description,confirmLabel,destructive,onConfirm}` (async-aware) · Sheet* `side: right|left`, `size: sm|md|lg|xl` · DropdownMenu* (item `destructive`) · Popover* · Tooltip `{content, side}` · Badge `variant: neutral|success|warning|danger|info|primary|outline` · Card* · Progress `tone` · Separator · Skeleton · `toast.success/error/warning/info(title, description?)` (Toaster mounted in Providers) · Collapsible, `AdvancedPanel`, `Accordion` · ScrollArea · Kbd.

**Composites (`components/composites/*`)** – PageHeader `{title description actions breadcrumbs meta}` · EmptyState `{icon title description action compact}` · ErrorState `{error title message onRetry retrying}` · LoadingState `{variant: skeleton|spinner|cards rows label}` · StatCard `{label value hint delta{label,direction,good} sparkline approximate loading href icon}` + `Sparkline` · StatusBadge · ConfidenceMeter · DataTable (above) · FilterBar `{search chips onClearAll views children actions}` · KeyValueList `{items:[{label,value,hint,copy,mono}] columns}` · Timeline `{items:[{id,title,description,time,tone,icon}]}` · CodeBlock `{code language wrap}` · CopyButton · RelativeTime `{value}` · ChartCard `{title description table height loading empty actions footer}` + `CHART_COLORS/chartColor(i)/chartAxisProps/chartGridProps/chartTooltipStyle`.

Format helpers (`lib/format.ts`): `formatNumber(n,{approximate,compact})`, `formatPercent`, `formatRatio`, `formatBytes`, `formatDuration`, `formatDate(Time)`, `formatRelative`, `titleCase`. Add the `tabular` utility class to numeric cells.

## Charts

Wrap every Recharts chart in `ChartCard` with an accessible `title`, a `table` fallback (toggle + screen-reader table) and use `chartColor(i)` in order (8 colours, theme-aware, restrained). Don't encode meaning in colour only – add legends/labels. Spread `chartAxisProps`, `chartGridProps`, `chartTooltipStyle` for consistent theming.

## Theming

CSS variables in `app/globals.css` (HSL channels) → Tailwind tokens: `background foreground card muted border primary accent destructive` and status colours `success|warning|danger|info|neutral` each with `-soft`, `-border`, `-foreground` (e.g. `bg-success-soft text-success-foreground border-success-border`). Dark mode is class-based (`.dark`), light by default, toggle persisted in `localStorage` (`useTheme`, `<ThemeToggle/>`, inline init script avoids flash). System font stack only (no network fonts). Radius 8px (`--radius`).

## Accessibility & testing notes

* Focus rings are global (`:focus-visible`) and in every primitive; dialogs/menus come from Radix (focus trap, Esc, arrow keys).
* Icon-only buttons need `aria-label`. Tables need a `caption`. Charts need a title + table fallback.
* ESLint uses `next/core-web-vitals` + TypeScript; `react-hooks/set-state-in-effect` is off (reading storage/props in effects is intentional), the React Compiler purity rules stay on (no `Date.now()`/`Math.random()` during render).
* Playwright config picks the preinstalled Chromium in `/opt/pw-browsers` if present. `e2e/foundation.spec.ts` covers login render, auth redirect, axe (serious/critical = 0) on /login and /design in light + dark, 375px overflow, theme toggle persistence and DataTable keyboard/paging.
