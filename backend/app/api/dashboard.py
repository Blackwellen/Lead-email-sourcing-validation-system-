"""Dashboard overview: real SQL aggregates (approximate for very large tables)."""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..security import app_settings, require_role
from .common import table_count, table_estimate

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"], dependencies=[Depends(require_role("viewer"))])

BIG = 1_000_000  # above this many rows aggregates are sampled and cached
CACHE_TTL_S = 30
_cache: dict[str, tuple[float, Any]] = {}


class Count(BaseModel):
    value: int
    approximate: bool = False


class Bucket(BaseModel):
    key: str
    count: int


class DayPoint(BaseModel):
    day: str
    discoveries: int = 0
    validations: int = 0


class WorkerHealth(BaseModel):
    worker_id: str
    hostname: str | None
    last_seen_seconds_ago: float
    healthy: bool
    rss_mb: float | None
    cpu_percent: float | None
    active_jobs: int


class FailedJob(BaseModel):
    id: int
    run_id: int | None
    stage: str
    state: str
    error_code: str | None
    error_detail: str | None
    attempts: int
    finished_at: datetime | None


class ActiveRun(BaseModel):
    id: int
    name: str
    status: str
    target_count: int
    progress: int
    started_at: datetime | None


class RecentExport(BaseModel):
    id: int
    preset: str
    format: str
    status: str
    row_count: int
    created_at: datetime


class Geo(BaseModel):
    country: str
    region: str | None = None
    count: int


class Overview(BaseModel):
    generated_at: datetime
    approximate: bool
    total_businesses: Count
    businesses_with_confirmed_domains: Count
    published_contacts: Count
    inferred_candidates: Count
    reachability_distribution: list[Bucket]
    catch_all_count: Count
    unknown_count: Count
    active_runs_count: int
    active_runs: list[ActiveRun]
    recent_exports: list[RecentExport]
    validation_freshness: list[Bucket]
    workers: list[WorkerHealth]
    workers_healthy: int
    latest_failed_jobs: list[FailedJob]
    source_distribution: list[Bucket]
    country_distribution: list[Bucket]
    geographic_distribution: list[Geo]
    time_series: list[DayPoint]


def _scaled(n: int, pct: float) -> int:
    return int(round(n * 100.0 / pct)) if pct < 100 else int(n)


async def _sample_pct(session: AsyncSession, table: str) -> float:
    est = await table_estimate(session, table)
    if est <= BIG:
        return 100.0
    return max(0.05, min(100.0, 500_000 * 100.0 / est))


def _from(table: str, pct: float) -> str:
    return table if pct >= 100 else f"{table} TABLESAMPLE SYSTEM ({pct:.4f})"


