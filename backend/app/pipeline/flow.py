"""Orchestration helpers for the default discovery strategy chain.

bulk data -> company matching -> official domain discovery -> own website crawl -> directory corroboration ->
document extraction -> controlled permutation research -> verification -> qualification -> export.

Everything here is deterministic and free of AI. The stage handlers (``handlers.py`` / ``discovery_stages.py``) call these
helpers; they own no transaction (the worker commits the job's session).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .. import jobqueue as jq
from ..crawler.crawl import CrawlResult
from ..crawler.types import EmailObservation
from ..crawler.urls import registrable_domain
from ..discovery import strategy as strat
from ..discovery.completeness import refresh_completeness
from ..discovery.effectiveness import MAX_COUNTERS  # noqa: F401  (re-exported for tests)
from ..discovery.scheduler import should_skip_expensive_work
from ..models import Contact, ContactEvidence, EmailAddress, EvidenceSource, Organisation, OrganisationDomain, OrganisationSource
from ..run_config import RunConfig
from ..sources.base import BusinessRecord
from ..sources.signals_util import MissingSourceUrl, record_signal
from ..worker import JobContext
from . import runtime
from .ingest import normalise_email, persist_observations

if TYPE_CHECKING:  # resolution imports app.pipeline.identity: import it lazily to avoid a package-init cycle
    from ..discovery.resolution import Resolution

log = logging.getLogger("pipeline.flow")

DOC_MAX_BYTES = 2_000_000
DOC_SUFFIXES = (".pdf", ".csv")
GAP_REQUEST_BUDGET = {"fast": 20, "balanced": 40, "deep": 80}


# --------------------------------------------------------------------------- run plan
@dataclass
class RunPlan:
    cfg: RunConfig
    plan: strat.DiscoveryPlan
    legacy: bool  # snapshot predates the discovery layer: behaviour identical to the pre-discovery pipeline

    @property
    def spec(self):
        return self.cfg.discovery

    def stage_on(self, name: str) -> bool:
        return self.plan.stage_enabled(name)

    @property
    def crawl_websites(self) -> bool:
        return self.legacy or self.stage_on("own_website_crawl")

    @property
    def gap_on(self) -> bool:
        return (not self.legacy) and bool(self.plan.gap_recovery.get("enabled"))

    @property
    def signals_on(self) -> bool:
        return (not self.legacy) and bool(self.plan.signals.get("enabled"))

    @property
    def network_on(self) -> bool:
        return (not self.legacy) and bool(self.plan.network.get("enabled"))

    @property
    def documents_on(self) -> bool:
        return (not self.legacy) and self.stage_on("document_extraction")

    @property
    def corroboration_on(self) -> bool:
        return (not self.legacy) and self.stage_on("directory_corroboration")

    @property
    def permutations_on(self) -> bool:
        return (not self.legacy) and (self.stage_on("controlled_permutation_research") or self.cfg.target.allow_inferred)

    @property
    def common_crawl_on(self) -> bool:
        return (not self.legacy) and "common_crawl" in self.plan.source_categories


async def load_plan(session: AsyncSession, run_id: int) -> RunPlan:
    snap = (await session.execute(text("SELECT configuration_snapshot FROM sourcing_runs WHERE id=:r"), {"r": run_id})).scalar_one()
    cfg = RunConfig.model_validate(snap)
    return RunPlan(cfg, strat.plan_from_config(cfg), legacy="discovery" not in (snap or {}))


# --------------------------------------------------------------------------- persistence of one source record
@dataclass
class Persisted:
    org_id: int
    created: bool
    decision: str
    signals_new: int = 0
    signals_dropped: int = 0
    links_new: int = 0
    resolution: "Resolution | None" = None


async def persist_discovered(session: AsyncSession, rec: BusinessRecord, adapter_id: str, run_id: int | None,
                             category_id: int | None = None) -> Persisted:
    """Company matching + provenance + signals + extra identifier links for ONE source record, each exactly once.

    Order: ``resolving_upsert_ex`` (matches against known companies, writes ``OrganisationSource`` provenance, the main
    ``OrganisationRecordLink`` and any review link) -> signals from ``rec.raw['signals']`` (``INSERT .. ON CONFLICT DO
    NOTHING`` on the dedupe hash; a signal without a source URL is dropped, never invented) -> companion identifier links for
    ``rec.raw['identifiers']`` that the resolver does not already know (LEI, wikidata ... one row per (record, type)).
    All three are idempotent, so re-processing a record after a crash or a resume changes nothing.
    """
    from ..discovery.resolution import _upsert_link, extract_identifiers, resolving_upsert_ex

    raw = dict(rec.raw or {})
    signals = list(raw.pop("signals", None) or [])  # bulky; would also blow the size cap of organisation_sources.raw
    idents = [i for i in (raw.get("identifiers") or []) if isinstance(i, dict) and i.get("type") and i.get("value")]
    slim = replace(rec, raw=raw)
    org_id, created, res = await resolving_upsert_ex(session, slim, adapter_id, run_id, category_id)
    out = Persisted(org_id, created, res.decision, resolution=res)
    for sig in signals:
        try:
            if await record_signal(session, org_id, adapter_id, sig):
                out.signals_new += 1
        except MissingSourceUrl:
            out.signals_dropped += 1
    if idents and res.decision in ("matched", "created"):
        known = {(t, v) for t, v in extract_identifiers(slim)}
        ref = (rec.source_ref or "")[:280]
        for i in idents:
            key = (str(i["type"]), str(i["value"]))
            if key in known or not ref:
                continue
            await _upsert_link(session, org_id, adapter_id, f"{ref}#{key[0]}"[:300], key[0][:30], key[1], str(i.get("basis", "identifier"))[:40],
                               float(i.get("confidence", 0.9)), "linked", None, {"companion_of": ref})
            out.links_new += 1
    return out


# --------------------------------------------------------------------------- source gate (rate / concurrency) + quotas
async def other_leased_discovers(session: AsyncSession, adapter_id: str, job_id: int) -> int:
    return (await session.execute(text(
        "SELECT count(*) FROM sourcing_jobs WHERE stage='discover' AND state='leased' AND id<>:j "
        "AND payload->>'adapter_id' = :a"), {"j": job_id, "a": adapter_id})).scalar_one()


@dataclass
class QuotaGuard:
    """Hard per-adapter / per-category / overall work-unit limits (one unit = one source record) for one discover job."""

    adapter_limit: int | None
    category_limit: int | None
    overall_limit: int | None
    used_adapter: int = 0
    used_category: int = 0
    used_overall: int = 0
    reason: str | None = None

    @classmethod
    async def load(cls, session: AsyncSession, rp: RunPlan, run_id: int, adapter_id: str, source_type: str | None) -> QuotaGuard:
        q = rp.plan.source_quotas
        cat = strat.category_of(adapter_id, source_type)
        g = cls(q.get(adapter_id), q.get(cat) if cat else None, rp.plan.overall_max_work)
        await g.refresh(session, run_id, adapter_id, cat)
        return g

    async def refresh(self, session: AsyncSession, run_id: int, adapter_id: str, cat: str | None) -> None:
        from ..sources.base import get_adapter

        rows = (await session.execute(text("SELECT adapter_id, records_seen FROM source_effectiveness WHERE run_id=:r"), {"r": run_id})).all()
        self.used_adapter = self.used_category = self.used_overall = 0
        for a, n in rows:
            n = int(n or 0)
            self.used_overall += n
            if a == adapter_id:
                self.used_adapter += n
            if cat and a != adapter_id:
                try:
                    st = get_adapter(a).info.source_type
                except KeyError:
                    st = None
                if strat.category_of(a, st) == cat:
                    self.used_category += n
        if cat:
            self.used_category += self.used_adapter

    def remaining(self) -> int | None:
        left = [lim - used for lim, used in ((self.adapter_limit, self.used_adapter), (self.category_limit, self.used_category),
                                             (self.overall_limit, self.used_overall)) if lim is not None]
        return max(0, min(left)) if left else None

    def take(self) -> bool:
        """Reserve one work unit; False (and ``reason``) when any limit is spent."""
        for name, lim, used in (("source_quota", self.adapter_limit, self.used_adapter),
                                ("category_quota", self.category_limit, self.used_category),
                                ("overall_work_limit", self.overall_limit, self.used_overall)):
            if lim is not None and used >= lim:
                self.reason = name
                return False
        self.used_adapter += 1
        self.used_category += 1
        self.used_overall += 1
        return True


# --------------------------------------------------------------------------- job helpers
async def enqueue_verify(session: AsyncSession, run_id: int | None, email_id: int, *, source_adapter: str | None = None, priority: int = 30,
                         extra: dict[str, Any] | None = None) -> bool:
    if run_id is None:
        return False
    payload = {"email_id": email_id, **({"source_adapter": source_adapter} if source_adapter else {}), **(extra or {})}
    return bool(await jq.enqueue(session, run_id=run_id, stage="verify", dedupe_key=f"email:{email_id}", payload=payload, priority=priority))


async def enqueue_unvalidated_verifies(session: AsyncSession, run_id: int | None, org_id: int, *, source_adapter: str | None = None) -> int:
    """Verify jobs for the organisation's addresses that have never been validated (e.g. found by documents / gap recovery)."""
    if run_id is None:
        return 0
    ids = (await session.execute(text(
        "SELECT e.id FROM email_addresses e WHERE e.organisation_id=:o AND e.last_validated_at IS NULL AND e.suppressed_at IS NULL "
        "AND NOT (EXISTS (SELECT 1 FROM contact_evidence ce JOIN evidence_sources es ON es.id=ce.evidence_id WHERE ce.email_id=e.id AND es.historical) "
        "AND NOT EXISTS (SELECT 1 FROM contact_evidence ce JOIN evidence_sources es ON es.id=ce.evidence_id WHERE ce.email_id=e.id AND NOT es.historical)) "
        "ORDER BY e.id LIMIT 200"), {"o": org_id})).scalars().all()
    n = 0
    for i in ids:
        n += int(await enqueue_verify(session, run_id, i, source_adapter=source_adapter))
    return n


