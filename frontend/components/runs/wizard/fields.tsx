'use client';
import * as React from 'react';
import { Controller, get, useFormContext, type FieldPath } from 'react-hook-form';
import { Input } from '@/components/ui/input';
import { FormField, Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { SimpleSelect, type SimpleSelectOption } from '@/components/ui/select';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import { MultiSelect } from '@/components/ui/combobox';
import { cn } from '@/lib/utils';
import type { WizardValues } from './schema';

export type Name = FieldPath<WizardValues>;
export const idOf = (name: string) => `f-${name.replace(/[.[\]]+/g, '-')}`;

export function useFieldError(name: string): string | undefined {
  const { formState } = useFormContext<WizardValues>();
  const e = get(formState.errors, name) as { message?: string } | undefined;
  return e?.message;
}

export function TextField({ name, label, hint, required, placeholder, className, type = 'text', maxLength }: {
  name: Name; label: React.ReactNode; hint?: React.ReactNode; required?: boolean; placeholder?: string; className?: string; type?: string; maxLength?: number;
}) {
  const { register } = useFormContext<WizardValues>();
  const err = useFieldError(name);
  const id = idOf(name);
  return (
    <FormField label={label} htmlFor={id} hint={hint} error={err} required={required} className={className}>
      <Input id={id} type={type} placeholder={placeholder} maxLength={maxLength} invalid={!!err} aria-describedby={err ? `${id}-error` : hint ? `${id}-hint` : undefined} {...register(name as never)} />
    </FormField>
  );
}

/** Integer input; empty becomes `null` when `nullable`, otherwise NaN (rejected by the schema with "Enter a number"). */
export function NumField({ name, label, hint, nullable, min, max, placeholder, className, suffix }: {
  name: Name; label: React.ReactNode; hint?: React.ReactNode; nullable?: boolean; min?: number; max?: number; placeholder?: string; className?: string; suffix?: string;
}) {
  const { control } = useFormContext<WizardValues>();
  const err = useFieldError(name);
  const id = idOf(name);
  return (
    <FormField label={label} htmlFor={id} hint={hint} error={err} className={className}>
      <Controller
        control={control}
        name={name}
        render={({ field }) => (
          <Input
            id={id} type="number" inputMode="numeric" min={min} max={max} placeholder={placeholder} invalid={!!err}
            aria-describedby={err ? `${id}-error` : hint ? `${id}-hint` : undefined}
            endAdornment={suffix ? <span className="pr-1 text-xs">{suffix}</span> : undefined}
            value={field.value === null || field.value === undefined || (typeof field.value === 'number' && Number.isNaN(field.value)) ? '' : String(field.value)}
            onChange={(e) => field.onChange(e.target.value === '' ? (nullable ? null : Number.NaN) : Number(e.target.value))}
            onBlur={field.onBlur} ref={field.ref}
          />
        )}
      />
    </FormField>
  );
}

export function SwitchField({ name, label, hint, disabled, onChange }: { name: Name; label: React.ReactNode; hint?: React.ReactNode; disabled?: boolean; onChange?: (v: boolean) => void }) {
  const { control } = useFormContext<WizardValues>();
  const id = idOf(name);
  return (
    <div className="flex items-start justify-between gap-4 py-1">
      <div className="min-w-0 space-y-0.5">
        <Label htmlFor={id} className={cn(disabled && 'opacity-60')}>{label}</Label>
        {hint && <p id={`${id}-hint`} className="text-xs text-muted-foreground">{hint}</p>}
      </div>
      <Controller control={control} name={name} render={({ field }) => (
        <Switch id={id} checked={!!field.value} disabled={disabled} aria-describedby={hint ? `${id}-hint` : undefined}
          onCheckedChange={(v) => { field.onChange(v); onChange?.(v); }} />
      )} />
    </div>
  );
}

export function SelectField({ name, label, hint, options, className, onChange, numeric }: {
  name: Name; label: React.ReactNode; hint?: React.ReactNode; options: SimpleSelectOption[]; className?: string; onChange?: (v: string) => void; numeric?: boolean;
}) {
  const { control } = useFormContext<WizardValues>();
  const err = useFieldError(name);
  const id = idOf(name);
  return (
    <FormField label={label} htmlFor={id} hint={hint} error={err} className={className}>
      <Controller control={control} name={name} render={({ field }) => (
        <SimpleSelect id={id} value={String(field.value ?? '')} onValueChange={(v) => { field.onChange(numeric ? Number(v) : v); onChange?.(v); }} options={options} invalid={!!err} />
      )} />
    </FormField>
  );
}

export interface RadioOption { value: string; label: React.ReactNode; hint?: React.ReactNode; disabled?: boolean }
/** Radio group rendered as selectable rows; keyboard: arrow keys move, Space selects (Radix). */
export function RadioField({ name, legend, options, onChange, columns = 1 }: { name: Name; legend: React.ReactNode; options: RadioOption[]; onChange?: (v: string) => void; columns?: 1 | 2 | 3 }) {
  const { control } = useFormContext<WizardValues>();
  const err = useFieldError(name);
  const id = idOf(name);
  return (
    <fieldset className="space-y-1.5">
      <legend id={`${id}-legend`} className="text-sm font-medium">{legend}</legend>
      <Controller control={control} name={name} render={({ field }) => (
        <RadioGroup aria-labelledby={`${id}-legend`} value={String(field.value ?? '')} onValueChange={(v) => { field.onChange(v); onChange?.(v); }}
          className={cn(columns === 2 && 'sm:grid-cols-2', columns === 3 && 'sm:grid-cols-3')}>
          {options.map((o) => (
            <label key={o.value} htmlFor={`${id}-${o.value}`} className={cn('flex cursor-pointer items-start gap-3 rounded-md border p-3 text-sm transition-colors hover:bg-muted/50 has-[[data-state=checked]]:border-primary has-[[data-state=checked]]:bg-accent/40', o.disabled && 'cursor-not-allowed opacity-50')}>
              <RadioGroupItem id={`${id}-${o.value}`} value={o.value} disabled={o.disabled} className="mt-0.5" />
              <span className="min-w-0"><span className="block font-medium">{o.label}</span>{o.hint && <span className="mt-0.5 block text-xs text-muted-foreground">{o.hint}</span>}</span>
            </label>
          ))}
        </RadioGroup>
      )} />
      {err && <p id={`${id}-error`} role="alert" className="text-xs font-medium text-destructive">{err}</p>}
    </fieldset>
  );
}

/** Free-text tags (keywords, exclusions, localities, postcode areas). Type then press Enter to add. */
export function ChipsField({ name, label, hint, placeholder }: { name: Name; label: React.ReactNode; hint?: React.ReactNode; placeholder?: string }) {
  const { control } = useFormContext<WizardValues>();
  const err = useFieldError(name);
  const id = idOf(name);
  return (
    <FormField label={label} htmlFor={id} hint={hint} error={err}>
      <Controller control={control} name={name} render={({ field }) => (
        <MultiSelect id={id} aria-label={typeof label === 'string' ? label : undefined} options={[]} allowCreate emptyText="Type a value and press Enter"
          value={(field.value as string[]) ?? []} onChange={field.onChange} placeholder={placeholder ?? 'Type and press Enter'} searchPlaceholder={placeholder ?? 'Type and press Enter'} invalid={!!err} />
      )} />
    </FormField>
  );
}

export function Section({ title, description, children, className }: { title: React.ReactNode; description?: React.ReactNode; children: React.ReactNode; className?: string }) {
  return (
    <section className={cn('space-y-4 rounded-lg border bg-card p-4 sm:p-5', className)}>
      <div>
        <h2 className="text-base font-semibold">{title}</h2>
        {description && <p className="mt-0.5 text-sm text-muted-foreground">{description}</p>}
      </div>
      {children}
    </section>
  );
}
