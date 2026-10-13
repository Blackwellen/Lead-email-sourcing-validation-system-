"""PostgreSQL-backed durable job queue (FOR UPDATE SKIP LOCKED + leases)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class Job:
    id: int
    run_id: int | None
    stage: str
    dedupe_key: str
    payload: dict[str, Any]
    checkpoint: dict[str, Any]
    attempts: int
    host_key: str | None = None


class RetryableError(Exception):
    """Transient failure: job goes back to the queue with exponential backoff."""

    def __init__(self, msg: str, code: str = "retry", delay_s: float | None = None):
        super().__init__(msg)
        self.code = code
        self.delay_s = delay_s


class PermanentError(Exception):
    """Job cannot succeed; marked failed (visible in UI, manually retryable)."""

    def __init__(self, msg: str, code: str = "permanent"):
        super().__init__(msg)
        self.code = code


class Requeue(Exception):
    """Put the job back without consuming an attempt (run paused, backpressure, cooperative yield)."""

    def __init__(self, delay_s: float = 2.0):
        super().__init__("requeue")
        self.delay_s = delay_s


async def requeue(s: AsyncSession, job_id: int, delay_s: float) -> None:
    await s.execute(
        text("UPDATE sourcing_jobs SET state='queued', attempts=GREATEST(attempts-1,0), leased_until=NULL, worker_id=NULL, next_attempt_at=now()+make_interval(secs => :d) WHERE id=:id"),
        {"d": delay_s, "id": job_id},
    )


async def enqueue(
    s: AsyncSession,
    *,
    run_id: int | None,
    stage: str,
    dedupe_key: str,
    payload: dict[str, Any] | None = None,
    priority: int = 100,
    host_key: str | None = None,
    delay_s: float = 0,
) -> int | None:
    """Idempotent enqueue: returns job id, or None if an identical job already exists.

    NOTE: run_id NULL jobs are never de-duplicated by the unique constraint (NULLs are distinct),
    so callers must use a non-null run_id for idempotency.
    """
    row = (
        await s.execute(
            text(
                """INSERT INTO sourcing_jobs (run_id, stage, dedupe_key, payload, checkpoint, priority, host_key, state, next_attempt_at)
                   VALUES (:run_id, :stage, :dk, CAST(:payload AS jsonb), '{}'::jsonb, :prio, :host, 'queued', now() + make_interval(secs => :delay))
                   ON CONFLICT (run_id, stage, dedupe_key) DO NOTHING RETURNING id"""
            ),
            {"run_id": run_id, "stage": stage, "dk": dedupe_key, "payload": json.dumps(payload or {}), "prio": priority, "host": host_key, "delay": delay_s},
        )
    ).first()
    return row[0] if row else None


async def claim(s: AsyncSession, worker_id: str, stages: list[str], limit: int, lease_s: int) -> list[Job]:
    """Lease up to ``limit`` runnable jobs. Jobs of paused/draft runs are not handed out.

    Nothing is handed out while the admin-controlled maintenance flag is on (cached ~2 s)."""
    from .settings_store import maintenance_active

    if await maintenance_active(s):
        await s.rollback()
        return []
    rows = (
        await s.execute(
            text(
                """UPDATE sourcing_jobs j SET state='leased', worker_id=:w, attempts=j.attempts+1,
                          leased_until=now()+make_interval(secs => :lease), started_at=COALESCE(j.started_at, now())
                   WHERE j.id IN (
                       SELECT sj.id FROM sourcing_jobs sj LEFT JOIN sourcing_runs r ON r.id=sj.run_id
                       WHERE sj.state='queued' AND sj.next_attempt_at <= now() AND sj.stage = ANY(:stages)
                         AND (sj.run_id IS NULL OR r.status = 'running')
                       ORDER BY sj.priority, sj.id LIMIT :n FOR UPDATE OF sj SKIP LOCKED)
                   RETURNING j.id, j.run_id, j.stage, j.dedupe_key, j.payload, j.checkpoint, j.attempts, j.host_key"""
            ),
            {"w": worker_id, "lease": lease_s, "stages": stages, "n": limit},
        )
    ).all()
    await s.commit()
    return [Job(id=r[0], run_id=r[1], stage=r[2], dedupe_key=r[3], payload=r[4], checkpoint=r[5], attempts=r[6], host_key=r[7]) for r in rows]


async def extend_lease(s: AsyncSession, job_ids: list[int], worker_id: str, lease_s: int) -> None:
    if not job_ids:
        return
    await s.execute(
        text("UPDATE sourcing_jobs SET leased_until=now()+make_interval(secs => :l) WHERE id = ANY(:ids) AND worker_id=:w AND state='leased'"),
        {"l": lease_s, "ids": job_ids, "w": worker_id},
    )
    await s.commit()


async def save_checkpoint(s: AsyncSession, job_id: int, checkpoint: dict[str, Any]) -> None:
    await s.execute(text("UPDATE sourcing_jobs SET checkpoint=CAST(:c AS jsonb) WHERE id=:id"), {"c": json.dumps(checkpoint), "id": job_id})


async def complete(s: AsyncSession, job_id: int) -> None:
    await s.execute(text("UPDATE sourcing_jobs SET state='done', finished_at=now(), leased_until=NULL, error_code=NULL, error_detail=NULL WHERE id=:id"), {"id": job_id})


async def fail(s: AsyncSession, job: Job, error: Exception, *, retryable: bool, max_attempts: int, code: str = "error", delay_s: float | None = None) -> str:
    """Record failure. Returns the new state (queued|failed|dead)."""
    detail = f"{type(error).__name__}: {error}"[:2000]
    if retryable and job.attempts < max_attempts:
        backoff = delay_s if delay_s is not None else min(3600, 5 * (2 ** (job.attempts - 1)))
        await s.execute(
            text("UPDATE sourcing_jobs SET state='queued', leased_until=NULL, worker_id=NULL, error_code=:c, error_detail=:d, next_attempt_at=now()+make_interval(secs => :b) WHERE id=:id"),
            {"c": code, "d": detail, "b": backoff, "id": job.id},
        )
        return "queued"
    state = "dead" if retryable else "failed"
    await s.execute(
        text("UPDATE sourcing_jobs SET state=:st, finished_at=now(), leased_until=NULL, error_code=:c, error_detail=:d WHERE id=:id"),
        {"st": state, "c": code, "d": detail, "id": job.id},
    )
    return state


async def reap_expired(s: AsyncSession, max_attempts: int) -> int:
    """Recover jobs whose worker died (lease expired). Exhausted ones go to the dead-letter state."""
    r = await s.execute(
        text(
            """UPDATE sourcing_jobs SET
                 state = CASE WHEN attempts >= :m THEN 'dead' ELSE 'queued' END,
                 error_code = 'lease_expired', error_detail = 'worker lease expired (crash or restart)',
                 leased_until = NULL, worker_id = NULL,
                 finished_at = CASE WHEN attempts >= :m THEN now() ELSE NULL END
               WHERE state='leased' AND leased_until < now()"""
        ),
        {"m": max_attempts},
    )
    await s.commit()
    return r.rowcount or 0


async def retry_jobs(s: AsyncSession, *, run_id: int | None = None, job_ids: list[int] | None = None, states: tuple[str, ...] = ("failed", "dead")) -> int:
    q = "UPDATE sourcing_jobs SET state='queued', attempts=0, error_code=NULL, error_detail=NULL, next_attempt_at=now(), finished_at=NULL, worker_id=NULL, leased_until=NULL WHERE state = ANY(:st)"
    params: dict[str, Any] = {"st": list(states)}
    if run_id is not None:
        q += " AND run_id=:r"
        params["r"] = run_id
    if job_ids is not None:
        q += " AND id = ANY(:ids)"
        params["ids"] = job_ids
    r = await s.execute(text(q), params)
    return r.rowcount or 0