async def set_stage(session: AsyncSession, run_id: int | None, org_id: int, stage: str) -> None:
    if run_id is None:
        return
    await session.execute(text("UPDATE run_organisations SET stage=:s WHERE run_id=:r AND organisation_id=:o"), {"s": stage[:24], "r": run_id, "o": org_id})


async def adapter_of_org(session: AsyncSession, org_id: int, payload: dict[str, Any] | None = None) -> str | None:
    a = (payload or {}).get("source_adapter")
    if a:
        return str(a)
    return (await session.execute(text("SELECT discovery_source FROM organisations WHERE id=:o"), {"o": org_id})).scalar_one_or_none()


async def pending_jobs_for_org(session: AsyncSession, run_id: int, org_id: int, exclude_job: int | None) -> bool:
    row = (await session.execute(text(
        """SELECT 1 FROM sourcing_jobs j WHERE j.run_id=:r AND j.state IN ('queued','leased') AND j.id <> :me AND (
               j.payload->>'organisation_id' = :os
               OR (j.stage IN ('verify','revalidate') AND j.payload->>'email_id' ~ '^[0-9]+$'
                   AND CASE WHEN j.payload->>'email_id' ~ '^[0-9]+$' THEN (j.payload->>'email_id')::bigint END
                       IN (SELECT id FROM email_addresses WHERE organisation_id=:o))) LIMIT 1"""),
        {"r": run_id, "o": org_id, "os": str(org_id), "me": exclude_job or 0})).first()
    return row is not None


