"""DISC-13: bounded gap-recovery ladder for organisations that still lack an official website / published e-mail.

The ladder is an ORDERED list of permitted steps (cheap and local first). Every executed step is written to
``OrganisationCompleteness.gap_log`` (+ ``gap_steps_tried``) with its outcome and yield, and is never repeated for the same
organisation inside ``ttl_days``. The run stops on: website AND published e-mail found, the request / search / step budget
being spent, the policy (``stop_if_no_website``), or the ladder being exhausted. Steps whose recent success rate in this run is
below a threshold (after a minimum sample) are skipped (marginal-yield rule). Only a completed ladder lets the completeness
engine say ``no`` - budget exhaustion leaves the answer ``unknown``.

Step implementations are injectable (``steps={name: async fn(ctx) -> StepResult}``) so the ladder is unit-testable offline.
"""
from __future__ import annotations

import re
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..crawler.ratelimit import RateLimiter
from ..crawler.robots import RobotsCache
from ..crawler.safe_http import SafeFetcher
from ..crawler.types import EmailObservation
from ..crawler.urls import registrable_domain
from ..models import (
    ContactEvidence,
    DocumentRecord,
    EmailAddress,
    EvidenceSource,
    Organisation,
    OrganisationCompleteness,
    OrganisationDomain,
    OrganisationSource,
    SourcingRun,
)
from ..pipeline.identity import (
    Candidate,
    candidate_domains_from_name,
    is_intermediary_domain,
    resolve_organisation_domain,
)
from ..pipeline.ingest import is_free_provider, normalise_email, persist_observations
from ..verify.permutations import HARD_ROLE_LIMIT, generate_role_candidates
from .completeness import Completeness, refresh_completeness

STEP_ORDER = (
    "recheck_imported_website", "resolve_bulk_candidate_domains", "search_provider", "common_crawl_lookup",
    "register_corroboration", "document_extraction", "role_address_candidates",
)
WEBSITE_STEPS = ("recheck_imported_website", "resolve_bulk_candidate_domains")
REQUIRES_ADAPTER: dict[str, tuple[str, ...]] = {
    "search_provider": ("brave_search", "searxng", "search"),
    "common_crawl_lookup": ("commoncrawl_index", "common_crawl"),
}
POLICIES = ("stop_if_no_website", "try_other_sources", "flag_for_review")


@dataclass
class GapBudget:
    max_requests: int = 40  # HTTP requests across all steps for ONE organisation
    max_search: int = 3  # search-provider queries for ONE organisation
    max_steps: int = len(STEP_ORDER)
    role_candidates: int = 3  # controlled role-address research (<= HARD_ROLE_LIMIT)
    requests_used: int = 0
    search_used: int = 0
    steps_used: int = 0

    @property
    def requests_left(self) -> int:
        return self.max_requests - self.requests_used

    @property
    def search_left(self) -> int:
        return self.max_search - self.search_used


@dataclass
class StepResult:
    outcome: str  # found | none | unavailable | error
    website: int = 0  # official website established (confirmed/high)
    emails: int = 0  # published/corroborated addresses persisted
    domains: int = 0  # new candidate domains discovered
    speculative: int = 0  # role_speculative addresses created (never counts as "email found")
    requests: int = 0
    searches: int = 0
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def useful(self) -> bool:
        return bool(self.website or self.emails or self.domains or self.speculative)


@dataclass
class GapDeps:
    """Optional collaborators for the default step implementations."""

    fetcher: SafeFetcher | None = None
    limiter: RateLimiter | None = None
    robots: RobotsCache | None = None
    search_fn: Callable[[Organisation], Awaitable[list[str]]] | None = None  # org -> urls/domains (one query)
    common_crawl_fn: Callable[[list[str]], Awaitable[list[str]]] | None = None  # candidate domains -> those present in the index
    resolve_fn: Callable[..., Awaitable[StepResult]] | None = None  # (ctx, candidates) -> StepResult (override the identity engine)


