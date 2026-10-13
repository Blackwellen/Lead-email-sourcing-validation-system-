from __future__ import annotations

import itertools

import pytest
from sqlalchemy import select, text

from app.discovery.effectiveness import metrics_by_adapter, metrics_for, record_effect
from app.discovery.scheduler import (
    PIPELINE_STAGES,
    SchedulerConfig,
    SourceBudget,
    SourceBudgets,
    compute_priority,
    default_strategy_pipeline,
    rebalance,
    should_skip_expensive_work,
    static_score,
)
from app.discovery.strategy import category_of, plan_from_config
from app.models import (
    Organisation,
    OrganisationDomain,
    RunOrganisation,
    SourceEffectiveness,
    SourcingRun,
)
from app.run_config import DiscoverySpec, RunConfig
from app.sources.base import get_adapter


# --------------------------------------------------------------------------- run_config
def test_run_config_backward_compatible():
    old = {"name": "x", "sources": ["csv_import"], "countries": ["gb"]}
    cfg = RunConfig.model_validate(old)
    d = cfg.discovery
    assert d.strategy == "balanced" and d.contact_discovery == "published_only" and d.business_scope == "new_only"
    assert d.network.enabled is False and d.network.max_depth == 1 and d.signals.enabled is False
    assert d.fallback_research == "stop_if_no_website" and d.search_budget == 0 and d.source_quotas == {}
    # round trip of a stored snapshot without the key
    snap = cfg.model_dump(mode="json")
    snap.pop("discovery")
    assert RunConfig.model_validate(snap).discovery == d


def test_run_config_discovery_validation():
    with pytest.raises(ValueError):
        DiscoverySpec(network={"max_depth": 4})
    with pytest.raises(ValueError):
        DiscoverySpec(strategy="turbo")
    with pytest.raises(ValueError):
        DiscoverySpec(source_quotas={"x": -1})
    d = DiscoverySpec(network={"enabled": True, "max_depth": 3, "source_budget": 10}, source_categories=["procurement", "procurement"])
    assert d.source_categories == ["procurement"] and d.network.max_depth == 3


def test_strategy_plan_translation():
    cfg = RunConfig(name="x", sources=["csv_import"], discovery={"strategy": "fast"})
    plan = plan_from_config(cfg)
    assert [s.name for s in plan.stages] == list(PIPELINE_STAGES)
    assert not plan.stage_enabled("directory_corroboration") and plan.stage_enabled("own_website_crawl")
    assert plan.contact_origins == ("published",) and not plan.allow_inferred
    deep = plan_from_config(RunConfig(name="x", sources=["a"], discovery={
        "strategy": "deep", "contact_discovery": "include_inferred", "source_categories": ["company_websites", "documents", "common_crawl"],
        "search_budget": 2, "network": {"enabled": True, "max_depth": 2}}))
    assert deep.stage_enabled("controlled_permutation_research") and deep.allow_inferred
    assert deep.gap_recovery["ladder"] == ["recheck_imported_website", "resolve_bulk_candidate_domains", "search_provider",
                                           "common_crawl_lookup", "register_corroboration", "document_extraction", "role_address_candidates"]
    assert deep.network["enabled"] and deep.network["max_depth"] == 2
    assert category_of("commoncrawl_index", "web_archive") == "common_crawl"
    assert category_of("companies_house_bulk", "bulk_dataset") == "bulk_open_data"
    assert category_of("csv_import", "manual_import") is None


def test_default_pipeline_presets():
    for s in ("fast", "balanced", "deep", "custom"):
        names = [p.name for p in default_strategy_pipeline(s)]
        assert names == list(PIPELINE_STAGES)
    assert not {p.name: p.enabled for p in default_strategy_pipeline("balanced")}["controlled_permutation_research"]
    with pytest.raises(ValueError):
        default_strategy_pipeline("nope")


# --------------------------------------------------------------------------- effectiveness
async def test_record_effect_atomic_and_global_rows(session):
    run = SourcingRun(name="r")
    session.add(run)
    await session.flush()
    await record_effect(session, "osm_geofabrik", run.id, records_seen=100, unique_businesses=80, duplicates=20, http_requests=5)
    await record_effect(session, "osm_geofabrik", run.id, records_seen=100, unique_businesses=70, duplicates=30, peak_rss_mb=300.0)
    await record_effect(session, "osm_geofabrik", run.id, peak_rss_mb=200.0)
    await record_effect(session, "osm_geofabrik", None, records_seen=1)
    rows = (await session.execute(select(SourceEffectiveness).order_by(SourceEffectiveness.id))).scalars().all()
    assert len(rows) == 2
    per_run = next(r for r in rows if r.run_id == run.id)
    glob = next(r for r in rows if r.run_id is None)
    assert per_run.records_seen == 200 and per_run.duplicates == 50 and per_run.peak_rss_mb == 300.0
    assert glob.records_seen == 201 and glob.unique_businesses == 150
    with pytest.raises(ValueError):
        await record_effect(session, "osm_geofabrik", run.id, bogus=1)
    with pytest.raises(ValueError):
        await record_effect(session, "osm_geofabrik", run.id, records_seen=-1)