async def finish_org(ctx: JobContext, org_id: int, *, force: bool = False) -> bool:
    """Refresh the organisation's completeness once none of its own jobs is still pending (end of its processing)."""
    run_id = ctx.job.run_id
    if run_id is not None and not force and await pending_jobs_for_org(ctx.session, run_id, org_id, ctx.job.id):
        return False
    await refresh_completeness(ctx.session, [org_id])
    return True


async def skip_decision(ctx: JobContext, rp: RunPlan, org_id: int, refresh: bool):
    """Duplicate / already-done work suppression before network work (``should_skip_expensive_work``)."""
    return await should_skip_expensive_work(ctx.session, org_id, ctx.job.run_id, refresh=refresh or rp.plan.refresh_existing)


# --------------------------------------------------------------------------- directory corroboration (DB only, no network)
async def corroborate(session: AsyncSession, org_id: int, run_id: int | None) -> list[int]:
    """Register / directory corroboration from data we already hold: an address published by a SECOND source record of the
    same organisation (``OrganisationSource.email_published``, e.g. a procurement notice or an industry register) becomes extra
    evidence for that address. ``persist_observations`` then applies its normal origin rules (a third-party-domain address seen on
    two distinct sources is ``corroborated``); nothing is fetched. Returns the ids of the addresses that gained evidence."""
    srcs = (await session.execute(select(OrganisationSource).where(
        OrganisationSource.organisation_id == org_id, OrganisationSource.email_published.is_not(None)))).scalars().all()
    ids: list[int] = []
    for s in srcs:
        em = normalise_email(s.email_published or "")
        if not em:
            continue
        url = s.source_url or f"urn:register:{s.adapter_id}:{s.source_ref}"
        ob = EmailObservation(email=em, method="register", page_url=url, context=f"published by source {s.adapter_id}")
        dom = (await session.execute(select(OrganisationDomain).where(OrganisationDomain.organisation_id == org_id)
                                     .order_by(OrganisationDomain.confidence.desc()).limit(1))).scalar_one_or_none()
        for eid, _created in await persist_observations(session, org_id, dom, [ob], run_id=run_id, source_adapter=s.adapter_id, licence=s.licence):
            ids.append(eid)
    return ids


