"""Additional worker stages of the discovery layer.

``network_expand``  bounded business-network expansion around one organisation (only when ``discovery.network.enabled``)
``gap_recovery``    bounded recovery ladder for organisations without a confirmed website / published contact
``signals``         business-signal scan of one organisation's news / careers / events pages (the crawl records signals inline)
``change_check``    re-check the monitored pages of one domain (new / removed addresses, snapshots preserved)
``similar_expand``  queue the best lookalikes of a seed into a run
``monitor``         run the due ``MonitorSchedule`` rows
``cc_lookup``       Common Crawl contact-page lookup -> archived contacts persisted as HISTORICAL, UNCONFIRMED evidence only
``completeness``    explicit completeness refresh for organisations / a run
"""
from __future__ import annotations

import asyncio
import itertools
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select, text

from .. import jobqueue as jq
from ..crawler.urls import registrable_domain
from ..discovery.effectiveness import record_effect
from ..models import Organisation, OrganisationDomain
from ..worker import JobContext, handler
from . import flow, runtime
from .measure import measured

log = logging.getLogger("pipeline.stages")

# Test / operator hooks. None = the real implementation.
CC_HITS = None  # async (domain: str, params: dict, max_hits: int) -> list[UrlHit]
CC_FETCH = None  # async (hit: UrlHit) -> ArchivedPage
CC_MAX_FETCH = 5
CC_MAX_HITS = 400


async def _plan(ctx: JobContext) -> tuple[flow.RunPlan, int]:
    if ctx.job.run_id is None:
        raise jq.PermanentError("job has no run", "no_run")
    return await flow.load_plan(ctx.session, ctx.job.run_id), ctx.job.run_id


async def _confirmed_domain(session, org_id: int) -> OrganisationDomain | None:
    return (await session.execute(select(OrganisationDomain).where(
        OrganisationDomain.organisation_id == org_id, OrganisationDomain.verified_match_status.in_(("confirmed", "high")),
        OrganisationDomain.parked.is_(False)).order_by(OrganisationDomain.confidence.desc(), OrganisationDomain.id).limit(1))).scalar_one_or_none()


# =========================================================================== network_expand
@handler("network_expand")
async def network_expand(ctx: JobContext) -> None:
    from ..discovery.graph import expand_network

    rp, run_id = await _plan(ctx)
    p = ctx.job.payload
    org_id = int(p["organisation_id"])
    spec = rp.cfg.discovery.network
    if not spec.enabled and not p.get("force"):
        await ctx.checkpoint({"skipped": "network_expansion_disabled"})
        return
    depth = max(1, min(int(p.get("max_depth", spec.max_depth)), spec.max_depth if not p.get("force") else 3))
    budget = max(1, min(int(p.get("budget", spec.source_budget)), spec.source_budget if not p.get("force") else 1000))
    src = await flow.adapter_of_org(ctx.session, org_id, p)
    net = await runtime.get_net(rp.cfg.performance.per_host_concurrency)
    fetcher, limiter, robots = net
    async with measured(ctx, src) as m:
        res = await expand_network(ctx.session, fetcher, org_id, run_id=run_id, max_depth=depth, budget=budget, limiter=limiter, robots=robots)
        m.add(http_requests=res.fetched)
        n = len(res.new_orgs) + len(res.matched_orgs)
        if n:
            await record_effect(ctx.session, "network_expansion", run_id, records_seen=n, unique_businesses=len(res.new_orgs),
                                duplicates=len(res.matched_orgs), http_requests=res.fetched, jobs=1)
        await ctx.checkpoint({"new_orgs": res.new_orgs, "matched_orgs": res.matched_orgs, "edges": res.edges, "fetched": res.fetched,
                              "budget_exhausted": res.budget_exhausted, "jobs_enqueued": res.jobs_enqueued, "depth_reached": res.depth_reached})


