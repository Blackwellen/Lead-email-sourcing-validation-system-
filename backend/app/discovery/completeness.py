"""DISC-14: research completeness with TRI-STATE flags.

``unknown``      the research step has not been performed (or produced nothing conclusive yet)
``in_progress``  work is queued / running, or a candidate awaits review
``yes``          established from stored evidence
``no``           the step WAS performed and the thing is genuinely unavailable

A flag is only ever ``no`` when the evidence for the corresponding research step exists (a crawl happened, the gap-recovery
ladder ended, a probe ran ...). ``missing_opportunities`` separates *incomplete research* (still worth doing) from
*genuinely unavailable* (researched, nothing there).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Organisation, OrganisationCompleteness

FLAGS = (
    "official_website", "contact_page_inspected", "published_email", "contact_person_corroborated",
    "email_infrastructure_checked", "recipient_evidence", "campaign_eligibility", "recently_checked",
)
WEIGHTS = {"official_website": 25, "published_email": 25, "contact_page_inspected": 10, "contact_person_corroborated": 10,
           "email_infrastructure_checked": 10, "recipient_evidence": 5, "campaign_eligibility": 10, "recently_checked": 5}
NEXT_STEP = {
    "official_website": "identity-resolve candidate domains / run gap recovery",
    "contact_page_inspected": "crawl the official site's contact / team pages",
    "published_email": "crawl contact pages; check documents and corroborating sources",
    "contact_person_corroborated": "corroborate a named contact from a second public source",
    "email_infrastructure_checked": "run DNS / MX verification (V02/V03)",
    "recipient_evidence": "SMTP diagnostics are only possible where the domain permits them",
    "campaign_eligibility": "run verification and marketing-eligibility triage",
    "recently_checked": "re-check the organisation (refresh)",
}
DOMAIN_STEPS = {"recheck_imported_website", "resolve_bulk_candidate_domains", "search_provider", "common_crawl_lookup", "register_corroboration"}
CORROBORATION_STEPS = {"register_corroboration", "document_extraction", "search_provider"}
CONTACT_PATH = r"(contact|about|team|people|staff|imprint|impressum|get-in-touch|enquir|leadership|directors)"


@dataclass
class Completeness:
    organisation_id: int
    flags: dict[str, str]
    score: int
    research_progress: float
    missing_opportunities: list[dict[str, Any]] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)
    needs_review: bool = False

    def __getitem__(self, k: str) -> str:
        return self.flags[k]


@dataclass
class _Facts:
    """Everything the evaluator needs about ONE organisation (loaded in bulk)."""

    org_id: int
    last_checked_at: datetime | None = None
    domains: list[dict[str, Any]] = field(default_factory=list)
    snapshot_kinds: set[str] = field(default_factory=set)
    contact_evidence_urls: int = 0
    emails_total: int = 0
    emails_published: int = 0
    emails_validated: int = 0
    emails_eligible: int = 0
    emails_recipient_evidence: int = 0
    delivery_events: int = 0
    named_with_email: int = 0
    corroborated_named: int = 0
    infra_checked: bool = False
    smtp_attempted: bool = False
    jobs_domain: bool = False
    jobs_verify: bool = False
    gap_log: list[dict[str, Any]] = field(default_factory=list)
    gap_exhausted: bool = False
    needs_review: bool = False


def evaluate(f: _Facts, *, window_days: int = 30, now: datetime | None = None) -> Completeness:
    """Pure function: facts -> tri-state flags, score and classification. Never invents research that did not happen."""
    now = now or datetime.now(UTC)
    flags: dict[str, str] = {}
    why: dict[str, str] = {}
    usable = [d for d in f.domains if (d["status"] in ("confirmed", "high") or d["manual_override"]) and not d["parked"]]
    reviewing = [d for d in f.domains if d["status"] == "review" and not d["parked"]]
    crawled = any(d["last_crawled_at"] for d in f.domains)
    domain_steps_run = {e.get("step") for e in f.gap_log if e.get("step") in DOMAIN_STEPS and e.get("outcome") not in ("skipped", "unavailable")}

    # --- official website
    if usable:
        flags["official_website"], why["official_website"] = "yes", "confirmed/high domain match"
    elif f.jobs_domain:
        flags["official_website"], why["official_website"] = "in_progress", "domain resolution queued/running"
    elif reviewing:
        flags["official_website"], why["official_website"] = "in_progress", "candidate domain awaiting review"
    else:
        checked = [d for d in f.domains if d["last_checked_at"]]
        all_rejected = bool(checked) and len(checked) == len(f.domains) and all(d["status"] == "mismatch" or d["parked"] for d in f.domains)
        researched = all_rejected or bool(domain_steps_run)
        if researched and f.gap_exhausted:
            flags["official_website"] = "no"
            why["official_website"] = ("all candidate domains rejected" if all_rejected else "no website found") + " after the gap-recovery ladder"
        else:
            flags["official_website"] = "unknown"
            why["official_website"] = ("candidates rejected but research ladder not finished" if researched else "no domain research recorded")

    # --- contact page inspected
    if f.snapshot_kinds & {"contact", "team"} or f.contact_evidence_urls:
        flags["contact_page_inspected"], why["contact_page_inspected"] = "yes", "contact/team page snapshot or page evidence exists"
    elif f.jobs_domain and not crawled:
        flags["contact_page_inspected"], why["contact_page_inspected"] = "in_progress", "crawl queued/running"
    elif crawled:
        flags["contact_page_inspected"], why["contact_page_inspected"] = "no", "site was crawled; no contact/team page found"
    else:
        flags["contact_page_inspected"], why["contact_page_inspected"] = "unknown", "no crawl performed"

    # --- published email
    if f.emails_published:
        flags["published_email"], why["published_email"] = "yes", f"{f.emails_published} published/corroborated address(es)"
    elif f.jobs_domain and not crawled:
        flags["published_email"], why["published_email"] = "in_progress", "crawl queued/running"
    elif flags["contact_page_inspected"] in ("yes", "no") and crawled:
        flags["published_email"], why["published_email"] = "no", "site crawled and contact pages inspected; no address published"
    elif flags["official_website"] == "no":
        flags["published_email"], why["published_email"] = "no", "no official website after the full research ladder"
    else:
        flags["published_email"], why["published_email"] = "unknown", "no completed crawl of an official site"

    # --- named contact corroborated
    if f.corroborated_named:
        flags["contact_person_corroborated"], why["contact_person_corroborated"] = "yes", "named contact with corroborating evidence"
    elif f.named_with_email and set(e.get("step") for e in f.gap_log) & CORROBORATION_STEPS:
        flags["contact_person_corroborated"], why["contact_person_corroborated"] = "no", "named contact found but no second source after corroboration steps"
    elif f.named_with_email:
        flags["contact_person_corroborated"], why["contact_person_corroborated"] = "in_progress", "named contact found; corroboration not attempted yet"
    elif crawled and flags["contact_page_inspected"] in ("yes", "no"):
        flags["contact_person_corroborated"], why["contact_person_corroborated"] = "no", "site inspected; no named contact published"
    else:
        flags["contact_person_corroborated"], why["contact_person_corroborated"] = "unknown", "no inspected site"

    # --- email infrastructure (only 'yes' or 'unknown'/'in_progress': a failed lookup is still a completed check)
    if f.infra_checked:
        flags["email_infrastructure_checked"], why["email_infrastructure_checked"] = "yes", "DNS/MX result stored"
    elif f.jobs_verify:
        flags["email_infrastructure_checked"], why["email_infrastructure_checked"] = "in_progress", "verification queued/running"
    else:
        flags["email_infrastructure_checked"], why["email_infrastructure_checked"] = "unknown", "no DNS/MX result stored"

    # --- recipient evidence
    if f.emails_recipient_evidence or f.delivery_events:
        flags["recipient_evidence"], why["recipient_evidence"] = "yes", "SMTP recipient evidence or delivery feedback exists"
    elif f.smtp_attempted:
        flags["recipient_evidence"], why["recipient_evidence"] = "no", "SMTP diagnostics attempted; inconclusive / not permitted"
    elif f.jobs_verify:
        flags["recipient_evidence"], why["recipient_evidence"] = "in_progress", "verification queued/running"
    else:
        flags["recipient_evidence"], why["recipient_evidence"] = "unknown", "no SMTP diagnostics attempted"

    # --- campaign eligibility
    if f.emails_eligible:
        flags["campaign_eligibility"], why["campaign_eligibility"] = "yes", "at least one address eligible under the marketing policy"
    elif f.jobs_verify:
        flags["campaign_eligibility"], why["campaign_eligibility"] = "in_progress", "verification queued/running"
    elif f.emails_total and f.emails_validated >= f.emails_total:
        flags["campaign_eligibility"], why["campaign_eligibility"] = "no", "all addresses validated; none eligible"
    else:
        flags["campaign_eligibility"], why["campaign_eligibility"] = "unknown", "addresses not yet validated" if f.emails_total else "no addresses"

    # --- recently checked
    if f.last_checked_at is None:
        flags["recently_checked"], why["recently_checked"] = "unknown", "never checked"
    elif f.last_checked_at >= now - timedelta(days=window_days):
        flags["recently_checked"], why["recently_checked"] = "yes", f"checked within {window_days} days"
    else:
        flags["recently_checked"], why["recently_checked"] = "no", f"last check older than {window_days} days"

    score = round(sum(w * (1.0 if flags[k] == "yes" else 0.25 if flags[k] == "in_progress" else 0.0) for k, w in WEIGHTS.items()))
    resolved = sum(1 for v in flags.values() if v in ("yes", "no"))
    missing = []
    for k in FLAGS:
        v = flags[k]
        if v == "yes":
            continue
        cls = {"unknown": "incomplete_research", "in_progress": "in_progress", "no": "genuinely_unavailable"}[v]
        if k == "recently_checked" and v == "no":
            cls = "stale"
        missing.append({"flag": k, "state": v, "classification": cls, "reason": why[k],
                        "next_step": NEXT_STEP[k] if cls in ("incomplete_research", "stale") else None})
    return Completeness(f.org_id, flags, int(score), round(resolved / len(FLAGS), 3), missing, why, f.needs_review)


# --------------------------------------------------------------------------- bulk loading
async def load_facts(session: AsyncSession, org_ids: list[int]) -> dict[int, _Facts]:
    facts = {i: _Facts(i) for i in org_ids}
    if not org_ids:
        return facts
    p = {"ids": list(org_ids), "idt": [str(i) for i in org_ids]}
    for oid, lc in (await session.execute(select(Organisation.id, Organisation.last_checked_at).where(Organisation.id.in_(org_ids)))).all():
        facts[oid].last_checked_at = lc
    for r in (await session.execute(text(
            "SELECT organisation_id, verified_match_status, parked, manual_override, last_checked_at, last_crawled_at "
            "FROM organisation_domains WHERE organisation_id = ANY(:ids)"), p)).all():
        facts[r[0]].domains.append({"status": r[1], "parked": r[2], "manual_override": r[3], "last_checked_at": r[4], "last_crawled_at": r[5]})
    for oid, kind in (await session.execute(text(
            "SELECT DISTINCT d.organisation_id, s.page_kind FROM website_snapshots s JOIN organisation_domains d ON d.id = s.domain_id "
            "WHERE d.organisation_id = ANY(:ids)"), p)).all():
        facts[oid].snapshot_kinds.add(kind)
    for oid, n in (await session.execute(text(
            "SELECT ce.organisation_id, count(DISTINCT es.id) FROM contact_evidence ce JOIN evidence_sources es ON es.id = ce.evidence_id "
            f"WHERE ce.organisation_id = ANY(:ids) AND es.source_url ~* '{CONTACT_PATH}' AND es.evidence_type <> 'page_removed' "
            "GROUP BY ce.organisation_id"), p)).all():
        facts[oid].contact_evidence_urls = n
    for r in (await session.execute(text(
            "SELECT organisation_id, count(*), count(*) FILTER (WHERE origin IN ('published','corroborated')), "
            "count(*) FILTER (WHERE last_validated_at IS NOT NULL), count(*) FILTER (WHERE marketing_status = 'eligible'), "
            "count(*) FILTER (WHERE recipient_evidence_status IN ('smtp_recipient_accepted','smtp_recipient_rejected')) "
            "FROM email_addresses WHERE organisation_id = ANY(:ids) AND suppressed_at IS NULL GROUP BY organisation_id"), p)).all():
        f = facts[r[0]]
        f.emails_total, f.emails_published, f.emails_validated, f.emails_eligible, f.emails_recipient_evidence = r[1:6]
    for oid, n in (await session.execute(text(
            "SELECT e.organisation_id, count(*) FROM delivery_events d JOIN email_addresses e ON e.id = d.email_id "
            "WHERE e.organisation_id = ANY(:ids) GROUP BY e.organisation_id"), p)).all():
        facts[oid].delivery_events = n
    for oid, n in (await session.execute(text(
            "SELECT c.organisation_id, count(DISTINCT c.id) FROM contacts c JOIN email_addresses e ON e.contact_id = c.id "
            "WHERE c.organisation_id = ANY(:ids) AND c.full_name IS NOT NULL GROUP BY c.organisation_id"), p)).all():
        facts[oid].named_with_email = n
    for oid, n in (await session.execute(text(
            "SELECT c.organisation_id, count(DISTINCT c.id) FROM contacts c JOIN email_addresses e ON e.contact_id = c.id "
            "WHERE c.organisation_id = ANY(:ids) AND c.full_name IS NOT NULL AND (e.origin = 'corroborated' OR "
            "(SELECT count(DISTINCT ce.evidence_id) FROM contact_evidence ce WHERE ce.email_id = e.id) >= 2) "
            "GROUP BY c.organisation_id"), p)).all():
        facts[oid].corroborated_named = n
    for (oid,) in (await session.execute(text(
            "SELECT DISTINCT e.organisation_id FROM email_addresses e JOIN validation_results v ON v.email_id = e.id "
            "WHERE e.organisation_id = ANY(:ids) AND v.check_type IN ('V02_domain','V03_mx')"), p)).all():
        facts[oid].infra_checked = True
    for (oid,) in (await session.execute(text(
            "SELECT DISTINCT e.organisation_id FROM email_addresses e JOIN domain_cache d ON d.domain = e.domain "
            "WHERE e.organisation_id = ANY(:ids)"), p)).all():
        facts[oid].infra_checked = True
    for (oid,) in (await session.execute(text(
            "SELECT DISTINCT e.organisation_id FROM email_addresses e JOIN validation_results v ON v.email_id = e.id "
            "WHERE e.organisation_id = ANY(:ids) AND v.check_type = 'V07_smtp' AND v.result <> 'untested'"), p)).all():
        facts[oid].smtp_attempted = True
    for (oid,) in (await session.execute(text(
            "SELECT DISTINCT (payload->>'organisation_id')::bigint FROM sourcing_jobs WHERE state IN ('queued','leased') "
            "AND stage IN ('resolve_domain','crawl','import_domain') AND payload->>'organisation_id' = ANY(:idt)"), p)).all():
        facts[oid].jobs_domain = True
    for (oid,) in (await session.execute(text(
            "SELECT DISTINCT e.organisation_id FROM sourcing_jobs j JOIN email_addresses e ON e.id = (j.payload->>'email_id')::bigint "
            "WHERE j.state IN ('queued','leased') AND j.stage IN ('verify','revalidate') AND j.payload->>'email_id' ~ '^[0-9]+$' "
            "AND e.organisation_id = ANY(:ids)"), p)).all():
        facts[oid].jobs_verify = True
    for row in (await session.execute(select(OrganisationCompleteness).where(OrganisationCompleteness.organisation_id.in_(org_ids))
            .execution_options(populate_existing=True))).scalars():
        f = facts[row.organisation_id]
        f.gap_log, f.gap_exhausted, f.needs_review = list(row.gap_log or []), row.gap_exhausted_at is not None, bool(row.needs_review)
    return facts


async def compute_completeness(session: AsyncSession, org_id: int, *, window_days: int = 30, now: datetime | None = None) -> Completeness:
    """Evaluate (without writing) the tri-state completeness of one organisation."""
    facts = await load_facts(session, [org_id])
    return evaluate(facts[org_id], window_days=window_days, now=now)


async def refresh_completeness(session: AsyncSession, org_ids: list[int], *, window_days: int = 30, now: datetime | None = None,
                               chunk: int = 500) -> dict[int, Completeness]:
    """Recompute and upsert ``organisation_completeness`` rows in bulk (gap-recovery memory fields are preserved)."""
    out: dict[int, Completeness] = {}
    ids = list(dict.fromkeys(org_ids))
    for i in range(0, len(ids), chunk):
        part = ids[i:i + chunk]
        facts = await load_facts(session, part)
        for oid in part:
            c = evaluate(facts[oid], window_days=window_days, now=now)
            out[oid] = c
            vals = {**c.flags, "score": c.score}
            stmt = pg_insert(OrganisationCompleteness).values(organisation_id=oid, **vals)
            await session.execute(stmt.on_conflict_do_update(index_elements=["organisation_id"], set_={**vals, "updated_at": datetime.now(UTC)}))
    return out