@dataclass
class StepContext:
    session: AsyncSession
    org: Organisation
    run_id: int | None
    budget: GapBudget
    deps: GapDeps
    allow_inferred: bool = False
    tried_domains: set[str] = field(default_factory=set)


StepImpl = Callable[[StepContext], Awaitable[StepResult]]


# --------------------------------------------------------------------------- marginal-yield tracker
class YieldTracker:
    """Per step-type success window across a run (``useful`` results / attempts)."""

    def __init__(self, window: int = 50) -> None:
        self.window = window
        self._hist: dict[str, deque[bool]] = {}

    def record(self, step: str, useful: bool) -> None:
        self._hist.setdefault(step, deque(maxlen=self.window)).append(bool(useful))

    def attempts(self, step: str) -> int:
        return len(self._hist.get(step, ()))

    def rate(self, step: str) -> float | None:
        h = self._hist.get(step)
        return (sum(h) / len(h)) if h else None

    def should_skip(self, step: str, *, min_sample: int, min_rate: float) -> bool:
        h = self._hist.get(step)
        return bool(h) and len(h) >= min_sample and (sum(h) / len(h)) < min_rate

    def to_dict(self) -> dict[str, list[int]]:
        return {k: [int(x) for x in v] for k, v in self._hist.items()}

    @classmethod
    def from_dict(cls, d: dict[str, list[int]] | None, window: int = 50) -> YieldTracker:
        t = cls(window)
        for k, v in (d or {}).items():
            t._hist[k] = deque((bool(x) for x in v), maxlen=window)
        return t

    @classmethod
    async def load(cls, session: AsyncSession, run_id: int | None) -> YieldTracker:
        if run_id is None:
            return cls()
        run = await session.get(SourcingRun, run_id)
        return cls.from_dict(((run.counters or {}) if run else {}).get("gap_yield"))

    async def save(self, session: AsyncSession, run_id: int | None) -> None:
        if run_id is None:
            return
        import json

        await session.execute(text(
            "UPDATE sourcing_runs SET counters = jsonb_set(COALESCE(counters,'{}'::jsonb), '{gap_yield}', CAST(:v AS jsonb)) WHERE id=:r"),
            {"v": json.dumps(self.to_dict()), "r": run_id})


# --------------------------------------------------------------------------- state helpers
async def _state(session: AsyncSession, org_id: int) -> tuple[bool, bool]:
    website = (await session.execute(text(
        "SELECT 1 FROM organisation_domains WHERE organisation_id=:o AND (verified_match_status IN ('confirmed','high') OR manual_override) "
        "AND NOT parked LIMIT 1"), {"o": org_id})).first() is not None
    email = (await session.execute(text(
        "SELECT 1 FROM email_addresses WHERE organisation_id=:o AND origin IN ('published','corroborated') AND suppressed_at IS NULL LIMIT 1"),
        {"o": org_id})).first() is not None
    return website, email


def _domains_from(values: list[Any]) -> list[str]:
    out: list[str] = []
    for v in values:
        if not v or not isinstance(v, str):
            continue
        s = v.strip()
        if "@" in s and "/" not in s:
            s = s.rpartition("@")[2]
        host = re.sub(r"^https?://", "", s, flags=re.I).split("/")[0].split(":")[0].lower().removeprefix("www.")
        reg = registrable_domain(host)
        if reg and not is_intermediary_domain(host) and not is_free_provider(host) and host not in out:
            out.append(host)
    return out