# =========================================================================== gap_recovery
def _search_fn(rp: flow.RunPlan):
    """Search-provider callback for the ladder, only when the run selected a configured provider AND allows queries."""
    cfg = rp.cfg
    if cfg.discovery.search_budget <= 0:
        return None
    from ..sources.base import get_adapter
    from ..sources.search_providers import QuotaExhausted, resolve_official_websites

    for aid in cfg.sources:
        if aid in ("brave_search", "searxng", "manual_search_results"):
            prov = get_adapter(aid, cfg.source_params.get(aid))._provider()  # type: ignore[attr-defined]

            async def fn(org: Organisation, _prov=prov) -> list[str]:
                try:
                    hits = await _prov.search(" ".join(x for x in (org.legal_name or org.trading_name, org.city) if x), country=org.country_code, count=5)
                except QuotaExhausted:
                    return []
                cands, _dropped = resolve_official_websites(hits, business_name=org.legal_name or org.trading_name, locality=org.city, max_candidates=3)
                return [c.url for c in cands]
            return fn
    return None


def _cc_params(rp: flow.RunPlan) -> dict[str, Any]:
    sp = rp.cfg.source_params
    return dict(sp.get("commoncrawl_archive") or sp.get("common_crawl") or {})


def _cc_present_fn(rp: flow.RunPlan):
    if not rp.common_crawl_on:
        return None
    params = _cc_params(rp)

    async def fn(domains: list[str]) -> list[str]:
        present: list[str] = []
        for d in domains[:3]:
            try:
                hits = await (CC_HITS or _default_cc_hits)(registrable_domain(d) or d, params, 1)
            except Exception as e:  # an unavailable index never blocks the ladder
                log.info("common crawl presence check failed: %s", e)
                return present
            if hits:
                present.append(d)
        return present
    return fn


@handler("gap_recovery")
async def gap_recovery(ctx: JobContext) -> None:
    from ..discovery.gap_recovery import GapBudget, GapDeps, run_gap_recovery

    rp, run_id = await _plan(ctx)
    p = ctx.job.payload
    org_id = int(p["organisation_id"])
    if not rp.gap_on:
        return
    org = await ctx.session.get(Organisation, org_id)
    if org is None or org.merged_into_id is not None:
        return
    src = await flow.adapter_of_org(ctx.session, org_id, p)
    spec = rp.cfg.discovery
    net = await runtime.get_net(rp.cfg.performance.per_host_concurrency)
    fetcher, limiter, robots = net
    search_fn, cc_fn = _search_fn(rp), _cc_present_fn(rp)
    avail = set(rp.cfg.sources)
    if search_fn:
        avail.add("search")
    if cc_fn:
        avail |= {"common_crawl", "commoncrawl_index"}
    budget = GapBudget(max_requests=flow.GAP_REQUEST_BUDGET.get(spec.strategy, 40), max_search=spec.search_budget,
                       role_candidates=int((rp.plan.stage("controlled_permutation_research").params if rp.plan.stage("controlled_permutation_research") else {}).get("max_role_candidates", 3)))
    deps = GapDeps(fetcher=fetcher, limiter=limiter, robots=robots, search_fn=search_fn, common_crawl_fn=cc_fn)
    async with measured(ctx, src) as m:
        res = await run_gap_recovery(ctx.session, org_id, run_id=run_id, budget=budget, available_adapters=avail, policy=spec.fallback_research,
                                     deps=deps, allow_inferred=rp.plan.allow_inferred or rp.cfg.target.allow_inferred,
                                     ladder=list(rp.plan.gap_recovery.get("ladder") or []) or None)
        m.add(http_requests=res.requests_used, confirmed_domains=sum(int(r.get("website", 0)) for r in res.ran),
              published_emails=sum(int(r.get("emails", 0)) for r in res.ran))
        stage = "complete" if res.complete else ("gap_exhausted" if res.exhausted else ("gap_review" if res.needs_review else "gap_tried"))
        await flow.set_stage(ctx.session, run_id, org_id, stage)
        await flow.enqueue_unvalidated_verifies(ctx.session, run_id, org_id, source_adapter=src)
        if res.website_found:
            dom = await _confirmed_domain(ctx.session, org_id)
            await flow.enqueue_followups(ctx, rp, org_id, source=p.get("source"), source_adapter=src, domain=dom.domain if dom else None)
        await ctx.checkpoint({"stop_reason": res.stop_reason, "ran": res.ran, "skipped": res.skipped, "requests_used": res.requests_used,
                              "website_found": res.website_found, "email_found": res.email_found, "exhausted": res.exhausted})
        await flow.finish_org(ctx, org_id)


