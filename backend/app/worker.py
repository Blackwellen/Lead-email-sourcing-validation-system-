"""Worker process: leases jobs from PostgreSQL, runs stage handlers, heartbeats, supervises runs."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import psutil
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from . import jobqueue as jq
from . import metrics, runs
from .config import get_settings
from .db import sessionmaker
from .logging_setup import setup_logging

log = logging.getLogger("worker")

Handler = Callable[["JobContext"], Awaitable[None]]
HANDLERS: dict[str, Handler] = {}


def handler(stage: str):
    def deco(fn: Handler) -> Handler:
        HANDLERS[stage] = fn
        return fn

    return deco


@dataclass
class JobContext:
    job: jq.Job
    session: AsyncSession
    worker: Worker
    extra: dict = field(default_factory=dict)
    deadline: float | None = None  # monotonic time after which a long (checkpointed) job should yield cooperatively

    def time_left(self) -> float | None:
        return None if self.deadline is None else self.deadline - time.monotonic()

    def should_yield(self) -> bool:
        """True once the job has used its soft time budget: checkpoint, commit and raise ``Requeue`` to resume later."""
        return self.deadline is not None and time.monotonic() >= self.deadline

    async def checkpoint(self, data: dict) -> None:
        """Persist a resume point (committed immediately, independent of the handler's transaction)."""
        async with sessionmaker()() as s:
            await jq.save_checkpoint(s, self.job.id, data)
            await s.commit()

    async def run_active(self) -> bool:
        return await self.worker.run_status_is_running(self.job.run_id)


class Worker:
    # per-stage concurrency ceilings (backpressure); tuned by run performance presets via settings
    DEFAULT_STAGE_LIMITS = {
        "discover": 2, "import_domain": 8, "resolve_domain": 8, "crawl": 8, "verify": 16, "revalidate": 16, "export": 1, "import": 1,
        # discovery layer: network / archive / recovery work is deliberately narrower than plain crawling
        "network_expand": 2, "gap_recovery": 4, "signals": 4, "change_check": 4, "similar_expand": 1, "monitor": 1, "cc_lookup": 2,
        "completeness": 4,
    }
    # per-job wall-clock limits (seconds). A job past its limit is cancelled and retried (attempt consumed); its lease is no longer
    # extended past limit + LEASE_GRACE_S, so a wedged job is reaped even if cancellation does not land.
    STAGE_TIMEOUT_S = {"discover": 3600.0, "export": 3600.0, "import": 3600.0, "retention": 3600.0, "similar_expand": 1800.0,
                       "network_expand": 1800.0, "gap_recovery": 1800.0, "monitor": 1800.0}
    DEFAULT_TIMEOUT_S = 900.0
    # streaming jobs (discover) checkpoint every batch: after this soft budget they commit, checkpoint and requeue themselves so
    # other work gets a turn and no job runs into the hard limit
    SOFT_BUDGET_S = {"discover": 600.0}
    LEASE_GRACE_S = 60.0
    REBALANCE_EVERY_S = 30.0  # scheduler.rebalance per running run (rate limited)
    MONITOR_EVERY_S = 300.0  # recurring-monitor tick (run_due_monitors) enqueued from the supervisor

    def __init__(self, concurrency: int | None = None, stages: list[str] | None = None, worker_id: str | None = None):
        self.settings = get_settings()
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.concurrency = concurrency or self.settings.worker_concurrency
        self.stages = stages or list(HANDLERS)
        self.stop_event = asyncio.Event()
        self.tasks: set[asyncio.Task] = set()
        self.stage_limits = {st: max(1, min(self.concurrency, self.DEFAULT_STAGE_LIMITS.get(st, 4))) for st in self.stages}
        self.stage_sems = {st: asyncio.Semaphore(n) for st, n in self.stage_limits.items()}
        self.inflight: dict[str, int] = {}
        self._attempts_cache: dict[int, tuple[float, int]] = {}
        self._status_cache: dict[int, tuple[float, str]] = {}
        self._last_rebalance: dict[int, float] = {}
        self._last_monitor_tick = 0.0
        self.proc = psutil.Process()
        self.max_rss_mb: float | None = None
        self.started = time.time()
        self.processed = 0

    # ------------------------------------------------------------------ helpers
    async def run_status_is_running(self, run_id: int | None) -> bool:
        if run_id is None:
            return True
        now = time.monotonic()
        hit = self._status_cache.get(run_id)
        if hit and now - hit[0] < 1.0:
            return hit[1] == "running"
        async with sessionmaker()() as s:
            st = (await s.execute(text("SELECT status FROM sourcing_runs WHERE id=:r"), {"r": run_id})).scalar_one_or_none()
        self._status_cache[run_id] = (now, st or "")
        return st == "running"

    def rss_mb(self) -> float:
        return self.proc.memory_info().rss / 1e6

    # ------------------------------------------------------------------ job execution
    def job_timeout(self, stage: str) -> float:
        return float(self.STAGE_TIMEOUT_S.get(stage, self.DEFAULT_TIMEOUT_S))

    async def max_attempts_for(self, run_id: int | None) -> int:
        """The run's ``performance.retry_max_attempts`` (system runs / no run: the worker setting)."""
        default = self.settings.max_job_attempts
        if run_id is None:
            return default
        now = time.monotonic()
        hit = self._attempts_cache.get(run_id)
        if hit and now - hit[0] < 30.0:
            return hit[1]
        n = default
        try:
            async with sessionmaker()() as s:
                snap = (await s.execute(text("SELECT configuration_snapshot FROM sourcing_runs WHERE id=:r"), {"r": run_id})).scalar_one_or_none() or {}
            if not snap.get("system"):
                v = (snap.get("performance") or {}).get("retry_max_attempts")
                if isinstance(v, int) and v >= 1:
                    n = v
        except Exception:
            log.exception("could not load retry policy for run %s", run_id)
        self._attempts_cache[run_id] = (now, n)
        return n

    async def _run_job(self, job: jq.Job) -> None:
        h = HANDLERS.get(job.stage)
        t0 = time.monotonic()
        outcome = "done"
        sem = self.stage_sems.get(job.stage)
        try:
            if h is None:
                raise jq.PermanentError(f"no handler for stage {job.stage}", "no_handler")
            if sem:
                await sem.acquire()
            try:
                soft = self.SOFT_BUDGET_S.get(job.stage)
                async with sessionmaker()() as s:
                    ctx = JobContext(job=job, session=s, worker=self, deadline=(time.monotonic() + soft) if soft else None)
                    try:
                        async with asyncio.timeout(self.job_timeout(job.stage)) as cm:
                            await h(ctx)
                            await s.commit()
                    except TimeoutError:
                        if cm.expired():
                            raise jq.RetryableError(f"job exceeded its {self.job_timeout(job.stage):.0f}s time limit", "job_timeout") from None
                        raise
            finally:
                if sem:
                    sem.release()
            async with sessionmaker()() as s:
                await jq.complete(s, job.id, worker_id=self.worker_id)
                await s.commit()
        except jq.Requeue as e:
            outcome = "requeue"
            async with sessionmaker()() as s:
                await jq.requeue(s, job.id, e.delay_s, worker_id=self.worker_id)
                await s.commit()
        except jq.RetryableError as e:
            outcome = "retry"
            await self._record_fail(job, e, True, e.code, e.delay_s)
        except jq.PermanentError as e:
            outcome = "failed"
            await self._record_fail(job, e, False, e.code)
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception as e:  # unexpected: treat as retryable up to max attempts
            outcome = "error"
            log.exception("job crashed", extra={"job_id": job.id, "stage": job.stage})
            await self._record_fail(job, e, True, "unhandled")
        finally:
            self.inflight[job.stage] = max(0, self.inflight.get(job.stage, 0) - 1)
            metrics.JOBS_TOTAL.labels(job.stage, outcome).inc()
            metrics.JOB_SECONDS.labels(job.stage).observe(time.monotonic() - t0)
            self.processed += 1

    async def _record_fail(self, job, err, retryable, code, delay=None):
        max_attempts = await self.max_attempts_for(job.run_id)
        async with sessionmaker()() as s:
            await jq.fail(s, job, err, retryable=retryable, max_attempts=max_attempts, code=code, delay_s=delay, worker_id=self.worker_id)
            await s.commit()

    def lease_ids(self) -> list[int]:
        """Jobs whose lease the heartbeat keeps alive: in flight and not past their time limit (+ grace)."""
        now = time.monotonic()
        out = []
        for t in list(self.tasks):
            jid = getattr(t, "job_id", None)
            if jid is None:
                continue
            started = getattr(t, "job_started", now)
            if now - started <= self.job_timeout(getattr(t, "job_stage", "")) + self.LEASE_GRACE_S:
                out.append(jid)
        return out

    # ------------------------------------------------------------------ loops
    async def heartbeat_loop(self):
        while not self.stop_event.is_set():
            try:
                rss = self.rss_mb()
                metrics.WORKER_RSS.set(rss)
                async with sessionmaker()() as s:
                    await s.execute(
                        text(
                            """INSERT INTO worker_heartbeats (worker_id, hostname, pid, started_at, last_seen_at, rss_mb, cpu_percent, active_jobs, stats)
                               VALUES (:w, :h, :p, to_timestamp(:st), now(), :rss, :cpu, :a, CAST(:stats AS jsonb))
                               ON CONFLICT (worker_id) DO UPDATE SET last_seen_at=now(), rss_mb=:rss, cpu_percent=:cpu, active_jobs=:a, stats=CAST(:stats AS jsonb)"""
                        ),
                        {"w": self.worker_id, "h": socket.gethostname(), "p": os.getpid(), "st": self.started, "rss": rss,
                         "cpu": self.proc.cpu_percent(None), "a": len(self.tasks),
                         "stats": f'{{"processed": {self.processed}}}'},
                    )
                    await jq.extend_lease(s, self.lease_ids(), self.worker_id, self.settings.worker_lease_seconds)
                    await s.commit()
            except Exception:
                log.exception("heartbeat failed")
            await self._sleep(self.settings.worker_heartbeat_seconds)

    LEADER_LOCK = 727001

    async def supervisor_loop(self):
        """Leader-elected maintenance: lease reaping, run state, scheduled starts, scheduler rebalance."""
        while not self.stop_event.is_set():
            try:
                await self.supervisor_tick()
            except Exception:
                log.exception("supervisor tick failed")
            await self._sleep(2.0)

    async def supervisor_tick(self) -> bool:
        """One supervision pass. Leadership is a SESSION-level advisory lock held for the whole tick (commits inside the tick do
        not release it), so two workers never supervise concurrently. Each run is evaluated in its own short transaction (its row
        lock is held only for that run, never across the tick) and a failing run is logged and skipped, never aborting the
        others. Returns whether this worker was the leader."""
        from .db import get_engine

        # one dedicated connection for the whole tick: a session-level lock belongs to the connection, so the session must not hand
        # its connection back to the pool between commits
        async with get_engine().connect() as conn:
            s = AsyncSession(bind=conn, expire_on_commit=False)
            got = bool((await s.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": self.LEADER_LOCK})).scalar())
            await s.commit()
            if not got:
                await s.close()
                return False
            try:
                n = await jq.reap_expired(s, self.settings.max_job_attempts)
                if n:
                    log.warning("recovered %d expired leases", n)
                try:
                    await runs.start_due_scheduled(s)
                    await s.commit()
                except Exception:
                    log.exception("starting scheduled runs failed")
                    await s.rollback()
                active = await runs.active_run_ids(s, include_system=False)  # system runs never auto-complete: never locked here
                await s.commit()
                for rid in active:
                    try:
                        if await runs.evaluate_run(s, rid) == "completed":
                            await self._final_completeness(s, rid)
                        await s.commit()
                    except Exception:
                        log.exception("supervising run %s failed", rid)
                        await s.rollback()
                try:
                    await self._scheduler_tick(s, active)
                    await s.commit()
                except Exception:
                    log.exception("scheduler tick failed")
                    await s.rollback()
            finally:
                released = False
                try:
                    await s.rollback()
                    released = bool((await s.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": self.LEADER_LOCK})).scalar())
                    await s.commit()
                except Exception:
                    log.exception("releasing supervisor leadership failed")
                finally:
                    await s.close()
                if not released:  # never return a connection that may still hold the leader lock to the pool
                    await conn.invalidate()
            return True

    async def _scheduler_tick(self, s: AsyncSession, active: list[int]) -> None:
        """Rate-limited discovery maintenance inside the supervisor transaction: source-scheduler rebalance per running run and the
        recurring-monitor tick. Failures are isolated in savepoints; they never stop run supervision."""
        now = time.monotonic()
        if active:
            await self._write_run_metrics(s, active, now)
            running = set((await s.execute(text(
                "SELECT id FROM sourcing_runs WHERE id = ANY(:ids) AND status='running' AND COALESCE(configuration_snapshot->>'system','false') <> 'true'"),
                {"ids": active})).scalars())
            for rid in active:
                if rid not in running or now - self._last_rebalance.get(rid, -1e9) < self.REBALANCE_EVERY_S:
                    continue
                self._last_rebalance[rid] = now
                try:
                    from .discovery.scheduler import rebalance

                    async with s.begin_nested():
                        await rebalance(s, rid)
                except Exception:
                    log.exception("scheduler rebalance failed for run %s", rid)
            for rid in [r for r in self._last_rebalance if r not in running]:
                self._last_rebalance.pop(rid, None)
        if now - self._last_monitor_tick >= self.MONITOR_EVERY_S:
            self._last_monitor_tick = now
            try:
                async with s.begin_nested():
                    await self._enqueue_monitor_tick(s)
            except Exception:
                log.exception("monitor tick failed")

    METRICS_EVERY_S = 15.0
    FINAL_COMPLETENESS_MAX_ORGS = 5000

    async def _final_completeness(self, s: AsyncSession, run_id: int) -> None:
        """Safety net behind the per-organisation refresh (two sibling jobs finishing together can each see the other as pending):
        when a run completes, bring the completeness rows of its organisations up to date (bounded)."""
        try:
            from .discovery.completeness import refresh_completeness

            async with s.begin_nested():
                ids = list((await s.execute(text("SELECT organisation_id FROM run_organisations WHERE run_id=:r ORDER BY organisation_id LIMIT :n"),
                                            {"r": run_id, "n": self.FINAL_COMPLETENESS_MAX_ORGS})).scalars())
                await refresh_completeness(s, ids)
        except Exception:
            log.exception("final completeness refresh failed for run %s", run_id)

    async def _write_run_metrics(self, s: AsyncSession, active: list[int], now: float) -> None:
        """One ``run_metrics`` row per run and minute (upserted while the minute is open): measured throughput, queue depth, failures."""
        if now - getattr(self, "_last_metrics", -1e9) < self.METRICS_EVERY_S:
            return
        self._last_metrics = now
        try:
            async with s.begin_nested():
                await s.execute(text(
                    """INSERT INTO run_metrics (run_id, bucket, metrics)
                       SELECT r.id, date_trunc('minute', now()), jsonb_build_object(
                           'jobs_done', count(*) FILTER (WHERE j.state='done' AND j.finished_at >= date_trunc('minute', now())),
                           'jobs_failed', count(*) FILTER (WHERE j.state IN ('failed','dead') AND j.finished_at >= date_trunc('minute', now())),
                           'queue_depth', count(*) FILTER (WHERE j.state='queued'),
                           'in_flight', count(*) FILTER (WHERE j.state='leased'),
                           'qualified', (SELECT count(*) FROM run_yield y WHERE y.run_id = r.id),
                           'qualified_this_minute', (SELECT count(*) FROM run_yield y WHERE y.run_id = r.id AND y.created_at >= date_trunc('minute', now())))
                       FROM sourcing_runs r LEFT JOIN sourcing_jobs j ON j.run_id = r.id
                       WHERE r.id = ANY(:ids) AND r.status IN ('running','stopping') GROUP BY r.id
                       ON CONFLICT (run_id, bucket) DO UPDATE SET metrics = EXCLUDED.metrics"""), {"ids": active})
        except Exception:
            log.exception("run metrics write failed")

    async def _enqueue_monitor_tick(self, s: AsyncSession) -> None:
        from .discovery.monitor import due_monitors
        from .system_runs import ensure_system_run

        if not await due_monitors(s):
            return
        rid = await ensure_system_run(s, "discovery-monitors")
        bucket = int(time.time() // self.MONITOR_EVERY_S)
        await jq.enqueue(s, run_id=rid, stage="monitor", dedupe_key=f"tick:{bucket}", payload={"limit": 20}, priority=95)

    async def _sleep(self, secs: float):
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=secs)
        except TimeoutError:
            pass

    def free_stage_slots(self) -> dict[str, int]:
        return {st: n - self.inflight.get(st, 0) for st, n in self.stage_limits.items() if n - self.inflight.get(st, 0) > 0}

    async def claim_loop(self):
        while not self.stop_event.is_set():
            free = self.concurrency - len(self.tasks)
            over_mem = self.max_rss_mb is not None and self.rss_mb() > self.max_rss_mb
            slots = self.free_stage_slots() if free > 0 else {}
            if free <= 0 or over_mem or not slots:
                await self._sleep(0.2 if (free <= 0 or not slots) and not over_mem else 2.0)  # backpressure
                continue
            async with sessionmaker()() as s:
                # claim per stage only up to the free capacity of that stage: no leased-but-idle jobs behind a stage semaphore
                jobs = await jq.claim(s, self.worker_id, self.stages, free, self.settings.worker_lease_seconds, stage_limits=slots)
            if not jobs:
                await self._sleep(self.settings.worker_poll_interval_s)
                continue
            for j in jobs:
                self.inflight[j.stage] = self.inflight.get(j.stage, 0) + 1
                t = asyncio.create_task(self._run_job(j))
                t.job_id = j.id  # type: ignore[attr-defined]
                t.job_stage = j.stage  # type: ignore[attr-defined]
                t.job_started = time.monotonic()  # type: ignore[attr-defined]
                self.tasks.add(t)
                t.add_done_callback(self.tasks.discard)
            metrics.WORKER_ACTIVE.set(len(self.tasks))

    async def run(self):
        loops = [asyncio.create_task(f()) for f in (self.heartbeat_loop, self.supervisor_loop, self.claim_loop)]
        await self.stop_event.wait()
        log.info("shutting down: waiting for %d in-flight jobs", len(self.tasks))
        if self.tasks:
            _, pending = await asyncio.wait(self.tasks, timeout=30)
            for t in pending:  # leave lease in place; supervisor will recover them
                t.cancel()
        for t in loops:
            t.cancel()
        async with sessionmaker()() as s:
            await s.execute(text("DELETE FROM worker_heartbeats WHERE worker_id=:w"), {"w": self.worker_id})
            await s.commit()

    def request_stop(self):
        self.stop_event.set()


def main():  # pragma: no cover
    from . import pipeline  # noqa: F401  (registers handlers)

    setup_logging()
    stages = [x for x in os.environ.get("LEAD_WORKER_STAGES", "").split(",") if x] or None
    w = Worker(stages=stages)
    mem = os.environ.get("LEAD_WORKER_MAX_RSS_MB")
    w.max_rss_mb = float(mem) if mem else None

    async def _go():
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, w.request_stop)
        await w.run()

    asyncio.run(_go())


if __name__ == "__main__":  # pragma: no cover
    # re-import under the canonical name: handlers register against app.worker.HANDLERS, not __main__.HANDLERS
    from app.worker import main as _canonical_main

    _canonical_main()
