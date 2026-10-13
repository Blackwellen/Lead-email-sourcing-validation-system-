'use client';
import * as React from 'react';
import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import { FormProvider, useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, ArrowRight, Check, CloudCheck, CloudOff, FolderOpen, Loader2, RotateCcw } from 'lucide-react';
import { api, ApiError, type Page, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { toast } from '@/lib/toast';
import { Button } from '@/components/ui/button';
import { SimpleSelect } from '@/components/ui/select';
import { Label } from '@/components/ui/label';
import { PageHeader } from '@/components/composites/page-header';
import { LoadingState } from '@/components/composites/loading-state';
import { ErrorState } from '@/components/composites/error-state';
import { RelativeTime } from '@/components/composites/relative-time';
import { RequireRole } from '@/lib/auth';
import { cn } from '@/lib/utils';
import { DEFAULT_VALUES, STEP_FIELDS, STEP_TITLES, fromConfig, serverFieldToForm, stepOfField, toPayload, wizardSchema, type WizardValues } from './schema';
import { Step1Business } from './step1-business';
import { Step2Sources } from './step2-sources';
import { Step3Contacts } from './step3-contacts';
import { Step4Validation } from './step4-validation';
import { Step5Review } from './step5-review';

const BACKUP_KEY = 'lead-run-wizard-backup:v1';
type SaveState = { kind: 'idle' } | { kind: 'saving' } | { kind: 'saved'; at: string } | { kind: 'local'; reason: string } | { kind: 'error'; message: string };
type RunSummary = Schema<'RunSummary'>;

function readBackup(): { values: WizardValues; savedAt: string } | null {
  try {
    const raw = localStorage.getItem(BACKUP_KEY);
    return raw ? (JSON.parse(raw) as { values: WizardValues; savedAt: string }) : null;
  } catch { return null; }
}
function writeBackup(values: WizardValues) {
  try { localStorage.setItem(BACKUP_KEY, JSON.stringify({ values, savedAt: new Date().toISOString() })); } catch { /* storage blocked or full */ }
}
function clearBackup() { try { localStorage.removeItem(BACKUP_KEY); } catch { /* ignore */ } }

export function RunWizard() {
  return (
    <RequireRole role="operator">
      <WizardInner />
    </RequireRole>
  );
}

function WizardInner() {
  const router = useRouter();
  const qc = useQueryClient();
  const params = useSearchParams();
  const draftParam = params.get('draft') ? Number(params.get('draft')) : null;

  const form = useForm<WizardValues>({ resolver: zodResolver(wizardSchema), defaultValues: DEFAULT_VALUES, mode: 'onTouched', shouldFocusError: true });
  const [step, setStep] = React.useState(0);
  const [maxStep, setMaxStep] = React.useState(0);
  const [draftId, setDraftId] = React.useState<number | null>(draftParam);
  const [save, setSave] = React.useState<SaveState>({ kind: 'idle' });
  const [savingDraft, setSavingDraft] = React.useState(false);
  const [starting, setStarting] = React.useState(false);
  const [checking, setChecking] = React.useState(false);
  const [serverErrors, setServerErrors] = React.useState<{ field: string; message: string }[]>([]);
  const [promptDismissed, setPromptDismissed] = React.useState(false);
  const headingRef = React.useRef<HTMLHeadingElement>(null);

  const [loadedFor, setLoadedFor] = React.useState<number | null>(null); // draft id whose config is in the form
  const [touched, setTouched] = React.useState(false);
  const suppress = React.useRef(false);
  const timer = React.useRef<ReturnType<typeof setTimeout> | null>(null);
  const inflight = React.useRef(false);
  const again = React.useRef(false);
  const draftIdRef = React.useRef(draftId);
  React.useEffect(() => { draftIdRef.current = draftId; }, [draftId]);

  /* ---- load an existing draft (?draft=id) ---- */
  const draftQ = useQuery({
    queryKey: qk.detail('runs', draftParam ?? 0),
    queryFn: ({ signal }) => api.get<Schema<'RunDetail'>>(`/api/runs/${draftParam}`, { signal }),
    enabled: draftParam !== null && Number.isFinite(draftParam) && loadedFor !== draftParam,
    staleTime: 0,
    gcTime: 0,
  });
  React.useEffect(() => {
    if (!draftQ.data || draftParam === null || loadedFor === draftParam) return;
    if (draftQ.data.status !== 'draft' && draftQ.data.status !== 'scheduled') return;
    setLoadedFor(draftParam);
    suppress.current = true;
    const cfg = { ...(draftQ.data.config as Record<string, unknown>) };
    if (!cfg.name) cfg.name = draftQ.data.name;
    form.reset(fromConfig(cfg));
    suppress.current = false;
    setDraftId(draftParam);
    setSave({ kind: 'saved', at: new Date().toISOString() });
  }, [draftQ.data, draftParam, form, loadedFor]);

  /* ---- resume prompt (latest server draft or local backup) ---- */
  const latestDraft = useQuery({
    queryKey: qk.custom('runs', 'latest-draft'),
    queryFn: ({ signal }) => api.get<Page<RunSummary>>('/api/runs', { query: { tab: 'drafts', limit: 1 }, signal }),
    enabled: draftParam === null,
    staleTime: 0,
  });
  const [backup, setBackup] = React.useState<{ values: WizardValues; savedAt: string } | null>(null);
  React.useEffect(() => { setBackup(readBackup()); }, []);
  const serverDraft = draftParam === null ? latestDraft.data?.items[0] : undefined;
  const showPrompt = draftParam === null && !promptDismissed && !touched && (!!serverDraft || !!backup);

  const persistRef = React.useRef<((o: { silent: boolean }) => Promise<number | null>) | null>(null);
  const schedule = React.useCallback(() => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => { void persistRef.current?.({ silent: true }); }, 1500);
  }, []);

  /* ---- persistence: server draft when possible, always a local backup ---- */
  const persist = React.useCallback(async (opts: { silent: boolean }): Promise<number | null> => {
    const values = form.getValues();
    writeBackup(values);
    const config = toPayload(values);
    if (inflight.current) { again.current = true; return draftIdRef.current; }
    inflight.current = true;
    setSave({ kind: 'saving' });
    try {
      let id = draftIdRef.current;
      if (id) {
        const res = await api.patch<Schema<'DraftSaved'>>(`/api/runs/${id}`, { config });
        void qc.invalidateQueries({ queryKey: qk.all('runs') });
        setSave({ kind: 'saved', at: new Date().toISOString() });
        if (!res.valid && !opts.silent) toast.info('Draft saved', 'It is not complete yet: ' + res.errors.slice(0, 2).map((e) => e.message).join('; '));
        else if (!opts.silent) toast.success('Draft saved');
      } else {
        const created = await api.post<RunSummary>('/api/runs', { config, action: 'draft' });
        id = created.id;
        setLoadedFor(id);
        draftIdRef.current = id;
        setDraftId(id);
        router.replace(`/runs/new?draft=${id}`, { scroll: false });
        void qc.invalidateQueries({ queryKey: qk.all('runs') });
        setSave({ kind: 'saved', at: new Date().toISOString() });
        if (!opts.silent) toast.success('Draft saved');
      }
      return id;
    } catch (e) {
      const err = e instanceof ApiError ? e : null;
      if (!draftIdRef.current && err && (err.status === 422)) {
        // POST /api/runs only stores complete configurations; until then the form is kept in this browser only.
        setSave({ kind: 'local', reason: 'Kept in this browser. The server draft is created once sources or imported domains are chosen and the configuration is valid.' });
        if (!opts.silent) toast.warning('Not saved to the server yet', err.message);
      } else {
        setSave({ kind: 'error', message: err?.message ?? 'Could not save the draft.' });
        if (!opts.silent) toast.error('Draft not saved', err?.message);
      }
      return null;
    } finally {
      inflight.current = false;
      if (again.current) { again.current = false; schedule(); }
    }
  }, [form, qc, router, schedule]);
  React.useEffect(() => { persistRef.current = persist; }, [persist]);

  React.useEffect(() => {
    const unsub = form.subscribe({ formState: { values: true }, callback: () => { if (suppress.current) return; setTouched(true); schedule(); } });
    return () => { unsub(); if (timer.current) clearTimeout(timer.current); };
  }, [form, schedule]);

  /* ---- navigation ---- */
  const go = (n: number) => {
    setStep(n);
    setMaxStep((m) => Math.max(m, n));
    requestAnimationFrame(() => { headingRef.current?.focus(); window.scrollTo({ top: 0 }); });
  };

  const checkStep = async (s: number): Promise<boolean> => {
    const names = STEP_FIELDS[s];
    if (names.length) {
      const ok = await form.trigger(names as never, { shouldFocus: true });
      if (!ok) return false;
    }
    if (s >= 4) return true;
    // server-side validation of the fields belonging to this step
    setChecking(true);
    try {
      const res = await api.post<Schema<'ValidateOut'>>('/api/runs/validate', toPayload(form.getValues()));
      const mine = (res.errors ?? []).filter((e) => stepOfField(e.field) === s);
      mine.forEach((e) => form.setError(serverFieldToForm(e.field) as never, { type: 'server', message: e.message }, { shouldFocus: true }));
      return mine.length === 0;
    } catch (e) {
      toast.error('Could not validate on the server', e instanceof Error ? e.message : undefined);
      return false;
    } finally { setChecking(false); }
  };

  const next = async () => {
    if (!(await checkStep(step))) return;
    void persist({ silent: true });
    go(step + 1);
  };

  const gotoStep = async (n: number) => {
    if (n === step) return;
    if (n < step) { go(n); return; }
    if (n > maxStep + 0) { // only forward through validation
      let s = step;
      while (s < n) { if (!(await checkStep(s))) { go(s); return; } s += 1; }
      go(n);
      return;
    }
    go(n);
  };

  /* ---- actions ---- */
  const saveDraftNow = async () => {
    setSavingDraft(true);
    try { await persist({ silent: false }); } finally { setSavingDraft(false); }
  };

  const start = async () => {
    setServerErrors([]);
    const ok = await form.trigger();
    if (!ok) {
      const bad = Object.keys(form.formState.errors);
      go(Math.min(...bad.map((f) => stepOfField(f)), 4));
      toast.error('Some fields need attention');
      return;
    }
    setStarting(true);
    try {
      const config = toPayload(form.getValues());
      let run: RunSummary;
      if (!draftIdRef.current || config.schedule_at) {
        run = await api.post<RunSummary>('/api/runs', { config, action: 'start' });
        if (draftIdRef.current) await api.delete(`/api/runs/${draftIdRef.current}`).catch(() => undefined);
      } else {
        await api.patch(`/api/runs/${draftIdRef.current}`, { config });
        run = await api.post<RunSummary>(`/api/runs/${draftIdRef.current}/start`);
      }
      clearBackup();
      if (timer.current) clearTimeout(timer.current);
      suppress.current = true;
      await qc.invalidateQueries({ queryKey: qk.all('runs') });
      await qc.invalidateQueries({ queryKey: qk.all('dashboard') });
      toast.success(run.status === 'scheduled' ? 'Run scheduled' : 'Run started', run.name);
      router.push(`/runs/${run.id}`);
    } catch (e) {
      const err = e instanceof ApiError ? e : null;
      const fields = err && Array.isArray(err.detail) ? (err.detail as { field: string; message: string }[]) : [];
      if (fields.length) {
        setServerErrors(fields);
        fields.forEach((f) => form.setError(serverFieldToForm(f.field) as never, { type: 'server', message: f.message }));
        go(4);
      }
      toast.error(err?.status === 409 ? 'Run could not be started' : 'Run not started', err?.message);
    } finally { setStarting(false); }
  };

  /* ---- presets ---- */
  const presetsQ = useQuery({
    queryKey: qk.custom('presets', 'run'),
    queryFn: ({ signal }) => api.get<Schema<'PresetOut'>[]>('/api/presets', { query: { kind: 'run' }, signal }),
    staleTime: 30_000,
  });
  const loadPreset = (id: string) => {
    const p = presetsQ.data?.find((x) => String(x.id) === id);
    if (!p) return;
    const cur = form.getValues();
    const v = fromConfig({ ...p.config, name: cur.name || (p.config as { name?: string }).name || p.name });
    form.reset(v);
    setTouched(true);
    schedule();
    toast.success(`Loaded preset “${p.name}”`);
  };

  /* ---- prompt actions ---- */
  const resumeServer = () => { if (serverDraft) router.replace(`/runs/new?draft=${serverDraft.id}`); setPromptDismissed(true); };
  const restoreBackup = () => {
    if (!backup) return;
    suppress.current = true; form.reset({ ...DEFAULT_VALUES, ...backup.values }); suppress.current = false;
    setTouched(true); setPromptDismissed(true); schedule(); toast.success('Restored the unsaved form from this browser');
  };
  const dismissPrompt = () => { clearBackup(); setBackup(null); setPromptDismissed(true); };

  /* ---- render ---- */
  if (draftParam !== null && draftQ.isPending && loadedFor !== draftParam) return <><Header /><LoadingState label="Loading draft…" /></>;
  if (draftParam !== null && draftQ.isError) return <><Header /><ErrorState error={draftQ.error} title="Draft could not be loaded" onRetry={() => draftQ.refetch()} /></>;
  if (draftParam !== null && draftQ.data && !['draft', 'scheduled'].includes(draftQ.data.status) && loadedFor !== draftParam) {
    return <><Header /><ErrorState title="This run can no longer be edited" message={`It is ${draftQ.data.status}. Only drafts and scheduled runs can be changed.`} onRetry={undefined} /><p className="mt-3 text-center"><Link className="text-primary underline" href={`/runs/${draftQ.data.id}`}>Open the run</Link></p></>;
  }

  return (
    <>
      <Header status={<SaveIndicator s={save} />} />
      {showPrompt && (
        <div role="region" aria-label="Resume draft" className="mb-4 flex flex-wrap items-center gap-3 rounded-lg border border-info-border bg-info-soft p-3 text-sm text-info-foreground">
          <FolderOpen className="size-4 shrink-0" aria-hidden />
          <p className="min-w-0 flex-1">
            {serverDraft
              ? <>You have an unfinished draft, <strong>{serverDraft.name}</strong>, created <RelativeTime value={serverDraft.created_at} />.</>
              : <>A form from this browser was not submitted (saved <RelativeTime value={backup?.savedAt} />).</>}
          </p>
          <div className="flex gap-2">
            <Button size="sm" onClick={serverDraft ? resumeServer : restoreBackup}><RotateCcw /> Resume draft</Button>
            <Button size="sm" variant="outline" onClick={dismissPrompt}>Start fresh</Button>
          </div>
        </div>
      )}
      <FormProvider {...form}>
        <form noValidate onSubmit={(e) => { e.preventDefault(); if (step < 4) void next(); }} aria-label="New sourcing run">
          <Stepper step={step} maxStep={maxStep} onGoto={(n) => void gotoStep(n)} />
          <div className="mt-5 space-y-4">
            <div className="flex flex-wrap items-end justify-between gap-3">
              <h2 ref={headingRef} tabIndex={-1} className="text-lg font-semibold outline-none" data-testid="step-heading">
                Step {step + 1} of 5: {STEP_TITLES[step]}
              </h2>
              {step === 0 && presetsQ.data && presetsQ.data.length > 0 && (
                <div className="flex items-center gap-2">
                  <Label htmlFor="load-preset" className="text-xs text-muted-foreground">Load preset</Label>
                  <SimpleSelect id="load-preset" className="h-8 w-52 text-xs" placeholder="Choose a preset…" onValueChange={loadPreset}
                    options={presetsQ.data.map((p) => ({ value: String(p.id), label: p.name }))} />
                </div>
              )}
            </div>
            {step === 0 && <Step1Business />}
            {step === 1 && <Step2Sources />}
            {step === 2 && <Step3Contacts />}
            {step === 3 && <Step4Validation />}
            {step === 4 && <Step5Review onGoto={(n) => go(n)} onSaveDraft={() => void saveDraftNow()} savingDraft={savingDraft} onStart={() => void start()} starting={starting} draftId={draftId} serverErrors={serverErrors} />}
          </div>
          {step < 4 && (
            <div className="sticky bottom-0 -mx-4 mt-6 flex items-center justify-between gap-2 border-t bg-background/95 px-4 py-3 backdrop-blur sm:-mx-6 sm:px-6">
              <Button type="button" variant="outline" disabled={step === 0} onClick={() => go(step - 1)}><ArrowLeft /> Back</Button>
              <Button type="button" onClick={() => void next()} loading={checking}>Next: {STEP_TITLES[step + 1]} <ArrowRight /></Button>
            </div>
          )}
          {step === 4 && <div className="mt-4"><Button type="button" variant="outline" onClick={() => go(3)}><ArrowLeft /> Back</Button></div>}
        </form>
      </FormProvider>
    </>
  );
}

