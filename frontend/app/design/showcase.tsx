'use client';
import * as React from 'react';
import { Area, AreaChart, Bar, BarChart, CartesianGrid, Legend, Tooltip as RTooltip, XAxis, YAxis } from 'recharts';
import { Download, Plus, Trash2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { FormField, Label } from '@/components/ui/label';
import { SimpleSelect } from '@/components/ui/select';
import { Combobox, MultiSelect } from '@/components/ui/combobox';
import { Checkbox } from '@/components/ui/checkbox';
import { Switch } from '@/components/ui/switch';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog';
import { ConfirmDialog } from '@/components/ui/alert-dialog';
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle, SheetTrigger } from '@/components/ui/sheet';
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/dropdown-menu';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { Tooltip } from '@/components/ui/tooltip';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Progress } from '@/components/ui/progress';
import { Separator } from '@/components/ui/separator';
import { Skeleton } from '@/components/ui/skeleton';
import { toast } from '@/components/ui/toast';
import { AdvancedPanel, Accordion } from '@/components/ui/collapsible';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Kbd } from '@/components/ui/kbd';
import { PageHeader } from '@/components/composites/page-header';
import { EmptyState } from '@/components/composites/empty-state';
import { ErrorState } from '@/components/composites/error-state';
import { LoadingState } from '@/components/composites/loading-state';
import { StatCard } from '@/components/composites/stat-card';
import { Sparkline } from '@/components/composites/sparkline';
import { StatusBadge, type StatusKind } from '@/components/composites/status-badge';
import { ConfidenceMeter } from '@/components/composites/confidence-meter';
import { DataTable, createColumnHelper } from '@/components/composites/data-table';
import { FilterBar, FilterSelect } from '@/components/composites/filter-bar';
import { KeyValueList } from '@/components/composites/key-value-list';
import { Timeline } from '@/components/composites/timeline';
import { CodeBlock } from '@/components/composites/code-block';
import { CopyButton } from '@/components/composites/copy-button';
import { RelativeTime } from '@/components/composites/relative-time';
import { ChartCard, chartAxisProps, chartColor, chartGridProps, chartTooltipStyle } from '@/components/composites/chart-card';
import { ThemeToggle } from '@/components/app/theme-toggle';
import { formatBytes, formatDuration, formatNumber, formatPercent } from '@/lib/format';
import type { Page } from '@/lib/api';

const STATUSES: Record<StatusKind, string[]> = {
  run: ['draft', 'scheduled', 'queued', 'running', 'paused', 'stopping', 'completed', 'failed', 'cancelled', 'archived'],
  reachability: ['safe', 'risky', 'invalid', 'unknown'],
  domain: ['confirmed', 'high', 'medium', 'low', 'mismatch', 'parked', 'unknown'],
  origin: ['published', 'corroborated', 'pattern_inferred', 'role_speculative'],
  marketing: ['eligible', 'needs_review', 'suppressed', 'opted_out'],
  syntax: ['valid', 'invalid'],
  mail: ['accepts_mail', 'no_mail_service', 'null_mx', 'temporary_dns_failure'],
  catchall: ['confirmed', 'suspected', 'rejected_unknown_recipient', 'untested'],
  job: ['queued', 'running', 'done', 'failed'],
  health: ['healthy', 'degraded', 'down', 'disabled'],
};

interface Row { id: number; email: string; company: string; reachability: string; origin: string; confidence: number; seen: string }
const COMPANIES = ['Northwind Plumbing', 'Harbour Dental', 'Alder & Finch LLP', 'Kestrel Logistics', 'Brightside Bakery', 'Orchard Veterinary'];
const ALL_ROWS: Row[] = Array.from({ length: 130 }, (_, i) => ({
  id: i + 1,
  email: `${['info', 'hello', 'office', 'jane.doe', 'sales'][i % 5]}@${COMPANIES[i % 6].toLowerCase().replace(/[^a-z]/g, '')}.example`,
  company: COMPANIES[i % 6],
  reachability: ['safe', 'risky', 'invalid', 'unknown'][(i * 7) % 4],
  origin: ['published', 'corroborated', 'pattern_inferred', 'role_speculative'][(i * 3) % 4],
  confidence: (i * 37) % 101,
  seen: new Date(Date.now() - i * 3_600_000 * 5).toISOString(),
}));