# =========================================================================== signals (explicit scan of one organisation)
@handler("signals")
async def signals_scan(ctx: JobContext) -> None:
    """On-demand scan (the normal crawl records signals inline from the pages it already fetched, without refetching)."""
    from ..discovery import signals as sig

    rp, run_id = await _plan(ctx)
    org_id = int(ctx.job.payload["organisation_id"])
    dom = await _confirmed_domain(ctx.session, org_id)
    if dom is None:
        raise jq.PermanentError("no confirmed domain to scan", "no_domain")
    fetcher, limiter, robots = await runtime.get_net(rp.cfg.performance.per_host_concurrency)
    sg = rp.cfg.discovery.signals
    mon = SimpleNamespace(config={"limit": 1, "freshness_days": sg.freshness_days, "types": sg.types or None},
                          last_result={"last_org_id": org_id - 1}, frequency="weekly")
    src = await flow.adapter_of_org(ctx.session, org_id, ctx.job.payload)
    async with measured(ctx, src) as m:
        stats = await sig.run_monitor(ctx.session, fetcher, mon, limiter=limiter, robots=robots)
        m.add(http_requests=stats.get("pages", 0))
        await ctx.checkpoint({"stats": stats})


# =========================================================================== change_check
@handler("change_check")
async def change_check(ctx: JobContext) -> None:
    from ..discovery.changes import check_site_for_changes

    dom = await ctx.session.get(OrganisationDomain, int(ctx.job.payload["domain_id"]))
    if dom is None:
        raise jq.PermanentError("domain vanished", "gone")
    fetcher, limiter, robots = await runtime.get_net()
    src = await flow.adapter_of_org(ctx.session, dom.organisation_id, ctx.job.payload)
    async with measured(ctx, src) as m:
        rep = await check_site_for_changes(ctx.session, fetcher, dom, run_id=ctx.job.run_id, limiter=limiter, robots=robots,
                                           max_pages=int(ctx.job.payload.get("max_pages", 6)))
        m.add(http_requests=rep.requests)
        await ctx.checkpoint({"requests": rep.requests, "emails_added": rep.emails_added, "emails_removed": rep.emails_removed,
                              "verify_jobs": rep.verify_jobs, "changed": rep.changed})
        await flow.finish_org(ctx, dom.organisation_id)


# =========================================================================== similar_expand
@handler("similar_expand")
async def similar_expand(ctx: JobContext) -> None:
    from ..discovery.similar import expand_from_seed

    run_id = ctx.job.run_id
    if run_id is None:
        raise jq.PermanentError("job has no run", "no_run")
    p = ctx.job.payload
    found = await expand_from_seed(ctx.session, dict(p.get("seed") or {}), run_id=run_id, limit=int(p.get("limit", 20)),
                                   countries=p.get("countries") or None, min_score=float(p.get("min_score", 0.35)))
    await ctx.checkpoint({"queued": [{"org_id": f["org_id"], "score": f["score"], "stage": f["stage"]} for f in found]})


# =========================================================================== monitor
@handler("monitor")
async def monitor(ctx: JobContext) -> None:
    from ..discovery.monitor import run_due_monitors

    fetcher, limiter, robots = await runtime.get_net()
    res = await run_due_monitors(ctx.session, fetcher, limiter=limiter, robots=robots, limit=int(ctx.job.payload.get("limit", 20)))
    await ctx.checkpoint({"ran": [{"id": r["id"], "kind": r["kind"], "status": r["status"]} for r in res]})


# =========================================================================== completeness
@handler("completeness")
async def completeness(ctx: JobContext) -> None:
    from ..discovery.completeness import refresh_completeness

    p = ctx.job.payload
    ids = [int(i) for i in p.get("organisation_ids") or ([p["organisation_id"]] if p.get("organisation_id") else [])]
    if not ids and p.get("target_run_id"):
        ids = list((await ctx.session.execute(text("SELECT organisation_id FROM run_organisations WHERE run_id=:r ORDER BY organisation_id LIMIT 5000"),
                                              {"r": int(p["target_run_id"])})).scalars())
    done = await refresh_completeness(ctx.session, ids)
    await ctx.checkpoint({"refreshed": len(done)})