async def test_metrics_ratios_none_when_insufficient(session):
    m = await metrics_for(session, "wikidata")
    assert all(v is None for v in m.derived.values())
    await record_effect(session, "wikidata", None, records_seen=5, unique_businesses=5, http_requests=40, eligible_contacts=2)
    m = await metrics_for(session, "wikidata")
    assert m.derived["unique_businesses_per_1000_records"] is None  # 5 < min_samples
    assert m.derived["duplicate_pct"] is None
    m = await metrics_for(session, "wikidata", min_samples=1)
    assert m.derived["unique_businesses_per_1000_records"] == 1000.0
    assert m.derived["http_requests_per_new_contact"] == 20.0
    assert m.derived["failure_pct"] is None  # no jobs measured
    await record_effect(session, "wikidata", None, records_seen=995, unique_businesses=745, duplicates=250, confirmed_domains=300,
                        published_emails=150, eligible_contacts=98, http_requests=1960, jobs=50, failures=5,
                        cpu_seconds=100.0, wall_seconds=7200.0, peak_rss_mb=420.0)
    m = await metrics_for(session, "wikidata")
    d = m.derived
    assert d["unique_businesses_per_1000_records"] == 750.0
    assert d["duplicate_pct"] == 25.0
    assert d["confirmed_domains_per_1000_businesses"] == 400.0
    assert d["published_emails_per_1000_businesses"] == 200.0
    assert d["eligible_contacts_per_1000_businesses"] == round(100 / 750 * 1000, 4)
    assert d["http_requests_per_new_contact"] == 20.0
    assert d["failure_pct"] == 10.0
    assert d["cpu_seconds_per_job"] == 2.0
    assert d["new_contacts_per_processing_hour"] == 50.0
    assert d["peak_rss_mb"] == 420.0
    allm = await metrics_by_adapter(session)
    assert set(allm) == {"wikidata"}
    assert (await metrics_for(session, None)).counters["records_seen"] == 1000


# --------------------------------------------------------------------------- priority math
def info(aid):
    return get_adapter(aid).info


def test_priority_static_hierarchy_and_exploration():
    cfg = SchedulerConfig()
    bulk = compute_priority(info("companies_house_bulk"), None, cfg)
    manual = compute_priority(info("csv_import"), None, cfg)
    assert static_score(10) == 1.0 and static_score(60) == 0.5
    assert bulk.score > manual.score
    assert bulk.exploration == cfg.explore_bonus  # untouched source gets the full bonus
    # once sampled, exploration is gone
    seasoned = {"records_seen": 1000, "http_requests": 0, "eligible_contacts": 0, "jobs": 100}
    assert compute_priority(info("csv_import"), seasoned, cfg).exploration == 0.0
    assert compute_priority(info("csv_import"), seasoned, cfg).score < manual.score


def test_priority_gain_duplicates_failures():
    cfg = SchedulerConfig()
    base = {"records_seen": 1000, "jobs": 100, "http_requests": 1000, "eligible_contacts": 20}
    great = {**base, "eligible_contacts": 200}
    poor = {**base, "eligible_contacts": 1}
    ag = info("osm_geofabrik")
    assert compute_priority(ag, great, cfg).score > compute_priority(ag, base, cfg).score > compute_priority(ag, poor, cfg).score
    dup = {**base, "duplicates": 900}
    assert compute_priority(ag, dup, cfg).score < compute_priority(ag, base, cfg).score
    assert compute_priority(ag, dup, cfg).duplicate_multiplier == pytest.approx(0.28)
    fail = {**base, "failures": 60}
    p = compute_priority(ag, fail, cfg)
    assert p.failure_multiplier == pytest.approx(0.4) and p.score < compute_priority(ag, base, cfg).score
    # smoothing: a single lucky contact on 1 request does not dominate
    lucky = compute_priority(ag, {"records_seen": 25, "http_requests": 1, "eligible_contacts": 1, "jobs": 1}, cfg)
    proven = compute_priority(ag, great, cfg)
    assert lucky.gain < proven.gain
    # deterministic
    assert compute_priority(ag, great, cfg) == compute_priority(ag, great, cfg)


