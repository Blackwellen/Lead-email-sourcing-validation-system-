'use client';
import * as React from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { formatNumber } from '@/lib/format';
import { useApiMutation } from '@/hooks/use-api-mutation';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Combobox } from '@/components/ui/combobox';
import { FormField } from '@/components/ui/label';
import { SimpleSelect } from '@/components/ui/select';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import { useCategoryOptions } from '@/components/contacts/shared';

type Row = Schema<'BusinessRow'>;
const INVALIDATE = [qk.all('businesses'), qk.all('categories'), qk.all('contacts')];

export function AddToCategoryDialog({ open, onOpenChange, ids, onDone }: { open: boolean; onOpenChange: (o: boolean) => void; ids: number[]; onDone: () => void }) {
  const cats = useCategoryOptions({ enabledOnly: true });
  const [cat, setCat] = React.useState<string | null>(null);
  React.useEffect(() => { if (open) setCat(null); }, [open]);
  const run = useApiMutation<Schema<'BusinessBulkOut'>, void>({
    mutationFn: () => api.post('/api/businesses/bulk', { action: 'add_to_category', ids, category_id: Number(cat) }),
    successMessage: (d) => `${d.affected} business${d.affected === 1 ? '' : 'es'} moved to the category`,
    invalidate: INVALIDATE,
  });
  return (
    <Dialog open={open} onOpenChange={(o) => !run.isPending && onOpenChange(o)}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add {formatNumber(ids.length)} business{ids.length === 1 ? '' : 'es'} to a category</DialogTitle>
          <DialogDescription>Each selected business is assigned to this category, replacing its current category.</DialogDescription>
        </DialogHeader>
        <FormField label="Category" htmlFor="bulk-cat" required>
          <Combobox id="bulk-cat" aria-label="Category" value={cat} onChange={setCat} options={cats.options} placeholder="Choose a category" />
        </FormField>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={run.isPending}>Cancel</Button>
          <Button loading={run.isPending} disabled={!cat} onClick={() => run.mutateAsync().then(() => { onOpenChange(false); onDone(); }).catch(() => undefined)}>Add to category</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function ExportBusinessesDialog({ open, onOpenChange, ids, onDone }: { open: boolean; onOpenChange: (o: boolean) => void; ids: number[]; onDone: () => void }) {
  const presets = useQuery({ queryKey: qk.custom('exports', 'presets'), queryFn: ({ signal }) => api.get<Schema<'PresetList'>>('/api/exports/presets', { signal }), enabled: open, staleTime: 300_000 });
  const [preset, setPreset] = React.useState('full_intelligence');
  const [format, setFormat] = React.useState<'csv' | 'xlsx' | 'jsonl'>('csv');
  const run = useApiMutation<Schema<'BusinessBulkOut'>, void>({
    mutationFn: () => api.post('/api/businesses/bulk', { action: 'export', ids, export_preset: preset, export_format: format }),
    successMessage: (d) => `Export #${d.export_id} created for ${d.affected} businesses. Find it on the Exports page.`,
    invalidate: [qk.all('exports')],
  });
  return (
    <Dialog open={open} onOpenChange={(o) => !run.isPending && onOpenChange(o)}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Export {formatNumber(ids.length)} selected business{ids.length === 1 ? '' : 'es'}</DialogTitle>
          <DialogDescription>Creates an export of the contacts belonging to the selected businesses. Presets that exclude suppressed addresses apply their own rules.</DialogDescription>
        </DialogHeader>
        <FormField label="Preset" htmlFor="exp-preset">
          <SimpleSelect id="exp-preset" aria-label="Preset" value={preset} onValueChange={setPreset} options={(presets.data?.presets ?? [{ id: 'full_intelligence', label: 'Full intelligence', description: '', columns: [] }]).map((p) => ({ value: p.id, label: p.label }))} />
        </FormField>
        <FormField label="Format" htmlFor="exp-format">
          <SimpleSelect id="exp-format" aria-label="Format" value={format} onValueChange={(v) => setFormat(v as typeof format)} options={[{ value: 'csv', label: 'CSV' }, { value: 'xlsx', label: 'Excel (xlsx)' }, { value: 'jsonl', label: 'JSON Lines' }]} />
        </FormField>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={run.isPending}>Cancel</Button>
          <Button loading={run.isPending} onClick={() => run.mutateAsync().then(() => { onOpenChange(false); onDone(); }).catch(() => undefined)}>Create export</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** Admin-only. The API has no dry-run, so the preview is computed from the selected rows already loaded in the table. */
export function MergeDialog({ open, onOpenChange, rows, onDone }: { open: boolean; onOpenChange: (o: boolean) => void; rows: Row[]; onDone: () => void }) {
  const [primary, setPrimary] = React.useState<string>('');
  const [ack, setAck] = React.useState(false);
  React.useEffect(() => { if (open) { setPrimary(String(rows[0]?.id ?? '')); setAck(false); } }, [open, rows]);
  const dups = rows.filter((r) => String(r.id) !== primary);
  const run = useApiMutation<Schema<'BusinessMergeOut'>, void>({
    mutationFn: () => api.post('/api/businesses/merge', { primary_id: Number(primary), duplicate_ids: dups.map((d) => d.id) }),
    successMessage: (d) => `Merged ${d.merged.length} into #${d.primary_id}: ${d.domains_moved} domains, ${d.contacts_moved} contacts, ${d.emails_moved} emails moved`,
    invalidate: INVALIDATE,
  });
  const name = (r: Row) => r.legal_name ?? r.trading_name ?? `Business #${r.id}`;
  return (
    <Dialog open={open} onOpenChange={(o) => !run.isPending && onOpenChange(o)}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>Merge duplicate businesses</DialogTitle>
          <DialogDescription>Choose the business to keep. The others are marked as merged into it; their domains, contacts, e-mail addresses, evidence and run links move to the kept business.</DialogDescription>
        </DialogHeader>
        <RadioGroup value={primary} onValueChange={setPrimary} aria-label="Business to keep" className="gap-2">
          {rows.map((r) => (
            <label key={r.id} className="flex cursor-pointer items-start gap-3 rounded-md border p-2.5 text-sm has-[[data-state=checked]]:border-primary">
              <RadioGroupItem value={String(r.id)} className="mt-0.5" aria-label={`Keep ${name(r)}`} />
              <span className="min-w-0">
                <span className="block break-words font-medium">{name(r)} <span className="font-normal text-muted-foreground">#{r.id}</span></span>
                <span className="block text-xs text-muted-foreground">{r.country_code}{r.region ? ` · ${r.region}` : ''} · {r.domain ?? 'no website'} · {r.email_count} emails</span>
              </span>
            </label>
          ))}
        </RadioGroup>
        <div className="rounded-md border bg-muted/40 p-3 text-sm" aria-live="polite" data-testid="merge-preview">
          <p className="font-medium">Preview</p>
          <ul className="mt-1 list-disc space-y-0.5 pl-5">
            <li>{dups.length} business{dups.length === 1 ? '' : 'es'} will be marked as merged and hidden from the default list.</li>
            <li>Up to {dups.reduce((n, r) => n + r.email_count, 0)} e-mail addresses will move to the kept business.</li>
            <li>Duplicate domains and identical contact identities collapse onto the kept business.</li>
            <li>This is recorded in the audit log. There is no automatic undo.</li>
          </ul>
        </div>
        <label className="flex items-start gap-2 text-sm"><Checkbox aria-label="I understand merging cannot be undone" checked={ack} onCheckedChange={(c) => setAck(c === true)} className="mt-0.5" />I understand merging cannot be undone automatically.</label>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={run.isPending}>Cancel</Button>
          <Button variant="destructive" loading={run.isPending} disabled={!ack || dups.length === 0} onClick={() => run.mutateAsync().then(() => { onOpenChange(false); onDone(); }).catch(() => undefined)}>Merge {dups.length} into selected</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
