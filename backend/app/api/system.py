"""System health and admin controls (pause-all, maintenance, restart-failed, SMTP egress check)."""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import time
from datetime import UTC, datetime
from typing import Any

import psutil
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import literal, text
from sqlalchemy.ext.asyncio import AsyncSession

from .. import jobqueue as jq
from ..db import get_session
from ..models import SourcingJob, User
from ..security import ApiError, app_settings, audit, require_role
from ..settings_store import get_setting, invalidate_maintenance_cache, set_setting
from .common import Page
from .dashboard import WorkerHealth
from .runs import JobRow, job_row, page_jobs

log = logging.getLogger("api.system")
router = APIRouter(prefix="/api/system", tags=["system"], dependencies=[Depends(require_role("viewer"))])
EGRESS_HOST, EGRESS_PORT = "gmail-smtp-in.l.google.com", 25


class QueueRow(BaseModel):
    stage: str
    state: str
    count: int


class ResourceInfo(BaseModel):
    cpu_percent: float
    cpu_count: int | None
    ram_total_mb: int
    ram_available_mb: int
    ram_percent: float
    disk_total_gb: float
    disk_free_gb: float
    disk_percent: float


class SmtpBrief(BaseModel):
    diagnostics_enabled: bool
    helo_host: str | None
    egress_port_25: str  # open | blocked | unknown (last explicit check)
    egress_checked_at: str | None = None


class SystemHealth(BaseModel):
    api: dict[str, Any]
    database: dict[str, Any]
    workers: list[WorkerHealth]
    workers_healthy: int
    queue: list[QueueRow]
    failed_jobs: int
    dead_jobs: int
    retrying_jobs: int
    jobs_by_attempts: dict[str, int]
    dns_timeout_rate: float | None
    dns_sample_size: int
    smtp: SmtpBrief
    browser_workers: dict[str, Any]
    resources: ResourceInfo
    maintenance: dict[str, Any]
    paused_by_pause_all: list[int]


class EgressResult(BaseModel):
    status: str
    host: str
    port: int
    detail: str
    checked_at: str


class PauseAllOut(BaseModel):
    paused_run_ids: list[int]


class ResumeOut(BaseModel):
    resumed_run_ids: list[int]
    skipped_run_ids: list[int]


class RestartOut(BaseModel):
    retried_jobs: int


class MaintenanceIn(BaseModel):
    enabled: bool
    message: str | None = Field(None, max_length=300)


class MaintenanceOut(BaseModel):
    enabled: bool
    message: str
    since: str | None = None