# --------------------------------------------------------------------------- hard caps
class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_rate_policy_hard_cap_under_simulated_load():
    clock = Clock()
    b = SourceBudget("osm", requests_per_minute=12, concurrency=3, burst=5, clock=clock)
    grants: list[float] = []
    in_flight_max = 0
    # hammer: 20 attempts per simulated second for 15 minutes, jobs finish after 0.7 s
    pending: list[float] = []
    for step in range(15 * 60 * 20):
        clock.t = step * 0.05
        for done in [p for p in pending if p <= clock.t]:
            pending.remove(done)
            b.release()
        if b.try_acquire():
            grants.append(clock.t)
            pending.append(clock.t + 0.7)
        in_flight_max = max(in_flight_max, b.in_flight)
    assert in_flight_max <= 3
    # in ANY sliding 60 s window there are at most 12 grants
    for i, g in enumerate(grants):
        in_win = sum(1 for x in grants[i:] if x < g + 60.0)
        assert in_win <= 12
    assert len(grants) >= 12 * 14  # and the limiter is actually usable (not starving)
    assert b.denied > 0


def test_quota_never_exceeded_and_registry():
    clock = Clock()
    b = SourceBudget("x", requests_per_minute=None, concurrency=10, max_work=7, clock=clock)
    assert sum(b.try_acquire() for _ in range(50)) == 7 and b.exhausted
    assert b.time_until_available() == float("inf")
    disc = DiscoverySpec(source_quotas={"osm_geofabrik": 100, "bulk_open_data": 5}, overall_max_work=6)
    infos = [info("companies_house_bulk"), info("osm_geofabrik"), info("wikidata")]
    reg = SourceBudgets.from_config(infos, disc, clock=clock)
    got = 0
    for i in range(100):
        clock.t += 0.01
        aid = ["companies_house_bulk", "osm_geofabrik", "wikidata"][i % 3]
        ok = reg.try_acquire(aid)
        got += ok
        if ok:
            reg.release(aid)
    assert reg.overall_used == got <= 6
    assert reg.category_used.get("bulk_open_data", 0) <= 5
    reg2 = SourceBudgets.from_config(infos, DiscoverySpec(source_quotas={"bulk_open_data": 3}), clock=clock)
    assert sum(1 for _ in range(10) if (reg2.try_acquire("companies_house_bulk") and (reg2.release("companies_house_bulk") or True))) == 3
    assert reg2.try_acquire("unknown") is False


# --------------------------------------------------------------------------- rebalance, fairness, quotas
async def _jobs(session, run_id, adapter, n, prio, start=0):
    for i in range(n):
        await session.execute(text(
            "INSERT INTO sourcing_jobs (run_id, stage, dedupe_key, payload, priority, state) "
            "VALUES (:r,'discover',:k, CAST(:p AS jsonb), :prio, 'queued')"),
            {"r": run_id, "k": f"{adapter}:{start + i}", "p": f'{{"adapter_id": "{adapter}"}}', "prio": prio})


async def _make_run(session, **disc):
    cfg = RunConfig(name="r", sources=["csv_import"], discovery=disc)
    run = SourcingRun(name="r", status="running", configuration_snapshot=cfg.model_dump(mode="json"))
    session.add(run)
    await session.flush()
    return run.id


async def _claim_one(session, run_id):
    row = (await session.execute(text(
        "UPDATE sourcing_jobs SET state='done', finished_at=now() WHERE id=(SELECT id FROM sourcing_jobs WHERE run_id=:r AND state='queued' "
        "ORDER BY priority, id LIMIT 1) RETURNING payload->>'adapter_id'"), {"r": run_id})).first()
    return row[0] if row else None


async def test_rebalance_bounded_change_and_scores(session):
    rid = await _make_run(session)
    await _jobs(session, rid, "companies_house_bulk", 5, 100)
    await _jobs(session, rid, "csv_import", 5, 100)
    await record_effect(session, "csv_import", None, records_seen=500, http_requests=900, eligible_contacts=0, jobs=50)
    res = await rebalance(session, rid, config=SchedulerConfig(max_step=7))
    assert res.scores["companies_house_bulk"] > res.scores["csv_import"]
    # bounded: each adapter's mean moved by at most max_step from 100
    assert 93 <= res.priorities["companies_house_bulk"] <= 107 and 93 <= res.priorities["csv_import"] <= 107
    assert res.priorities["companies_house_bulk"] < res.priorities["csv_import"]
    again = await rebalance(session, rid, config=SchedulerConfig(max_step=7))
    assert again.priorities["companies_house_bulk"] <= res.priorities["companies_house_bulk"]
    eff = (await session.execute(select(SourceEffectiveness).where(SourceEffectiveness.run_id == rid))).scalars().all()
    assert {e.adapter_id for e in eff} == {"companies_house_bulk", "csv_import"} and all(e.priority_score > 0 for e in eff)


