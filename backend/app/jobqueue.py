"""PostgreSQL-backed durable job queue (FOR UPDATE SKIP LOCKED + leases)."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("jobqueue")

# runs in these states take no new work: jobs still in flight when a run stops must not leave queued children behind
CLOSED_RUN_STATES = ("stopping", "completed", "cancelled", "failed", "archived")


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


def _owner(worker_id: str | None) -> tuple[str, dict[str, Any]]:
    """Ownership guard: only the worker that holds the lease may settle a job (a stale worker whose lease expired and was re-claimed
    by another worker must not complete / requeue / fail it)."""
    if worker_id is None:
        return "", {}
    return " AND worker_id=:w AND state='leased'", {"w": worker_id}


def _settled(r, job_id: int, worker_id: str | None, action: str) -> bool:
    ok = (r.rowcount or 0) > 0
    if not ok and worker_id is not None:
        log.warning("job %s: %s ignored: lease no longer held by %s", job_id, action, worker_id)
    return ok


async def requeue(s: AsyncSession, job_id: int, delay_s: float, *, worker_id: str | None = None) -> bool:
    guard, gp = _owner(worker_id)
    r = await s.execute(
        text("UPDATE sourcing_jobs SET state='queued', attempts=GREATEST(attempts-1,0), leased_until=NULL, worker_id=NULL, "
             f"next_attempt_at=now()+make_interval(secs => :d) WHERE id=:id{guard}"),
        {"d": delay_s, "id": job_id, **gp},
    )
    return _settled(r, job_id, worker_id, "requeue")


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
    """Idempotent enqueue: returns job id, or None if an identical job already exists or the run takes no new work
    (stopping / completed / cancelled / failed / archived; hidden system runs always accept work).

    NOTE: run_id NULL jobs are never de-duplicated by the unique constraint (NULLs are distinct),
    so callers must use a non-null run_id for idempotency.
    """
    row = (
        await s.execute(
            text(
                """INSERT INTO sourcing_jobs (run_id, stage, dedupe_key, payload, checkpoint, priority, host_key, state, next_attempt_at)
                   SELECT :run_id, :stage, :dk, CAST(:payload AS jsonb), '{}'::jsonb, :prio, :host, 'queued', now() + make_interval(secs => :delay)
                   WHERE NOT EXISTS (SELECT 1 FROM sourcing_runs r WHERE r.id = :run_id AND r.status = ANY(:closed)
                                     AND COALESCE(r.configuration_snapshot->>'system', 'false') <> 'true')
                   ON CONFLICT (run_id, stage, dedupe_key) DO NOTHING RETURNING id"""
            ),
            {"run_id": run_id, "stage": stage, "dk": dedupe_key, "payload": json.dumps(payload or {}), "prio": priority, "host": host_key,
             "delay": delay_s, "closed": list(CLOSED_RUN_STATES)},
        )
    ).first()
    return row[0] if row else None


async def claim(s: AsyncSession, worker_id: str, stages: list[str], limit: int, lease_s: int,
                stage_limits: dict[str, int] | None = None) -> list[Job]:
    """Lease up to ``limit`` runnable jobs. Jobs of paused/draft runs are not handed out.

    ``stage_limits`` (stage -> free slots) caps how many jobs of each stage are leased, so a worker never holds leased jobs it
    cannot start yet (they would sit idle behind a stage semaphore while other stages starve).

    Nothing is handed out while the admin-controlled maintenance flag is on (cached ~2 s)."""
    from .settings_store import maintenance_active

    if await maintenance_active(s):
        await s.rollback()
        return []
    eligible = """sj.state='queued' AND sj.next_attempt_at <= now() AND (sj.run_id IS NULL OR r.status = 'running')"""
    if stage_limits is not None:
        lim = {st: int(n) for st, n in stage_limits.items() if n > 0 and st in stages}
        if not lim or limit <= 0:
            await s.rollback()
            return []
        pick = f"""SELECT x.id FROM unnest(CAST(:lst AS text[]), CAST(:lnn AS int[])) AS lim(stage, n)
                   CROSS JOIN LATERAL (
                       SELECT sj.id, sj.priority FROM sourcing_jobs sj LEFT JOIN sourcing_runs r ON r.id=sj.run_id
                       WHERE {eligible} AND sj.stage = lim.stage
                       ORDER BY sj.priority, sj.id LIMIT lim.n FOR UPDATE OF sj SKIP LOCKED) x
                   ORDER BY x.priority, x.id LIMIT :n"""
        params: dict[str, Any] = {"lst": list(lim), "lnn": list(lim.values())}
    else:
        pick = f"""SELECT sj.id FROM sourcing_jobs sj LEFT JOIN sourcing_runs r ON r.id=sj.run_id
                   WHERE {eligible} AND sj.stage = ANY(:stages)
                   ORDER BY sj.priority, sj.id LIMIT :n FOR UPDATE OF sj SKIP LOCKED"""
        params = {"stages": stages}
    rows = (
        await s.execute(
            text(
                f"""UPDATE sourcing_jobs j SET state='leased', worker_id=:w, attempts=j.attempts+1,
                          leased_until=now()+make_interval(secs => :lease), started_at=COALESCE(j.started_at, now())
                   WHERE j.id IN ({pick})
                   RETURNING j.id, j.run_id, j.stage, j.dedupe_key, j.payload, j.checkpoint, j.attempts, j.host_key"""
            ),
            {"w": worker_id, "lease": lease_s, "n": limit, **params},
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


async def complete(s: AsyncSession, job_id: int, *, worker_id: str | None = None) -> bool:
    guard, gp = _owner(worker_id)
    r = await s.execute(
        text(f"UPDATE sourcing_jobs SET state='done', finished_at=now(), leased_until=NULL, error_code=NULL, error_detail=NULL WHERE id=:id{guard}"),
        {"id": job_id, **gp})
    return _settled(r, job_id, worker_id, "complete")


async def fail(s: AsyncSession, job: Job, error: Exception, *, retryable: bool, max_attempts: int, code: str = "error", delay_s: float | None = None,
               worker_id: str | None = None) -> str:
    """Record failure. Returns the new state (queued|failed|dead), or 'ignored' when ``worker_id`` no longer holds the lease."""
    detail = f"{type(error).__name__}: {error}"[:2000]
    guard, gp = _owner(worker_id)
    if retryable and job.attempts < max_attempts:
        backoff = delay_s if delay_s is not None else min(3600, 5 * (2 ** (job.attempts - 1)))
        r = await s.execute(
            text("UPDATE sourcing_jobs SET state='queued', leased_until=NULL, worker_id=NULL, error_code=:c, error_detail=:d, "
                 f"next_attempt_at=now()+make_interval(secs => :b) WHERE id=:id{guard}"),
            {"c": code, "d": detail, "b": backoff, "id": job.id, **gp},
        )
        return "queued" if _settled(r, job.id, worker_id, "fail") else "ignored"
    state = "dead" if retryable else "failed"
    r = await s.execute(
        text(f"UPDATE sourcing_jobs SET state=:st, finished_at=now(), leased_until=NULL, error_code=:c, error_detail=:d WHERE id=:id{guard}"),
        {"st": state, "c": code, "d": detail, "id": job.id, **gp},
    )
    return state if _settled(r, job.id, worker_id, "fail") else "ignored"


async def reap_expired(s: AsyncSession, max_attempts: int, *, commit: bool = True) -> int:
    """Recover jobs whose worker died (lease expired). Exhausted ones go to the dead-letter state.

    The attempt limit is the run's ``performance.retry_max_attempts`` when its snapshot has one (else ``max_attempts``).
    ``commit=False`` leaves the transaction open (the supervisor holds leadership across its whole tick)."""
    r = await s.execute(
        text(
            """UPDATE sourcing_jobs j SET
                 state = CASE WHEN j.attempts >= COALESCE(lim.m, :m) THEN 'dead' ELSE 'queued' END,
                 error_code = 'lease_expired', error_detail = 'worker lease expired (crash, restart or job time limit)',
                 leased_until = NULL, worker_id = NULL,
                 finished_at = CASE WHEN j.attempts >= COALESCE(lim.m, :m) THEN now() ELSE NULL END
               FROM sourcing_jobs j2 LEFT JOIN LATERAL (
                   SELECT CASE WHEN (r.configuration_snapshot->'performance'->>'retry_max_attempts') ~ '^[0-9]+$'
                               THEN CAST(r.configuration_snapshot->'performance'->>'retry_max_attempts' AS int) END AS m
                   FROM sourcing_runs r WHERE r.id = j2.run_id) lim ON true
               WHERE j.id = j2.id AND j.state='leased' AND j.leased_until < now()"""
        ),
        {"m": max_attempts},
    )
    if commit:
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
