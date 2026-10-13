"""Discovery dashboard API: overview, measured source effectiveness, scheduler state, recurring monitors, lookalike search.

Everything is derived from stored, measured data. A ratio whose denominator is below the minimum sample is ``null`` (never a
guess); "high-performing industries" and "missing-contact opportunities" report counts together with their sample sizes.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .. import jobqueue as jq
from ..db import get_session
from ..discovery import effectiveness as eff
from ..discovery import monitor as mon
from ..discovery import scheduler as sched
from ..discovery.similar import expand_from_seed, find_similar
from ..models import MonitorSchedule, SourcingRun, User
from ..security import ApiError, app_settings, audit, require_role
from ..sources.access_policy import check_url, is_forbidden_key, path_inside
from ..sources.base import SOURCE_TYPES, get_adapter, list_adapters
from ..sources.catalogue_full import source_catalogue
from ..sources.signals_util import SIGNAL_TYPES
from ..system_runs import ensure_system_run
from .common import Page, SortSpec, apply_keyset, finish_page
from .runs_support import uploads_dir

log = logging.getLogger("api.discovery")
router = APIRouter(prefix="/api/discovery", tags=["discovery"], dependencies=[Depends(require_role("viewer"))])
MIN_SAMPLE = eff.DEFAULT_MIN_SAMPLES
INDUSTRY_MIN_BUSINESSES = 10
FREQUENCIES = ("daily", "weekly", "monthly")


# --------------------------------------------------------------------------- models
class CountBy(BaseModel):
    key: str
    count: int


class CoverageRow(BaseModel):
    country_code: str
    businesses: int
    with_confirmed_website: int
    with_published_email: int


class IndustryRow(BaseModel):
    category_id: int | None
    name: str
    businesses: int
    with_published_email: int
    published_email_rate: float | None = Field(description="null when fewer than the minimum sample of businesses")
    min_sample: int = INDUSTRY_MIN_BUSINESSES


class OpportunitySample(BaseModel):
    organisation_id: int
    name: str | None
    country_code: str
    official_website: str
    published_email: str
    score: int


class OpportunityGroup(BaseModel):
    classification: Literal["incomplete_research", "genuinely_unavailable", "in_progress"]
    count: int
    explanation: str
    samples: list[OpportunitySample]


class RestrictedSource(BaseModel):
    id: str
    name: str
    status: str
    reason: str
    licence: str


class MetricsOut(BaseModel):
    adapter_id: str | None
    run_id: int | None
    counters: dict[str, float]
    derived: dict[str, float | None]
    min_samples: int


class OverviewOut(BaseModel):
    generated_at: datetime
    totals: dict[str, int]
    businesses_by_source: list[CountBy]
    websites_by_source: list[CountBy]
    emails_by_source: list[CountBy]
    completeness_score_distribution: list[CountBy]
    completeness_flags: dict[str, dict[str, int]]
    organisations_without_assessment: int
    geographic_coverage: list[CoverageRow]
    high_performing_industries: list[IndustryRow]
    missing_contact_opportunities: list[OpportunityGroup]
    restricted_sources: list[RestrictedSource]
    source_effectiveness: list[MetricsOut]


class EffectivenessOut(BaseModel):
    min_samples: int
    all_time: list[MetricsOut]
    run: list[MetricsOut] | None = None
    recent_runs: list[dict[str, Any]]


class SchedulerRow(BaseModel):
    adapter_id: str
    name: str
    source_type: str
    enabled: bool
    static_priority: int
    static_score: float
    priority_score: float
    stored_priority_score: float | None = Field(None, description="value written by the last scheduler.rebalance for the run (if any)")
    samples: int
    explanation: dict[str, Any]
    rate_policy: dict[str, Any]


class SchedulerOut(BaseModel):
    run_id: int | None
    rows: list[SchedulerRow]


class MonitorOut(BaseModel):
    id: int
    kind: str
    name: str
    frequency: str
    config: dict[str, Any]
    enabled: bool
    last_run_at: datetime | None
    next_run_at: datetime | None
    last_result: dict[str, Any]
    created_by: int | None


class MonitorIn(BaseModel):
    kind: Literal["new_registrations", "website_changes", "signals"]
    name: str = Field(min_length=1, max_length=200)
    frequency: Literal["daily", "weekly", "monthly"] = "weekly"
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class MonitorPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    frequency: Literal["daily", "weekly", "monthly"] | None = None
    config: dict[str, Any] | None = None
    enabled: bool | None = None


class RunNowOut(BaseModel):
    monitor_id: int
    queued: bool
    job_id: int | None = None
    run_id: int


class SeedIn(BaseModel):
    org_id: int | None = Field(None, ge=1)
    category_id: int | None = Field(None, ge=1)
    run_id: int | None = Field(None, ge=1)
    limit: int = Field(20, ge=1, le=100)
    countries: list[str] = Field(default_factory=list, max_length=20)
    min_score: float = Field(0.35, ge=0, le=1)


class SimilarExpandIn(SeedIn):
    target_run_id: int = Field(ge=1, description="the run the lookalikes are queued into")


class SimilarItem(BaseModel):
    org_id: int
    name: str | None
    country_code: str | None
    city: str | None = None
    score: float
    explanation: str = Field(description="how the score is composed, e.g. '0.812 = sic=0.90x0.30 + ...'")
    factors: dict[str, dict[str, Any]] = Field(default_factory=dict)


class SimilarOut(BaseModel):
    seed: dict[str, int]
    items: list[SimilarItem]


class SimilarExpandOut(BaseModel):
    target_run_id: int
    queued: list[dict[str, Any]]


# --------------------------------------------------------------------------- helpers
def _metrics(m: eff.Metrics) -> MetricsOut:
    return MetricsOut(adapter_id=m.adapter_id, run_id=m.run_id, counters=m.counters, derived=m.derived, min_samples=m.min_samples)


async def _by(session: AsyncSession, sql: str) -> list[CountBy]:
    return [CountBy(key=str(k or "unknown"), count=int(n)) for k, n in (await session.execute(text(sql))).all()]


# --------------------------------------------------------------------------- overview
@router.get("/overview", response_model=OverviewOut)
async def discovery_overview(session: AsyncSession = Depends(get_session)) -> OverviewOut:
    live = "o.merged_into_id IS NULL"
    totals = dict((await session.execute(text(
        f"""SELECT (SELECT count(*) FROM organisations o WHERE {live}) AS businesses,
                   (SELECT count(DISTINCT d.organisation_id) FROM organisation_domains d JOIN organisations o ON o.id=d.organisation_id
                      WHERE {live} AND d.verified_match_status IN ('confirmed','high') AND NOT d.parked) AS with_confirmed_website,
                   (SELECT count(DISTINCT e.organisation_id) FROM email_addresses e JOIN organisations o ON o.id=e.organisation_id
                      WHERE {live} AND e.origin IN ('published','corroborated') AND e.suppressed_at IS NULL) AS with_published_email,
                   (SELECT count(*) FROM business_signals) AS signals,
                   (SELECT count(*) FROM business_relationships) AS relationships,
                   (SELECT count(*) FROM organisation_record_links WHERE status='review') AS pending_reviews"""))).mappings().one())
    by_biz = await _by(session, f"SELECT o.discovery_source, count(*) FROM organisations o WHERE {live} GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 50")
    by_web = await _by(session, f"""SELECT o.discovery_source, count(DISTINCT o.id) FROM organisations o JOIN organisation_domains d ON d.organisation_id=o.id
                                    WHERE {live} AND d.verified_match_status IN ('confirmed','high') AND NOT d.parked GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 50""")
    by_mail = await _by(session, f"""SELECT o.discovery_source, count(*) FROM email_addresses e JOIN organisations o ON o.id=e.organisation_id
                                     WHERE {live} AND e.origin IN ('published','corroborated') AND e.suppressed_at IS NULL GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 50""")
    dist = await _by(session, f"""SELECT CASE WHEN c.score < 20 THEN '0-19' WHEN c.score < 40 THEN '20-39' WHEN c.score < 60 THEN '40-59'
                                       WHEN c.score < 80 THEN '60-79' ELSE '80-100' END AS bucket, count(*)
                                  FROM organisation_completeness c JOIN organisations o ON o.id=c.organisation_id WHERE {live} GROUP BY 1 ORDER BY 1""")
    flags: dict[str, dict[str, int]] = {}
    for f in ("official_website", "contact_page_inspected", "published_email", "contact_person_corroborated", "email_infrastructure_checked",
              "recipient_evidence", "campaign_eligibility", "recently_checked"):
        flags[f] = {k: int(n) for k, n in (await session.execute(text(
            f"SELECT c.{f}, count(*) FROM organisation_completeness c JOIN organisations o ON o.id=c.organisation_id WHERE {live} GROUP BY 1"))).all()}
    unassessed = (await session.execute(text(
        f"SELECT count(*) FROM organisations o WHERE {live} AND NOT EXISTS (SELECT 1 FROM organisation_completeness c WHERE c.organisation_id=o.id)"))).scalar_one()
    geo = [CoverageRow(country_code=r[0], businesses=r[1], with_confirmed_website=r[2], with_published_email=r[3]) for r in (await session.execute(text(
        f"""SELECT o.country_code, count(*), count(*) FILTER (WHERE EXISTS (SELECT 1 FROM organisation_domains d WHERE d.organisation_id=o.id
                    AND d.verified_match_status IN ('confirmed','high') AND NOT d.parked)),
                   count(*) FILTER (WHERE EXISTS (SELECT 1 FROM email_addresses e WHERE e.organisation_id=o.id AND e.origin IN ('published','corroborated')
                    AND e.suppressed_at IS NULL))
            FROM organisations o WHERE {live} GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 60"""))).all()]
    inds = []
    for cid, name, n, with_mail in (await session.execute(text(
            f"""SELECT o.category_id, COALESCE(cat.name, o.industry, 'Uncategorised') AS name, count(*) AS n,
                       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM email_addresses e WHERE e.organisation_id=o.id AND e.origin IN ('published','corroborated')
                        AND e.suppressed_at IS NULL)) AS with_mail
                FROM organisations o LEFT JOIN categories cat ON cat.id=o.category_id WHERE {live} GROUP BY 1, 2
                ORDER BY with_mail DESC, n DESC, 2 LIMIT 40"""))).all():
        rate = round(with_mail / n, 4) if n >= INDUSTRY_MIN_BUSINESSES else None
        inds.append(IndustryRow(category_id=cid, name=name, businesses=n, with_published_email=with_mail, published_email_rate=rate))
    inds.sort(key=lambda r: (-(r.published_email_rate if r.published_email_rate is not None else -1), -r.businesses, r.name))
    opp = await _opportunities(session)
    restricted = []
    for r in source_catalogue():
        if not r["automated_allowed"] or r["health"]["status"] in ("restricted", "info_only", "not_implemented"):
            restricted.append(RestrictedSource(id=r["id"], name=r["name"], status=r["health"]["status"], reason=r["health"]["message"], licence=r["licence"] or ""))
    restricted.sort(key=lambda x: x.id)
    metrics = [_metrics(m) for m in (await eff.metrics_by_adapter(session, None)).values()]
    metrics.sort(key=lambda m: m.adapter_id or "")
    return OverviewOut(
        generated_at=datetime.now(UTC), totals={k: int(v) for k, v in totals.items()}, businesses_by_source=by_biz, websites_by_source=by_web,
        emails_by_source=by_mail, completeness_score_distribution=dist, completeness_flags=flags, organisations_without_assessment=int(unassessed),
        geographic_coverage=geo, high_performing_industries=inds[:15], missing_contact_opportunities=opp, restricted_sources=restricted[:100],
        source_effectiveness=metrics)


_OPP_EXPLAIN = {
    "incomplete_research": "Research steps have not been performed yet (website or contact page not inspected): worth running.",
    "genuinely_unavailable": "Research WAS performed and the contact/website is genuinely not published or findable.",
    "in_progress": "Work is queued or running for these organisations.",
}


async def _opportunities(session: AsyncSession) -> list[OpportunityGroup]:
    cls_sql = """CASE WHEN c.published_email = 'yes' THEN NULL
                      WHEN c.published_email = 'in_progress' OR c.official_website = 'in_progress' THEN 'in_progress'
                      WHEN c.published_email = 'no' THEN 'genuinely_unavailable'
                      ELSE 'incomplete_research' END"""
    out = []
    counts = {k: int(n) for k, n in (await session.execute(text(
        f"SELECT {cls_sql} AS cls, count(*) FROM organisation_completeness c JOIN organisations o ON o.id=c.organisation_id "
        f"WHERE o.merged_into_id IS NULL GROUP BY 1"))).all() if k}
    for cls in ("incomplete_research", "genuinely_unavailable", "in_progress"):
        rows = (await session.execute(text(
            f"""SELECT o.id, COALESCE(o.legal_name, o.trading_name), o.country_code, c.official_website, c.published_email, c.score
                FROM organisation_completeness c JOIN organisations o ON o.id=c.organisation_id
                WHERE o.merged_into_id IS NULL AND ({cls_sql}) = :cls ORDER BY c.score DESC, o.id LIMIT 10"""), {"cls": cls})).all()
        out.append(OpportunityGroup(classification=cls, count=counts.get(cls, 0), explanation=_OPP_EXPLAIN[cls],
                                    samples=[OpportunitySample(organisation_id=r[0], name=r[1], country_code=r[2], official_website=r[3], published_email=r[4], score=r[5]) for r in rows]))
    return out


# --------------------------------------------------------------------------- effectiveness / scheduler
@router.get("/effectiveness", response_model=EffectivenessOut)
async def source_effectiveness(
    run_id: int | None = Query(None, ge=1), min_samples: int = Query(MIN_SAMPLE, ge=1, le=100000), runs: int = Query(5, ge=0, le=20),
    session: AsyncSession = Depends(get_session),
) -> EffectivenessOut:
    """Measured counters + derived ratios per adapter (all-time and per run). A ratio is ``null`` below ``min_samples``."""
    all_time = [_metrics(m) for m in (await eff.metrics_by_adapter(session, None, min_samples=min_samples)).values()]
    all_time.sort(key=lambda m: m.adapter_id or "")
    one = None
    if run_id is not None:
        if await session.get(SourcingRun, run_id) is None:
            raise ApiError(404, "not_found", "Run not found.")
        one = sorted((_metrics(m) for m in (await eff.metrics_by_adapter(session, run_id, min_samples=min_samples)).values()), key=lambda m: m.adapter_id or "")
    recent = []
    if runs:
        ids = (await session.execute(text(
            "SELECT DISTINCT e.run_id, r.name FROM source_effectiveness e JOIN sourcing_runs r ON r.id=e.run_id WHERE e.run_id IS NOT NULL "
            "ORDER BY e.run_id DESC LIMIT :n"), {"n": runs})).all()
        for rid, name in ids:
            ms = sorted((_metrics(m) for m in (await eff.metrics_by_adapter(session, rid, min_samples=min_samples)).values()), key=lambda m: m.adapter_id or "")
            recent.append({"run_id": rid, "name": name, "adapters": [m.model_dump() for m in ms]})
    return EffectivenessOut(min_samples=min_samples, all_time=all_time, run=one, recent_runs=recent)


@router.get("/scheduler", response_model=SchedulerOut)
async def scheduler_state(run_id: int | None = Query(None, ge=1), session: AsyncSession = Depends(get_session)) -> SchedulerOut:
    """Priority score per runnable source: static hierarchy x measured information gain (see ``scheduler.compute_priority``)."""
    from ..models import SourceAdapterState, SourceEffectiveness

    states = {s.adapter_id: s for s in (await session.execute(select(SourceAdapterState))).scalars()}
    glob = await eff.metrics_by_adapter(session, None)
    runm = await eff.metrics_by_adapter(session, run_id) if run_id else {}
    stored: dict[str, float] = {}
    if run_id:
        stored = {r.adapter_id: r.priority_score for r in (await session.execute(select(SourceEffectiveness).where(SourceEffectiveness.run_id == run_id))).scalars()}
    cfg = sched.SchedulerConfig()
    rows = []
    for a in list_adapters():
        info = a.info
        if info.info_only:
            continue
        m = runm.get(info.adapter_id) if runm.get(info.adapter_id) and runm[info.adapter_id].counters.get("records_seen", 0) >= cfg.min_sample else glob.get(info.adapter_id)
        ps = sched.compute_priority(info, m, cfg)
        st = states.get(info.adapter_id)
        rows.append(SchedulerRow(
            adapter_id=info.adapter_id, name=info.name, source_type=info.source_type, enabled=bool(st.enabled) if st else False,
            static_priority=info.default_priority, static_score=ps.static, priority_score=ps.score, stored_priority_score=stored.get(info.adapter_id),
            samples=ps.samples, explanation={**ps.explanation, "gain": ps.gain, "exploration": ps.exploration, "duplicate_multiplier": ps.duplicate_multiplier,
                                             "failure_multiplier": ps.failure_multiplier},
            rate_policy={"requests_per_minute": info.rate_policy.requests_per_minute, "concurrency": info.rate_policy.concurrency}))
    rows.sort(key=lambda r: (-r.priority_score, r.adapter_id))
    return SchedulerOut(run_id=run_id, rows=rows)


# --------------------------------------------------------------------------- monitors
def _clean_monitor_config(kind: str, cfg: dict[str, Any], up: Path) -> dict[str, Any]:
    if len(json.dumps(cfg, default=str)) > 4096:
        raise ApiError(413, "payload_too_large", "Monitor configuration is limited to 4 KB.")

    def intr(k: str, lo: int, hi: int) -> int:
        v = cfg[k]
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise ApiError(422, "invalid_config_value", f"'{k}' must be an integer between {lo} and {hi}.")
        return v

    allowed: dict[str, Any] = {
        "website_changes": {"older_than_days": (1, 365), "limit": (1, 200), "max_pages": (1, 20)},
        "signals": {"limit": (1, 200), "freshness_days": (1, 3650), "types": None},
        "new_registrations": {"current_path": "path", "previous_path": "path", "previous_index": "path", "url": "url", "auto_download": "bool",
                              "mode": "str", "days": (1, 400), "kinds": "list", "limit": (1, 100000), "sic_codes": "list", "postcodes": "list", "keywords": "list",
                              "active_only": "bool", "commit_every": (0, 100000)},
    }[kind]
    out: dict[str, Any] = {}
    for k, v in cfg.items():
        if is_forbidden_key(k):
            raise ApiError(422, "invalid_config_key", f"'{k[:60]}' cannot be configured: index stores and working directories are derived from the server settings.")
        if k not in allowed:
            raise ApiError(422, "invalid_config_key", f"'{k[:60]}' is not a valid setting for a {kind} monitor (allowed: {', '.join(sorted(allowed))}).")
        rule = allowed[k]
        if isinstance(rule, tuple):
            out[k] = intr(k, *rule)
        elif k == "types":
            if not isinstance(v, list) or any(t not in SIGNAL_TYPES for t in v):
                raise ApiError(422, "invalid_config_value", f"'types' must be a list of: {', '.join(SIGNAL_TYPES)}.")
            out[k] = v
        elif rule == "path":
            if not path_inside(v, Path(os.path.realpath(up))):
                raise ApiError(422, "invalid_path", f"'{k}' must point to a file uploaded through the Source Explorer.")
            out[k] = v
        elif rule == "url":
            if not isinstance(v, str) or not v.lower().startswith(("http://", "https://")):
                raise ApiError(422, "invalid_config_value", f"'{k}' must be an http(s) URL.")
            problem = check_url(v)
            if problem:
                raise ApiError(422, "invalid_config_value", f"'{k}' {problem}.")
            out[k] = v
        elif rule == "bool":
            if not isinstance(v, bool):
                raise ApiError(422, "invalid_config_value", f"'{k}' must be true or false.")
            out[k] = v
        elif rule == "list":
            if not isinstance(v, list) or len(v) > 100 or any(not isinstance(x, str | int) for x in v):
                raise ApiError(422, "invalid_config_value", f"'{k}' must be a short list of strings.")
            out[k] = v
        else:
            if not isinstance(v, str) or len(v) > 100:
                raise ApiError(422, "invalid_config_value", f"'{k}' must be a short string.")
            out[k] = v
    return out


def _monitor_out(m: MonitorSchedule) -> MonitorOut:
    return MonitorOut(id=m.id, kind=m.kind, name=m.name, frequency=m.frequency, config=m.config or {}, enabled=m.enabled, last_run_at=m.last_run_at,
                      next_run_at=m.next_run_at, last_result=m.last_result or {}, created_by=m.created_by)


async def _monitor(session: AsyncSession, mid: int) -> MonitorSchedule:
    m = await session.get(MonitorSchedule, mid)
    if m is None:
        raise ApiError(404, "not_found", "Monitor not found.")
    return m


@router.post("/monitors", response_model=MonitorOut, status_code=201)
async def create_monitor(body: MonitorIn, request: Request, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> MonitorOut:
    cfg = _clean_monitor_config(body.kind, body.config, uploads_dir(app_settings(request)))
    m = MonitorSchedule(kind=body.kind, name=body.name.strip(), frequency=body.frequency, config=cfg, enabled=body.enabled, last_result={}, created_by=user.id)
    session.add(m)
    await session.flush()
    await audit(session, user, "discovery.monitor_create", "monitor", m.id, {"after": {"kind": m.kind, "name": m.name, "frequency": m.frequency, "config": cfg, "enabled": m.enabled}})
    await session.commit()
    await session.refresh(m)
    return _monitor_out(m)


_MON_SORTS = {"id": SortSpec(MonitorSchedule.id, "int")}


@router.get("/monitors", response_model=Page[MonitorOut])
async def list_monitors(kind: str | None = Query(None, max_length=24), enabled: bool | None = None, limit: int = Query(50, ge=1, le=200), cursor: str | None = None,
                        session: AsyncSession = Depends(get_session)) -> Page[MonitorOut]:
    if kind and kind not in mon.KINDS:
        raise ApiError(422, "invalid_filter", f"'kind' must be one of: {', '.join(mon.KINDS)}.")
    q = select(MonitorSchedule, MonitorSchedule.id.label("_sv"), MonitorSchedule.id.label("_id"))
    if kind:
        q = q.where(MonitorSchedule.kind == kind)
    if enabled is not None:
        q = q.where(MonitorSchedule.enabled.is_(enabled))
    q = apply_keyset(q, _MON_SORTS, "id", "desc", MonitorSchedule.id, cursor, limit)
    rows, nxt = finish_page((await session.execute(q)).all(), "id", "desc", limit)
    return Page[MonitorOut](items=[_monitor_out(r[0]) for r in rows], next_cursor=nxt)


@router.get("/monitors/{monitor_id}", response_model=MonitorOut)
async def get_monitor(monitor_id: int, session: AsyncSession = Depends(get_session)) -> MonitorOut:
    return _monitor_out(await _monitor(session, monitor_id))


@router.patch("/monitors/{monitor_id}", response_model=MonitorOut)
async def update_monitor(monitor_id: int, body: MonitorPatch, request: Request, user: User = Depends(require_role("operator")),
                         session: AsyncSession = Depends(get_session)) -> MonitorOut:
    m = await _monitor(session, monitor_id)
    before = {"name": m.name, "frequency": m.frequency, "config": m.config, "enabled": m.enabled}
    if body.name is not None:
        m.name = body.name.strip()
    if body.frequency is not None and body.frequency != m.frequency:
        m.frequency = body.frequency
        m.next_run_at = mon.next_run_after(body.frequency, m.last_run_at) if m.last_run_at else None
    if body.config is not None:
        m.config = _clean_monitor_config(m.kind, body.config, uploads_dir(app_settings(request)))
    if body.enabled is not None:
        m.enabled = body.enabled
    await audit(session, user, "discovery.monitor_enable" if body.enabled is True and body.model_fields_set == {"enabled"} else
                "discovery.monitor_disable" if body.enabled is False and body.model_fields_set == {"enabled"} else "discovery.monitor_update",
                "monitor", m.id, {"before": before, "after": {"name": m.name, "frequency": m.frequency, "config": m.config, "enabled": m.enabled}})
    await session.commit()
    await session.refresh(m)
    return _monitor_out(m)


@router.delete("/monitors/{monitor_id}", status_code=204)
async def delete_monitor(monitor_id: int, user: User = Depends(require_role("admin")), session: AsyncSession = Depends(get_session)) -> None:
    m = await _monitor(session, monitor_id)
    await audit(session, user, "discovery.monitor_delete", "monitor", m.id, {"before": {"kind": m.kind, "name": m.name, "frequency": m.frequency, "config": m.config}})
    await session.delete(m)
    await session.commit()


@router.post("/monitors/{monitor_id}/run-now", response_model=RunNowOut)
async def run_monitor_now(monitor_id: int, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> RunNowOut:
    """Make the monitor due and queue a ``monitor`` job for the worker (the recurring tick would pick it up as well)."""
    m = await _monitor(session, monitor_id)
    if not m.enabled:
        raise ApiError(409, "monitor_disabled", "Enable the monitor before running it.")
    m.next_run_at = datetime.now(UTC)
    rid = await ensure_system_run(session, "discovery-monitors")
    jid = await jq.enqueue(session, run_id=rid, stage="monitor", dedupe_key=f"now:{m.id}:{int(time.time())}", payload={"limit": 20, "monitor_id": m.id}, priority=40)
    await audit(session, user, "discovery.monitor_run_now", "monitor", m.id, {"job_id": jid})
    await session.commit()
    return RunNowOut(monitor_id=m.id, queued=jid is not None, job_id=jid, run_id=rid)


# --------------------------------------------------------------------------- lookalike search
def _seed(body: SeedIn) -> dict[str, int]:
    seed = {k: v for k, v in (("org_id", body.org_id), ("category_id", body.category_id), ("run_id", body.run_id)) if v}
    if not seed:
        raise ApiError(422, "seed_required", "Provide one of org_id, category_id or run_id as the seed.")
    return seed


@router.post("/similar", response_model=SimilarOut)
async def similar_businesses(body: SeedIn, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> SimilarOut:
    """Explainable lookalikes of a seed organisation / category / run (deterministic scoring; nothing is queued)."""
    seed = _seed(body)
    found = await find_similar(session, seed, limit=body.limit, countries=body.countries or None, min_score=body.min_score)
    items = [SimilarItem(org_id=c.org_id, name=c.name, country_code=c.country_code, city=c.city, score=c.score, explanation=c.explain(), factors=c.factors)
             for c in found]
    return SimilarOut(seed=seed, items=items)


@router.post("/similar/expand", response_model=SimilarExpandOut)
async def similar_expand(body: SimilarExpandIn, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> SimilarExpandOut:
    """Queue the best lookalikes into a run (``resolve_domain`` / ``crawl`` jobs; candidates already in the run are skipped)."""
    seed = _seed(body)
    run = await session.get(SourcingRun, body.target_run_id)
    if run is None:
        raise ApiError(404, "not_found", "Target run not found.")
    if run.status in ("completed", "failed", "cancelled", "stopping"):
        raise ApiError(409, "run_not_active", f"The run is {run.status}; choose a draft, running or paused run.")
    out = await expand_from_seed(session, seed, run_id=run.id, limit=body.limit, countries=body.countries or None, min_score=body.min_score)
    await audit(session, user, "discovery.similar_expand", "run", run.id, {"seed": seed, "queued": len(out), "org_ids": [o["org_id"] for o in out][:50]})
    await session.commit()
    return SimilarExpandOut(target_run_id=run.id, queued=out)


_ = (SOURCE_TYPES, get_adapter)
