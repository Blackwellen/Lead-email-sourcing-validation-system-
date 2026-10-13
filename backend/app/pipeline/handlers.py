"""Stage handlers wiring sources -> identity -> crawl -> verification -> yield counting."""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select, text

from .. import jobqueue as jq
from .. import runs
from ..config import get_settings
from ..crawler.crawl import CrawlOptions, crawl_domain
from ..models import Organisation, OrganisationDomain, RunOrganisation
from ..run_config import RunConfig
from ..sources.base import DiscoveryQuery, NotPermitted, get_adapter, record_adapter_run
from ..sources.persist import upsert_business
from ..worker import JobContext, handler
from . import runtime
from .identity import Candidate, candidate_domains_from_name, resolve_organisation_domain
from .ingest import persist_observations

log = logging.getLogger("pipeline")

VERIFY_RESOLVER = None  # tests inject a fake DNS resolver; None = real DNS
MAX_QUEUE_DEPTH = 3000  # backpressure: discovery pauses when this many jobs wait for the run
CHECKPOINT_EVERY = 200


async def _cfg(ctx: JobContext) -> tuple[RunConfig, int]:
    run_id = ctx.job.run_id
    if run_id is None:
        raise jq.PermanentError("job has no run", "no_run")
    row = (await ctx.session.execute(text("SELECT configuration_snapshot FROM sourcing_runs WHERE id=:r"), {"r": run_id})).scalar_one()
    return RunConfig.model_validate(row), run_id


async def _queue_depth(ctx: JobContext, run_id: int) -> int:
    return (await ctx.session.execute(text("SELECT count(*) FROM sourcing_jobs WHERE run_id=:r AND state IN ('queued','leased') AND stage<>'discover'"), {"r": run_id})).scalar_one()


async def build_query(ctx: JobContext, cfg: RunConfig) -> DiscoveryQuery:
    from ..taxonomy import expand_category_terms

    postcodes = list(cfg.postcodes)
    localities = list(cfg.localities)
    if cfg.countries == ["GB"] and (cfg.regions or localities):
        try:
            from ..geo import plan_coverage

            plan = plan_coverage({"regions": cfg.regions, "localities": localities, "postcodes": postcodes})
            postcodes = list(dict.fromkeys([*postcodes, *plan.postcode_areas, *plan.postcode_selectors]))
            localities = list(dict.fromkeys([*localities, *plan.localities]))
        except Exception:  # planning is an optimisation, never a blocker
            log.exception("coverage planning failed; using raw selection")
    q = DiscoveryQuery(countries=cfg.countries, regions=cfg.regions, localities=localities, postcodes=postcodes,
                       keywords=list(cfg.keywords), exclusions=list(cfg.exclusions), active_only=cfg.active_only)
    if cfg.category_ids:
        q = q.with_terms(await expand_category_terms(ctx.session, cfg.category_ids))
    return q


@handler("discover")
async def discover(ctx: JobContext) -> None:
    cfg, run_id = await _cfg(ctx)
    p = ctx.job.payload
    adapter_id = p["adapter_id"]
    params = {**cfg.source_params.get(adapter_id, {}), **p.get("params", {})}
    try:
        adapter = get_adapter(adapter_id, params or None)
    except KeyError:
        raise jq.PermanentError(f"unknown adapter {adapter_id}", "unknown_adapter") from None
    query = await build_query(ctx, cfg)
    cp = ctx.job.checkpoint.get("cp")
    seen = int(ctx.job.checkpoint.get("seen", 0))
    created_n = int(ctx.job.checkpoint.get("created", 0))
    category_id = cfg.category_ids[0] if cfg.category_ids else None
    n_batch = 0
    try:
        async for rec, new_cp in adapter.iter_businesses(query, cp):
            if not rec.country_code:
                rec.country_code = cfg.countries[0]
            org_id, created = await upsert_business(ctx.session, rec, run_id=run_id, category_id=category_id)
            seen += 1
            created_n += int(created)
            await ctx.session.execute(
                text("INSERT INTO run_organisations (run_id, organisation_id, stage) VALUES (:r,:o,'discovered') ON CONFLICT DO NOTHING"),
                {"r": run_id, "o": org_id},
            )
            if created or cfg.refresh_existing:
                await jq.enqueue(ctx.session, run_id=run_id, stage="resolve_domain", dedupe_key=f"org:{org_id}", payload={"organisation_id": org_id}, priority=50)
            cp = new_cp
            n_batch += 1
            if n_batch >= CHECKPOINT_EVERY:
                n_batch = 0
                await ctx.session.commit()
                await ctx.checkpoint({"cp": cp, "seen": seen, "created": created_n})
                if not await ctx.run_active():
                    raise jq.Requeue(3.0)
                if await _queue_depth(ctx, run_id) > MAX_QUEUE_DEPTH:
                    raise jq.Requeue(5.0)  # backpressure: downstream is saturated
    except NotPermitted as e:
        raise jq.PermanentError(str(e), "not_permitted") from e
    except jq.Requeue:
        await ctx.session.commit()
        raise
    await ctx.checkpoint({"cp": cp, "seen": seen, "created": created_n, "finished": True})
    await record_adapter_run(ctx.session, adapter_id, ok=True, recovered=created_n)
    log.info("discover finished", extra={"run_id": run_id, "stage": "discover"})


