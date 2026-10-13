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
    REBALANCE_EVERY_S = 30.0  # scheduler.rebalance per running run (rate limited)
    MONITOR_EVERY_S = 300.0  # recurring-monitor tick (run_due_monitors) enqueued from the supervisor

    def __init__(self, concurrency: int | None = None, stages: list[str] | None = None, worker_id: str | None = None):
        self.settings = get_settings()
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.concurrency = concurrency or self.settings.worker_concurrency
        self.stages = stages or list(HANDLERS)
        self.stop_event = asyncio.Event()
        self.tasks: set[asyncio.Task] = set()
        self.stage_sems = {st: asyncio.Semaphore(min(self.concurrency, self.DEFAULT_STAGE_LIMITS.get(st, 4))) for st in self.stages}
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
                async with sessionmaker()() as s:
                    ctx = JobContext(job=job, session=s, worker=self)
                    await h(ctx)
                    await s.commit()
            finally:
                if sem:
                    sem.release()
            async with sessionmaker()() as s:
                await jq.complete(s, job.id)
                await s.commit()
        except jq.Requeue as e:
            outcome = "requeue"
            async with sessionmaker()() as s:
                await jq.requeue(s, job.id, e.delay_s)
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
            metrics.JOBS_TOTAL.labels(job.stage, outcome).inc()
            metrics.JOB_SECONDS.labels(job.stage).observe(time.monotonic() - t0)
            self.processed += 1

    async def _record_fail(self, job, err, retryable, code, delay=None):
        async with sessionmaker()() as s:
            await jq.fail(s, job, err, retryable=retryable, max_attempts=self.settings.max_job_attempts, code=code, delay_s=delay)
            await s.commit()

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
                    ids = [t.job_id for t in self.tasks if hasattr(t, "job_id")]
                    await jq.extend_lease(s, ids, self.worker_id, self.settings.worker_lease_seconds)
                    await s.commit()
            except Exception:
                log.exception("heartbeat failed")
            await self._sleep(self.settings.worker_heartbeat_seconds)

    async def supervisor_loop(self):
        """Leader-elected (advisory lock) maintenance: lease reaping, run state, scheduled starts."""
        while not self.stop_event.is_set():
            try:
                async with sessionmaker()() as s:
                    got = (await s.execute(text("SELECT pg_try_advisory_xact_lock(727001)"))).scalar()
                    if got:
                        n = await jq.reap_expired(s, self.settings.max_job_attempts)
                        if n:
                            log.warning("recovered %d expired leases", n)
                        await runs.start_due_scheduled(s)
                        active = await runs.active_run_ids(s)
                        for rid in active:
                            if await runs.evaluate_run(s, rid) == "completed":
                                await self._final_completeness(s, rid)
                        await self._scheduler_tick(s, active)
                        await s.commit()
            except Exception:
                log.exception("supervisor tick failed")
            await self._sleep(2.0)

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

    async def claim_loop(self):
        while not self.stop_event.is_set():
            free = self.concurrency - len(self.tasks)
            over_mem = self.max_rss_mb is not None and self.rss_mb() > self.max_rss_mb
            if free <= 0 or over_mem:
                await self._sleep(0.2 if free <= 0 else 2.0)  # backpressure
                continue
            async with sessionmaker()() as s:
                jobs = await jq.claim(s, self.worker_id, self.stages, free, self.settings.worker_lease_seconds)
            if not jobs:
                await self._sleep(self.settings.worker_poll_interval_s)
                continue
            for j in jobs:
                t = asyncio.create_task(self._run_job(j))
                t.job_id = j.id  # type: ignore[attr-defined]
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
