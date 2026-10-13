import { z } from 'zod';

/* ------------------------------------------------------------------ vocab */
export const TARGET_DEFINITIONS = [
  { value: 'distinct_published_emails', label: 'Distinct published emails', hint: 'Unique email addresses published on the business’s own pages.' },
  { value: 'validated_contacts', label: 'Validated contacts', hint: 'Emails whose confidence is at least the minimum you set below.' },
  { value: 'campaign_eligible_emails', label: 'Campaign-eligible emails', hint: 'Qualified emails whose marketing status is “eligible”.' },
  { value: 'business_email_pairs', label: 'Business + email pairs', hint: 'Each distinct (business, email) pair counts once.' },
  { value: 'distinct_email_domains', label: 'Distinct email domains', hint: 'One count per registrable email domain.' },
  { value: 'distinct_companies', label: 'Distinct companies', hint: 'Counts discovered businesses, whether or not they have an email.' },
  { value: 'companies_with_domains', label: 'Companies with verified websites', hint: 'Counts businesses whose website match is confirmed or high.' },
] as const;

export const UNIQUENESS_MODES = [
  { value: 'email', label: 'Unique emails globally', hint: 'Each address counts once, however many businesses publish it. No per-company cap.' },
  { value: 'organisation', label: 'One email per business', hint: 'At most one qualifying address per business.' },
  { value: 'domain', label: 'One email per domain', hint: 'At most one qualifying address per registrable domain.' },
  { value: 'multi', label: 'Several per company, with a maximum', hint: 'Unique emails, capped per company at the number you choose.' },
] as const;

export const SOURCE_CATEGORY_OPTIONS = [
  { value: 'bulk_open_data', label: 'Bulk open data', hint: 'Government and open-data snapshots processed in one pass.' },
  { value: 'industry_registers', label: 'Industry registers', hint: 'Professional and trade registers that permit automated collection.' },
  { value: 'licensed_directories', label: 'Licensed directories', hint: 'Directories you hold a licence or API access for.' },
  { value: 'procurement', label: 'Procurement notices', hint: 'Public contract awards and tenders.' },
  { value: 'company_websites', label: 'Company-owned websites', hint: 'Crawl each business’s own site politely.' },
  { value: 'common_crawl', label: 'Common Crawl', hint: 'Look up pages in the public web archive.' },
  { value: 'documents', label: 'Public documents', hint: 'PDFs and other public documents on company sites.' },
  { value: 'network_expansion', label: 'Business network expansion', hint: 'Follow partner, member and supplier links from known businesses.' },
] as const;

export const SIGNAL_TYPES = ['new_location', 'new_service', 'expansion', 'hiring', 'contract_award', 'supplier_partnership', 'event', 'accreditation', 'new_registration', 'exhibitor', 'sponsor', 'registry_change'] as const;

export const VERIFY_STAGES = [
  { key: 'syntax', label: 'Syntax and normalisation', hint: 'Instant, local CPU only.' },
  { key: 'dns', label: 'DNS / MX lookup', hint: 'Domain mail records; cached per domain.' },
  { key: 'flags', label: 'Disposable / free / role flags', hint: 'Local list checks.' },
  { key: 'provider', label: 'Mail provider classification', hint: 'Records the MX provider.' },
  { key: 'dns_diagnostics', label: 'SPF / DMARC presence', hint: 'Never affects whether an address exists.' },
  { key: 'catch_all', label: 'Catch-all detection', hint: 'Requires SMTP diagnostics enabled for authorised domains.' },
  { key: 'smtp', label: 'SMTP recipient diagnostics', hint: 'Requires SMTP diagnostics enabled; port 25 is often blocked.' },
  { key: 'delivery', label: 'Delivery history', hint: 'Uses imported feedback only.' },
] as const;
export type VerifyStageKey = (typeof VERIFY_STAGES)[number]['key'];

/* ------------------------------------------------------------------ domains */
const HOST = /^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/;
const HEADER_TOKENS = new Set(['domain', 'domains', 'website', 'websites', 'url', 'urls', 'site', 'host', 'hostname']);

export interface ParsedDomains { valid: string[]; invalid: { line: number; value: string }[]; duplicates: number }

