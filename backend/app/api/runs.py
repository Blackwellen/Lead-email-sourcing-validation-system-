"""Sourcing runs: list/create/detail/lifecycle/jobs/SSE/estimate/validate."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, literal, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .. import jobqueue as jq
from .. import runs as core
from ..config import Settings
from ..db import get_session, sessionmaker
from ..models import (
    ContactEvidence,
    EmailAddress,
    Organisation,
    RunOrganisation,
    RunYield,
    SourcingJob,
    SourcingRun,
    User,
)
from ..run_config import RunConfig
from ..security import SESSION_COOKIE, ApiError, app_settings, audit, require_role, resolve_session
from ..system_runs import not_system_filter
from .common import Ok, Page, SortSpec, apply_keyset, check_enum, clean_q, finish_page, prefix_pattern
from .contacts import ContactRow, contact_rows
from .runs_support import EstimateOut, FieldError, ValidateOut, estimate_config, validate_config

log = logging.getLogger("api.runs")
router = APIRouter(prefix="/api/runs", tags=["runs"], dependencies=[Depends(require_role("viewer"))])

TABS = {
    "active": ["running", "queued", "pausing", "stopping"],
    "scheduled": ["scheduled"],
    "paused": ["paused"],
    "completed": ["completed", "cancelled"],
    "failed": ["failed"],
    "drafts": ["draft"],
}
YIELD_BASED_NOT = {"distinct_companies", "companies_with_domains"}


# --------------------------------------------------------------------------- schemas
class RunSummary(BaseModel):
    id: int
    name: str
    status: str
    archived: bool
    target_count: int
    target_definition: str
    uniqueness: str
    progress: int
    percent: float
    created_at: datetime
    created_by: int | None = None
    scheduled_for: datetime | None = None
    started_at: datetime | None = None
    paused_at: datetime | None = None
    finished_at: datetime | None = None
    stop_reason: str | None = None
    category_id: int | None = None
    destination_category_id: int | None = None


class StopCondition(BaseModel):
    definition: str
    target: int
    progress: int
    percent: float
    max_runtime_minutes: int | None = None
    max_work_jobs: int | None = None
    stop_reason: str | None = None


class FunnelRow(BaseModel):
    stage: str
    state: str
    count: int


class ThroughputPoint(BaseModel):
    minute: datetime
    stage: str
    completed: int


class ThroughputOut(BaseModel):
    window_minutes: int
    series: list[ThroughputPoint]
    per_stage_per_minute: dict[str, float]
    yield_series: list[dict[str, Any]]
    yield_per_minute: float
    metrics: list[dict[str, Any]] = Field(default_factory=list)


class EtaOut(BaseModel):
    status: str  # estimated | unknown | complete
    reason: str | None = None
    remaining: int
    rate_per_minute_min: float | None = None
    rate_per_minute_max: float | None = None
    eta_seconds_min: int | None = None
    eta_seconds_max: int | None = None
    eta_at_min: datetime | None = None
    eta_at_max: datetime | None = None
    basis: str | None = None


class RateLimitedSource(BaseModel):
    source: str
    jobs: int
    last_error_code: str | None = None


class RunDetail(RunSummary):
    config: dict[str, Any]
    counters: dict[str, Any]
    stop_condition: StopCondition
    funnel: list[FunnelRow]
    queue_backlog: dict[str, int]
    throughput: ThroughputOut
    eta: EtaOut
    limiting_stage: str | None = None
    limiting_reason: str | None = None
    rate_limited_sources: list[RateLimitedSource]
    effective_concurrency: dict[str, Any]
    config_errors: list[FieldError] = Field(default_factory=list)


class RunCreateIn(BaseModel):
    config: RunConfig
    action: str = Field("draft", pattern="^(draft|start)$")


class RunDraftPatchIn(BaseModel):
    config: dict[str, Any] = Field(description="Partial wizard payload; top-level keys replace the stored ones")
    name: str | None = Field(None, min_length=1, max_length=200)


class DraftSaved(BaseModel):
    run: RunSummary
    valid: bool
    errors: list[FieldError]


class RetryOut(BaseModel):
    run: RunSummary
    retried_jobs: int


class JobRow(BaseModel):
    id: int
    stage: str
    state: str
    dedupe_key: str
    attempts: int
    error_code: str | None = None
    error_detail: str | None = None
    host_key: str | None = None
    next_attempt_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    payload: dict[str, Any]


class TabCounts(BaseModel):
    active: int
    scheduled: int
    paused: int
    completed: int
    failed: int
    drafts: int
    archived: int


class OrgRow(BaseModel):
    organisation_id: int
    name: str | None
    country_code: str
    region: str | None = None
    city: str | None = None
    stage: str
    category_id: int | None = None
    discovery_source: str | None = None


def _pct(progress: int, target: int) -> float:
    return round(min(100.0, 100.0 * progress / target), 1) if target else 0.0


def summary(run: SourcingRun) -> RunSummary:
    c = run.counters or {}
    prog = int(c.get("progress", core.progress_value(run.target_definition, c)) if c else 0)
    return RunSummary(
        id=run.id, name=run.name, status=run.status, archived=run.archived, target_count=run.target_count,
        target_definition=run.target_definition, uniqueness=run.uniqueness, progress=prog, percent=_pct(prog, run.target_count),
        created_at=run.created_at, created_by=run.created_by, scheduled_for=run.scheduled_for, started_at=run.started_at,
        paused_at=run.paused_at, finished_at=run.finished_at, stop_reason=run.stop_reason, category_id=run.category_id,
        destination_category_id=run.destination_category_id,
    )


async def _load(session: AsyncSession, run_id: int) -> SourcingRun:
    run = (await session.execute(select(SourcingRun).where(SourcingRun.id == run_id, not_system_filter()))).scalar_one_or_none()
    if run is None:
        raise ApiError(404, "not_found", "Run not found.")
    return run


def _state_error(e: core.RunStateError) -> ApiError:
    msg = str(e)
    if msg == "run not found":
        return ApiError(404, "not_found", "Run not found.")
    return ApiError(409, "invalid_run_state", msg[0].upper() + msg[1:] + ".")


def _snapshot_cfg(run: SourcingRun) -> tuple[RunConfig | None, list[FieldError]]:
    from pydantic import ValidationError

    try:
        return RunConfig.model_validate(run.configuration_snapshot), []
    except ValidationError as e:
        return None, [FieldError(field=".".join(str(p) for p in x["loc"]), message=str(x["msg"]).removeprefix("Value error, ")) for x in e.errors()]


# --------------------------------------------------------------------------- list / counts
SORTS = {
    "id": SortSpec(SourcingRun.id, "int"),
    "created_at": SortSpec(SourcingRun.created_at, "dt"),
    "name": SortSpec(func.lower(SourcingRun.name), "str"),
}


@router.get("", response_model=Page[RunSummary])
async def list_runs(
    tab: str | None = Query(None, description="active|scheduled|paused|completed|failed|drafts|archived"),
    q: str | None = Query(None, max_length=100),
    sort: str = "id", direction: str = "desc", limit: int = Query(50, ge=1, le=200), cursor: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> Page[RunSummary]:
    check_enum(tab, set(TABS) | {"archived"}, "tab")
    spec = SORTS.get(sort)
    stmt = select(SourcingRun, spec.column.label("_sv") if spec else literal(0).label("_sv"), SourcingRun.id.label("_id")).where(not_system_filter())
    if tab == "archived":
        stmt = stmt.where(SourcingRun.archived.is_(True))
    else:
        stmt = stmt.where(SourcingRun.archived.is_(False))
        if tab:
            stmt = stmt.where(SourcingRun.status.in_(TABS[tab]))
    if clean_q(q):
        stmt = stmt.where(func.lower(SourcingRun.name).like(prefix_pattern(q), escape="\\"))
    stmt = apply_keyset(stmt, SORTS, sort, direction, SourcingRun.id, cursor, limit)
    rows, nxt = finish_page((await session.execute(stmt)).all(), sort, direction, limit)
    return Page[RunSummary](items=[summary(r[0]) for r in rows], next_cursor=nxt)


@router.get("/tab-counts", response_model=TabCounts)
async def run_tab_counts(session: AsyncSession = Depends(get_session)) -> TabCounts:
    rows = (await session.execute(select(SourcingRun.status, SourcingRun.archived, func.count()).where(not_system_filter()).group_by(SourcingRun.status, SourcingRun.archived))).all()
    out = dict.fromkeys(TABS, 0) | {"archived": 0}
    for status, archived, n in rows:
        if archived:
            out["archived"] += n
            continue
        for tab, sts in TABS.items():
            if status in sts:
                out[tab] += n
    return TabCounts(**out)


# --------------------------------------------------------------------------- estimate / validate
@router.post("/validate", response_model=ValidateOut, dependencies=[Depends(require_role("operator"))])
async def validate_run_config(payload: dict[str, Any], request: Request, session: AsyncSession = Depends(get_session)) -> ValidateOut:
    _, out = await validate_config(session, payload, app_settings(request))
    return out


@router.post("/estimate", response_model=EstimateOut, dependencies=[Depends(require_role("operator"))])
async def estimate_run(payload: dict[str, Any], request: Request, session: AsyncSession = Depends(get_session)) -> EstimateOut:
    return await estimate_config(session, payload, app_settings(request))


# --------------------------------------------------------------------------- create / draft
@router.post("", response_model=RunSummary, status_code=201)
async def create_run(body: RunCreateIn, request: Request, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> RunSummary:
    cfg, v = await validate_config(session, body.config.model_dump(mode="json"), app_settings(request))
    if not v.valid:
        raise ApiError(422, "invalid_run_config", "The run configuration has problems: " + "; ".join(f"{e.field}: {e.message}" for e in v.errors[:5]), [e.model_dump() for e in v.errors])
    assert cfg is not None
    run = await core.create_run(session, cfg, user_id=user.id, status="draft")
    scheduled_dt = None
    if cfg.schedule_at:
        scheduled_dt = datetime.fromisoformat(cfg.schedule_at.replace("Z", "+00:00"))
        if scheduled_dt.tzinfo is None:
            scheduled_dt = scheduled_dt.replace(tzinfo=UTC)
    if body.action == "start":
        if scheduled_dt:
            run.status, run.scheduled_for = "scheduled", scheduled_dt
        else:
            try:
                await core.start_run(session, run.id)
            except core.RunStateError as e:
                raise _state_error(e) from None
    elif scheduled_dt:
        run.scheduled_for = scheduled_dt
    await audit(session, user, "run.create", "run", run.id, {"name": run.name, "action": body.action, "status": run.status})
    await session.commit()
    return summary(run)


@router.patch("/{run_id}", response_model=DraftSaved)
async def autosave_draft(run_id: int, body: RunDraftPatchIn, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> DraftSaved:
    run = (await session.execute(select(SourcingRun).where(SourcingRun.id == run_id, not_system_filter()).with_for_update())).scalar_one_or_none()
    if run is None:
        raise ApiError(404, "not_found", "Run not found.")
    if run.status not in ("draft", "scheduled"):
        raise ApiError(409, "invalid_run_state", f"Only drafts can be edited; this run is {run.status}.")
    merged = dict(run.configuration_snapshot or {}) | body.config
    if body.name:
        merged["name"] = body.name
    cfg, errors = _snapshot_cfg_from(merged)
    if cfg is not None:
        merged = cfg.model_dump(mode="json")
        run.name, run.target_count = cfg.name, cfg.target.count
        run.target_definition, run.uniqueness = cfg.target.definition.value, cfg.target.uniqueness.value
        run.category_id = cfg.category_ids[0] if cfg.category_ids else None
        run.destination_category_id = cfg.destination_category_id
    elif isinstance(merged.get("name"), str) and merged["name"].strip():
        run.name = merged["name"][:200]
    run.configuration_snapshot = merged
    await audit(session, user, "run.draft_save", "run", run.id, {"valid": cfg is not None})
    await session.commit()
    return DraftSaved(run=summary(run), valid=cfg is not None, errors=errors)


def _snapshot_cfg_from(raw: dict[str, Any]) -> tuple[RunConfig | None, list[FieldError]]:
    from pydantic import ValidationError

    try:
        return RunConfig.model_validate(raw), []
    except ValidationError as e:
        return None, [FieldError(field=".".join(str(p) for p in x["loc"]), message=str(x["msg"]).removeprefix("Value error, ")) for x in e.errors()]


@router.delete("/{run_id}", response_model=Ok)
async def delete_draft(run_id: int, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> Ok:
    run = await _load(session, run_id)
    if run.status != "draft":
        raise ApiError(409, "invalid_run_state", f"Only drafts can be deleted; this run is {run.status}. Archive it instead.")
    await audit(session, user, "run.delete_draft", "run", run.id, {"name": run.name})
    await session.delete(run)
    await session.commit()
    return Ok(message="Draft deleted.")


# --------------------------------------------------------------------------- lifecycle actions
async def _transition(session: AsyncSession, user: User, run_id: int, action: str, fn) -> RunSummary:
    await _load(session, run_id)
    try:
        run = await fn()
    except core.RunStateError as e:
        await session.rollback()
        raise _state_error(e) from None
    await audit(session, user, f"run.{action}", "run", run_id, {"status": run.status})
    await session.commit()
    return summary(run)


@router.post("/{run_id}/start", response_model=RunSummary)
async def start_run(run_id: int, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> RunSummary:
    run = await _load(session, run_id)
    if run.status in ("draft", "scheduled", "failed", "queued"):
        cfg, errors = _snapshot_cfg(run)
        if cfg is None:
            raise ApiError(422, "invalid_run_config", "This draft is incomplete: " + "; ".join(f"{e.field}: {e.message}" for e in errors[:5]), [e.model_dump() for e in errors])
    return await _transition(session, user, run_id, "start", lambda: core.start_run(session, run_id))


@router.post("/{run_id}/pause", response_model=RunSummary)
async def pause_run(run_id: int, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> RunSummary:
    return await _transition(session, user, run_id, "pause", lambda: core.pause_run(session, run_id))


@router.post("/{run_id}/resume", response_model=RunSummary)
async def resume_run(run_id: int, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> RunSummary:
    return await _transition(session, user, run_id, "resume", lambda: core.resume_run(session, run_id))


@router.post("/{run_id}/stop", response_model=RunSummary)
async def stop_run(run_id: int, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> RunSummary:
    return await _transition(session, user, run_id, "stop", lambda: core.stop_run(session, run_id))


@router.post("/{run_id}/retry-failed", response_model=RetryOut)
async def retry_failed_jobs(run_id: int, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> RetryOut:
    run = await _load(session, run_id)
    if run.status in ("draft", "scheduled"):
        raise ApiError(409, "invalid_run_state", f"A run in state {run.status} has no jobs to retry.")
    n = await core.retry_failed(session, run_id)
    await audit(session, user, "run.retry_failed", "run", run_id, {"jobs": n})
    await session.commit()
    await session.refresh(run)
    return RetryOut(run=summary(run), retried_jobs=n)


@router.post("/{run_id}/archive", response_model=RunSummary)
async def archive_run(run_id: int, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> RunSummary:
    return await _transition(session, user, run_id, "archive", lambda: core.archive_run(session, run_id))


@router.post("/{run_id}/duplicate", response_model=RunSummary, status_code=201)
async def duplicate_run(run_id: int, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> RunSummary:
    await _load(session, run_id)
    try:
        run = await core.duplicate_run(session, run_id, user.id)
    except core.RunStateError as e:
        raise _state_error(e) from None
    except Exception as e:  # source snapshot no longer valid
        raise ApiError(409, "invalid_run_config", f"This run's stored configuration can no longer be copied: {e}") from None
    await audit(session, user, "run.duplicate", "run", run.id, {"from": run_id})
    await session.commit()
    return summary(run)


# --------------------------------------------------------------------------- detail
async def throughput(session: AsyncSession, run_id: int, window: int) -> ThroughputOut:
    rows = (await session.execute(
        text("""SELECT date_trunc('minute', finished_at) AS m, stage, count(*) FROM sourcing_jobs
                WHERE run_id=:r AND state='done' AND finished_at > now() - make_interval(mins => :w)
                GROUP BY 1,2 ORDER BY 1"""), {"r": run_id, "w": window})).all()
    yrows = (await session.execute(
        text("""SELECT date_trunc('minute', created_at) AS m, count(*) FROM run_yield
                WHERE run_id=:r AND created_at > now() - make_interval(mins => :w) GROUP BY 1 ORDER BY 1"""), {"r": run_id, "w": window})).all()
    mrows = (await session.execute(text("SELECT bucket, metrics FROM run_metrics WHERE run_id=:r ORDER BY bucket DESC LIMIT :n"), {"r": run_id, "n": window})).all()
    per: dict[str, int] = {}
    for _, stage, n in rows:
        per[stage] = per.get(stage, 0) + n
    return ThroughputOut(
        window_minutes=window,
        series=[ThroughputPoint(minute=m, stage=st, completed=n) for m, st, n in rows],
        per_stage_per_minute={k: round(v / window, 2) for k, v in per.items()},
        yield_series=[{"minute": m.isoformat(), "count": n} for m, n in yrows],
        yield_per_minute=round(sum(n for _, n in yrows) / window, 3),
        metrics=[{"bucket": b.isoformat(), **(m or {})} for b, m in reversed(mrows)],
    )


async def compute_eta(session: AsyncSession, run: SourcingRun, progress: int) -> EtaOut:
    remaining = max(0, run.target_count - progress)
    if remaining == 0:
        return EtaOut(status="complete", remaining=0)
    if run.status != "running":
        return EtaOut(status="unknown", remaining=remaining, reason=f"The run is {run.status}; no ETA while it is not running.")
    if run.target_definition in YIELD_BASED_NOT:
        return EtaOut(status="unknown", remaining=remaining, reason="Progress for this target type has no timestamped history, so a rate cannot be observed.")
    now = datetime.now(UTC)
    elapsed_min = max(1.0, (now - (run.started_at or now)).total_seconds() / 60)
    rows = (await session.execute(text(
        """SELECT count(*) FILTER (WHERE created_at > now() - interval '5 minutes'),
                  count(*) FILTER (WHERE created_at > now() - interval '15 minutes'),
                  count(*) FILTER (WHERE created_at > now() - interval '60 minutes') FROM run_yield WHERE run_id=:r"""), {"r": run.id})).one()
    c5, c15, c60 = (int(x) for x in rows)
    if c60 < 5:
        return EtaOut(status="unknown", remaining=remaining, reason=f"Only {c60} result(s) in the last hour; not enough data for a trustworthy estimate yet.")
    rates = [r for r in (c5 / min(5.0, elapsed_min), c15 / min(15.0, elapsed_min), c60 / min(60.0, elapsed_min)) if r > 0]
    lo, hi = min(rates), max(rates)
    from datetime import timedelta

    s_min, s_max = int(remaining / hi * 60), int(remaining / lo * 60)
    return EtaOut(
        status="estimated", remaining=remaining, rate_per_minute_min=round(lo, 3), rate_per_minute_max=round(hi, 3),
        eta_seconds_min=s_min, eta_seconds_max=s_max, eta_at_min=now + timedelta(seconds=s_min), eta_at_max=now + timedelta(seconds=s_max),
        basis="Observed qualified-result rate over the last 5, 15 and 60 minutes (range = slowest to fastest window).",
    )


@router.get("/{run_id}", response_model=RunDetail)
async def get_run(run_id: int, request: Request, window_minutes: int = Query(15, ge=1, le=240), session: AsyncSession = Depends(get_session)) -> RunDetail:
    run = await _load(session, run_id)
    counters = dict(run.counters or {})
    if not counters:
        counters = await core.compute_counters(session, run_id)
        counters["progress"] = core.progress_value(run.target_definition, counters)
    progress = int(counters.get("progress", 0))
    cfg, cfg_errors = _snapshot_cfg(run)
    funnel = [FunnelRow(stage=s, state=st, count=n) for s, st, n in (await session.execute(
        select(SourcingJob.stage, SourcingJob.state, func.count()).where(SourcingJob.run_id == run_id).group_by(SourcingJob.stage, SourcingJob.state).order_by(SourcingJob.stage, SourcingJob.state))).all()]
    backlog: dict[str, int] = {}
    for f in funnel:
        if f.state in ("queued", "leased"):
            backlog[f.stage] = backlog.get(f.stage, 0) + f.count
    tp = await throughput(session, run_id, window_minutes)
    limiting, reason = None, None
    if backlog:
        ranked = sorted(backlog.items(), key=lambda kv: (-kv[1], tp.per_stage_per_minute.get(kv[0], 0.0)))
        limiting = ranked[0][0]
        rate = tp.per_stage_per_minute.get(limiting, 0.0)
        reason = f"Largest backlog ({ranked[0][1]} job(s) waiting or running); throughput {rate}/min over the last {window_minutes} min."
    rl = (await session.execute(text(
        """SELECT coalesce(payload->>'adapter_id', host_key, stage) AS src, count(*), max(error_code) FROM sourcing_jobs
           WHERE run_id=:r AND error_code IN ('rate_limited','http_429','429','quota_exceeded') AND state IN ('queued','failed','dead','leased')
           GROUP BY 1 ORDER BY 2 DESC LIMIT 20"""), {"r": run_id})).all()
    settings: Settings = app_settings(request)
    leased = sum(f.count for f in funnel if f.state == "leased")
    workers = (await session.execute(text("SELECT count(*) FROM worker_heartbeats WHERE last_seen_at > now() - make_interval(secs => :t)"), {"t": settings.worker_heartbeat_seconds * 4})).scalar_one()
    perf = cfg.performance if cfg else None
    conc = {
        "configured_http_concurrency": perf.http_concurrency if perf else None,
        "configured_per_host": perf.per_host_concurrency if perf else None,
        "configured_worker_limit": perf.worker_limit if perf else None,
        "preset": perf.preset if perf else None,
        "jobs_running_now": leased,
        "workers_online": int(workers),
        "worker_slots_total": int(workers) * settings.worker_concurrency,
        "effective": min(x for x in (int(workers) * settings.worker_concurrency, (perf.http_concurrency if perf and perf.http_concurrency else 10**9), (perf.worker_limit * settings.worker_concurrency) if perf and perf.worker_limit else 10**9)) if workers else 0,
    }
    s = summary(run)
    return RunDetail(
        **s.model_dump(), config=run.configuration_snapshot or {}, counters=counters,
        stop_condition=StopCondition(
            definition=run.target_definition, target=run.target_count, progress=progress, percent=_pct(progress, run.target_count),
            max_runtime_minutes=cfg.target.max_runtime_minutes if cfg else None, max_work_jobs=cfg.target.max_work_jobs if cfg else None, stop_reason=run.stop_reason,
        ),
        funnel=funnel, queue_backlog=backlog, throughput=tp, eta=await compute_eta(session, run, progress),
        limiting_stage=limiting, limiting_reason=reason,
        rate_limited_sources=[RateLimitedSource(source=r[0], jobs=r[1], last_error_code=r[2]) for r in rl],
        effective_concurrency=conc, config_errors=cfg_errors,
    )


# --------------------------------------------------------------------------- jobs / contacts / organisations
JOB_STATES = {"queued", "leased", "done", "failed", "dead", "cancelled", "paused"}


@router.get("/{run_id}/jobs", response_model=Page[JobRow])
async def list_run_jobs(
    run_id: int, state: str | None = Query("failed,dead", description="comma separated job states"), stage: str | None = Query(None, max_length=24),
    error_code: str | None = Query(None, max_length=80), limit: int = Query(50, ge=1, le=200), cursor: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> Page[JobRow]:
    await _load(session, run_id)
    states = [x for x in (state or "").split(",") if x]
    if any(x not in JOB_STATES for x in states):
        raise ApiError(422, "invalid_filter", f"state must be a comma separated list of: {', '.join(sorted(JOB_STATES))}.")
    sorts = {"id": SortSpec(SourcingJob.id, "int")}
    stmt = select(SourcingJob, SourcingJob.id.label("_sv"), SourcingJob.id.label("_id")).where(SourcingJob.run_id == run_id)
    if states:
        stmt = stmt.where(SourcingJob.state.in_(states))
    if stage:
        stmt = stmt.where(SourcingJob.stage == stage)
    if error_code:
        stmt = stmt.where(SourcingJob.error_code == error_code)
    stmt = apply_keyset(stmt, sorts, "id", "desc", SourcingJob.id, cursor, limit)
    rows, nxt = finish_page((await session.execute(stmt)).all(), "id", "desc", limit)
    items = [JobRow(id=j.id, stage=j.stage, state=j.state, dedupe_key=j.dedupe_key, attempts=j.attempts, error_code=j.error_code, error_detail=j.error_detail,
                    host_key=j.host_key, next_attempt_at=j.next_attempt_at, started_at=j.started_at, finished_at=j.finished_at, payload=j.payload) for j, *_ in rows]
    return Page[JobRow](items=items, next_cursor=nxt)


@router.post("/{run_id}/jobs/{job_id}/retry", response_model=JobRow)
async def retry_run_job(run_id: int, job_id: int, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> JobRow:
    await _load(session, run_id)
    job = (await session.execute(select(SourcingJob).where(SourcingJob.id == job_id, SourcingJob.run_id == run_id))).scalar_one_or_none()
    if job is None:
        raise ApiError(404, "not_found", "Job not found in this run.")
    if job.state not in ("failed", "dead"):
        raise ApiError(409, "invalid_job_state", f"Only failed or dead jobs can be retried; this job is {job.state}.")
    await jq.retry_jobs(session, run_id=run_id, job_ids=[job_id])
    await audit(session, user, "run.retry_job", "job", job_id, {"run_id": run_id, "stage": job.stage})
    await session.commit()
    await session.refresh(job)
    return JobRow(id=job.id, stage=job.stage, state=job.state, dedupe_key=job.dedupe_key, attempts=job.attempts, error_code=job.error_code, error_detail=job.error_detail,
                  host_key=job.host_key, next_attempt_at=job.next_attempt_at, started_at=job.started_at, finished_at=job.finished_at, payload=job.payload)


@router.get("/{run_id}/contacts", response_model=Page[ContactRow])
async def list_run_contacts(
    run_id: int, qualified_only: bool = False, sort: str = "id", direction: str = "desc", limit: int = Query(50, ge=1, le=500), cursor: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> Page[ContactRow]:
    await _load(session, run_id)
    if qualified_only:
        cond = EmailAddress.id.in_(select(RunYield.email_id).where(RunYield.run_id == run_id))
    else:
        cond = EmailAddress.id.in_(select(ContactEvidence.email_id).where(ContactEvidence.run_id == run_id, ContactEvidence.email_id.is_not(None)))
    return await contact_rows(session, [cond], sort, direction, limit, cursor, with_total=False)


@router.get("/{run_id}/organisations", response_model=Page[OrgRow])
async def list_run_organisations(run_id: int, stage: str | None = Query(None, max_length=24), limit: int = Query(50, ge=1, le=500), cursor: str | None = None, session: AsyncSession = Depends(get_session)) -> Page[OrgRow]:
    await _load(session, run_id)
    sorts = {"id": SortSpec(RunOrganisation.organisation_id, "int")}
    stmt = (
        select(RunOrganisation.organisation_id, RunOrganisation.stage, Organisation.legal_name, Organisation.trading_name, Organisation.country_code, Organisation.region,
               Organisation.city, Organisation.category_id, Organisation.discovery_source, RunOrganisation.organisation_id.label("_sv"), RunOrganisation.organisation_id.label("_id"))
        .join(Organisation, Organisation.id == RunOrganisation.organisation_id).where(RunOrganisation.run_id == run_id)
    )
    if stage:
        stmt = stmt.where(RunOrganisation.stage == stage)
    stmt = apply_keyset(stmt, sorts, "id", "asc", RunOrganisation.organisation_id, cursor, limit)
    rows, nxt = finish_page((await session.execute(stmt)).all(), "id", "asc", limit)
    return Page[OrgRow](items=[OrgRow(organisation_id=r[0], stage=r[1], name=r[2] or r[3], country_code=r[4], region=r[5], city=r[6], category_id=r[7], discovery_source=r[8]) for r in rows], next_cursor=nxt)


# --------------------------------------------------------------------------- SSE
async def sse_user(request: Request) -> User:
    """Cookie auth for EventSource without keeping a DB connection for the stream's lifetime."""
    async with sessionmaker()() as s:
        found = await resolve_session(s, request.cookies.get(SESSION_COOKIE))
    if found is None:
        raise ApiError(401, "not_authenticated", "You are not signed in, or your session has expired. Please sign in again.")
    return found[0]