@handler("import_domain")
async def import_domain(ctx: JobContext) -> None:
    """Operator-supplied domain list (Scenario C): the operator asserts the domain; evidence says so."""
    cfg, run_id = await _cfg(ctx)
    dom = ctx.job.payload["domain"].lower().strip()
    from ..crawler.urls import registrable_domain
    from ..sources.base import BusinessRecord

    reg = registrable_domain(dom)
    if not reg:
        raise jq.PermanentError(f"invalid domain {dom}", "bad_domain")
    rec = BusinessRecord(source_ref=f"import:{reg}", legal_name=reg, country_code=cfg.countries[0], website=f"https://{dom}",
                         adapter_id="csv_import", source_url=None, raw={"imported_domain": True})
    org_id, _ = await upsert_business(ctx.session, rec, run_id=run_id)
    await ctx.session.execute(text("INSERT INTO run_organisations (run_id, organisation_id, stage) VALUES (:r,:o,'discovered') ON CONFLICT DO NOTHING"), {"r": run_id, "o": org_id})
    await ctx.session.execute(
        text("""UPDATE organisation_domains SET verified_match_status='high', confidence=0.8,
                evidence = evidence || CAST(:ev AS jsonb), last_checked_at=now()
                WHERE organisation_id=:o AND registrable_domain=:d AND NOT manual_override"""),
        {"o": org_id, "d": reg, "ev": '[{"type":"operator_supplied","detail":"domain supplied by operator import","weight":0.8}]'},
    )
    await jq.enqueue(ctx.session, run_id=run_id, stage="crawl", dedupe_key=f"org:{org_id}:{reg}", payload={"organisation_id": org_id, "domain": dom}, priority=40)


def _crawl_options(cfg: RunConfig, org: Organisation | None = None) -> CrawlOptions:
    return CrawlOptions(max_pages=cfg.crawl_max_pages, keywords=tuple(cfg.keywords), company_hint=(org.legal_name if org else None),
                        allow_js_render=cfg.performance.browser_workers > 0)


async def _persist_and_queue(ctx: JobContext, cfg: RunConfig, run_id: int, org_id: int, dom_row: OrganisationDomain, crawl) -> int:
    pairs = await persist_observations(ctx.session, org_id, dom_row, crawl.observations, run_id=run_id)
    n = 0
    for email_id, _created in pairs:
        await jq.enqueue(ctx.session, run_id=run_id, stage="verify", dedupe_key=f"email:{email_id}", payload={"email_id": email_id}, priority=30)
        n += 1
    await ctx.session.execute(text("UPDATE organisation_domains SET last_crawled_at=now() WHERE id=:i"), {"i": dom_row.id})
    await ctx.session.execute(text("UPDATE organisations SET last_checked_at=now() WHERE id=:o"), {"o": org_id})
    await ctx.session.execute(text("UPDATE run_organisations SET stage='crawled' WHERE run_id=:r AND organisation_id=:o"), {"r": run_id, "o": org_id})
    return n