# =========================================================================== Common Crawl
async def _default_cc_hits(domain: str, params: dict[str, Any], max_hits: int):
    from ..sources.commoncrawl import CDXClient, ColumnarIndexReader

    path = params.get("columnar_index_path") or params.get("path")
    if path:  # operator-configured OFFLINE cc-index Parquet files: no network at all
        def scan():
            r = ColumnarIndexReader(path, registered_domains=[domain], status=200)
            return list(itertools.islice(r.iter_hits(), max_hits))
        return await asyncio.to_thread(scan)
    async with CDXClient(min_interval_s=float(params.get("min_interval_s", 1.0))) as c:
        return [h async for h, _p in c.query(domain, crawl_id=params.get("crawl_id"), match_type="domain", limit=max_hits, max_pages=1)]


async def _default_cc_fetch(hit):
    from ..sources.commoncrawl import fetch_warc_record

    return await fetch_warc_record(hit)


@handler("cc_lookup")
async def cc_lookup(ctx: JobContext) -> None:
    """Contact-page URL hits for the organisation's confirmed domain from Common Crawl (CDX API with polite pacing, or an offline
    columnar index), archived pages parsed and stored ONLY as historical / unconfirmed evidence (``cc_persist``). A live crawl
    of the site confirms what it re-observes; the rest stays flagged and is never counted as a yield."""
    from ..sources import cc_persist
    from ..sources.base import get_adapter
    from ..sources.commoncrawl import CommonCrawlContactPageFinder, extract_archived_observations

    rp, run_id = await _plan(ctx)
    org_id = int(ctx.job.payload["organisation_id"])
    if not rp.common_crawl_on:
        return
    org = await ctx.session.get(Organisation, org_id)
    dom = await _confirmed_domain(ctx.session, org_id)
    if org is None or dom is None:
        await ctx.checkpoint({"skipped": "no_confirmed_domain"})
        return
    info = get_adapter("commoncrawl_archive").info
    gate = runtime.adapter_gate(info)
    if not gate.try_acquire():
        raise jq.Requeue(max(1.0, min(30.0, gate.time_until_available())))
    params = _cc_params(rp)
    src = await flow.adapter_of_org(ctx.session, org_id, ctx.job.payload)
    try:
        async with measured(ctx, src) as m:
            try:
                hits = await (CC_HITS or _default_cc_hits)(dom.registrable_domain, params, CC_MAX_HITS)
            except Exception as e:  # missing pyarrow / unreachable index: honest "unavailable", not a failure of the run
                await ctx.checkpoint({"unavailable": f"{type(e).__name__}: {str(e)[:200]}"})
                return
            m.add(http_requests=0 if params.get("columnar_index_path") or params.get("path") else 1)
            finder = CommonCrawlContactPageFinder(kinds=params.get("kinds") or ("contact", "legal", "team", "about"), include_documents=False)
            cands = [c for c in finder.best_per_domain(hits, per_domain=CC_MAX_FETCH) if c.hit.has_warc][:CC_MAX_FETCH]
            stored, confirmed_now = 0, 0
            for c in cands:
                try:
                    page = await (CC_FETCH or _default_cc_fetch)(c.hit)
                except Exception as e:
                    log.info("archived page fetch failed (%s): %s", c.hit.url, e)
                    continue
                m.add(http_requests=1)
                ex = extract_archived_observations(page, company_hint=org.legal_name or org.trading_name)
                obs = [o for o in ex.emails if registrable_domain(o.email.rpartition("@")[2]) == dom.registrable_domain]
                if not obs:
                    continue
                ts = page.fetch_time or c.hit.fetch_time or datetime.now(UTC)
                for r in await cc_persist.persist_historical_observations(ctx.session, org_id, obs, archive_timestamp=ts, crawl_id=c.hit.crawl_id or None, run_id=run_id):
                    stored += 1
                    if not r.unconfirmed:  # the same address was already observed live: the archive merely corroborates it
                        await ctx.session.execute(text(
                            "UPDATE contact_evidence ce SET context = COALESCE(ce.context,'{}'::jsonb) || CAST(:p AS jsonb) FROM evidence_sources es "
                            "WHERE es.id = ce.evidence_id AND es.historical AND ce.email_id = :e"),
                            {"p": '{"unconfirmed": false, "confirmed_live_at": "%s"}' % datetime.now(UTC).isoformat(), "e": r.email_id})
                        confirmed_now += 1
            await ctx.checkpoint({"hits": len(hits), "pages_fetched": len(cands), "historical_emails": stored, "already_live": confirmed_now})
            await flow.finish_org(ctx, org_id)
    finally:
        gate.release()
