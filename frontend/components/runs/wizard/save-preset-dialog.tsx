'use client';
import * as React from 'react';
import { useFormContext } from 'react-hook-form';
import { BookmarkPlus } from 'lucide-react';
import { api, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { useApiMutation } from '@/hooks/use-api-mutation';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { FormField } from '@/components/ui/label';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog';
import { toPayload, type WizardValues } from './schema';

/** Save the current configuration as a reusable run preset (POST /api/presets). */
export function SavePresetDialog() {
  const { getValues } = useFormContext<WizardValues>();
  const [open, setOpen] = React.useState(false);
  const [name, setName] = React.useState('');
  const [error, setError] = React.useState<string | null>(null);
  const save = useApiMutation<Schema<'PresetOut'>, string>({
    mutationFn: (n) => api.post<Schema<'PresetOut'>>('/api/presets', { kind: 'run', name: n, config: toPayload(getValues()) }),
    errorMessage: false,
    successMessage: (p) => `Preset “${p.name}” saved`,
    invalidate: [qk.custom('presets', 'run')],
    onSuccess: () => { setOpen(false); setName(''); setError(null); },
    onError: (e) => setError(e.message),
  });
  return (
    <Dialog open={open} onOpenChange={(o) => { setOpen(o); if (!o) setError(null); }}>
      <DialogTrigger asChild><Button type="button" variant="outline"><BookmarkPlus /> Save as preset</Button></DialogTrigger>
      <DialogContent>
        <form onSubmit={(e) => { e.preventDefault(); e.stopPropagation(); if (!name.trim()) { setError('Give the preset a name'); return; } save.mutate(name.trim()); }} noValidate>
          <DialogHeader>
            <DialogTitle>Save as preset</DialogTitle>
            <DialogDescription>Stores this configuration (not the run itself) so you can load it at the start of a new run.</DialogDescription>
          </DialogHeader>
          <div className="my-4">
            <FormField label="Preset name" htmlFor="preset-name" error={error ?? undefined} required>
              <Input id="preset-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={200} invalid={!!error} aria-describedby={error ? 'preset-name-error' : undefined} autoFocus />
            </FormField>
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setOpen(false)}>Cancel</Button>
            <Button type="submit" loading={save.isPending}>Save preset</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
