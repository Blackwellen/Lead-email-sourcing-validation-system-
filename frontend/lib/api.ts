import type { components } from './api-types';
import { setApiStatus } from './api-status';

/* ------------------------------------------------------------------ types */
export type Schemas = components['schemas'];
/** `Schema<'RunSummary'>` -> components['schemas']['RunSummary'] */
export type Schema<K extends keyof Schemas> = Schemas[K];
/** Backend cursor page envelope: `{items, next_cursor, total_estimate, approximate}` */
export interface Page<T> {
  items: T[];
  next_cursor: string | null;
  total_estimate: number | null;
  approximate: boolean;
}
export type Role = 'admin' | 'operator' | 'viewer';
export type Session = Schema<'SessionOut'>;
export type User = Schema<'UserOut'>;

/* ------------------------------------------------------------------ errors */
export interface FieldIssue { field: string; message: string }

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  /** field path -> message (from 422 `detail: [{field,message}]`) */
  readonly fields: Record<string, string>;
  readonly detail: unknown;
  readonly retryAfter: number | null;
  readonly requestId: string | null;
  constructor(init: { status: number; code: string; message: string; detail?: unknown; retryAfter?: number | null; requestId?: string | null }) {
    super(init.message);
    this.name = 'ApiError';
    this.status = init.status;
    this.code = init.code;
    this.detail = init.detail;
    this.retryAfter = init.retryAfter ?? null;
    this.requestId = init.requestId ?? null;
    this.fields = {};
    if (Array.isArray(init.detail)) {
      for (const d of init.detail as Partial<FieldIssue>[]) {
        if (d && typeof d.field === 'string' && typeof d.message === 'string' && !(d.field in this.fields)) this.fields[d.field] = d.message;
      }
    }
  }
  get isNetwork() { return this.status === 0 || this.code === 'unreachable'; }
  get isValidation() { return this.status === 422 || this.code === 'validation_error'; }
  get isAuth() { return this.status === 401; }
  get isForbidden() { return this.status === 403; }
  get isMaintenance() { return this.code === 'maintenance_mode'; }
}

export function isApiError(e: unknown): e is ApiError {
  return e instanceof ApiError;
}
export function errorMessage(e: unknown, fallback = 'Something went wrong.') {
  return e instanceof Error && e.message ? e.message : fallback;
}

/* ------------------------------------------------------------------ csrf */
const CSRF_COOKIE = 'lead_csrf';
let memoryCsrf: string | null = null;
export function setCsrfToken(t: string | null) { memoryCsrf = t; }
export function getCsrfToken(): string | null {
  if (typeof document !== 'undefined') {
    const m = document.cookie.match(new RegExp(`(?:^|; )${CSRF_COOKIE}=([^;]*)`));
    if (m) return decodeURIComponent(m[1]);
  }
  return memoryCsrf;
}

/* ------------------------------------------------------------------ request */
export type Query = Record<string, string | number | boolean | null | undefined | (string | number)[]>;

export interface RequestOptions {
  query?: Query;
  /** JSON body (objects are serialised) or FormData (sent as multipart). */
  body?: unknown;
  signal?: AbortSignal;
  headers?: Record<string, string>;
  /** Don't redirect to /login on 401 (used by useMe and the login form). */
  skipAuthRedirect?: boolean;
}

/** Arrays are sent comma-joined (backend filters accept CSV); null/undefined/'' are dropped. */
export function buildQuery(q?: Query): string {
  if (!q) return '';
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(q)) {
    if (v === null || v === undefined || v === '') continue;
    if (Array.isArray(v)) { if (v.length) sp.set(k, v.join(',')); }
    else sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : '';
}

let redirecting = false;
function redirectToLogin() {
  if (typeof window === 'undefined' || redirecting) return;
  if (window.location.pathname.startsWith('/login')) return;
  redirecting = true;
  const next = window.location.pathname + window.location.search;
  // Full navigation on purpose: resets all client state/caches for the next user.
  // eslint-disable-next-line @next/next/no-location-assign-relative-destination
  window.location.assign(`/login?next=${encodeURIComponent(next)}`);
}