/** Accepts one domain per line or comma/semicolon/space separated; strips scheme, `www.`, paths; flags what is not a domain. */
export function parseDomains(text: string): ParsedDomains {
  const seen = new Set<string>();
  const invalid: ParsedDomains['invalid'] = [];
  let duplicates = 0;
  text.split(/\r?\n/).forEach((line, idx) => {
    for (const raw of line.split(/[\s,;]+/)) {
      let t = raw.trim().replace(/^["']|["']$/g, '');
      if (!t) continue;
      if (idx === 0 && HEADER_TOKENS.has(t.toLowerCase())) continue;
      t = t.toLowerCase().replace(/^[a-z][a-z0-9+.-]*:\/\//, '').replace(/^www\./, '').replace(/[/?#].*$/, '').replace(/\.$/, '');
      if (!HOST.test(t)) { invalid.push({ line: idx + 1, value: raw.slice(0, 60) }); continue; }
      if (seen.has(t)) { duplicates += 1; continue; }
      seen.add(t);
    }
  });
  return { valid: [...seen], invalid, duplicates };
}

/* ------------------------------------------------------------------ schema */
const intOrNull = (min: number, max: number, msg?: string) =>
  z.number({ error: 'Enter a number' }).int('Whole numbers only').min(min, msg ?? `Must be at least ${min}`).max(max, `Must be at most ${max}`).nullable();
const intReq = (min: number, max: number) =>
  z.number({ error: 'Enter a number' }).int('Whole numbers only').min(min, `Must be at least ${min}`).max(max, `Must be at most ${max}`);

export const wizardSchema = z.object({
  name: z.string().trim().min(1, 'Give the run a name').max(200, 'Use 200 characters or fewer'),
  destination_category_id: z.number().nullable(),
  countries: z.array(z.string()).min(1, 'Choose at least one country'),
  regions: z.array(z.string()),
  localities: z.array(z.string()),
  postcodes: z.array(z.string()),
  category_ids: z.array(z.number()),
  keywords: z.array(z.string()),
  exclusions: z.array(z.string()),
  active_only: z.boolean(),
  refresh_existing: z.boolean(),
  sources: z.array(z.string()),
  source_params: z.record(z.string(), z.record(z.string(), z.unknown())),
  imported_domains_text: z.string(),
  crawl_max_pages: intReq(1, 100),
  target: z.object({
    definition: z.string(),
    count: intReq(1, 50_000_000),
    uniqueness_mode: z.enum(['email', 'organisation', 'domain', 'multi']),
    max_contacts_per_company: intReq(1, 1000),
    contact_type: z.enum(['named', 'role', 'both']),
    published_only: z.boolean(),
    allow_inferred: z.boolean(),
    min_confidence: intReq(0, 100),
    exclude_free_providers: z.boolean(),
    exclude_catchall_or_inconclusive: z.boolean(),
    exclude_previous_bounces: z.boolean(),
    exclude_suppressed: z.boolean(),
    max_runtime_minutes: intOrNull(1, 10_000_000),
    max_work_jobs: intOrNull(1, 1_000_000_000),
  }),
  verification: z.object({
    mode: z.enum(['fast', 'deep', 'custom']),
    custom: z.record(z.string(), z.boolean()),
  }),
  performance: z.object({
    preset: z.enum(['conservative', 'balanced', 'advanced']),
    batch_size: z.number(),
    http_concurrency: intOrNull(1, 2000),
    per_host_concurrency: intReq(1, 16),
    dns_concurrency: intOrNull(1, 2000),
    worker_limit: intOrNull(1, 512),
    browser_workers: intReq(0, 8),
    max_ram_mb: intOrNull(128, 100_000_000),
    desired_websites_per_hour: intOrNull(1, 1_000_000_000),
    desired_businesses_per_hour: intOrNull(1, 1_000_000_000),
    desired_records_per_hour: intOrNull(1, 1_000_000_000),
    max_requests_per_host_per_min: intReq(1, 100_000),
    max_daily_operations: intOrNull(1, 1_000_000_000),
    retry_max_attempts: intReq(1, 10),
    resume_existing: z.boolean(),
  }),
  schedule_at: z.string(),
  discovery: z.object({
    strategy: z.enum(['fast', 'balanced', 'deep', 'custom']),
    source_categories: z.array(z.string()),
    contact_discovery: z.enum(['published_only', 'published_plus_corroborated', 'include_inferred']),
    business_scope: z.enum(['new_only', 'refresh_existing', 'both']),
    network: z.object({ enabled: z.boolean(), max_depth: intReq(1, 3), source_budget: intReq(1, 100_000) }),
    signals: z.object({ enabled: z.boolean(), types: z.array(z.string()), freshness_days: intReq(1, 3650) }),
    quota_rows: z.array(z.object({ key: z.string().trim().min(1, 'Name the source or category'), value: intReq(0, 1_000_000_000) })),
    overall_max_work: intOrNull(1, 1_000_000_000),
    fallback_research: z.enum(['stop_if_no_website', 'try_other_sources', 'flag_for_review']),
    search_budget: intReq(0, 50),
  }),
}).superRefine((v, ctx) => {
  const parsed = parseDomains(v.imported_domains_text);
  if (parsed.invalid.length) {
    const first = parsed.invalid[0];
    ctx.addIssue({ code: 'custom', path: ['imported_domains_text'], message: `${parsed.invalid.length} entr${parsed.invalid.length === 1 ? 'y is' : 'ies are'} not valid domains (first: “${first.value}” on line ${first.line}). Use e.g. example.com.` });
  }
  if (v.sources.length === 0 && parsed.valid.length === 0) {
    ctx.addIssue({ code: 'custom', path: ['sources'], message: 'Choose at least one source or import a list of domains.' });
  }
  if (v.schedule_at) {
    const t = new Date(v.schedule_at).getTime();
    if (Number.isNaN(t)) ctx.addIssue({ code: 'custom', path: ['schedule_at'], message: 'Enter a valid date and time.' });
    else if (t <= Date.now()) ctx.addIssue({ code: 'custom', path: ['schedule_at'], message: 'The scheduled time must be in the future.' });
  }
  if (v.target.uniqueness_mode === 'multi' && v.target.max_contacts_per_company < 1) {
    ctx.addIssue({ code: 'custom', path: ['target', 'max_contacts_per_company'], message: 'Choose a maximum of at least 1.' });
  }
});

export type WizardValues = z.infer<typeof wizardSchema>;

export const DEFAULT_VALUES: WizardValues = {
  name: '',
  destination_category_id: null,
  countries: ['GB'],
  regions: [],
  localities: [],
  postcodes: [],
  category_ids: [],
  keywords: [],
  exclusions: [],
  active_only: true,
  refresh_existing: false,
  sources: [],
  source_params: {},
  imported_domains_text: '',
  crawl_max_pages: 12,
  target: {
    definition: 'distinct_published_emails', count: 100, uniqueness_mode: 'email', max_contacts_per_company: 5, contact_type: 'both',
    published_only: true, allow_inferred: false, min_confidence: 40, exclude_free_providers: true, exclude_catchall_or_inconclusive: false,
    exclude_previous_bounces: true, exclude_suppressed: true, max_runtime_minutes: null, max_work_jobs: null,
  },
  verification: { mode: 'fast', custom: { syntax: true, dns: true, flags: true, provider: false, dns_diagnostics: false, catch_all: false, smtp: false, delivery: false } },
  performance: {
    preset: 'conservative', batch_size: 250, http_concurrency: null, per_host_concurrency: 1, dns_concurrency: null, worker_limit: null, browser_workers: 0,
    max_ram_mb: null, desired_websites_per_hour: null, desired_businesses_per_hour: null, desired_records_per_hour: null,
    max_requests_per_host_per_min: 30, max_daily_operations: null, retry_max_attempts: 4, resume_existing: true,
  },
  schedule_at: '',
  discovery: {
    strategy: 'balanced', source_categories: ['bulk_open_data', 'industry_registers', 'company_websites'], contact_discovery: 'published_only', business_scope: 'new_only',
    network: { enabled: false, max_depth: 1, source_budget: 50 }, signals: { enabled: false, types: [], freshness_days: 180 },
    quota_rows: [], overall_max_work: null, fallback_research: 'stop_if_no_website', search_budget: 0,
  },
};

/** Fields validated when leaving each step (the review step validates everything). */
export const STEP_FIELDS: (readonly string[])[] = [
  ['name', 'destination_category_id', 'countries', 'regions', 'localities', 'postcodes', 'category_ids', 'keywords', 'exclusions'],
  ['sources', 'imported_domains_text', 'source_params', 'crawl_max_pages'],
  ['target'],
  ['verification', 'performance', 'schedule_at', 'discovery'],
  [],
];
export const STEP_TITLES = ['What businesses', 'Where should we look', 'What contacts', 'Validation and speed', 'Review and start'] as const;

/** Server field path prefix -> step index (for routing server validation errors to the right step). */
export function stepOfField(field: string): number {
  const head = field.split('.')[0];
  const i = STEP_FIELDS.findIndex((fs, idx) => idx < 4 && (fs.includes(head) || (head === 'imported_domains' && idx === 1)));
  return i === -1 ? 4 : i;
}

/** Map a server field path to the wizard form field path. */
export function serverFieldToForm(field: string): string {
  if (field.startsWith('imported_domains')) return 'imported_domains_text';
  if (field.startsWith('discovery.source_quotas')) return 'discovery.quota_rows';
  if (field === 'target.uniqueness') return 'target.uniqueness_mode';
  return field;
}

/* ------------------------------------------------------------------ conversions */
export function toLocalInput(iso: string | null | undefined): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** Form values -> RunConfig payload (what /api/runs/validate, /estimate and POST /api/runs accept). */
export function toPayload(v: WizardValues): Record<string, unknown> {
  const { uniqueness_mode, ...target } = v.target;
  const uniq = uniqueness_mode === 'multi' ? 'email' : uniqueness_mode;
  const maxPer = uniqueness_mode === 'email' ? 1000 : uniqueness_mode === 'multi' ? v.target.max_contacts_per_company : v.target.max_contacts_per_company;
  const { quota_rows, ...discovery } = v.discovery;
  const parsed = parseDomains(v.imported_domains_text);
  const params: Record<string, Record<string, unknown>> = {};
  for (const [id, p] of Object.entries(v.source_params)) {
    if (!v.sources.includes(id)) continue;
    const clean = Object.fromEntries(Object.entries(p).filter(([, val]) => val !== '' && val !== null && val !== undefined));
    if (Object.keys(clean).length) params[id] = clean;
  }
  return {
    name: v.name.trim(),
    destination_category_id: v.destination_category_id,
    countries: v.countries,
    regions: v.regions,
    localities: v.localities,
    postcodes: v.postcodes,
    category_ids: v.category_ids,
    keywords: v.keywords,
    exclusions: v.exclusions,
    active_only: v.active_only,
    refresh_existing: v.refresh_existing,
    sources: v.sources,
    source_params: params,
    imported_domains: parsed.valid,
    target: { ...target, uniqueness: uniq, max_contacts_per_company: maxPer },
    verification: { mode: v.verification.mode, custom: v.verification.mode === 'custom' ? v.verification.custom : {} },
    performance: v.performance,
    schedule_at: v.schedule_at ? new Date(v.schedule_at).toISOString() : null,
    crawl_max_pages: v.crawl_max_pages,
    discovery: {
      ...discovery,
      source_quotas: Object.fromEntries(quota_rows.filter((r) => r.key.trim()).map((r) => [r.key.trim(), r.value])),
    },
  };
}

type Obj = Record<string, unknown>;
const isObj = (x: unknown): x is Obj => !!x && typeof x === 'object' && !Array.isArray(x);
function merge<T>(base: T, over: unknown): T {
  if (!isObj(base) || !isObj(over)) return (over === undefined ? base : (over as T));
  const out: Obj = { ...base };
  for (const [k, val] of Object.entries(over)) out[k] = k in base ? merge((base as Obj)[k], val) : val;
  return out as T;
}

/** Stored RunConfig (possibly partial / from a preset) -> form values. */
export function fromConfig(cfg: unknown): WizardValues {
  const c = isObj(cfg) ? cfg : {};
  const base = structuredClone(DEFAULT_VALUES);
  const { imported_domains, schedule_at, target, discovery, ...rest } = c as Obj;
  const v = merge(base, rest) as WizardValues;
  if (Array.isArray(imported_domains)) v.imported_domains_text = imported_domains.join('\n');
  if (typeof schedule_at === 'string') v.schedule_at = toLocalInput(schedule_at);
  if (isObj(target)) {
    const { uniqueness, ...t } = target;
    v.target = merge(v.target, t);
    const maxPer = Number(t.max_contacts_per_company ?? v.target.max_contacts_per_company);
    v.target.uniqueness_mode = uniqueness === 'organisation' ? 'organisation' : uniqueness === 'domain' ? 'domain' : (maxPer >= 1000 ? 'email' : 'multi');
    if (uniqueness === undefined) v.target.uniqueness_mode = 'email';
  }
  if (isObj(discovery)) {
    const { source_quotas, ...d } = discovery;
    v.discovery = merge(v.discovery, d);
    if (isObj(source_quotas)) v.discovery.quota_rows = Object.entries(source_quotas).map(([key, value]) => ({ key, value: Number(value) }));
  }
  return v;
}