async def _resolve(ctx: StepContext, cands: list[Candidate]) -> StepResult:
    """Identity-resolve candidate domains (crawl + score) and ingest any addresses their crawl found."""
    cands = [c for c in cands if c.domain not in ctx.tried_domains][:5]
    if not cands:
        return StepResult("none", detail={"reason": "no new candidate domains"})
    if ctx.deps.resolve_fn is not None:
        return await ctx.deps.resolve_fn(ctx, cands)
    if ctx.deps.fetcher is None:
        return StepResult("unavailable", detail={"reason": "no fetcher configured"})
    if ctx.budget.requests_left <= 0:
        return StepResult("none", detail={"reason": "request budget exhausted"})
    ctx.tried_domains.update(c.domain for c in cands)
    res = await resolve_organisation_domain(ctx.session, ctx.deps.fetcher, ctx.org.id, cands, limiter=ctx.deps.limiter, robots=ctx.deps.robots,
                                            max_candidates=min(5, max(1, ctx.budget.requests_left // 3)))
    requests = sum(len(cr.pages) for cr in res.crawls.values())
    website = 1 if res.best is not None and res.best.verified_match_status in ("confirmed", "high") else 0
    emails = 0
    if website:
        for row, _m in res.rows:
            cr = res.crawls.get(row.domain)
            if cr is not None and row.verified_match_status in ("confirmed", "high") and cr.observations:
                made = await persist_observations(ctx.session, ctx.org.id, row, cr.observations, run_id=ctx.run_id, source_adapter="gap_recovery")
                emails += len(made)
    return StepResult("found" if website or emails else "none", website=website, emails=emails, domains=len(res.rows),
                      requests=requests, detail={"candidates": [c.domain for c in cands]})


async def _source_candidates(ctx: StepContext) -> tuple[list[str], list[str]]:
    srcs = (await ctx.session.execute(select(OrganisationSource).where(OrganisationSource.organisation_id == ctx.org.id))).scalars().all()
    websites = [s.website for s in srcs]
    bulk: list[Any] = []
    for s in srcs:
        raw = s.raw or {}
        bulk += [s.email_published, raw.get("email"), raw.get("website"), raw.get("url"), raw.get("domain"), raw.get("web")]
    return _domains_from(websites), _domains_from(bulk)


# --------------------------------------------------------------------------- default steps
async def step_recheck_imported_website(ctx: StepContext) -> StepResult:
    sites, _bulk = await _source_candidates(ctx)
    rows = (await ctx.session.execute(select(OrganisationDomain.domain).where(
        OrganisationDomain.organisation_id == ctx.org.id, OrganisationDomain.verified_match_status.in_(("unknown", "review"))))).scalars().all()
    doms = list(dict.fromkeys(sites + _domains_from(list(rows))))
    return await _resolve(ctx, [Candidate(d, "dataset", "website published by the source dataset") for d in doms])


async def step_resolve_bulk_candidate_domains(ctx: StepContext) -> StepResult:
    sites, bulk = await _source_candidates(ctx)
    doms = [d for d in bulk if d not in sites]
    return await _resolve(ctx, [Candidate(d, "dataset", "domain derived from an official bulk field") for d in doms])


async def step_search_provider(ctx: StepContext) -> StepResult:
    if ctx.deps.search_fn is None:
        return StepResult("unavailable", detail={"reason": "no search provider configured"})
    urls = await ctx.deps.search_fn(ctx.org)
    doms = _domains_from(list(urls or []))
    r = await _resolve(ctx, [Candidate(d, "search", "search result") for d in doms])
    r.searches += 1
    return r


async def step_common_crawl_lookup(ctx: StepContext) -> StepResult:
    if ctx.deps.common_crawl_fn is None:
        return StepResult("unavailable", detail={"reason": "common crawl lookup not configured"})
    guesses = [c.domain for c in candidate_domains_from_name(ctx.org.legal_name or ctx.org.trading_name, country_code=ctx.org.country_code, max_candidates=8)]
    present = await ctx.deps.common_crawl_fn(guesses)
    r = await _resolve(ctx, [Candidate(d, "guess", "name-derived; present in the Common Crawl index") for d in present or []])
    r.requests += 1
    return r


async def step_register_corroboration(ctx: StepContext) -> StepResult:
    """Other already-imported records for the same company (same number, or same normalised name + postcode) may carry a website."""
    from ..sources.persist import normalise_name

    org = ctx.org
    conds = []
    if org.company_number:
        conds.append("(o.country_code = :cc AND o.company_number = :cn)")
    nm = normalise_name(org.legal_name or org.trading_name)
    if nm and org.postal_code:
        conds.append("(lower(o.legal_name) LIKE :pre AND o.postal_code = :pc)")
    if not conds:
        return StepResult("none", detail={"reason": "no identifier to corroborate with"})
    rows = (await ctx.session.execute(text(
        "SELECT s.website, s.email_published, s.raw FROM organisation_sources s JOIN organisations o ON o.id = s.organisation_id "
        f"WHERE o.id <> :id AND ({' OR '.join(conds)}) LIMIT 50"),
        {"id": org.id, "cc": org.country_code, "cn": org.company_number, "pre": (nm.split()[0] + "%") if nm else "", "pc": org.postal_code})).all()
    vals: list[Any] = []
    for w, e, raw in rows:
        vals += [w, e, (raw or {}).get("website"), (raw or {}).get("url")]
    return await _resolve(ctx, [Candidate(d, "dataset", "same company in another imported dataset") for d in _domains_from(vals)])


async def step_document_extraction(ctx: StepContext) -> StepResult:
    """Addresses already extracted from known public documents for this organisation but not yet stored as contacts."""
    recs = (await ctx.session.execute(select(DocumentRecord).where(DocumentRecord.organisation_id == ctx.org.id))).scalars().all()
    obs: list[EmailObservation] = []
    for r in recs:
        f = (r.extracted or {}).get("fields", {})
        for e in f.get("emails", []) or []:
            ne = normalise_email(e)
            if not ne:
                continue
            exists = (await ctx.session.execute(select(EmailAddress.id).where(EmailAddress.email_normalised == ne))).first()
            if exists is None:
                obs.append(EmailObservation(email=ne, method=r.doc_type if r.doc_type != "html" else "text",
                                            page_url=r.source_url or f"urn:sha256:{r.file_sha256}", context=r.locator,
                                            name=f.get("name"), title=f.get("role"), phone=f.get("phone")))
    if not obs:
        return StepResult("none", detail={"documents": len(recs)})
    made = await persist_observations(ctx.session, ctx.org.id, None, obs, run_id=ctx.run_id, source_adapter="gap_recovery")
    return StepResult("found", emails=len(made), detail={"documents": len(recs)})


async def step_role_address_candidates(ctx: StepContext) -> StepResult:
    """Controlled role-mailbox research: few candidates, confirmed domain only, ALWAYS flagged ``role_speculative``."""
    dom = (await ctx.session.execute(select(OrganisationDomain).where(
        OrganisationDomain.organisation_id == ctx.org.id, OrganisationDomain.verified_match_status.in_(("confirmed", "high")),
        OrganisationDomain.parked.is_(False)).order_by(OrganisationDomain.confidence.desc()).limit(1))).scalar_one_or_none()
    if dom is None:
        return StepResult("unavailable", detail={"reason": "no confirmed domain"})
    existing = (await ctx.session.execute(select(EmailAddress.local_part).where(
        EmailAddress.organisation_id == ctx.org.id, EmailAddress.registrable_domain == dom.registrable_domain))).scalars().all()
    limit = max(0, min(ctx.budget.role_candidates, HARD_ROLE_LIMIT))
    cands = generate_role_candidates(dom.domain, ctx.org.industry, {"published_locals": list(existing)}, limit=limit)
    made = 0
    now = datetime.now(UTC)
    for c in cands:
        ne = normalise_email(c.email)
        if not ne:
            continue
        ins = pg_insert(EmailAddress).values(
            organisation_id=ctx.org.id, email_original=c.email, email_normalised=ne, local_part=c.local_part, domain=c.domain,
            registrable_domain=dom.registrable_domain, source_type="permutation", origin="role_speculative", role_type="generic",
            confidence=c.confidence_hint, needs_review=True, first_seen_at=now, last_seen_at=now).on_conflict_do_nothing(
            index_elements=["email_normalised"]).returning(EmailAddress.id)
        eid = (await ctx.session.execute(ins)).scalar_one_or_none()
        if eid is None:
            continue
        made += 1
        ev = pg_insert(EvidenceSource).values(source_adapter="gap_recovery", source_url=f"permutation:{dom.domain}", evidence_type="permutation",
                                              content_hash=ne, source_reliability=0.1, parser_version="gap-1")
        ev_id = (await ctx.session.execute(ev.on_conflict_do_update(constraint="uq_evidence_identity", set_={"observed_at": now})
                                            .returning(EvidenceSource.id))).scalar_one()
        await ctx.session.execute(pg_insert(ContactEvidence).values(
            email_id=eid, organisation_id=ctx.org.id, evidence_id=ev_id, run_id=ctx.run_id, extraction_method="permutation",
            context={"pattern": c.pattern, "note": c.note, "origin": "role_speculative"}).on_conflict_do_nothing())
    return StepResult("found" if made else "none", speculative=made)


DEFAULT_STEPS: dict[str, StepImpl] = {
    "recheck_imported_website": step_recheck_imported_website,
    "resolve_bulk_candidate_domains": step_resolve_bulk_candidate_domains,
    "search_provider": step_search_provider,
    "common_crawl_lookup": step_common_crawl_lookup,
    "register_corroboration": step_register_corroboration,
    "document_extraction": step_document_extraction,
    "role_address_candidates": step_role_address_candidates,
}


# --------------------------------------------------------------------------- orchestration
@dataclass
class GapResult:
    org_id: int
    ran: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    stop_reason: str = ""
    complete: bool = False  # website AND published e-mail present
    website_found: bool = False
    email_found: bool = False
    exhausted: bool = False
    needs_review: bool = False
    requests_used: int = 0
    searches_used: int = 0
    completeness: Completeness | None = None


async def run_gap_recovery(
    session: AsyncSession, org_id: int, *, run_id: int | None, budget: GapBudget | int | None = None,
    available_adapters: set[str] | list[str] | None = None, policy: str = "stop_if_no_website", ttl_days: int = 30,
    steps: dict[str, StepImpl] | None = None, deps: GapDeps | None = None, tracker: YieldTracker | None = None,
    min_success_rate: float = 0.05, min_sample: int = 20, allow_inferred: bool = False, ladder: list[str] | None = None,
    now: datetime | None = None,
) -> GapResult:
    """Run the ladder for one organisation. Deterministic; every cap is hard. See the module docstring."""
    if policy not in POLICIES:
        raise ValueError(f"unknown fallback policy {policy!r}")
    now = now or datetime.now(UTC)
    org = await session.get(Organisation, org_id)
    if org is None:
        raise ValueError(f"organisation {org_id} not found")
    bud = budget if isinstance(budget, GapBudget) else GapBudget(max_requests=int(budget) if budget is not None else 40)
    impls = {**DEFAULT_STEPS, **(steps or {})}
    avail = set(available_adapters) if available_adapters is not None else None
    order = [s for s in (ladder or STEP_ORDER) if s in impls][:bud.max_steps]
    tracker = tracker if tracker is not None else await YieldTracker.load(session, run_id)
    out = GapResult(org_id)

    row = (await session.execute(select(OrganisationCompleteness).where(OrganisationCompleteness.organisation_id == org_id)
                                 .execution_options(populate_existing=True))).scalar_one_or_none()
    if row is None:
        await session.execute(pg_insert(OrganisationCompleteness).values(organisation_id=org_id).on_conflict_do_nothing())
        row = (await session.execute(select(OrganisationCompleteness).where(OrganisationCompleteness.organisation_id == org_id)
                                     .execution_options(populate_existing=True))).scalar_one()
    log: list[dict[str, Any]] = list(row.gap_log or [])
    tried: list[str] = list(row.gap_steps_tried or [])
    ctx = StepContext(session, org, run_id, bud, deps or GapDeps(), allow_inferred)

    def recently_done(step: str) -> bool:
        for e in reversed(log):
            if e.get("step") == step and e.get("outcome") not in ("error", "unavailable"):
                try:
                    return datetime.fromisoformat(e["at"]) >= now - timedelta(days=ttl_days)
                except (KeyError, ValueError):
                    return False
        return False

    settled = 0
    stopped_early = False
    website, email = await _state(session, org_id)
    for step in order:
        if website and email:
            out.stop_reason = "website_and_email_found"
            stopped_early = True
            break
        if policy == "stop_if_no_website" and step not in WEBSITE_STEPS and not website:
            out.stop_reason, stopped_early = "no_website_policy", True
            out.exhausted = True  # the policy ended the permitted research after the website steps
            break
        if bud.requests_left <= 0:
            out.stop_reason, stopped_early = "request_budget_exhausted", True
            break
        if step == "search_provider" and (bud.search_left <= 0):
            out.skipped.append({"step": step, "reason": "search_budget"})
            settled += 1
            continue
        need = REQUIRES_ADAPTER.get(step)
        if need and (avail is None or not (set(need) & avail)) and step not in (steps or {}):
            out.skipped.append({"step": step, "reason": "adapter_unavailable"})
            settled += 1
            continue
        if step == "role_address_candidates" and (not allow_inferred or not website):
            out.skipped.append({"step": step, "reason": "inferred_not_allowed" if not allow_inferred else "no_confirmed_domain"})
            settled += 1
            continue
        if recently_done(step):
            out.skipped.append({"step": step, "reason": "ttl"})
            settled += 1
            continue
        if tracker.should_skip(step, min_sample=min_sample, min_rate=min_success_rate):
            out.skipped.append({"step": step, "reason": "low_yield"})
            settled += 1
            continue
        try:
            async with session.begin_nested():
                res = await impls[step](ctx)
        except Exception as e:  # a failing step must not abort the ladder
            res = StepResult("error", detail={"error": f"{type(e).__name__}: {e}"[:300]})
        bud.requests_used += res.requests
        bud.search_used += res.searches
        bud.steps_used += 1
        out.ran.append({"step": step, "outcome": res.outcome, "website": res.website, "emails": res.emails, "domains": res.domains,
                        "speculative": res.speculative, "requests": res.requests})
        if res.outcome == "unavailable":
            out.skipped.append({"step": step, "reason": "unavailable:" + str(res.detail.get("reason", ""))})
            settled += 1
            continue
        if res.outcome != "error":
            tracker.record(step, res.useful)
            settled += 1
            if step not in tried:
                tried.append(step)
        log.append({"step": step, "at": now.isoformat(), "outcome": res.outcome,
                    "yield": {"website": res.website, "emails": res.emails, "domains": res.domains, "speculative": res.speculative},
                    "requests": res.requests, "searches": res.searches})
        website, email = await _state(session, org_id)
    else:
        out.stop_reason = "ladder_exhausted"

    out.website_found, out.email_found = website, email
    out.complete = website and email
    if out.stop_reason == "ladder_exhausted" and not stopped_early:
        out.exhausted = settled >= len(order) and not out.complete
    if out.complete:
        out.exhausted = False
    out.requests_used, out.searches_used = bud.requests_used, bud.search_used
    out.needs_review = bool(policy == "flag_for_review" and out.exhausted and not website) or bool(row.needs_review)

    await session.execute(
        pg_insert(OrganisationCompleteness).values(organisation_id=org_id).on_conflict_do_update(
            index_elements=["organisation_id"],
            set_={"gap_steps_tried": tried, "gap_log": log[-100:],
                  "gap_exhausted_at": now if out.exhausted else (None if out.complete else row.gap_exhausted_at),
                  "needs_review": out.needs_review}))
    await tracker.save(session, run_id)
    res_map = await refresh_completeness(session, [org_id], now=now)
    out.completeness = res_map[org_id]
    return out