export async function request<T = unknown>(method: string, path: string, opts: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json', ...opts.headers };
  let body: BodyInit | undefined;
  if (opts.body instanceof FormData) body = opts.body;
  else if (opts.body !== undefined) { headers['Content-Type'] = 'application/json'; body = JSON.stringify(opts.body); }
  if (method !== 'GET' && method !== 'HEAD') {
    const csrf = getCsrfToken();
    if (csrf) headers['X-CSRF-Token'] = csrf;
  }

  let res: Response;
  try {
    res = await fetch(path + buildQuery(opts.query), { method, headers, body, credentials: 'include', signal: opts.signal, cache: 'no-store' });
  } catch (e) {
    if ((e as Error)?.name === 'AbortError') throw e;
    setApiStatus({ unreachable: true });
    throw new ApiError({ status: 0, code: 'network_error', message: 'Cannot reach the server. Check your connection and try again.' });
  }

  // Proxy errors from Next's rewrite when the backend is down.
  // (The real API always answers errors as JSON, so a non-JSON 5xx means the proxy could not reach it.)
  const proxyFailure = res.status >= 500 && !(res.headers.get('content-type') || '').includes('json') && res.status !== 503;
  if (res.status === 502 || res.status === 504 || proxyFailure) {
    setApiStatus({ unreachable: true });
  } else {
    setApiStatus({ unreachable: false });
  }

  if (res.ok) {
    if (res.status === 204) return undefined as T;
    const ct = res.headers.get('content-type') || '';
    if (ct.includes('application/json')) return (await res.json()) as T;
    return (await res.blob()) as unknown as T;
  }

  let code = 'error';
  let message = res.statusText || `Request failed (${res.status})`;
  let detail: unknown;
  try {
    const j = await res.json();
    if (j?.error) { code = j.error.code ?? code; message = j.error.message ?? message; detail = j.error.detail; }
    else if (Array.isArray(j?.detail)) {
      code = 'validation_error'; message = 'The request is invalid.';
      detail = j.detail.map((d: { loc?: (string | number)[]; msg?: string }) => ({ field: (d.loc ?? []).filter((p) => p !== 'body').join('.'), message: d.msg ?? 'Invalid value' }));
    } else if (typeof j?.detail === 'string') message = j.detail;
  } catch {
    if (proxyFailure || res.status === 502 || res.status === 504) { code = 'unreachable'; message = 'The API is unreachable right now.'; }
  }
  const ra = res.headers.get('retry-after');
  const err = new ApiError({
    status: res.status, code, message, detail,
    retryAfter: ra && !Number.isNaN(Number(ra)) ? Number(ra) : null,
    requestId: res.headers.get('x-request-id'),
  });

  if (res.status === 503 && code === 'maintenance_mode') setApiStatus({ maintenance: { message } });
  if (res.status === 401 && !opts.skipAuthRedirect) redirectToLogin();
  throw err;
}

export const api = {
  get: <T = unknown>(path: string, opts?: Omit<RequestOptions, 'body'>) => request<T>('GET', path, opts),
  post: <T = unknown>(path: string, body?: unknown, opts?: Omit<RequestOptions, 'body'>) => request<T>('POST', path, { ...opts, body }),
  put: <T = unknown>(path: string, body?: unknown, opts?: Omit<RequestOptions, 'body'>) => request<T>('PUT', path, { ...opts, body }),
  patch: <T = unknown>(path: string, body?: unknown, opts?: Omit<RequestOptions, 'body'>) => request<T>('PATCH', path, { ...opts, body }),
  delete: <T = unknown>(path: string, opts?: RequestOptions) => request<T>('DELETE', path, opts),
};

/** Trigger a browser download of a blob (e.g. export files). */
export function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename; document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
