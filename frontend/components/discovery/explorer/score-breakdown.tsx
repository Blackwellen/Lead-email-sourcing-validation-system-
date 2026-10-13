'use client';
import * as React from 'react';
import { ChevronDown } from 'lucide-react';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { cn } from '@/lib/utils';
import { titleCase } from '@/lib/format';
import { SCORE_LABEL } from './explorer-utils';

/** Relevance score with its full, deterministic breakdown (catalogue_full.SCORE_WEIGHTS). */
export function ScoreBreakdown({ score, breakdown, name }: { score: number | null; breakdown: Record<string, number>; name: string }) {
  const parts = Object.entries(breakdown).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  if (score === null) return null;
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button type="button" aria-label={`Relevance score ${score} for ${name}: show breakdown`} data-testid="score-button"
          className="inline-flex shrink-0 items-center gap-1 rounded-md border bg-muted/50 px-2 py-1 text-xs font-semibold tabular hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
          Score {formatScore(score)} <ChevronDown className="size-3" aria-hidden />
        </button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-72" data-testid="score-breakdown">
        <p className="mb-2 text-xs font-semibold">How this score is made</p>
        {parts.length === 0 ? <p className="text-xs text-muted-foreground">No scoring factor applied (no filters matched a weighted factor).</p> : (
          <table className="w-full text-xs">
            <caption className="sr-only">Score breakdown for {name}</caption>
            <thead className="sr-only"><tr><th scope="col">Factor</th><th scope="col">Points</th></tr></thead>
            <tbody>
              {parts.map(([k, v]) => (
                <tr key={k} className="border-b last:border-0">
                  <td className="py-1 pr-2">{SCORE_LABEL[k] ?? titleCase(k)}</td>
                  <td className={cn('py-1 text-right font-medium tabular', v < 0 && 'text-danger')}>{v > 0 ? '+' : v < 0 ? '−' : ''}{formatScore(Math.abs(v))}</td>
                </tr>
              ))}
            </tbody>
            <tfoot><tr><th scope="row" className="pt-1.5 text-left font-semibold">Total</th><td className="pt-1.5 text-right font-semibold tabular">{formatScore(score)}</td></tr></tfoot>
          </table>
        )}
        <p className="mt-2 text-2xs text-muted-foreground">Ties are broken by source id, so the same search always gives the same order.</p>
      </PopoverContent>
    </Popover>
  );
}

const formatScore = (n: number) => (Number.isInteger(n) ? String(n) : n.toFixed(1));
