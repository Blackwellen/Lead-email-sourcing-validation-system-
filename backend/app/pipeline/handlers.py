"""Stage handlers wiring sources -> company matching -> domain discovery -> crawl -> verification -> yield counting.

``discover`` persists source records through the resolution engine (duplicates are matched, not re-created), measures
what each source really delivered (``source_effectiveness``), enforces the run's per-source / per-category / overall work
quotas and the source's rate policy, and only queues website work for businesses that need it. ``resolve_domain`` / ``crawl``
run the rest of the strategy chain (see ``flow.py``). The additional discovery stages live in ``discovery_stages.py``.
"""
from __future__ import annotations

import logging
import time

from sqlalchemy import select, text

from .. import jobqueue as jq
from .. import runs
from ..config import get_settings
from ..crawler.crawl import CrawlOptions, CrawlResult, crawl_domain
from ..discovery import strategy as strat
from ..discovery.effectiveness import record_effect
from ..models import Organisation, OrganisationDomain
from ..run_config import RunConfig
from ..sources.base import DiscoveryQuery, NotPermitted, get_adapter, record_adapter_run
from ..sources.persist import upsert_business
from ..worker import JobContext, handler
from . import flow, runtime
from .identity import Candidate, candidate_domains_from_name, resolve_organisation_domain
from .ingest import persist_observations
from .measure import measured

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


async def _plan(ctx: JobContext) -> tuple[flow.RunPlan, int]:
    run_id = ctx.job.run_id
    if run_id is None:
        raise jq.PermanentError("job has no run", "no_run")
    return await flow.load_plan(ctx.session, run_id), run_id


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


# =========================================================================== discover
@handler("discover")
async def discover(ctx: JobContext) -> None:
    rp, run_id = await _plan(ctx)
    cfg, plan = rp.cfg, rp.plan
    p = ctx.job.payload
    adapter_id = p["adapter_id"]
    from ..models import SourceAdapterState
    from ..sources.secret_config import open_config

    stored = await ctx.session.get(SourceAdapterState, adapter_id)  # operator configuration (Source Explorer); secrets opened here
    params = {**open_config(stored.config if stored else None), **cfg.source_params.get(adapter_id, {}), **p.get("params", {})}
    try:
        adapter = get_adapter(adapter_id, params or None)
    except KeyError:
        raise jq.PermanentError(f"unknown adapter {adapter_id}", "unknown_adapter") from None
    info = adapter.info
    if not rp.legacy and not strat.adapter_allowed(cfg.discovery, adapter_id, info.source_type):
        raise jq.PermanentError(f"source {adapter_id} belongs to category '{strat.category_of(adapter_id, info.source_type)}' "
                                f"which this run did not select", "category_not_selected")
    # ---- hard constraints: concurrency across ALL workers, and the adapter's request-rate policy (per process)
    if await flow.other_leased_discovers(ctx.session, adapter_id, ctx.job.id) >= max(1, info.rate_policy.concurrency):
        raise jq.Requeue(2.0)
    gate = runtime.adapter_gate(info)
    if not gate.try_acquire():
        raise jq.Requeue(max(1.0, min(30.0, gate.time_until_available())))
    holding = True
    quota = await flow.QuotaGuard.load(ctx.session, rp, run_id, adapter_id, info.source_type)
    cat = strat.category_of(adapter_id, info.source_type)
    query = await build_query(ctx, cfg)
    cp = ctx.job.checkpoint.get("cp")
    seen = int(ctx.job.checkpoint.get("seen", 0))
    created_n = int(ctx.job.checkpoint.get("created", 0))
    category_id = cfg.category_ids[0] if cfg.category_ids else None
    stop_reason: str | None = None
    batch = {"records_seen": 0, "unique_businesses": 0, "duplicates": 0}
    batch_limit = CHECKPOINT_EVERY

    async def flush() -> None:
        deltas = {k: v for k, v in batch.items() if v}
        if deltas:
            await record_effect(ctx.session, adapter_id, run_id, **deltas)
        for k in batch:
            batch[k] = 0

    try:
        async with measured(ctx, adapter_id) as m:
            try:
                rem = quota.remaining()
                if rem is not None:
                    batch_limit = max(1, min(CHECKPOINT_EVERY, rem))
                n_batch = 0
                async for rec, new_cp in adapter.iter_businesses(query, cp):
                    if not quota.take():
                        stop_reason = quota.reason
                        break
                    if not rec.country_code:
                        rec.country_code = cfg.countries[0]
                    out = await flow.persist_discovered(ctx.session, rec, adapter_id, run_id, category_id)
                    seen += 1
                    batch["records_seen"] += 1
                    if out.created:
                        created_n += 1
                        batch["unique_businesses"] += 1
                    else:
                        batch["duplicates"] += 1
                    # (resolving_upsert_ex already joined the organisation to the run as 'discovered'; a matched record is joined too)
                    await _queue_website_work(ctx, rp, run_id, adapter_id, out)
                    cp = new_cp
                    n_batch += 1
                    if n_batch >= batch_limit:
                        n_batch = 0
                        await flush()
                        await ctx.session.commit()
                        await ctx.checkpoint({"cp": cp, "seen": seen, "created": created_n})
                        if not await ctx.run_active():
                            raise jq.Requeue(3.0)
                        if await _queue_depth(ctx, run_id) > MAX_QUEUE_DEPTH:
                            raise jq.Requeue(5.0)  # backpressure: downstream is saturated
                        await quota.refresh(ctx.session, run_id, adapter_id, cat)
                        rem = quota.remaining()
                        batch_limit = CHECKPOINT_EVERY if rem is None else max(1, min(CHECKPOINT_EVERY, rem))
                        gate.release()
                        holding = False
                        if not gate.try_acquire():  # next batch must fit the adapter's request-rate policy
                            raise jq.Requeue(max(1.0, min(30.0, gate.time_until_available())))
                        holding = True
            except NotPermitted as e:
                raise jq.PermanentError(str(e), "not_permitted") from e
            except jq.Requeue:
                await flush()
                await ctx.session.commit()
                raise
            await flush()
            await ctx.checkpoint({"cp": cp, "seen": seen, "created": created_n, "finished": True, **({"stopped": stop_reason} if stop_reason else {})})
            await record_adapter_run(ctx.session, adapter_id, ok=True, recovered=created_n)
            if stop_reason:
                log.info("discover stopped by %s", stop_reason, extra={"run_id": run_id, "stage": "discover"})
    finally:
        if holding:
            gate.release()
    log.info("discover finished", extra={"run_id": run_id, "stage": "discover"})