async def test_fairness_no_starvation_simulation(session):
    rid = await _make_run(session)
    await _jobs(session, rid, "companies_house_bulk", 150, 10)  # favoured source with endless work
    await _jobs(session, rid, "csv_import", 150, 200)  # starved by strict priority order without the guard
    cfg = SchedulerConfig(min_share=0.15, fairness_window=40, fairness_min_window=10)
    order: list[str] = []
    for i in range(200):
        a = await _claim_one(session, rid)
        order.append(a)
        if i % 10 == 9:
            await rebalance(session, rid, config=cfg)
    last = order[100:]
    share = last.count("csv_import") / len(last)
    assert share >= 0.12, share  # starving source keeps at least ~its minimum share
    assert order[:100].count("csv_import") > 0
    # control: without rebalancing the low-priority source gets nothing
    rid2 = await _make_run(session)
    await _jobs(session, rid2, "companies_house_bulk", 150, 10)
    await _jobs(session, rid2, "csv_import", 150, 200)
    ctl = [await _claim_one(session, rid2) for _ in range(100)]
    assert ctl.count("csv_import") == 0


async def test_rebalance_enforces_quotas(session):
    rid = await _make_run(session, source_quotas={"csv_import": 3}, overall_max_work=100)
    await _jobs(session, rid, "csv_import", 10, 50)
    await _jobs(session, rid, "companies_house_bulk", 10, 50)
    for _ in range(3):
        await session.execute(text("UPDATE sourcing_jobs SET state='done' WHERE id=(SELECT id FROM sourcing_jobs WHERE run_id=:r AND state='queued' "
                                   "AND payload->>'adapter_id'='csv_import' LIMIT 1)"), {"r": rid})
    res = await rebalance(session, rid)
    assert res.cancelled == {"csv_import": 7}
    left = (await session.execute(text("select count(*) from sourcing_jobs where run_id=:r and state='queued' and payload->>'adapter_id'='csv_import'"),
                                  {"r": rid})).scalar_one()
    assert left == 0
    rid3 = await _make_run(session, overall_max_work=4)
    await _jobs(session, rid3, "csv_import", 6, 50)
    await session.execute(text("UPDATE sourcing_jobs SET state='done' WHERE id IN (SELECT id FROM sourcing_jobs WHERE run_id=:r LIMIT 4)"), {"r": rid3})
    res = await rebalance(session, rid3)
    assert res.cancelled == {"csv_import": 2}


# --------------------------------------------------------------------------- skip duplicates
async def test_should_skip_expensive_work(session):
    from datetime import UTC, datetime, timedelta

    def org(n, **kw):
        return Organisation(legal_name=n, country_code="GB", canonical_identity=f"GB:nm:{n}", **kw)

    fresh, stale, merged, dup = org("fresh"), org("stale"), org("merged"), org("dup")
    session.add_all([fresh, stale, merged, dup])
    await session.flush()
    merged.merged_into_id = fresh.id
    now = datetime.now(UTC)
    session.add_all([
        OrganisationDomain(organisation_id=fresh.id, domain="fresh.example.com", registrable_domain="example.com",
                           verified_match_status="confirmed", last_crawled_at=now - timedelta(days=2)),
        OrganisationDomain(organisation_id=stale.id, domain="stale.example.com", registrable_domain="example.com",
                           verified_match_status="confirmed", last_crawled_at=now - timedelta(days=400)),
    ])
    await session.execute(text(
        "INSERT INTO email_addresses (organisation_id,email_original,email_normalised,local_part,domain,registrable_domain,source_type,origin)"
        " VALUES (:o,'a@fresh.example.com','a@fresh.example.com','a','fresh.example.com','example.com','mailto','published')"), {"o": fresh.id})
    await session.flush()
    d = await should_skip_expensive_work(session, fresh.id, None)
    assert d and d.reason == "confirmed_domain_and_fresh_contacts"
    assert not await should_skip_expensive_work(session, stale.id, None)
    assert (await should_skip_expensive_work(session, merged.id)).reason == "merged_duplicate"
    assert not await should_skip_expensive_work(session, dup.id)
    assert not await should_skip_expensive_work(session, fresh.id, refresh=True)
    rid = await _make_run(session)
    session.add(RunOrganisation(run_id=rid, organisation_id=dup.id, stage="crawled"))
    await session.flush()
    assert (await should_skip_expensive_work(session, dup.id, rid, refresh=True)).reason == "already_processed_in_run"
    assert (await should_skip_expensive_work(session, 99999)).reason == "missing"
    _ = itertools
