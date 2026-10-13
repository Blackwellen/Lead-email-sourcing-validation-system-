import * as React from 'react';
import {
  Archive, AtSign, Ban, CheckCheck, CircleCheck, CircleHelp, CircleX, Clock, FileText, HelpCircle, Layers, ListChecks,
  Loader2, Mail, MailX, Pause, Pencil, Play, ShieldAlert, ShieldCheck, ShieldQuestion, Sparkles, Square, TriangleAlert, UserRoundSearch, type LucideIcon,
} from 'lucide-react';
import { Badge, type BadgeVariant } from '@/components/ui/badge';
import { titleCase } from '@/lib/format';
import { cn } from '@/lib/utils';

export type StatusKind = 'run' | 'job' | 'reachability' | 'domain' | 'origin' | 'marketing' | 'mail' | 'catchall' | 'syntax' | 'health';

interface Meta { label: string; variant: BadgeVariant; Icon: LucideIcon; description?: string; spin?: boolean }
const m = (label: string, variant: BadgeVariant, Icon: LucideIcon, description?: string, spin?: boolean): Meta => ({ label, variant, Icon, description, spin });

/**
 * Status vocabulary. Every status has a distinct label AND icon in addition to colour.
 * Syntax validity is deliberately neutral with no check mark: a well-formed address is not a verified one.
 */
const MAP: Record<StatusKind, Record<string, Meta>> = {
  run: {
    draft: m('Draft', 'neutral', Pencil, 'Not started yet'),
    scheduled: m('Scheduled', 'info', Clock),
    queued: m('Queued', 'info', Clock),
    running: m('Running', 'info', Play, 'In progress', true),
    paused: m('Paused', 'warning', Pause),
    stopping: m('Stopping', 'warning', Square),
    completed: m('Completed', 'success', CircleCheck),
    failed: m('Failed', 'danger', CircleX),
    cancelled: m('Cancelled', 'neutral', Ban),
    stopped: m('Stopped', 'neutral', Square),
    archived: m('Archived', 'neutral', Archive),
  },
  job: {
    queued: m('Queued', 'info', Clock),
    running: m('Running', 'info', Play, undefined, true),
    paused: m('Paused', 'warning', Pause),
    done: m('Done', 'success', CircleCheck),
    failed: m('Failed', 'danger', CircleX),
    cancelled: m('Cancelled', 'neutral', Ban),
  },
  reachability: {
    safe: m('Safe', 'success', ShieldCheck, 'No adverse evidence found for a published or corroborated address'),
    risky: m('Risky', 'warning', ShieldAlert, 'Some adverse or uncertain evidence'),
    invalid: m('Invalid', 'danger', MailX, 'Evidence the address will not accept mail'),
    unknown: m('Unknown', 'neutral', ShieldQuestion, 'Not enough evidence either way'),
  },
  domain: {
    confirmed: m('Confirmed', 'success', CircleCheck, 'Domain verified as belonging to the business'),
    high: m('High match', 'success', CircleCheck),
    medium: m('Medium match', 'info', CircleHelp),
    probable: m('Probable', 'info', CircleHelp),
    low: m('Low match', 'warning', TriangleAlert),
    mismatch: m('Mismatch', 'danger', CircleX),
    rejected: m('Rejected', 'danger', CircleX),
    parked: m('Parked', 'warning', Pause),
    unknown: m('Unverified', 'neutral', CircleHelp),
  },
  origin: {
    published: m('Published', 'success', FileText, 'Found published on the business’s own pages'),
    corroborated: m('Corroborated', 'success', Layers, 'Published and confirmed by a second source'),
    pattern_inferred: m('Pattern inferred', 'info', Sparkles, 'Generated from the domain’s observed address pattern'),
    role_speculative: m('Role speculative', 'warning', UserRoundSearch, 'Guessed role mailbox (e.g. info@) with no direct evidence'),
  },
  marketing: {
    eligible: m('Eligible', 'success', ListChecks),
    needs_review: m('Needs review', 'warning', TriangleAlert),
    suppressed: m('Suppressed', 'danger', Ban),
    opted_out: m('Opted out', 'danger', Ban),
    ineligible: m('Ineligible', 'neutral', Ban),
  },
  mail: {
    accepts_mail: m('Accepts mail', 'success', Mail, 'MX or address records found'),
    no_mail_service: m('No mail service', 'danger', MailX),
    null_mx: m('Null MX', 'danger', MailX, 'Domain explicitly refuses mail'),
    temporary_dns_failure: m('DNS failure', 'warning', TriangleAlert, 'Temporary DNS failure'),
    unknown: m('Unknown', 'neutral', HelpCircle),
  },
  catchall: {
    confirmed: m('Catch-all', 'warning', CheckCheck, 'Server accepts any recipient'),
    suspected: m('Catch-all suspected', 'warning', TriangleAlert),
    rejected_unknown_recipient: m('Rejects unknown', 'success', Mail, 'Server rejects made-up recipients'),
    unknown: m('Unknown', 'neutral', HelpCircle),
    untested: m('Untested', 'neutral', HelpCircle),
  },
  syntax: {
    valid: m('Syntax OK', 'neutral', AtSign, 'Well-formed address. This does NOT mean the mailbox exists.'),
    valid_syntax: m('Syntax OK', 'neutral', AtSign, 'Well-formed address. This does NOT mean the mailbox exists.'),
    invalid: m('Bad syntax', 'danger', CircleX),
    unknown: m('Unchecked', 'neutral', HelpCircle),
  },
  health: {
    healthy: m('Healthy', 'success', CircleCheck),
    ok: m('OK', 'success', CircleCheck),
    degraded: m('Degraded', 'warning', TriangleAlert),
    down: m('Down', 'danger', CircleX),
    disabled: m('Disabled', 'neutral', Ban),
    unknown: m('Unknown', 'neutral', HelpCircle),
  },
};

export function statusMeta(kind: StatusKind, status: string | null | undefined): Meta {
  const key = (status ?? 'unknown').toLowerCase();
  return MAP[kind][key] ?? m(titleCase(key), 'neutral', CircleHelp);
}

export function StatusBadge({ kind, status, className, iconOnly, label }: {
  kind: StatusKind;
  status: string | null | undefined;
  className?: string;
  /** Hide the text visually (kept for screen readers) – use only in very dense cells. */
  iconOnly?: boolean;
  /** Override the displayed label. */
  label?: string;
}) {
  const meta = statusMeta(kind, status);
  const text = label ?? meta.label;
  return (
    <Badge variant={meta.variant} className={className} title={meta.description ?? text}>
      {meta.spin ? <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden /> : <meta.Icon aria-hidden />}
      <span className={cn(iconOnly && 'sr-only')}>{text}</span>
    </Badge>
  );
}
