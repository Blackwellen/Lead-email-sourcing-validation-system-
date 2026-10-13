'use client';
import * as React from 'react';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { api, type Schema } from '@/lib/api';
import { qk } from '@/lib/query-keys';
import { useDebounce } from '@/hooks/use-debounce';
import { toPayload, type WizardValues } from './schema';

export type RunEstimate = Schema<'RunEstimateOut'>;
export type RunValidation = Schema<'ValidateOut'>;

/** POST /api/runs/estimate for the current form values (debounced). `valid:false` means the payload is not yet complete. */
export function useRunEstimate(values: WizardValues, enabled = true) {
  const json = useDebounce(React.useMemo(() => JSON.stringify(toPayload(values)), [values]), 700);
  return useQuery({
    queryKey: qk.custom('runs', 'estimate', json),
    queryFn: ({ signal }) => api.post<RunEstimate>('/api/runs/estimate', JSON.parse(json), { signal }),
    enabled,
    staleTime: 60_000,
    placeholderData: keepPreviousData,
  });
}

/** POST /api/verification/estimate for the chosen verification mode (cost text, per-stage options and limits). */
export function useVerificationEstimate(values: WizardValues, enabled = true) {
  const body = useDebounce(JSON.stringify({ mode: values.verification.mode, options: values.verification.mode === 'custom' ? values.verification.custom : {}, count: Math.max(1, Number.isFinite(values.target.count) ? values.target.count : 1) }), 500);
  return useQuery({
    queryKey: qk.custom('verification', 'estimate', body),
    queryFn: ({ signal }) => api.post<Schema<'VerificationEstimateOut'>>('/api/verification/estimate', JSON.parse(body), { signal }),
    enabled,
    staleTime: 60_000,
    placeholderData: keepPreviousData,
  });
}

/** POST /api/runs/validate (server-side referential and safety checks) for the current form values. */
export function useRunValidation(values: WizardValues, enabled = true) {
  const json = useDebounce(React.useMemo(() => JSON.stringify(toPayload(values)), [values]), 500);
  return useQuery({
    queryKey: qk.custom('runs', 'validate', json),
    queryFn: ({ signal }) => api.post<RunValidation>('/api/runs/validate', JSON.parse(json), { signal }),
    enabled,
    staleTime: 30_000,
    placeholderData: keepPreviousData,
  });
}