# --------------------------------------------------------------------------- document extraction (linked public documents)
def linked_documents(crawl: CrawlResult, dom: OrganisationDomain, limit: int) -> list[str]:
    reg = dom.registrable_domain
    seen: dict[str, int] = {}
    for x in getattr(crawl, "_extractions", []) or []:
        for ln in x.links:
            u = ln.url
            try:
                sp = urlsplit(u)
            except ValueError:
                continue
            if sp.scheme not in ("http", "https") or not sp.path.lower().endswith(DOC_SUFFIXES):
                continue
            if registrable_domain(sp.hostname or "") != reg:
                continue
            hint = 1 if re.search(r"(contact|team|staff|member|director|exhibit|supplier|catalog|price)", sp.path, re.I) else 0
            seen.setdefault(u, hint)
    return [u for u, _h in sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))][:max(0, limit)]


async def extract_documents(ctx: JobContext, rp: RunPlan, org: Organisation, dom: OrganisationDomain, crawl: CrawlResult,
                            fetcher, limiter, robots) -> dict[str, int]:
    """Parse small public PDFs / CSVs linked from the crawled site and store the structured rows (never evaluates formulas;
    size, row, page and time limits; robots + per-host rate limit honoured)."""
    from ..discovery.documents import DocLimits, parse_document, persist_document_records

    stage = rp.plan.stage("document_extraction")
    max_docs = int((stage.params if stage else {}).get("max_documents_per_site", 2))
    out = {"documents": 0, "records": 0, "emails": 0, "requests": 0}
    lim = DocLimits(max_bytes=DOC_MAX_BYTES, max_rows=2000, max_records=500, max_pages=20, max_seconds=5.0)
    for url in linked_documents(crawl, dom, max_docs):
        if not (await robots.check(url)).allowed:
            continue
        async with limiter.acquire(urlsplit(url).netloc) as slot:
            res = await fetcher.fetch(url, accept={"application/pdf", "text/csv", "text/plain", "application/octet-stream", "application/csv"},
                                      max_bytes=DOC_MAX_BYTES, conditional=False)
            slot.report(res.status, res.retry_after() if res.status in (429, 503) else None)
        out["requests"] += 1
        if not res.usable_body or res.status != 200 or res.truncated:
            continue
        warns: list[str] = []
        recs = parse_document(res.body, filename=urlsplit(url).path.rsplit("/", 1)[-1], mime=res.content_type, limits=lim, warnings=warns)
        if not recs:
            continue
        summ = await persist_document_records(ctx.session, recs, source_url=res.final_url or url, adapter="document_extraction", org_id=org.id,
                                              run_id=ctx.job.run_id, create_orgs=False)
        out["documents"] += 1
        out["records"] += summ.stored
        out["emails"] += summ.emails
    return out


