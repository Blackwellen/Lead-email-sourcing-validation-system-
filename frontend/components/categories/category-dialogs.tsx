'use client';
import * as React from 'react';
import { Upload } from 'lucide-react';
import { api, ApiError, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { formatNumber } from '@/lib/format';
import { useApiMutation } from '@/hooks/use-api-mutation';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Combobox, MultiSelect } from '@/components/ui/combobox';
import { FormField } from '@/components/ui/label';
import { AdvancedPanel } from '@/components/ui/collapsible';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { flattenCategories, useCategoryTree, useSourceOptions, type CategoryNode } from '@/components/contacts/shared';

type Cat = Schema<'CategoryOut'>;
const INVALIDATE = [qk.all('categories'), qk.all('businesses'), qk.all('contacts')];

function descendantIds(n: CategoryNode): Set<number> {
  const out = new Set<number>([n.id]);
  for (const c of n.children ?? []) descendantIds(c).forEach((i) => out.add(i));
  return out;
}
/** Parent choices: every category except the one being edited and its own descendants (no cycles). */
function useParentOptions(exclude?: number) {
  const tree = useCategoryTree();
  return React.useMemo(() => {
    const flat = flattenCategories(tree.data ?? []);
    const self = exclude ? flat.find((f) => f.node.id === exclude)?.node : undefined;
    const banned = self ? descendantIds(self) : new Set<number>();
    return flat.filter((f) => !banned.has(f.node.id) && f.depth < 2).map((f) => ({ value: String(f.node.id), label: f.path }));
  }, [tree.data, exclude]);
}