async def _queue_website_work(ctx: JobContext, rp: flow.RunPlan, run_id: int, adapter_id: str, out: flow.Persisted) -> None:
    """Strategy decision per record: queue ``resolve_domain`` only for businesses that need website research."""
    if not rp.crawl_websites:
        return
    refresh = rp.plan.refresh_existing
    if out.created:
        go = rp.plan.include_new
    elif refresh:
        go = not await flow.skip_decision(ctx, rp, out.org_id, True)  # merged / already processed in this run -> no repeat
    else:  # a known business: only if nobody has ever researched it (duplicates must not cost network)
        go = False
        if rp.plan.include_new:
            checked = (await ctx.session.execute(text("SELECT last_checked_at FROM organisations WHERE id=:o"), {"o": out.org_id})).scalar_one_or_none()
            go = checked is None and not await flow.skip_decision(ctx, rp, out.org_id, False)
    if go:
        await jq.enqueue(ctx.session, run_id=run_id, stage="resolve_domain", dedupe_key=f"org:{out.org_id}",
                         payload={"organisation_id": out.org_id, "source_adapter": adapter_id}, priority=50)


# =========================================================================== import_domain
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
    await jq.enqueue(ctx.session, run_id=run_id, stage="crawl", dedupe_key=f"org:{org_id}:{reg}",
                     payload={"organisation_id": org_id, "domain": dom, "source_adapter": "csv_import"}, priority=40)


# =========================================================================== crawl helpers
def _crawl_options(cfg: RunConfig, org: Organisation | None = None, rp: flow.RunPlan | None = None) -> CrawlOptions:
    kw: dict = {}
    if rp is not None and not rp.legacy:
        kw.update(max_pages=rp.plan.crawl["max_pages"], max_depth=rp.plan.crawl["max_depth"], time_budget_s=float(rp.plan.crawl["time_budget_s"]))
        kw["capture_html"] = rp.signals_on
    else:
        kw["max_pages"] = cfg.crawl_max_pages
    return CrawlOptions(keywords=tuple(cfg.keywords), company_hint=(org.legal_name if org else None),
                        allow_js_render=cfg.performance.browser_workers > 0, **kw)