function Header({ status }: { status?: React.ReactNode }) {
  return (
    <PageHeader
      title="New sourcing run"
      description="Define who to find, where to look, which contacts count and how hard to work."
      breadcrumbs={[{ label: 'Runs', href: '/runs' }, { label: 'New run' }]}
      meta={status}
    />
  );
}

function SaveIndicator({ s }: { s: SaveState }) {
  const base = 'inline-flex items-center gap-1.5 text-xs';
  return (
    <span aria-live="polite" data-testid="save-status" className={cn(base, s.kind === 'error' ? 'text-danger' : 'text-muted-foreground')}>
      {s.kind === 'saving' && <><Loader2 className="size-3.5 animate-spin" aria-hidden /> Saving draft…</>}
      {s.kind === 'saved' && <><CloudCheck className="size-3.5" aria-hidden /> Draft saved <RelativeTime value={s.at} /></>}
      {s.kind === 'local' && <><CloudOff className="size-3.5" aria-hidden /> {s.reason}</>}
      {s.kind === 'error' && <><CloudOff className="size-3.5" aria-hidden /> Draft not saved: {s.message}</>}
      {s.kind === 'idle' && <>Changes are saved automatically as a draft.</>}
    </span>
  );
}

function Stepper({ step, maxStep, onGoto }: { step: number; maxStep: number; onGoto: (n: number) => void }) {
  return (
    <nav aria-label="Wizard steps">
      <ol className="flex items-center gap-1 sm:gap-2">
        {STEP_TITLES.map((t, i) => {
          const done = i < step || (i <= maxStep && i !== step);
          const current = i === step;
          return (
            <li key={t} className="flex min-w-0 flex-1 items-center">
              <button
                type="button" onClick={() => onGoto(i)} aria-current={current ? 'step' : undefined}
                className={cn('flex min-w-0 flex-1 items-center gap-2 rounded-md border px-2 py-2 text-left text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring max-sm:justify-center',
                  current ? 'border-primary bg-accent/50 font-medium' : 'hover:bg-muted/60', !current && !done && 'text-muted-foreground')}
              >
                <span className={cn('flex size-6 shrink-0 items-center justify-center rounded-full border text-xs font-semibold', current ? 'border-primary bg-primary text-primary-foreground' : done ? 'border-success-border bg-success-soft text-success-foreground' : 'bg-background')}>
                  {done && !current ? <Check className="size-3.5" aria-hidden /> : i + 1}
                </span>
                <span className="truncate max-sm:sr-only">{t}</span>
                <span className="sr-only">{done && !current ? ' (visited)' : ''}</span>
              </button>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