async def _snapshot(run_id: int) -> dict[str, Any] | None:
    async with sessionmaker()() as s:
        run = (await s.execute(select(SourcingRun).where(SourcingRun.id == run_id))).scalar_one_or_none()
        if run is None:
            return None
        states = {st: n for st, n in (await s.execute(select(SourcingJob.state, func.count()).where(SourcingJob.run_id == run_id).group_by(SourcingJob.state))).all()}
        done_1m = (await s.execute(text("SELECT count(*) FROM sourcing_jobs WHERE run_id=:r AND state='done' AND finished_at > now() - interval '1 minute'"), {"r": run_id})).scalar_one()
        yield_1m = (await s.execute(text("SELECT count(*) FROM run_yield WHERE run_id=:r AND created_at > now() - interval '1 minute'"), {"r": run_id})).scalar_one()
        qy = int((await s.execute(text("SELECT count(*) FROM run_yield WHERE run_id=:r"), {"r": run_id})).scalar_one())
    c = dict(run.counters or {})
    prog = int(c.get("progress", qy if run.target_definition not in YIELD_BASED_NOT else 0))
    return {
        "run_id": run_id, "status": run.status, "stop_reason": run.stop_reason, "at": datetime.now(UTC).isoformat(),
        "counters": c, "progress": prog, "target": run.target_count, "percent": _pct(prog, run.target_count),
        "job_states": states, "throughput": {"jobs_done_last_minute": int(done_1m), "qualified_last_minute": int(yield_1m)},
        "qualified_total": qy,
    }