async def _process_crawl(ctx: JobContext, rp: flow.RunPlan, org: Organisation, dom_row: OrganisationDomain, crawl: CrawlResult, *,
                         source: str | None, src_adapter: str | None, m, net) -> flow.CrawlOutcome:
    """Everything that follows a live crawl of a confirmed official site, in strategy order."""
    s, run_id = ctx.session, ctx.job.run_id
    out = flow.CrawlOutcome()
    fetcher, limiter, robots = net
    pairs = await persist_observations(s, org.id, dom_row, crawl.observations, run_id=run_id)
    out.emails = len(pairs)
    if crawl.observations:  # a live observation confirms archived (Common Crawl) contacts of this organisation
        from ..sources import cc_persist

        if await cc_persist.unconfirmed_emails(s, org.id):
            out.confirmed_historical = len(await cc_persist.confirm_against_live(s, org.id, dom_row, crawl.observations, run_id=run_id))
    for email_id, _created in pairs:
        out.verify_jobs += int(await flow.enqueue_verify(s, run_id, email_id, source_adapter=src_adapter))
    new_ids = [i for i, c in pairs if c]
    if new_ids:
        n_pub = (await s.execute(text("SELECT count(*) FROM email_addresses WHERE id = ANY(:i) AND origin IN ('published','corroborated')"), {"i": new_ids})).scalar_one()
        m.add(published_emails=int(n_pub))
    await s.execute(text("UPDATE organisation_domains SET last_crawled_at=now() WHERE id=:i"), {"i": dom_row.id})
    await s.execute(text("UPDATE organisations SET last_checked_at=now() WHERE id=:o"), {"o": org.id})
    await flow.set_stage(s, run_id, org.id, "crawled")
    # --- business signals from the pages we already fetched (no refetch)
    if rp.signals_on and crawl.captured_html:
        from ..discovery.signals import detect_and_record

        sg = rp.plan.signals
        for url, html in crawl.captured_html:
            for _id, created in await detect_and_record(s, org, url, html, source_adapter="website_signals",
                                                        freshness_days=sg["freshness_days"], types=sg["types"] or None):
                out.signals += int(created)
    # --- directory / register corroboration (database only)
    if rp.corroboration_on:
        out.corroborated = len(await flow.corroborate(s, org.id, run_id))
    # --- public documents linked from the site
    if rp.documents_on:
        d = await flow.extract_documents(ctx, rp, org, dom_row, crawl, fetcher, limiter, robots)
        out.documents = d["documents"]
        m.add(http_requests=d["requests"])
    # --- controlled permutation research (opt-in, confirmed domain only, never published)
    if rp.permutations_on and dom_row.verified_match_status in ("confirmed", "high"):
        stage = rp.plan.stage("controlled_permutation_research")
        lim = int((stage.params if stage else {}).get("max_role_candidates", 3))
        out.speculative = len(await flow.research_permutations(s, org, dom_row, run_id, role_limit=lim))
    await flow.enqueue_unvalidated_verifies(s, run_id, org.id, source_adapter=src_adapter)
    await flow.enqueue_followups(ctx, rp, org.id, source=source, source_adapter=src_adapter, domain=dom_row.domain)
    if not pairs and not await _has_published(s, org.id):
        await flow.enqueue_gap(ctx, rp, org.id, "no_contact_found", src_adapter)
    await flow.finish_org(ctx, org.id)
    return out


async def _has_published(session, org_id: int) -> bool:
    return (await session.execute(text("SELECT 1 FROM email_addresses WHERE organisation_id=:o AND origin IN ('published','corroborated') LIMIT 1"), {"o": org_id})).first() is not None


# =========================================================================== resolve_domain
@handler("resolve_domain")
async def resolve_domain(ctx: JobContext) -> None:
    rp, run_id = await _plan(ctx)
    cfg = rp.cfg
    payload = ctx.job.payload
    org_id = payload["organisation_id"]
    org = await ctx.session.get(Organisation, org_id)
    if org is None:
        raise jq.PermanentError("organisation vanished", "gone")
    src_adapter = await flow.adapter_of_org(ctx.session, org_id, payload)
    async with measured(ctx, src_adapter) as m:
        if not rp.crawl_websites:
            return
        skip = await flow.skip_decision(ctx, rp, org_id, bool(payload.get("refresh")))
        if skip:
            await flow.set_stage(ctx.session, run_id, org_id, "skipped_" + (skip.reason or "dup")[:14])
            log.info("resolve_domain skipped (%s)", skip.reason, extra={"run_id": run_id})
            return
        net = await runtime.get_net(cfg.performance.per_host_concurrency)
        fetcher, limiter, robots = net
        existing = (await ctx.session.execute(select(OrganisationDomain).where(OrganisationDomain.organisation_id == org_id))).scalars().all()
        was_confirmed = any(d.verified_match_status in ("confirmed", "high") for d in existing)
        cands: list[Candidate | str] = [Candidate(d.domain, source="dataset") for d in existing if d.verified_match_status != "mismatch"]
        if not cands:  # guessed domains are probe candidates only; identity scoring decides (capped at 'review')
            cands = list(candidate_domains_from_name(org.legal_name or org.trading_name, country_code=org.country_code, max_candidates=4))
        if not cands:
            await flow.set_stage(ctx.session, run_id, org_id, "no_website")
            await flow.enqueue_gap(ctx, rp, org_id, "no_candidate_domain", src_adapter)
            await flow.finish_org(ctx, org_id)
            return
        max_c = int(rp.plan.stage("official_domain_discovery").params.get("max_candidates", 5)) if (not rp.legacy and rp.plan.stage("official_domain_discovery")) else 5
        res = await resolve_organisation_domain(ctx.session, fetcher, org_id, cands, crawl_options=_crawl_options(cfg, org, rp), limiter=limiter,
                                                robots=robots, max_candidates=max_c)
        m.add(http_requests=sum(c.stats.pages_attempted for c in res.crawls.values()))
        best = res.best
        if best is not None and best.verified_match_status in ("confirmed", "high") and best.domain in res.crawls:
            if not was_confirmed:
                m.add(confirmed_domains=1)
            await _process_crawl(ctx, rp, org, best, res.crawls[best.domain], source=payload.get("source"), src_adapter=src_adapter, m=m, net=net)
        else:
            await flow.set_stage(ctx.session, run_id, org_id, "domain_unconfirmed")
            await flow.enqueue_gap(ctx, rp, org_id, "domain_unconfirmed", src_adapter)
            await flow.finish_org(ctx, org_id)


