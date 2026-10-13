import type { Schema } from '@/lib/api';

export type Source = Schema<'SourceOut'>;

export type AccessKind = 'manual_only' | 'catalogue' | 'needs_config' | 'enabled' | 'disabled' | 'unregistered';

export interface AccessInfo { kind: AccessKind; label: string; variant: 'danger' | 'neutral' | 'warning' | 'success' | 'info'; explain: string }

/** Single place that turns catalogue flags + DB state into the status shown in the table and drawer. */
export function accessOf(s: Source): AccessInfo {
  if (!s.registered) return { kind: 'unregistered', label: 'Not registered', variant: 'neutral', explain: 'This source has stored state or data but its adapter is not registered in this deployment.' };
  if (s.automated_collection_permitted === false) {
    return { kind: 'manual_only', label: 'Manual import only', variant: 'danger', explain: 'Automated collection not permitted by this source’s terms. It can only be used by importing a file you are entitled to use (Sources → Upload file); it cannot be enabled for automated runs.' };
  }
  if (s.info_only) return { kind: 'catalogue', label: 'Catalogue only', variant: 'neutral', explain: 'Catalogue entry only. It describes a source but yields no records here.' };
  if (s.requires_config && !s.configured) return { kind: 'needs_config', label: 'Needs configuration', variant: 'warning', explain: 'This source needs configuration (for example an uploaded file path or credentials) before runs can use it.' };
  if (s.enabled) return { kind: 'enabled', label: 'Enabled', variant: 'success', explain: 'Runs may use this source.' };
  return { kind: 'disabled', label: 'Disabled', variant: 'neutral', explain: 'Runs will not use this source until an administrator enables it.' };
}

export function canToggle(s: Source) {
  return s.registered && s.automated_collection_permitted !== false && !s.info_only;
}

export function successRate(s: Source): number | null {
  const total = s.runs_ok + s.runs_failed;
  return total ? s.runs_ok / total : null;
}

/** Keys the access-requirements text documents, e.g. "Config: 'path' (+ optional 'country_code')". */
export function documentedKeys(s: Source): string[] {
  const keys = new Set<string>();
  for (const m of (s.access_requirements ?? '').matchAll(/'([a-z_][a-z0-9_]*)'/g)) keys.add(m[1]);
  if (s.adapter_id === 'csv_import') ['max_bytes', 'max_rows', 'max_cols'].forEach((k) => keys.add(k));
  return [...keys].sort();
}

export const MASK = '********';

export interface ConfigCheck { value: Record<string, unknown> | null; errors: string[]; warnings: string[] }

/** Client-side validation before PUT /config. The server remains the authority (path keys must reference uploaded files). */
export function checkConfig(text: string, s: Source): ConfigCheck {
  const errors: string[] = [];
  const warnings: string[] = [];
  let parsed: unknown;
  try { parsed = JSON.parse(text || '{}'); } catch (e) {
    return { value: null, errors: [`Not valid JSON: ${(e as Error).message}`], warnings };
  }
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) return { value: null, errors: ['The configuration must be a JSON object, for example {"path": "…"}.'], warnings };
  const obj = parsed as Record<string, unknown>;
  const documented = new Set(documentedKeys(s));
  const existing = new Set(Object.keys(s.config ?? {}));
  const secretRe = /(key|secret|token|password|passwd|credential|guid)/i;
  for (const [k, v] of Object.entries(obj)) {
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(k)) errors.push(`Key “${k}” is not a valid name (letters, digits and underscores only).`);
    else if (documented.size > 0 && !documented.has(k) && !existing.has(k)) errors.push(`Key “${k}” is not a permitted setting for this source. Permitted: ${[...documented].join(', ')}.`);
    if (typeof v === 'string' && v.length > 2000) errors.push(`Value of “${k}” is too long.`);
    if (secretRe.test(k) && typeof v === 'string' && v !== MASK && v !== '') warnings.push(`“${k}” looks like a secret. It will be masked (********) in this view afterwards; leave the mask in place to keep the stored value.`);
  }
  if (documented.size === 0 && Object.keys(obj).length > 0) warnings.push('This source documents no settings, so keys cannot be checked here; the server will validate them.');
  if (JSON.stringify(obj).length > 32 * 1024) errors.push('Configuration is limited to 32 KB.');
  return { value: errors.length ? null : obj, errors, warnings };
}
