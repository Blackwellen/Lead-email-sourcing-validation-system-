'use client';
import * as React from 'react';
import { useQuery } from '@tanstack/react-query';
import { ClipboardCheck } from 'lucide-react';
import { api, type ApiError, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { formatDateTime, formatPercent } from '@/lib/format';
import { ErrorState } from '@/components/composites/error-state';
import { LoadingState } from '@/components/composites/loading-state';
import { Badge } from '@/components/ui/badge';
import { Progress } from '@/components/ui/progress';
import { FLAG_LABELS, OPPORTUNITY_LABELS, TRI, TriStateBadge, flagLabel, type TriState } from './shared';

type Completeness = Schema<'CompletenessOut'>;

export function useCompleteness(orgId: number | string | null) {
  return useQuery<Completeness, ApiError>({
    queryKey: qk.custom('businesses', 'completeness', String(orgId)),
    queryFn: ({ signal }) => api.get<Completeness>(`/api/organisations/${orgId}/completeness`, { signal }),
    enabled: orgId !== null && orgId !== '',
  });
}

/** Compact one-line summary (score + counts per state) for headers. */
export function CompletenessSummary({ orgId }: { orgId: number | string }) {
  const q = useCompleteness(orgId);
  if (!q.data) return null;
  const counts = countStates(q.data.flags);
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground" data-testid="completeness-summary">
      <ClipboardCheck className="size-3.5" aria-hidden />Research completeness <strong className="tabular text-foreground">{q.data.score}</strong>/100
      <span className="sr-only">: {(Object.keys(TRI) as TriState[]).map((s) => `${counts[s]} ${TRI[s].label}`).join(', ')}</span>
    </span>
  );
}

function countStates(flags: Record<string, string>) {
  const c: Record<TriState, number> = { yes: 0, no: 0, unknown: 0, in_progress: 0 };
  for (const v of Object.values(flags)) if (v in c) c[v as TriState] += 1;
  return c;
}

/**
 * Research completeness for one business: 8 tri-state flags (+ "in progress"), each with the reason, plus the score.
 * "Not researched yet" (unknown) is neutral and visually distinct from "No – researched" (genuinely unavailable).
 */
export function CompletenessPanel({ orgId, headingLevel = 2 }: { orgId: number | string; headingLevel?: 2 | 3 }) {
  const q = useCompleteness(orgId);
  const H = (headingLevel === 2 ? 'h2' : 'h3') as 'h2';
  if (q.isPending) return <LoadingState rows={4} label="Loading research completeness" />;
  if (q.isError) return <ErrorState compact error={q.error} onRetry={() => q.refetch()} />;
  const c = q.data;
  const counts = countStates(c.flags);
  const order = Object.keys(FLAG_LABELS).filter((k) => k in c.flags).concat(Object.keys(c.flags).filter((k) => !(k in FLAG_LABELS)));
  return (
    <section aria-labelledby={`compl-${orgId}`} className="space-y-4" data-testid="completeness-panel">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <H id={`compl-${orgId}`} className="text-sm font-semibold">Research completeness</H>
          <p className="text-xs text-muted-foreground">Evaluated live from stored evidence{c.stored_updated_at ? `; worker snapshot ${formatDateTime(c.stored_updated_at)}` : ''}.</p>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          {c.needs_review && <Badge variant="warning">Needs review</Badge>}
          {c.gap_exhausted && <Badge variant="neutral">Research ladder exhausted</Badge>}
        </div>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="space-y-1">
          <div className="flex items-baseline justify-between text-xs"><span className="text-muted-foreground">Completeness score</span><span className="text-base font-semibold tabular">{c.score}<span className="text-xs text-muted-foreground">/100</span></span></div>
          <Progress value={c.score} aria-label="Completeness score" />
        </div>
        <div className="space-y-1">
          <div className="flex items-baseline justify-between text-xs"><span className="text-muted-foreground">Research steps resolved (yes or no)</span><span className="text-base font-semibold tabular">{formatPercent(c.research_progress * 100, 0)}</span></div>
          <Progress value={c.research_progress * 100} aria-label="Research steps resolved" />
        </div>
      </div>
      <p className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground" aria-label="State counts">
        {(['yes', 'no', 'in_progress', 'unknown'] as TriState[]).map((s) => <span key={s}><span className="tabular font-medium text-foreground">{counts[s]}</span> {TRI[s].label.toLowerCase()}</span>)}
      </p>
      <dl className="divide-y rounded-lg border">
        {order.map((k) => (
          <div key={k} className="grid gap-1 px-3 py-2 sm:grid-cols-[minmax(0,14rem)_auto_minmax(0,1fr)] sm:items-center sm:gap-3" data-testid={`flag-${k}`}>
            <dt className="text-sm font-medium">{flagLabel(k)}</dt>
            <dd><TriStateBadge state={c.flags[k]} /></dd>
            <dd className="text-xs text-muted-foreground">{c.reasons[k] ?? ''}</dd>
          </div>
        ))}
      </dl>
      {c.missing_opportunities.length > 0 && (
        <div>
          <h4 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">What could still be found</h4>
          <ul className="space-y-1.5 text-sm">
            {c.missing_opportunities.map((m) => {
              const meta = OPPORTUNITY_LABELS[m.classification] ?? { label: m.classification, variant: 'neutral' as const };
              return (
                <li key={m.flag} className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                  <Badge variant={meta.variant}>{meta.label}</Badge>
                  <span className="font-medium">{flagLabel(m.flag)}</span>
                  {m.next_step && <span className="text-xs text-muted-foreground">Next: {m.next_step}</span>}
                </li>
              );
            })}
          </ul>
        </div>
      )}
      <details className="text-xs text-muted-foreground">
        <summary className="cursor-pointer rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">What the states mean</summary>
        <ul className="mt-2 space-y-1">
          {(['yes', 'no', 'in_progress', 'unknown'] as TriState[]).map((s) => <li key={s} className="flex flex-wrap items-center gap-2"><TriStateBadge state={s} /> {c.legend[s] ?? TRI[s].help}</li>)}
        </ul>
        {c.gap_steps_tried.length > 0 && <p className="mt-2">Recovery steps tried: {c.gap_steps_tried.join(', ')}</p>}
      </details>
    </section>
  );
}