async function fakeFetch({ cursor, limit, filters, sort }: { cursor: string | null; limit: number; filters: { q?: string; reach?: string }; sort?: { id: string; desc: boolean } | null }): Promise<Page<Row>> {
  await new Promise((r) => setTimeout(r, 250));
  let rows = ALL_ROWS.filter((r) => (!filters.q || r.email.includes(filters.q) || r.company.toLowerCase().includes(filters.q.toLowerCase())) && (!filters.reach || r.reachability === filters.reach));
  if (sort) rows = [...rows].sort((a, b) => ((a as never)[sort.id] > (b as never)[sort.id] ? 1 : -1) * (sort.desc ? -1 : 1));
  const start = cursor ? Number(cursor) : 0;
  const items = rows.slice(start, start + limit);
  return { items, next_cursor: start + limit < rows.length ? String(start + limit) : null, total_estimate: rows.length, approximate: false };
}

const col = createColumnHelper<Row>();
const columns = [
  col.accessor('email', { header: 'Email', cell: (i) => <span className="flex items-center gap-1 font-medium">{i.getValue()}<CopyButton value={i.getValue()} label="Copy email" /></span> }),
  col.accessor('company', { header: 'Business', enableSorting: false, meta: { hideBelow: 'md' } }),
  col.accessor('reachability', { header: 'Reachability', cell: (i) => <StatusBadge kind="reachability" status={i.getValue()} /> }),
  col.accessor('origin', { header: 'Origin', meta: { hideBelow: 'lg' }, cell: (i) => <StatusBadge kind="origin" status={i.getValue()} /> }),
  col.accessor('confidence', { header: 'Confidence', cell: (i) => <ConfidenceMeter value={i.getValue()} /> }),
  col.accessor('seen', { header: 'Last seen', meta: { hideBelow: 'sm' }, cell: (i) => <RelativeTime value={i.getValue()} className="text-muted-foreground" /> }),
];

const series = Array.from({ length: 12 }, (_, i) => ({ month: `M${i + 1}`, found: 120 + i * 14 + (i % 3) * 9, verified: 60 + i * 11 }));

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-3" aria-labelledby={`s-${title}`}>
      <h2 id={`s-${title}`} className="border-b pb-2 text-base font-semibold">{title}</h2>
      {children}
    </section>
  );
}

