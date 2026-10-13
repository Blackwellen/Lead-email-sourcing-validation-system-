import type { Schema } from '@/lib/api';
import type { BadgeVariant } from '@/components/ui/badge';

export type ExplorerItem = Schema<'ExplorerItem'>;
export type ExplorerDetail = Schema<'ExplorerDetail'>;

export const RESTRICTED_LABEL = 'Restricted — manual import or written permission only';

export const ACCESS_LABEL: Record<string, string> = {
  bulk_file: 'Bulk file', api: 'API', manual_import: 'Manual import', restricted: 'Restricted',
};
export const COST_LABEL: Record<string, string> = { free: 'Free', free_registration: 'Free (registration)', paid: 'Paid' };
export const CREDENTIALS_LABEL: Record<string, string> = { none: 'No credentials', optional: 'Credentials optional', required: 'Credentials required' };

/** Human labels for `catalogue_full.SCORE_WEIGHTS` keys (the API returns the raw breakdown). */
export const SCORE_LABEL: Record<string, string> = {
  country_exact: 'Covers the chosen country',
  country_worldwide: 'Worldwide coverage',
  industry_exact: 'Serves this business type',
  industry_partial: 'Partly matches business type',
  industry_generic: 'All industries',
  q_name_token: 'Search words in name',
  q_id_token: 'Search words in id',
  q_industry_token: 'Search words in industries',
  q_text_token: 'Search words in description',
  automated_allowed: 'Automated access permitted',
  configured: 'Already configured',
  live_verified: 'Live-verified',
  restricted: 'Restricted terms (penalty)',
};

/** Automated-access permission status shown on every result. */
export function automationStatus(s: Pick<ExplorerItem, 'automated_allowed' | 'info_only' | 'adapter_id'>): { label: string; variant: BadgeVariant; restricted: boolean } {
  if (!s.automated_allowed) return { label: RESTRICTED_LABEL, variant: 'danger', restricted: true };
  if (s.info_only || !s.adapter_id) return { label: 'Catalogue only — no automated adapter', variant: 'neutral', restricted: false };
  return { label: 'Automated access permitted', variant: 'success', restricted: false };
}

export const SECRET_KEY_RE = /(key|secret|token|password|passwd|credential)/i;
export const CONFIG_KEY_RE = /^[a-z][a-z0-9_]{0,47}$/;
export const MASK = '********';
const PERMISSION_KEYS = new Set(['permission_basis', 'permission_note']);

/**
 * The API exposes no machine-readable config schema, so suggested keys are derived from the requirements text
 * ('quoted' names and snake_case identifiers) plus keys already set. Users can add any other valid key.
 */
export function suggestedKeys(d: ExplorerDetail): string[] {
  const text = d.requirements.access_requirements ?? '';
  const out = new Set<string>();
  for (const m of text.matchAll(/'([a-z][a-z0-9_]{0,47})'/g)) out.add(m[1]);
  for (const m of text.matchAll(/\b([a-z][a-z0-9]*_[a-z0-9_]+)\b/g)) out.add(m[1]);
  for (const k of d.requirements.config_keys_set) out.add(k);
  for (const k of d.requirements.secret_keys_set) out.add(k);
  for (const k of PERMISSION_KEYS) out.delete(k);
  return [...out].filter((k) => CONFIG_KEY_RE.test(k));
}

/** "true"/"false" -> boolean, plain numbers -> number, everything else stays a string. */
export function parseConfigValue(v: string): string | number | boolean {
  const t = v.trim();
  if (t === 'true') return true;
  if (t === 'false') return false;
  if (/^-?\d+(\.\d+)?$/.test(t) && t.length < 16) return Number(t);
  return t;
}

export const permissionLabel: Record<string, { label: string; variant: BadgeVariant }> = {
  not_automatable: { label: 'Not automatable', variant: 'neutral' },
  restricted: { label: 'Restricted', variant: 'danger' },
  permission_declared: { label: 'Permission declared', variant: 'success' },
  open_licence: { label: 'Open licence', variant: 'success' },
  permission_needed: { label: 'Permission needed', variant: 'warning' },
};
export const BASIS_LABEL: Record<string, string> = {
  open_licence: 'Open licence', licence_agreement: 'Licence agreement', api_terms: 'API terms of service', written_permission: 'Written permission', public_domain: 'Public domain',
};