@router.get("/health", response_model=SystemHealth)
async def system_health(request: Request, session: AsyncSession = Depends(get_session)) -> SystemHealth:
    settings = app_settings(request)
    t0 = time.perf_counter()
    await session.execute(text("SELECT 1"))
    db_ms = round((time.perf_counter() - t0) * 1000, 1)
    healthy_after = settings.worker_heartbeat_seconds * 3
    wk = (await session.execute(text("SELECT worker_id, hostname, extract(epoch FROM now()-last_seen_at), rss_mb, cpu_percent, active_jobs FROM worker_heartbeats ORDER BY last_seen_at DESC LIMIT 100"))).all()
    workers = [WorkerHealth(worker_id=w[0], hostname=w[1], last_seen_seconds_ago=round(float(w[2]), 1), healthy=float(w[2]) <= healthy_after, rss_mb=w[3], cpu_percent=w[4], active_jobs=w[5]) for w in wk]
    q = (await session.execute(text("SELECT stage, state, count(*) FROM sourcing_jobs WHERE state IN ('queued','leased','failed','dead','paused') GROUP BY 1,2 ORDER BY 1,2"))).all()
    attempts = (await session.execute(text("SELECT least(attempts, 5), count(*) FROM sourcing_jobs WHERE state IN ('queued','leased') AND attempts > 0 GROUP BY 1"))).all()
    # DNS timeout rate over the most recent validation results (index-friendly: id range, not a time scan)
    d = (await session.execute(text(
        """SELECT count(*) FILTER (WHERE result IN ('timeout','temporary_failure') OR reason_code IN ('dns_timeout','temporary_dns_failure','timeout')), count(*)
           FROM validation_results WHERE id > COALESCE((SELECT max(id) FROM validation_results), 0) - 20000 AND check_type IN ('V02_domain','V03_mx')"""))).one()
    sample = int(d[1])
    egress = await get_setting(session, "smtp_egress_last", None) or {}
    vm = psutil.virtual_memory()
    try:
        du = psutil.disk_usage(settings.data_dir if __import__("os").path.exists(settings.data_dir) else "/")
    except OSError:
        du = psutil.disk_usage("/")
    from ..settings_store import maintenance_state

    maint = await maintenance_state(session)
    paused = (await get_setting(session, "pause_all", None) or {}).get("run_ids", [])
    by_state = {(r[0], r[1]): r[2] for r in q}
    pw = importlib.util.find_spec("playwright") is not None
    return SystemHealth(
        api={"status": "ok", "version": request.app.version, "env": settings.env}, database={"status": "ok", "latency_ms": db_ms},
        workers=workers, workers_healthy=sum(1 for w in workers if w.healthy), queue=[QueueRow(stage=r[0], state=r[1], count=r[2]) for r in q],
        failed_jobs=sum(v for (_, s), v in by_state.items() if s == "failed"), dead_jobs=sum(v for (_, s), v in by_state.items() if s == "dead"),
        retrying_jobs=sum(n for a, n in attempts if a > 1), jobs_by_attempts={str(a): n for a, n in attempts},
        dns_timeout_rate=round(int(d[0]) / sample, 4) if sample else None, dns_sample_size=sample,
        smtp=SmtpBrief(diagnostics_enabled=settings.smtp_diagnostics_enabled, helo_host=settings.smtp_helo_host or None, egress_port_25=egress.get("status", "unknown"), egress_checked_at=egress.get("checked_at")),
        browser_workers={"playwright_installed": pw, "available": pw, "note": None if pw else "Playwright is not installed; JavaScript-rendered pages cannot be crawled."},
        resources=ResourceInfo(cpu_percent=psutil.cpu_percent(interval=None), cpu_count=psutil.cpu_count(), ram_total_mb=round(vm.total / 1e6), ram_available_mb=round(vm.available / 1e6),
                               ram_percent=vm.percent, disk_total_gb=round(du.total / 1e9, 1), disk_free_gb=round(du.free / 1e9, 1), disk_percent=du.percent),
        maintenance={"enabled": maint["enabled"], "message": maint["message"], "since": maint["since"]}, paused_by_pause_all=paused,
    )


@router.post("/smtp-egress-check", response_model=EgressResult)
async def smtp_egress_check(user: User = Depends(require_role("admin")), session: AsyncSession = Depends(get_session)) -> EgressResult:
    """Open (and immediately close) a TCP connection to a well-known public MX on port 25. Sends nothing."""
    status, detail = "unknown", ""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(EGRESS_HOST, EGRESS_PORT), timeout=4)
        try:
            banner = await asyncio.wait_for(reader.readline(), timeout=2)
            detail = "Connected; server banner received." if banner.startswith(b"220") else "Connected."
        except (TimeoutError, OSError):
            detail = "Connected (no banner within 2 s)."
        writer.close()
        status = "open"
    except TimeoutError:
        status, detail = "blocked", "Connection timed out after 4 s; outbound port 25 is probably filtered by your network or provider."
    except ConnectionRefusedError:
        status, detail = "blocked", "Connection refused."
    except OSError as e:
        if getattr(e, "errno", None) in (101, 113, 111):  # network/host unreachable, refused
            status, detail = "blocked", f"Network error: {e.strerror or type(e).__name__}"
        else:
            status, detail = "unknown", f"Could not determine ({type(e).__name__}: {str(e)[:120]}); DNS or general connectivity may be unavailable."
    res = {"status": status, "host": EGRESS_HOST, "port": EGRESS_PORT, "detail": detail, "checked_at": datetime.now(UTC).isoformat()}
    await set_setting(session, "smtp_egress_last", res)
    await audit(session, user, "system.smtp_egress_check", "system", None, {"status": status})
    await session.commit()
    return EgressResult(**res)