export function DesignShowcase() {
  const [multi, setMulti] = React.useState<string[]>(['plumbing']);
  const [single, setSingle] = React.useState<string | null>(null);
  const [search, setSearch] = React.useState('');
  const [reach, setReach] = React.useState<string | undefined>();
  const [now] = React.useState(() => Date.now());
  const [checked, setChecked] = React.useState<boolean | 'indeterminate'>('indeterminate');
  const options = [
    { value: 'plumbing', label: 'Plumbing', description: 'Trades' }, { value: 'dental', label: 'Dental clinics' },
    { value: 'legal', label: 'Law firms' }, { value: 'logistics', label: 'Logistics' }, { value: 'bakery', label: 'Bakeries' },
  ];

  return (
    <div className="mx-auto max-w-6xl space-y-10 px-4 py-8 sm:px-6">
      <PageHeader title="Design system" description="Every primitive and composite, for visual QA. Development only." actions={<ThemeToggle />} />

      <Section title="Buttons">
        <div className="flex flex-wrap items-center gap-2">
          <Button>Primary</Button><Button variant="secondary">Secondary</Button><Button variant="outline">Outline</Button>
          <Button variant="ghost">Ghost</Button><Button variant="destructive">Destructive</Button><Button variant="link">Link</Button>
          <Button loading>Saving</Button><Button disabled>Disabled</Button><Button size="sm">Small</Button><Button size="lg">Large</Button>
          <Button size="icon" aria-label="Add"><Plus /></Button><Button variant="outline"><Download /> With icon</Button>
        </div>
      </Section>

      <Section title="Form controls">
        <div className="grid gap-4 md:grid-cols-2">
          <FormField label="Text input" htmlFor="d-in" hint="Helper text"><Input id="d-in" placeholder="Business name" /></FormField>
          <FormField label="With error" htmlFor="d-err" error="This field is required" required><Input id="d-err" invalid aria-describedby="d-err-error" /></FormField>
          <FormField label="Textarea" htmlFor="d-ta"><Textarea id="d-ta" placeholder="Notes…" /></FormField>
          <FormField label="Select" htmlFor="d-sel"><SimpleSelect id="d-sel" onValueChange={() => undefined} options={[{ value: 'a', label: 'Operator' }, { value: 'b', label: 'Viewer' }]} placeholder="Choose role" /></FormField>
          <FormField label="Combobox (single)" htmlFor="d-cb"><Combobox id="d-cb" options={options} value={single} onChange={setSingle} clearable placeholder="Pick a category" /></FormField>
          <FormField label="Multi-select with chips" htmlFor="d-ms"><MultiSelect id="d-ms" options={options} value={multi} onChange={setMulti} allowCreate placeholder="Pick categories" /></FormField>
          <div className="flex items-center gap-2"><Checkbox id="d-cbx" checked={checked} onCheckedChange={setChecked} /><Label htmlFor="d-cbx">Checkbox (indeterminate)</Label></div>
          <div className="flex items-center gap-2"><Switch id="d-sw" defaultChecked /><Label htmlFor="d-sw">Switch</Label></div>
          <RadioGroup defaultValue="x" aria-label="Uniqueness">
            <div className="flex items-center gap-2"><RadioGroupItem value="x" id="r1" /><Label htmlFor="r1">Email</Label></div>
            <div className="flex items-center gap-2"><RadioGroupItem value="y" id="r2" /><Label htmlFor="r2">Organisation</Label></div>
          </RadioGroup>
        </div>
      </Section>

      <Section title="Badges and status vocabulary">
        <div className="flex flex-wrap gap-2">
          <Badge variant="neutral">Neutral</Badge><Badge variant="success">Success</Badge><Badge variant="warning">Warning</Badge>
          <Badge variant="danger">Danger</Badge><Badge variant="info">Info</Badge><Badge variant="primary">Primary</Badge><Badge variant="outline">Outline</Badge>
        </div>
        <div className="grid gap-3 md:grid-cols-2">
          {(Object.keys(STATUSES) as StatusKind[]).map((k) => (
            <div key={k} className="rounded-lg border p-3">
              <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">{k}</p>
              <div className="flex flex-wrap gap-1.5">{STATUSES[k].map((s) => <StatusBadge key={s} kind={k} status={s} />)}</div>
            </div>
          ))}
        </div>
        <div className="flex flex-wrap items-center gap-6">
          {[8, 40, 62, 91].map((v) => <ConfidenceMeter key={v} value={v} />)}<ConfidenceMeter value={null} />
        </div>
      </Section>

      <Section title="Stat cards">
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          <StatCard label="Contacts found" value={48213} delta={{ label: '+12% vs last week', direction: 'up', good: true }} sparkline={<Sparkline data={[3, 5, 4, 7, 9, 8, 12]} />} />
          <StatCard label="Bounce risk" value="4.2%" delta={{ label: '+0.6 pts', direction: 'up', good: false }} />
          <StatCard label="Distinct businesses" value={1250000} approximate hint="Capped count" />
          <StatCard label="Loading" value={null} loading />
        </div>
      </Section>

      <Section title="Tabs, accordion, panels">
        <Tabs defaultValue="a">
          <TabsList><TabsTrigger value="a" count={12}>Active</TabsTrigger><TabsTrigger value="b" count={3}>Paused</TabsTrigger><TabsTrigger value="c">Completed</TabsTrigger></TabsList>
          <TabsContent value="a"><p className="text-sm text-muted-foreground">Active content panel.</p></TabsContent>
          <TabsContent value="b"><p className="text-sm text-muted-foreground">Paused content panel.</p></TabsContent>
          <TabsContent value="c"><p className="text-sm text-muted-foreground">Completed content panel.</p></TabsContent>
        </Tabs>
        <AdvancedPanel description="Rarely needed"><p className="text-sm">Crawl politeness, concurrency overrides…</p></AdvancedPanel>
        <Accordion items={[{ id: '1', title: 'How is confidence computed?', content: 'From evidence weights per signal.' }, { id: '2', title: 'What does Safe mean?', content: 'No adverse evidence for a published or corroborated address.' }]} />
      </Section>

      <Section title="Overlays">
        <div className="flex flex-wrap gap-2">
          <Dialog>
            <DialogTrigger asChild><Button variant="outline">Dialog</Button></DialogTrigger>
            <DialogContent><DialogHeader><DialogTitle>Rename run</DialogTitle><DialogDescription>Give the run a clear name.</DialogDescription></DialogHeader><Input aria-label="Run name" defaultValue="UK dentists" /><DialogFooter><Button>Save</Button></DialogFooter></DialogContent>
          </Dialog>
          <ConfirmDialog trigger={<Button variant="destructive"><Trash2 /> Confirm dialog</Button>} title="Delete this draft?" description="This cannot be undone." confirmLabel="Delete" destructive onConfirm={() => new Promise((r) => setTimeout(r, 600))} />
          <Sheet>
            <SheetTrigger asChild><Button variant="outline">Drawer</Button></SheetTrigger>
            <SheetContent size="md"><SheetHeader><SheetTitle>Contact detail</SheetTitle><SheetDescription>Evidence and history</SheetDescription></SheetHeader><KeyValueList items={[{ label: 'Email', value: 'info@harbourdental.example', copy: 'info@harbourdental.example' }, { label: 'Origin', value: <StatusBadge kind="origin" status="published" /> }]} /></SheetContent>
          </Sheet>
          <DropdownMenu>
            <DropdownMenuTrigger asChild><Button variant="outline">Menu</Button></DropdownMenuTrigger>
            <DropdownMenuContent><DropdownMenuLabel>Actions</DropdownMenuLabel><DropdownMenuItem>Duplicate</DropdownMenuItem><DropdownMenuSeparator /><DropdownMenuItem destructive>Delete</DropdownMenuItem></DropdownMenuContent>
          </DropdownMenu>
          <Popover><PopoverTrigger asChild><Button variant="outline">Popover</Button></PopoverTrigger><PopoverContent><p className="text-sm">Popover content with <Kbd>Esc</Kbd> to close.</p></PopoverContent></Popover>
          <Tooltip content="Helpful hint"><Button variant="outline">Tooltip</Button></Tooltip>
          <Button variant="outline" onClick={() => toast.success('Run started', 'UK dentists is now running')}>Toast success</Button>
          <Button variant="outline" onClick={() => toast.error('Action failed', 'The server rejected the request')}>Toast error</Button>
        </div>
      </Section>

      <Section title="Feedback states">
        <div className="grid gap-4 md:grid-cols-3">
          <EmptyState compact title="No runs yet" description="Create your first run to get started." action={<Button size="sm">New run</Button>} />
          <ErrorState compact message="The API returned 500." onRetry={() => undefined} />
          <div className="space-y-3 rounded-lg border p-4"><LoadingState rows={3} /><Progress value={64} aria-label="Progress" /><Skeleton className="h-8 w-1/2" /></div>
        </div>
        <ScrollArea className="h-24 rounded-md border"><div className="space-y-1 p-3 text-sm">{Array.from({ length: 12 }, (_, i) => <p key={i}>Scrollable line {i + 1}</p>)}</div></ScrollArea>
        <Separator />
      </Section>

      <Section title="Detail components">
        <div className="grid gap-6 md:grid-cols-2">
          <Card><CardHeader><CardTitle>Key / value</CardTitle><CardDescription>Detail panels</CardDescription></CardHeader><CardContent>
            <KeyValueList columns={2} items={[{ label: 'Domain', value: 'harbourdental.example', copy: 'harbourdental.example' }, { label: 'Match', value: <StatusBadge kind="domain" status="confirmed" /> }, { label: 'Size', value: formatBytes(1536000) }, { label: 'Duration', value: formatDuration(4000) }, { label: 'Rate', value: formatPercent(12.5) }, { label: 'Count', value: formatNumber(12345) }]} />
          </CardContent></Card>
          <Card><CardHeader><CardTitle>Timeline</CardTitle></CardHeader><CardContent>
            <Timeline items={[{ id: 1, title: 'Run completed', time: new Date(now - 600000), tone: 'success' }, { id: 2, title: 'Verification started', time: new Date(now - 7200000), tone: 'info' }, { id: 3, title: 'Run created', time: new Date(now - 86400000 * 2) }]} />
          </CardContent></Card>
        </div>
        <CodeBlock language="json" code={JSON.stringify({ error: { code: 'validation_error', message: 'The request is invalid', detail: [{ field: 'target_count', message: 'Must be > 0' }] } }, null, 2)} />
      </Section>

      <Section title="Charts">
        <div className="grid gap-4 lg:grid-cols-2">
          <ChartCard title="Contacts per month" description="Found vs verified" table={{ columns: ['Month', 'Found', 'Verified'], rows: series.map((s) => [s.month, s.found, s.verified]) }}>
            <AreaChart data={series} margin={{ left: -16, right: 8, top: 8 }}>
              <CartesianGrid {...chartGridProps} /><XAxis dataKey="month" {...chartAxisProps} /><YAxis {...chartAxisProps} />
              <RTooltip {...chartTooltipStyle} /><Legend />
              <Area dataKey="found" name="Found" stroke={chartColor(0)} fill={chartColor(0)} fillOpacity={0.12} />
              <Area dataKey="verified" name="Verified" stroke={chartColor(1)} fill={chartColor(1)} fillOpacity={0.12} />
            </AreaChart>
          </ChartCard>
          <ChartCard title="Origin mix" description="Categorical palette" table={{ columns: ['Origin', 'Count'], rows: [['Published', 420], ['Corroborated', 210], ['Pattern', 130], ['Role', 60]] }}>
            <BarChart data={[{ n: 'Published', v: 420 }, { n: 'Corroborated', v: 210 }, { n: 'Pattern', v: 130 }, { n: 'Role', v: 60 }]} margin={{ left: -16, right: 8, top: 8 }}>
              <CartesianGrid {...chartGridProps} /><XAxis dataKey="n" {...chartAxisProps} /><YAxis {...chartAxisProps} /><RTooltip {...chartTooltipStyle} />
              <Bar dataKey="v" name="Contacts" fill={chartColor(0)} radius={[4, 4, 0, 0]} />
            </BarChart>
          </ChartCard>
        </div>
      </Section>

      <Section title="Filter bar and data table">
        <FilterBar
          search={{ value: search, onChange: setSearch, placeholder: 'Search email or business' }}
          chips={[...(search ? [{ id: 'q', label: `Search: ${search}`, onRemove: () => setSearch('') }] : []), ...(reach ? [{ id: 'r', label: `Reachability: ${reach}`, onRemove: () => setReach(undefined) }] : [])]}
          onClearAll={() => { setSearch(''); setReach(undefined); }}
          views={{ items: [], onApply: () => undefined, onSave: () => toast.info('Saved views are a hook point') }}
        >
          <FilterSelect label="Reachability" value={reach} onChange={setReach} options={['safe', 'risky', 'invalid', 'unknown'].map((v) => ({ value: v, label: v }))} />
        </FilterBar>
        <DataTable<Row, { q?: string; reach?: string }>
          caption="Example contacts"
          tableId="design-demo"
          columns={columns}
          queryKey={['design', 'rows']}
          fetchPage={fakeFetch}
          filters={{ q: search || undefined, reach }}
          defaultPageSize={25}
          pageSizes={[25, 50]}
          selectable
          onRowOpen={(r) => toast.info(`Open ${r.email}`)}
          rowActions={(r) => [{ label: 'Copy email', onSelect: () => navigator.clipboard?.writeText(r.email) }, 'separator', { label: 'Suppress', destructive: true, onSelect: () => toast.warning('Suppressed', r.email) }]}
          bulkActions={({ ids, clear }) => <Button size="sm" variant="outline" onClick={() => { toast.success(`Queued ${ids.length} for revalidation`); clear(); }}>Revalidate</Button>}
        />
      </Section>
    </div>
  );
}