# --------------------------------------------------------------------------- controlled permutation research
async def research_permutations(session: AsyncSession, org: Organisation, dom: OrganisationDomain, run_id: int | None, *,
                                role_limit: int = 3, named_limit: int = 2, max_people: int = 3) -> list[int]:
    """Persist a few speculative addresses on a CONFIRMED domain: role mailboxes (``role_speculative``) and, for people
    evidenced on the site who have no published address, named patterns (``pattern_inferred``, pattern taken from the addresses
    this domain really publishes). Always ``needs_review``, evidence type ``permutation``, reliability 0.1; never ``published``.
    Returns the ids of the addresses created (existing addresses are left untouched)."""
    from ..verify.permutations import generate_named_candidates, generate_role_candidates, infer_patterns

    if dom.verified_match_status not in ("confirmed", "high") or dom.parked:
        return []
    published = (await session.execute(
        select(EmailAddress.local_part, EmailAddress.contact_id).where(
            EmailAddress.organisation_id == org.id, EmailAddress.registrable_domain == dom.registrable_domain,
            EmailAddress.origin.in_(("published", "corroborated"))))).all()
    locals_ = [r[0] for r in published]
    contacts = (await session.execute(select(Contact).where(Contact.organisation_id == org.id, Contact.given_name.is_not(None),
                                                            Contact.family_name.is_not(None)).order_by(Contact.id))).scalars().all()
    by_id = {c.id: c for c in contacts}
    pairs = [(by_id[cid].given_name, by_id[cid].family_name, loc) for loc, cid in published if cid in by_id]
    patterns = [h.pattern for h in infer_patterns(pairs)] if pairs else None
    have_email = {cid for _l, cid in published if cid}
    cands = list(generate_role_candidates(dom.domain, org.industry, {"published_locals": locals_}, limit=role_limit))
    people = 0
    for c in contacts:
        if c.id in have_email or people >= max_people:
            continue
        got = generate_named_candidates(c.given_name or "", c.family_name or "", dom.domain, patterns=patterns, limit=named_limit)
        if got:
            people += 1
            cands += [(cand, c.id) for cand in got]  # type: ignore[misc]
    created: list[int] = []
    from datetime import UTC, datetime

    from sqlalchemy.dialects.postgresql import insert as pg_insert

    now = datetime.now(UTC)
    for item in cands:
        cand, contact_id = item if isinstance(item, tuple) else (item, None)
        ne = normalise_email(cand.email)
        if not ne:
            continue
        origin = cand.origin if cand.origin in ("pattern_inferred", "role_speculative") else "role_speculative"
        from .ingest import classify_role

        ins = pg_insert(EmailAddress).values(
            organisation_id=org.id, contact_id=contact_id, email_original=cand.email, email_normalised=ne, local_part=cand.local_part,
            domain=cand.domain, registrable_domain=dom.registrable_domain, source_type="permutation", origin=origin,
            role_type=classify_role(cand.local_part) if origin == "role_speculative" else None, confidence=cand.confidence_hint,
            needs_review=True, first_seen_at=now, last_seen_at=now).on_conflict_do_nothing(index_elements=["email_normalised"]).returning(EmailAddress.id)
        eid = (await session.execute(ins)).scalar_one_or_none()
        if eid is None:
            continue
        created.append(eid)
        ev = pg_insert(EvidenceSource).values(source_adapter="permutation_research", source_url=f"permutation:{dom.domain}", evidence_type="permutation",
                                              content_hash=ne, source_reliability=0.1, parser_version="perm-1")
        ev_id = (await session.execute(ev.on_conflict_do_update(constraint="uq_evidence_identity", set_={"observed_at": now})
                                       .returning(EvidenceSource.id))).scalar_one()
        await session.execute(pg_insert(ContactEvidence).values(
            email_id=eid, contact_id=contact_id, organisation_id=org.id, evidence_id=ev_id, run_id=run_id, extraction_method="permutation",
            context={"pattern": cand.pattern, "note": cand.note, "origin": origin, "speculative": True}).on_conflict_do_nothing())
    return created


# --------------------------------------------------------------------------- follow-up job planning
async def enqueue_followups(ctx: JobContext, rp: RunPlan, org_id: int, *, source: str | None, source_adapter: str | None,
                            domain: str | None = None) -> dict[str, bool]:
    """Jobs that follow a confirmed official domain: network expansion (not for orgs found BY expansion), Common Crawl lookup."""
    run_id = ctx.job.run_id
    out: dict[str, bool] = {}
    if run_id is None:
        return out
    if rp.network_on and source != "network_expansion":
        net = rp.plan.network
        out["network_expand"] = bool(await jq.enqueue(
            ctx.session, run_id=run_id, stage="network_expand", dedupe_key=f"net:{org_id}",
            payload={"organisation_id": org_id, "max_depth": net["max_depth"], "budget": net["source_budget"], "source_adapter": source_adapter},
            priority=70))
    if rp.common_crawl_on:
        out["cc_lookup"] = bool(await jq.enqueue(ctx.session, run_id=run_id, stage="cc_lookup", dedupe_key=f"cc:{org_id}",
                                                 payload={"organisation_id": org_id, "domain": domain, "source_adapter": source_adapter}, priority=65))
    return out


async def enqueue_gap(ctx: JobContext, rp: RunPlan, org_id: int, reason: str, source_adapter: str | None) -> bool:
    if ctx.job.run_id is None or not rp.gap_on:
        return False
    return bool(await jq.enqueue(ctx.session, run_id=ctx.job.run_id, stage="gap_recovery", dedupe_key=f"gap:{org_id}",
                                 payload={"organisation_id": org_id, "reason": reason, "source_adapter": source_adapter}, priority=75))


@dataclass
class CrawlOutcome:
    emails: int = 0
    verify_jobs: int = 0
    signals: int = 0
    documents: int = 0
    corroborated: int = 0
    speculative: int = 0
    confirmed_historical: int = 0
    notes: dict[str, Any] = field(default_factory=dict)
