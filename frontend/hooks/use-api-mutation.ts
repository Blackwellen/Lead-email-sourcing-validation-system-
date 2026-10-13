'use client';
import { useMutation, useQueryClient, type QueryKey, type UseMutationOptions } from '@tanstack/react-query';
import { ApiError } from '@/lib/api';
import { toast } from '@/lib/toast';

export interface ApiMutationOptions<TData, TVars, TCtx = unknown>
  extends Omit<UseMutationOptions<TData, ApiError, TVars, TCtx>, 'mutationFn'> {
  mutationFn: (vars: TVars) => Promise<TData>;
  /** Toast shown on success. String or function of (data, vars). `false`/undefined = none. */
  successMessage?: string | false | ((data: TData, vars: TVars) => string);
  /** Override the error toast title (description is the server message). `false` = no toast (e.g. you render field errors). */
  errorMessage?: string | false;
  /** Query keys (prefixes) to invalidate on success. */
  invalidate?: QueryKey[];
}

/**
 * useMutation + toast + invalidation.
 * Validation (422) errors are NOT toasted by default when `errorMessage:false`; otherwise they are, and
 * the caller can still read `error.fields` to map them onto form inputs (see `applyFieldErrors`).
 */
export function useApiMutation<TData = unknown, TVars = void, TCtx = unknown>(opts: ApiMutationOptions<TData, TVars, TCtx>) {
  const qc = useQueryClient();
  const { successMessage, errorMessage, invalidate, onSuccess, onError, ...rest } = opts;
  return useMutation<TData, ApiError, TVars, TCtx>({
    ...rest,
    onSuccess: async (data, vars, ctx, meta) => {
      if (successMessage) toast.success(typeof successMessage === 'function' ? successMessage(data, vars) : successMessage);
      if (invalidate?.length) await Promise.all(invalidate.map((queryKey) => qc.invalidateQueries({ queryKey })));
      return (onSuccess as ((...a: unknown[]) => unknown) | undefined)?.(data, vars, ctx, meta);
    },
    onError: (err, vars, ctx, meta) => {
      if (errorMessage !== false && !(err instanceof DOMException)) {
        if (err.isMaintenance) toast.warning('System is in maintenance mode', err.message);
        else if (err.isNetwork) toast.error('Cannot reach the server', err.message);
        else toast.error(errorMessage || (err.isForbidden ? 'Not allowed' : err.isValidation ? 'Please check the form' : 'Action failed'), err.message);
      }
      return (onError as ((...a: unknown[]) => unknown) | undefined)?.(err, vars, ctx, meta);
    },
  });
}

/** Map `ApiError.fields` onto react-hook-form: `applyFieldErrors(err, form.setError)`. Returns true if any were applied. */
export function applyFieldErrors(
  err: unknown,
  setError: (name: never, error: { type: string; message: string }) => void,
): boolean {
  if (!(err instanceof ApiError)) return false;
  const entries = Object.entries(err.fields);
  entries.forEach(([field, message]) => setError(field as never, { type: 'server', message }));
  return entries.length > 0;
}