@router.get("/{run_id}/events", response_class=StreamingResponse, responses={200: {"content": {"text/event-stream": {}}, "description": "Server-sent events: `snapshot` every interval, `: heartbeat` comments"}})
async def run_events(
    run_id: int, request: Request, interval: float = Query(2.0, ge=0.2, le=30), max_events: int | None = Query(None, ge=1, le=100000, description="Close after N snapshots (default: until the client disconnects or the run ends)"),
    _user: User = Depends(sse_user),
):
    async with sessionmaker()() as s:
        exists = (await s.execute(select(SourcingRun.id).where(SourcingRun.id == run_id))).scalar_one_or_none()
    if exists is None:
        raise ApiError(404, "not_found", "Run not found.")

    async def gen():
        sent = 0
        last_hb = asyncio.get_event_loop().time()
        yield "retry: 3000\n\n"
        while True:
            if await request.is_disconnected():
                return
            try:
                snap = await _snapshot(run_id)
            except Exception:
                log.exception("sse snapshot failed")
                yield "event: error\ndata: {\"message\": \"snapshot failed\"}\n\n"
                return
            if snap is None:
                yield "event: closed\ndata: {\"reason\": \"run_deleted\"}\n\n"
                return
            yield f"event: snapshot\nid: {sent}\ndata: {json.dumps(snap, default=str)}\n\n"
            sent += 1
            if max_events and sent >= max_events:
                return
            if snap["status"] in ("completed", "failed", "cancelled", "archived") and sent > 1:
                yield "event: closed\ndata: {\"reason\": \"run_finished\"}\n\n"
                return
            waited = 0.0
            while waited < interval:
                step = min(0.5, interval - waited)
                await asyncio.sleep(step)
                waited += step
                if await request.is_disconnected():
                    return
            now = asyncio.get_event_loop().time()
            if now - last_hb > 15:
                yield ": heartbeat\n\n"
                last_hb = now

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})