# =========================================================================== crawl
@handler("crawl")
async def crawl(ctx: JobContext) -> None:
    rp, run_id = await _plan(ctx)
    cfg = rp.cfg
    p = ctx.job.payload
    org_id = p["organisation_id"]
    org = await ctx.session.get(Organisation, org_id)
    if org is None:
        raise jq.PermanentError("organisation vanished", "gone")
    dom_row = None
    if p.get("domain"):
        from ..crawler.urls import registrable_domain

        reg = registrable_domain(p["domain"])
        dom_row = (await ctx.session.execute(select(OrganisationDomain).where(OrganisationDomain.organisation_id == org_id, OrganisationDomain.registrable_domain == reg))).scalars().first()
    if dom_row is None:
        dom_row = (await ctx.session.execute(select(OrganisationDomain).where(OrganisationDomain.organisation_id == org_id, OrganisationDomain.verified_match_status.in_(("confirmed", "high"))).limit(1))).scalars().first()
    if dom_row is None:
        raise jq.PermanentError("no confirmed domain to crawl", "no_domain")
    src_adapter = await flow.adapter_of_org(ctx.session, org_id, p)
    async with measured(ctx, src_adapter) as m:
        skip = await flow.skip_decision(ctx, rp, org_id, bool(p.get("refresh")) or bool(p.get("source")))
        if skip and skip.reason == "merged_duplicate":
            await flow.set_stage(ctx.session, run_id, org_id, "skipped_merged")
            return
        net = await runtime.get_net(cfg.performance.per_host_concurrency)
        fetcher, limiter, robots = net
        result = await crawl_domain(fetcher, dom_row.website_url or dom_row.domain, options=_crawl_options(cfg, org, rp), limiter=limiter, robots=robots)
        m.add(http_requests=result.stats.pages_attempted)
        if result.health and getattr(result.health, "error_code", None) in ("dns_failure", "tls_failure") and not result.observations:
            raise jq.RetryableError(f"domain unhealthy: {result.health.error_code}", "domain_unhealthy", delay_s=300)
        await _process_crawl(ctx, rp, org, dom_row, result, source=p.get("source"), src_adapter=src_adapter, m=m, net=net)


# =========================================================================== verify / revalidate
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
        org_id = (await ctx.session.execute(text("SELECT organisation_id FROM email_addresses WHERE id=:e"), {"e": email_id})).scalar_one_or_none()
        if counted:
            log.info("yield counted", extra={"run_id": run_id})
            adapter = await flow.adapter_of_org(ctx.session, org_id, payload) if org_id else payload.get("source_adapter")
            if adapter:
                await record_effect(ctx.session, adapter, run_id, eligible_contacts=1)
        if org_id:
            await flow.finish_org(ctx, org_id)
    _ = out
    batch_id = payload.get("batch_id")
    if batch_id:
        await ctx.session.execute(text("UPDATE verification_batches SET done=done+1, status=CASE WHEN done+1>=total THEN 'done' ELSE status END WHERE id=:b"), {"b": batch_id})


@handler("revalidate")
async def revalidate(ctx: JobContext) -> None:
    await verify(ctx)


_ = time  # (kept for tests that monkeypatch handlers.time)
