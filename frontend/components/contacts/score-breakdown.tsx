import * as React from 'react';
import { ConfidenceMeter } from '@/components/composites/confidence-meter';
import { Progress } from '@/components/ui/progress';
import { cn } from '@/lib/utils';
import { titleCase } from '@/lib/format';

export interface BreakdownComponent { component: string; points: number; max: number; note?: string }
export interface Breakdown { score?: number; reachability?: string; components?: BreakdownComponent[]; caps_applied?: string[]; reasons?: string[]; disclaimer?: string }

const CAP_TEXT = (c: string) => {
  const m = /^(.*)_max_(\d+)$/.exec(c);
  return m ? `${titleCase(m[1])} (score limited to ${m[2]})` : c === 'syntax_invalid_zero' ? 'Invalid syntax (score set to 0)' : titleCase(c);
};

/** Bar list of confidence components, penalties and caps. Everything shown comes from the stored breakdown. */
export function ScoreBreakdown({ confidence, breakdown }: { confidence: number; breakdown: Breakdown }) {
  const comps = breakdown.components ?? [];
  const positive = comps.filter((c) => c.max > 0);
  const adjustments = comps.filter((c) => c.max <= 0);
  const has = comps.length > 0;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <p className="tabular text-3xl font-semibold" aria-label={`Confidence score ${Math.round(confidence)} out of 100`}>{Math.round(confidence)}<span className="text-base font-normal text-muted-foreground"> / 100</span></p>
        <ConfidenceMeter value={confidence} />
      </div>
      {!has && <p className="text-sm text-muted-foreground">No score breakdown has been stored for this address yet (it has not been validated).</p>}
      {has && (
        <>
          <ul className="space-y-2.5" aria-label="Score components">
            {positive.map((c) => {
              const pct = c.max ? (c.points / c.max) * 100 : 0;
              return (
                <li key={c.component} className="grid gap-1">
                  <div className="flex items-baseline justify-between gap-3 text-sm">
                    <span className="font-medium">{titleCase(c.component)}</span>
                    <span className="tabular text-xs text-muted-foreground">{c.points} / {c.max} points</span>
                  </div>
                  <Progress value={pct} tone={pct >= 70 ? 'success' : pct >= 35 ? 'warning' : 'danger'} aria-label={`${titleCase(c.component)} ${c.points} of ${c.max} points`} />
                  {c.note && <p className="text-xs text-muted-foreground">{c.note}</p>}
                </li>
              );
            })}
          </ul>
          {adjustments.length > 0 && (
            <div>
              <h4 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Penalties and adjustments</h4>
              <ul className="space-y-1.5">
                {adjustments.map((c) => (
                  <li key={c.component} className="flex items-baseline justify-between gap-3 text-sm">
                    <span><span className="font-medium">{titleCase(c.component)}</span>{c.note && <span className="block text-xs text-muted-foreground">{c.note}</span>}</span>
                    <span className={cn('tabular shrink-0 font-medium', c.points < 0 ? 'text-danger-foreground' : '')}>{c.points > 0 ? `+${c.points}` : c.points}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          <div>
            <h4 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Caps applied</h4>
            {(breakdown.caps_applied ?? []).length === 0
              ? <p className="text-sm text-muted-foreground">No caps were applied to this score.</p>
              : <ul className="list-disc space-y-1 pl-5 text-sm">{breakdown.caps_applied!.map((c) => <li key={c}>{CAP_TEXT(c)}</li>)}</ul>}
          </div>
          {(breakdown.reasons ?? []).length > 0 && (
            <div>
              <h4 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Reachability reasoning</h4>
              <ul className="list-disc space-y-1 pl-5 text-sm">{breakdown.reasons!.map((r) => <li key={r}>{r}</li>)}</ul>
            </div>
          )}
        </>
      )}
    </div>
  );
}
