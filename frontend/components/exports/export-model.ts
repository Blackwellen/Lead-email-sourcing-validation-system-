import { z } from 'zod';
import type { Schema } from '@/lib/api';

export type ExportRow = Schema<'ExportOut'>;
export type PresetInfo = Schema<'PresetInfo'>;

export type Uniqueness = 'email' | 'organisation' | 'domain';
export type ContactType = 'both' | 'named' | 'role';

/**
 * Mirror of the preset defaults in backend/app/exports.py (PRESETS[*]["defaults"]) for the fields the form shows.
 * The form always sends these four explicitly so that what you see in the builder is exactly what is exported.
 * Other preset defaults (free_provider, has_job_title, marketing_eligible_only) are applied by the server.
 */
export interface PresetDefaults { published_only: boolean; uniqueness: Uniqueness; exclude_suppressed: boolean; contact_type: ContactType }
const BASE: PresetDefaults = { published_only: false, uniqueness: 'email', exclude_suppressed: false, contact_type: 'both' };
export const PRESET_DEFAULTS: Record<string, PresetDefaults> = {
  full_intelligence: BASE,
  published_business_emails: { ...BASE, published_only: true, exclude_suppressed: true },
  named_decision_makers: { ...BASE, contact_type: 'named', exclude_suppressed: true },
  role_company_emails: { ...BASE, contact_type: 'role', exclude_suppressed: true },
  one_per_company: { ...BASE, uniqueness: 'organisation', exclude_suppressed: true },
  one_per_domain: { ...BASE, uniqueness: 'domain', exclude_suppressed: true },
  listmonk_ready: { ...BASE, exclude_suppressed: true },
  validation_audit: BASE,
};
/** What the server adds on top of the visible toggles for each preset. */
export const PRESET_EXTRAS: Record<string, string> = {
  published_business_emails: 'Also excludes free-mail providers (gmail.com etc.).',
  named_decision_makers: 'Also requires a recorded job title.',
  role_company_emails: 'Also excludes free-mail providers.',
  listmonk_ready: 'Server-enforced: only marketing-eligible, non-suppressed addresses are included, always as CSV.',
  validation_audit: 'Adds audit columns (origin, syntax status, evidence, bounce flags, confidence breakdown).',
};

export const REACHABILITY = [
  { value: 'safe', label: 'Safe (no adverse evidence)' },
  { value: 'risky', label: 'Risky' },
  { value: 'unknown', label: 'Unknown' },
  { value: 'invalid', label: 'Invalid' },
];

const optNum = (label: string, min: number, max: number, int = false) =>
  z.string().trim().refine((v) => v === '' || (!Number.isNaN(Number(v)) && Number(v) >= min && Number(v) <= max && (!int || Number.isInteger(Number(v)))), `${label} must be ${int ? 'a whole number' : 'a number'} from ${min} to ${max}`);

export const builderSchema = z.object({
  preset: z.string().min(1),
  format: z.enum(['csv', 'xlsx', 'jsonl']),
  category_id: z.string(),
  country: z.array(z.string()),
  region: z.array(z.string()),
  industry: z.string().max(200, 'Industry is limited to 200 characters'),
  reachability: z.array(z.string()),
  min_confidence: optNum('Minimum confidence', 0, 100),
  published_only: z.boolean(),
  uniqueness: z.enum(['email', 'organisation', 'domain']),
  contact_type: z.enum(['both', 'named', 'role']),
  exclude_catch_all: z.boolean(),
  exclude_unknown: z.boolean(),
  exclude_suppressed: z.boolean(),
  freshness_days: optNum('Freshness', 1, 3650, true),
  source: z.array(z.string()),
});
export type BuilderValues = z.infer<typeof builderSchema>;

export function defaultValues(preset: string): BuilderValues {
  return {
    preset, format: 'csv', category_id: '', country: [], region: [], industry: '', reachability: [], min_confidence: '',
    ...(PRESET_DEFAULTS[preset] ?? BASE), exclude_catch_all: false, exclude_unknown: false, freshness_days: '', source: [],
  };
}

export interface ExportBody { preset: string; format: 'csv' | 'xlsx' | 'jsonl'; filters: Record<string, unknown>; wait?: boolean | null }

export function toBody(v: BuilderValues): ExportBody {
  const f: Record<string, unknown> = {
    published_only: v.published_only, uniqueness: v.uniqueness, contact_type: v.contact_type, exclude_suppressed: v.preset === 'listmonk_ready' ? true : v.exclude_suppressed,
    exclude_catch_all: v.exclude_catch_all, exclude_unknown: v.exclude_unknown,
  };
  if (v.category_id) f.category_id = [Number(v.category_id)];
  if (v.country.length) f.country = v.country;
  if (v.region.length) f.region = v.region;
  if (v.industry.trim()) f.industry = v.industry.trim();
  if (v.reachability.length) f.reachability = v.reachability;
  if (v.min_confidence.trim() !== '') f.min_confidence = Number(v.min_confidence);
  if (v.freshness_days.trim() !== '') f.freshness_days = Number(v.freshness_days);
  if (v.source.length) f.source = v.source;
  return { preset: v.preset, format: v.preset === 'listmonk_ready' ? 'csv' : v.format, filters: f };
}

const LABELS: Record<string, string> = {
  category_id: 'Category', country: 'Country', region: 'Region', industry: 'Industry', reachability: 'Verification', min_confidence: 'Min confidence', max_confidence: 'Max confidence',
  published_only: 'Published only', uniqueness: 'Uniqueness', contact_type: 'Contact type', exclude_catch_all: 'Exclude catch-all', exclude_unknown: 'Exclude unknown', exclude_suppressed: 'Exclude suppressed',
  freshness_days: 'Validated within (days)', source: 'Sources', marketing_eligible_only: 'Marketing-eligible only', has_job_title: 'Has job title', free_provider: 'Free provider', q: 'Search', city: 'City',
};
/** Human readable [label, value] pairs from the stored filters object (defaults are omitted by the server). */
export function summariseFilters(filters: Record<string, unknown> | null | undefined): [string, string][] {
  const out: [string, string][] = [];
  for (const [k, v] of Object.entries(filters ?? {})) {
    if (k.startsWith('_')) continue;
    if (v === null || v === undefined || v === '' || (Array.isArray(v) && v.length === 0)) continue;
    const val = typeof v === 'boolean' ? (v ? 'yes' : 'no') : Array.isArray(v) ? v.join(', ') : typeof v === 'object' ? JSON.stringify(v) : String(v);
    out.push([LABELS[k] ?? k.replaceAll('_', ' '), val]);
  }
  return out;
}