@router.post("/pause-all", response_model=PauseAllOut)
async def pause_all(user: User = Depends(require_role("admin")), session: AsyncSession = Depends(get_session)) -> PauseAllOut:
    ids = list((await session.execute(
        text("UPDATE sourcing_runs SET status='paused', paused_at=now() WHERE status='running' RETURNING id"))).scalars())
    prev = (await get_setting(session, "pause_all", None) or {}).get("run_ids", [])
    await set_setting(session, "pause_all", {"run_ids": sorted(set(prev) | set(ids)), "at": datetime.now(UTC).isoformat(), "by": user.id})
    await audit(session, user, "system.pause_all", "system", None, {"runs": len(ids)})
    await session.commit()
    return PauseAllOut(paused_run_ids=sorted(ids))


@router.post("/resume-processing", response_model=ResumeOut)
async def resume_processing(user: User = Depends(require_role("admin")), session: AsyncSession = Depends(get_session)) -> ResumeOut:
    ids = (await get_setting(session, "pause_all", None) or {}).get("run_ids", [])
    resumed = list((await session.execute(
        text("UPDATE sourcing_runs SET status='running', paused_at=NULL WHERE id = ANY(:ids) AND status='paused' RETURNING id"), {"ids": ids})).scalars()) if ids else []
    await set_setting(session, "pause_all", {"run_ids": []})
    await audit(session, user, "system.resume_processing", "system", None, {"runs": len(resumed)})
    await session.commit()
    return ResumeOut(resumed_run_ids=sorted(resumed), skipped_run_ids=sorted(set(ids) - set(resumed)))


@router.post("/restart-failed", response_model=RestartOut)
async def restart_failed(user: User = Depends(require_role("admin")), session: AsyncSession = Depends(get_session)) -> RestartOut:
    n = await jq.retry_jobs(session)
    await audit(session, user, "system.restart_failed", "system", None, {"jobs": n})
    await session.commit()
    return RestartOut(retried_jobs=n)


@router.get("/maintenance", response_model=MaintenanceOut)
async def get_maintenance(session: AsyncSession = Depends(get_session)) -> MaintenanceOut:
    v = await get_setting(session, "maintenance", None) or {}
    return MaintenanceOut(enabled=bool(v.get("enabled")), message=v.get("message") or "", since=v.get("since"))


@router.put("/maintenance", response_model=MaintenanceOut)
async def set_maintenance(body: MaintenanceIn, user: User = Depends(require_role("admin")), session: AsyncSession = Depends(get_session)) -> MaintenanceOut:
    v = {"enabled": body.enabled, "message": (body.message or "").strip(), "since": datetime.now(UTC).isoformat() if body.enabled else None, "by": user.id}
    await set_setting(session, "maintenance", v)
    await audit(session, user, "system.maintenance", "system", None, {"enabled": body.enabled, "message": v["message"]})
    await session.commit()
    invalidate_maintenance_cache()
    return MaintenanceOut(enabled=body.enabled, message=v["message"], since=v["since"])




# --------------------------------------------------------------------------- failed jobs (any run, including run-less jobs)
@router.get("/failed-jobs", response_model=Page[JobRow])
async def failed_jobs(stage: str | None = Query(None, max_length=24), error_code: str | None = Query(None, max_length=80),
                      state: str | None = Query("failed,dead", description="comma separated job states"), run_id: int | None = Query(None, ge=1),
                      limit: int = Query(50, ge=1, le=200), cursor: str | None = None, session: AsyncSession = Depends(get_session)) -> Page[JobRow]:
    """Failed / dead jobs across all runs, hidden system runs and jobs without a run."""
    cond = SourcingJob.run_id == run_id if run_id else literal(True)
    return await page_jobs(session, cond, state, stage, error_code, limit, cursor)


@router.post("/jobs/{job_id}/retry", response_model=JobRow)
async def retry_any_job(job_id: int, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> JobRow:
    job = await session.get(SourcingJob, job_id)
    if job is None:
        raise ApiError(404, "not_found", "Job not found.")
    if job.state not in ("failed", "dead"):
        raise ApiError(409, "invalid_job_state", f"Only failed or dead jobs can be retried; this job is {job.state}.")
    await jq.retry_jobs(session, job_ids=[job_id])
    await audit(session, user, "system.retry_job", "job", job_id, {"run_id": job.run_id, "stage": job.stage})
    await session.commit()
    await session.refresh(job)
    return job_row(job)