@handler("resolve_domain")
async def resolve_domain(ctx: JobContext) -> None:
    cfg, run_id = await _cfg(ctx)
    org_id = ctx.job.payload["organisation_id"]
    org = await ctx.session.get(Organisation, org_id)
    if org is None:
        raise jq.PermanentError("organisation vanished", "gone")
    fetcher, limiter, robots = await runtime.get_net(cfg.performance.per_host_concurrency)
    existing = (await ctx.session.execute(select(OrganisationDomain).where(OrganisationDomain.organisation_id == org_id))).scalars().all()
    cands: list[Candidate | str] = [Candidate(d.domain, source="dataset") for d in existing if d.verified_match_status != "mismatch"]
    if not cands:  # guessed domains are probe candidates only; identity scoring decides (capped at 'review')
        cands = list(candidate_domains_from_name(org.legal_name or org.trading_name, country_code=org.country_code, max_candidates=4))
    if not cands:
        await ctx.session.execute(text("UPDATE run_organisations SET stage='no_website' WHERE run_id=:r AND organisation_id=:o"), {"r": run_id, "o": org_id})
        return
    res = await resolve_organisation_domain(ctx.session, fetcher, org_id, cands, crawl_options=_crawl_options(cfg, org), limiter=limiter, robots=robots, max_candidates=5)
    best = res.best
    if best is not None and best.verified_match_status in ("confirmed", "high") and best.domain in res.crawls:
        await _persist_and_queue(ctx, cfg, run_id, org_id, best, res.crawls[best.domain])
    else:
        await ctx.session.execute(text("UPDATE run_organisations SET stage='domain_unconfirmed' WHERE run_id=:r AND organisation_id=:o"), {"r": run_id, "o": org_id})


@handler("crawl")
async def crawl(ctx: JobContext) -> None:
    cfg, run_id = await _cfg(ctx)
    p = ctx.job.payload
    org_id = p["organisation_id"]
    org = await ctx.session.get(Organisation, org_id)
    dom_row = None
    if p.get("domain"):
        from ..crawler.urls import registrable_domain

        reg = registrable_domain(p["domain"])
        dom_row = (await ctx.session.execute(select(OrganisationDomain).where(OrganisationDomain.organisation_id == org_id, OrganisationDomain.registrable_domain == reg))).scalars().first()
    if dom_row is None:
        dom_row = (await ctx.session.execute(select(OrganisationDomain).where(OrganisationDomain.organisation_id == org_id, OrganisationDomain.verified_match_status.in_(("confirmed", "high"))).limit(1))).scalars().first()
    if dom_row is None:
        raise jq.PermanentError("no confirmed domain to crawl", "no_domain")
    fetcher, limiter, robots = await runtime.get_net(cfg.performance.per_host_concurrency)
    result = await crawl_domain(fetcher, dom_row.website_url or dom_row.domain, options=_crawl_options(cfg, org), limiter=limiter, robots=robots)
    if result.health and getattr(result.health, "error_code", None) in ("dns_failure", "tls_failure") and not result.observations:
        raise jq.RetryableError(f"domain unhealthy: {result.health.error_code}", "domain_unhealthy", delay_s=300)
    await _persist_and_queue(ctx, cfg, run_id, org_id, dom_row, result)


@handler("verify")
async def verify(ctx: JobContext) -> None:
    from ..verify.pipeline import VerifyOptions, verify_email

    payload = ctx.job.payload
    email_id = payload["email_id"]
    run_id = ctx.job.run_id
    mode, options = payload.get("mode"), None
    if run_id is not None:
        cfg, _ = await _cfg(ctx)
        mode = mode or cfg.verification.mode
        if mode == "custom" and cfg.verification.custom:
            options = VerifyOptions(**cfg.verification.custom)
    mode = mode or "fast"
    if payload.get("options"):
        options = VerifyOptions(**payload["options"])
    out = await verify_email(ctx.session, email_id, mode=mode, options=options, settings=get_settings(), resolver=VERIFY_RESOLVER)
    if run_id is not None:
        counted = await runs.record_yield(ctx.session, run_id, email_id)
        if counted:
            log.info("yield counted", extra={"run_id": run_id})
    batch_id = payload.get("batch_id")
    if batch_id:
        await ctx.session.execute(text("UPDATE verification_batches SET done=done+1, status=CASE WHEN done+1>=total THEN 'done' ELSE status END WHERE id=:b"), {"b": batch_id})


@handler("revalidate")
async def revalidate(ctx: JobContext) -> None:
    await verify(ctx)
