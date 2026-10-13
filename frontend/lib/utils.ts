import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function sleep(ms: number) {
  return new Promise((r) => setTimeout(r, ms));
}

/**
 * Only allow same-origin relative redirects (`?next=`). Returns pathname + search + hash of the resolved URL, or `fallback`.
 * Rejects: anything not starting with a single '/', protocol-relative `//host`, ANY backslash (browsers treat `/\host` as
 * `//host`), control characters/whitespace (`/\t/host` is stripped to `//host` by the URL parser), and /login loops. The value is
 * then resolved against the current origin and must stay on it.
 */
export function safeNext(next: string | null | undefined, fallback = '/dashboard') {
  if (!next || typeof next !== 'string' || next.length > 2048) return fallback;
  if (!next.startsWith('/') || next.startsWith('//') || /[\\\u0000-\u0020\u007f-\u009f]/.test(next)) return fallback;
  const origin = typeof window !== 'undefined' && window.location?.origin && window.location.origin !== 'null' ? window.location.origin : 'http://localhost';
  let url: URL;
  try { url = new URL(next, origin); } catch { return fallback; }
  if (url.origin !== origin || (url.protocol !== 'http:' && url.protocol !== 'https:')) return fallback;
  if (url.pathname === '/login' || url.pathname.startsWith('/login/')) return fallback;
  return url.pathname + url.search + url.hash;
}