@router.get("/overview", response_model=Overview)
async def overview(request: Request, session: AsyncSession = Depends(get_session)) -> Overview:
    settings = app_settings(request)
    cached = _cache.get("overview")
    if cached and time.monotonic() - cached[0] < CACHE_TTL_S:
        return cached[1]
    approx_any = False

    org_n, org_approx = await table_count(session, "organisations", BIG)
    em_n, em_approx = await table_count(session, "email_addresses", BIG)
    epct = await _sample_pct(session, "email_addresses")
    opct = await _sample_pct(session, "organisations")
    dpct = await _sample_pct(session, "organisation_domains")
    approx_any = org_approx or em_approx or epct < 100 or opct < 100 or dpct < 100

    # businesses with confirmed / high domains
    r = (await session.execute(text(f"SELECT count(DISTINCT organisation_id) FROM {_from('organisation_domains', dpct)} WHERE verified_match_status IN ('confirmed','high')"))).scalar_one()
    with_dom = Count(value=_scaled(r, dpct), approximate=dpct < 100)

    # e-mail aggregates (single pass)
    row = (await session.execute(text(
        f"""SELECT count(*) FILTER (WHERE origin IN ('published','corroborated')) AS pub,
                   count(*) FILTER (WHERE origin IN ('pattern_inferred','role_speculative')) AS inf,
                   count(*) FILTER (WHERE catch_all_status IN ('confirmed','suspected')) AS catchall,
                   count(*) FILTER (WHERE overall_reachability = 'unknown') AS unk,
                   count(*) FILTER (WHERE last_validated_at IS NULL) AS f_never,
                   count(*) FILTER (WHERE last_validated_at > now() - interval '7 days') AS f_7,
                   count(*) FILTER (WHERE last_validated_at <= now() - interval '7 days' AND last_validated_at > now() - interval '30 days') AS f_30,
                   count(*) FILTER (WHERE last_validated_at <= now() - interval '30 days' AND last_validated_at > now() - interval '90 days') AS f_90,
                   count(*) FILTER (WHERE last_validated_at <= now() - interval '90 days') AS f_old
            FROM {_from('email_addresses', epct)}"""))).one()
    pub, inf, catchall, unk = (_scaled(int(x), epct) for x in row[:4])
    fresh = [Bucket(key=k, count=_scaled(int(v), epct)) for k, v in zip(("never", "<7d", "7-30d", "30-90d", ">90d"), row[4:], strict=True)]
    reach = [Bucket(key=k or "unknown", count=_scaled(n, epct)) for k, n in (await session.execute(text(f"SELECT overall_reachability, count(*) FROM {_from('email_addresses', epct)} GROUP BY 1 ORDER BY 2 DESC"))).all()]

    # runs
    runs = (await session.execute(text(
        """SELECT id, name, status, target_count, COALESCE((counters->>'progress')::int, 0), started_at FROM sourcing_runs
           WHERE status IN ('running','pausing','stopping','queued') AND COALESCE(configuration_snapshot->>'system','false') <> 'true' ORDER BY id DESC LIMIT 10"""))).all()
    active_count = (await session.execute(text("SELECT count(*) FROM sourcing_runs WHERE status IN ('running','pausing','stopping','queued') AND COALESCE(configuration_snapshot->>'system','false') <> 'true'"))).scalar_one()
    exports = (await session.execute(text("SELECT id, preset, format, status, row_count, created_at FROM exports ORDER BY id DESC LIMIT 5"))).all()

    # workers
    healthy_after = settings.worker_heartbeat_seconds * 3
    wk = (await session.execute(text("SELECT worker_id, hostname, extract(epoch FROM now()-last_seen_at), rss_mb, cpu_percent, active_jobs FROM worker_heartbeats ORDER BY last_seen_at DESC LIMIT 50"))).all()
    workers = [WorkerHealth(worker_id=w[0], hostname=w[1], last_seen_seconds_ago=round(float(w[2]), 1), healthy=float(w[2]) <= healthy_after, rss_mb=w[3], cpu_percent=w[4], active_jobs=w[5]) for w in wk]

    failed = (await session.execute(text("SELECT id, run_id, stage, state, error_code, left(error_detail, 300), attempts, finished_at FROM sourcing_jobs WHERE state IN ('failed','dead') ORDER BY id DESC LIMIT 10"))).all()

    src = (await session.execute(text(f"SELECT COALESCE(discovery_source,'unknown'), count(*) FROM {_from('organisations', opct)} WHERE merged_into_id IS NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 20"))).all()
    geo = (await session.execute(text(f"SELECT country_code, region, count(*) FROM {_from('organisations', opct)} WHERE merged_into_id IS NULL GROUP BY 1,2 ORDER BY 3 DESC LIMIT 60"))).all()
    countries: dict[str, int] = {}
    for cc, _rg, n in (await session.execute(text(f"SELECT country_code, NULL, count(*) FROM {_from('organisations', opct)} WHERE merged_into_id IS NULL GROUP BY 1 ORDER BY 3 DESC LIMIT 60"))).all():
        countries[cc] = _scaled(n, opct)

    days: dict[str, DayPoint] = {}
    for d, n in (await session.execute(text(f"SELECT date_trunc('day', first_discovered_at)::date, count(*) FROM {_from('organisations', opct)} WHERE first_discovered_at > now() - interval '30 days' GROUP BY 1"))).all():
        days.setdefault(str(d), DayPoint(day=str(d))).discoveries = _scaled(n, opct)
    for d, n in (await session.execute(text(f"SELECT date_trunc('day', last_validated_at)::date, count(*) FROM {_from('email_addresses', epct)} WHERE last_validated_at > now() - interval '30 days' GROUP BY 1"))).all():
        days.setdefault(str(d), DayPoint(day=str(d))).validations = _scaled(n, epct)

    out = Overview(
        generated_at=datetime.now().astimezone(), approximate=approx_any,
        total_businesses=Count(value=org_n, approximate=org_approx), businesses_with_confirmed_domains=with_dom,
        published_contacts=Count(value=pub, approximate=epct < 100), inferred_candidates=Count(value=inf, approximate=epct < 100),
        reachability_distribution=reach, catch_all_count=Count(value=catchall, approximate=epct < 100), unknown_count=Count(value=unk, approximate=epct < 100),
        active_runs_count=active_count, active_runs=[ActiveRun(id=x[0], name=x[1], status=x[2], target_count=x[3], progress=x[4], started_at=x[5]) for x in runs],
        recent_exports=[RecentExport(id=x[0], preset=x[1], format=x[2], status=x[3], row_count=x[4], created_at=x[5]) for x in exports],
        validation_freshness=fresh, workers=workers, workers_healthy=sum(1 for w in workers if w.healthy),
        latest_failed_jobs=[FailedJob(id=x[0], run_id=x[1], stage=x[2], state=x[3], error_code=x[4], error_detail=x[5], attempts=x[6], finished_at=x[7]) for x in failed],
        source_distribution=[Bucket(key=k, count=_scaled(n, opct)) for k, n in src],
        country_distribution=[Bucket(key=k, count=v) for k, v in countries.items()],
        geographic_distribution=[Geo(country=cc, region=rg, count=_scaled(n, opct)) for cc, rg, n in geo],
        time_series=sorted(days.values(), key=lambda p: p.day),
    )
    if approx_any:
        _cache["overview"] = (time.monotonic(), out)
    return out