export function CategoryFormDialog({ open, onOpenChange, category, defaultParentId }: {
  open: boolean; onOpenChange: (o: boolean) => void; category?: Cat | null; defaultParentId?: number | null;
}) {
  const edit = !!category;
  const parents = useParentOptions(category?.id);
  const sources = useSourceOptions();
  const [name, setName] = React.useState('');
  const [parent, setParent] = React.useState<string | null>(null);
  const [keywords, setKeywords] = React.useState<string[]>([]);
  const [excluded, setExcluded] = React.useState<string[]>([]);
  const [synonyms, setSynonyms] = React.useState<string[]>([]);
  const [defSources, setDefSources] = React.useState<string[]>([]);
  const [sic, setSic] = React.useState<string[]>([]);
  const [osm, setOsm] = React.useState<string[]>([]);
  const [err, setErr] = React.useState<Record<string, string>>({});
  React.useEffect(() => {
    if (!open) return;
    setName(category?.name ?? ''); setParent(category?.parent_id ? String(category.parent_id) : defaultParentId ? String(defaultParentId) : null);
    setKeywords(category?.keywords ?? []); setExcluded(category?.excluded_keywords ?? []); setSynonyms([]); setDefSources(category?.default_sources ?? []);
    setSic(category?.sic_codes ?? []); setOsm(category?.osm_tags ?? []); setErr({});
  }, [open, category, defaultParentId]);

  const save = useApiMutation<Cat, void>({
    mutationFn: () => edit
      ? api.put(`/api/categories/${category!.id}`, { name: name.trim(), set_parent: true, parent_id: parent ? Number(parent) : null, keywords, excluded_keywords: excluded, default_sources: defSources, sic_codes: sic, osm_tags: osm })
      : api.post('/api/categories', { name: name.trim(), parent_id: parent ? Number(parent) : null, keywords, synonyms, excluded_keywords: excluded, default_sources: defSources, sic_codes: sic, osm_tags: osm }),
    successMessage: (c) => (edit ? `Category “${c.name}” updated` : `Category “${c.name}” created`),
    errorMessage: false, invalidate: INVALIDATE,
    onError: (e) => setErr(Object.keys(e.fields).length ? e.fields : { form: e.message }),
  });
  const submit = () => {
    if (!name.trim()) { setErr({ name: 'Enter a name.' }); return; }
    save.mutateAsync().then(() => onOpenChange(false)).catch(() => undefined);
  };
  return (
    <Dialog open={open} onOpenChange={(o) => !save.isPending && onOpenChange(o)}>
      <DialogContent className="max-w-xl">
        <form onSubmit={(e) => { e.preventDefault(); submit(); }} className="grid gap-4">
          <DialogHeader>
            <DialogTitle>{edit ? `Edit “${category!.name}”` : 'New category'}</DialogTitle>
            <DialogDescription>Categories nest up to three levels: category, subcategory, specialism. Keywords drive discovery; excluded keywords remove false positives.</DialogDescription>
          </DialogHeader>
          <FormField label="Name" htmlFor="cat-name" required error={err.name}><Input id="cat-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={200} invalid={!!err.name} aria-describedby={err.name ? 'cat-name-error' : undefined} autoFocus /></FormField>
          <FormField label="Parent category" htmlFor="cat-parent" error={err.parent_id} hint="Leave empty for a top-level category.">
            <Combobox id="cat-parent" aria-label="Parent category" clearable value={parent} onChange={setParent} options={parents} placeholder="Top level" />
          </FormField>
          <FormField label="Keywords" htmlFor="cat-kw" hint="Type and press Enter to add."><MultiSelect id="cat-kw" aria-label="Keywords" value={keywords} onChange={setKeywords} options={[]} allowCreate placeholder="Add keywords" /></FormField>
          <FormField label="Excluded keywords" htmlFor="cat-ex" hint="Businesses matching these are not assigned to this category."><MultiSelect id="cat-ex" aria-label="Excluded keywords" value={excluded} onChange={setExcluded} options={[]} allowCreate placeholder="Add excluded keywords" /></FormField>
          {!edit && <FormField label="Synonyms" htmlFor="cat-syn"><MultiSelect id="cat-syn" aria-label="Synonyms" value={synonyms} onChange={setSynonyms} options={[]} allowCreate placeholder="Add synonyms" /></FormField>}
          <FormField label="Default source strategies" htmlFor="cat-src" hint="Sources pre-selected when a run targets this category."><MultiSelect id="cat-src" aria-label="Default sources" value={defSources} onChange={setDefSources} options={sources.options} allowCreate placeholder="Add sources" /></FormField>
          <AdvancedPanel title="Classification codes">
            <div className="grid gap-3 sm:grid-cols-2">
              <FormField label="SIC codes" htmlFor="cat-sic"><MultiSelect id="cat-sic" aria-label="SIC codes" value={sic} onChange={setSic} options={[]} allowCreate placeholder="e.g. 43220" /></FormField>
              <FormField label="OSM tags" htmlFor="cat-osm"><MultiSelect id="cat-osm" aria-label="OSM tags" value={osm} onChange={setOsm} options={[]} allowCreate placeholder="e.g. craft=plumber" /></FormField>
            </div>
          </AdvancedPanel>
          {err.form && <p role="alert" className="text-sm font-medium text-destructive">{err.form}</p>}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)} disabled={save.isPending}>Cancel</Button>
            <Button type="submit" loading={save.isPending}>{edit ? 'Save changes' : 'Create category'}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function SynonymsDialog({ open, onOpenChange, category }: { open: boolean; onOpenChange: (o: boolean) => void; category: Cat | null }) {
  const [syn, setSyn] = React.useState<string[]>([]);
  React.useEffect(() => { if (open) setSyn(category?.synonyms ?? []); }, [open, category]);
  const save = useApiMutation<Cat, void>({
    mutationFn: () => api.put(`/api/categories/${category!.id}/synonyms`, { synonyms: syn }),
    successMessage: 'Synonyms saved', invalidate: INVALIDATE,
  });
  return (
    <Dialog open={open} onOpenChange={(o) => !save.isPending && onOpenChange(o)}>
      <DialogContent>
        <DialogHeader><DialogTitle>Synonyms for “{category?.name}”</DialogTitle><DialogDescription>Alternative names used to match businesses to this category. Saving replaces the full list.</DialogDescription></DialogHeader>
        <FormField label="Synonyms" htmlFor="syn-ed" hint="Type a word and press Enter."><MultiSelect id="syn-ed" aria-label="Synonyms" value={syn} onChange={setSyn} options={[]} allowCreate placeholder="Add synonym" /></FormField>
        <DialogFooter><Button variant="outline" onClick={() => onOpenChange(false)} disabled={save.isPending}>Cancel</Button><Button loading={save.isPending} onClick={() => save.mutateAsync().then(() => onOpenChange(false)).catch(() => undefined)}>Save synonyms</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function MergeCategoryDialog({ open, onOpenChange, category, onDone }: { open: boolean; onOpenChange: (o: boolean) => void; category: CategoryNode | null; onDone?: () => void }) {
  const tree = useCategoryTree();
  const options = React.useMemo(() => {
    const banned = category ? descendantIds(category) : new Set<number>();
    return flattenCategories(tree.data ?? []).filter((f) => !banned.has(f.node.id) && f.node.enabled).map((f) => ({ value: String(f.node.id), label: f.path }));
  }, [tree.data, category]);
  const [target, setTarget] = React.useState<string | null>(null);
  React.useEffect(() => { if (open) setTarget(null); }, [open]);
  const merge = useApiMutation<Schema<'CategoryMergeOut'>, void>({
    mutationFn: () => api.post(`/api/categories/${category!.id}/merge`, { target_id: Number(target) }),
    successMessage: (d) => `Merged: ${d.businesses_moved} businesses and ${d.children_moved} sub-categories moved`, invalidate: INVALIDATE,
  });
  return (
    <Dialog open={open} onOpenChange={(o) => !merge.isPending && onOpenChange(o)}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Merge “{category?.name}” into another category</DialogTitle>
          <DialogDescription>All {formatNumber(category?.business_count ?? 0)} businesses, sub-categories and run references move to the target. This category’s name and synonyms become synonyms of the target, and this category is disabled.</DialogDescription>
        </DialogHeader>
        <FormField label="Merge into" htmlFor="merge-target" required><Combobox id="merge-target" aria-label="Merge into" value={target} onChange={setTarget} options={options} placeholder="Choose the category to keep" /></FormField>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={merge.isPending}>Cancel</Button>
          <Button variant="destructive" loading={merge.isPending} disabled={!target} onClick={() => merge.mutateAsync().then(() => { onOpenChange(false); onDone?.(); }).catch(() => undefined)}>Merge category</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function ImportTaxonomyDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const [file, setFile] = React.useState<File | null>(null);
  const [preview, setPreview] = React.useState<Schema<'ImportOut'> | null>(null);
  const [problems, setProblems] = React.useState<string[]>([]);
  const ref = React.useRef<HTMLInputElement>(null);
  React.useEffect(() => { if (open) { setFile(null); setPreview(null); setProblems([]); } }, [open]);
  const run = useApiMutation<Schema<'ImportOut'>, boolean>({
    mutationFn: (dry) => { const fd = new FormData(); fd.append('file', file!); return api.post('/api/categories/import', fd, { query: { dry_run: dry } }); },
    errorMessage: false, invalidate: INVALIDATE,
    onError: (e: ApiError) => setProblems(Array.isArray(e.detail) && e.detail.every((x) => typeof x === 'string') ? (e.detail as string[]) : [e.message]),
  });
  return (
    <Dialog open={open} onOpenChange={(o) => !run.isPending && onOpenChange(o)}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Import taxonomy</DialogTitle>
          <DialogDescription>Upload a JSON or CSV taxonomy (columns: slug, name, parent_slug, level, enabled, keywords, synonyms, excluded_keywords, sic_codes, osm_tags, default_sources; separate list values with “|”). Existing slugs are updated. Preview first: nothing changes until you import.</DialogDescription>
        </DialogHeader>
        <div className="flex flex-wrap items-center gap-2">
          <input ref={ref} type="file" accept=".json,.csv,application/json,text/csv,text/plain" className="sr-only" aria-label="Taxonomy file" onChange={(e) => { setFile(e.target.files?.[0] ?? null); setPreview(null); setProblems([]); }} />
          <Button type="button" variant="outline" size="sm" onClick={() => ref.current?.click()}><Upload /> Choose file…</Button>
          <span className="text-sm text-muted-foreground">{file ? file.name : 'No file chosen'}</span>
        </div>
        {problems.length > 0 && <ul role="alert" className="list-disc space-y-0.5 pl-5 text-sm text-destructive">{problems.slice(0, 8).map((p) => <li key={p}>{p}</li>)}</ul>}
        {preview && <p className="rounded-md border p-2 text-sm" data-testid="import-preview">{preview.dry_run ? 'Preview' : 'Imported'}: <strong>{preview.created}</strong> to create, <strong>{preview.updated}</strong> to update.</p>}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={run.isPending}>Close</Button>
          <Button variant="outline" disabled={!file} loading={run.isPending} onClick={() => { setProblems([]); run.mutateAsync(true).then(setPreview).catch(() => undefined); }}>Preview (dry run)</Button>
          <Button disabled={!file || !preview?.dry_run} loading={run.isPending} onClick={() => run.mutateAsync(false).then(setPreview).catch(() => undefined)}>Import</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
