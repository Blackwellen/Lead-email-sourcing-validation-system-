"""Measured (never estimated) per-source effectiveness for worker jobs.

``measured(ctx, adapter_id)`` wraps a handler body. On exit it atomically adds the REAL job count, wall time, event-loop
thread CPU time, the worker's observed peak RSS and whatever the handler reported (HTTP requests it actually made,
confirmed domains, published emails ...) to ``source_effectiveness`` through :func:`effectiveness.record_effect`.
A failing job records ``failures=1`` from a separate session (the job's own transaction is rolled back).

CPU seconds are the CPU time of the event-loop thread while the job was in flight. With several jobs running
concurrently that is an upper bound for each of them (they share the thread); it is never extrapolated.
"""
from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from ..db import sessionmaker
from ..discovery.effectiveness import ALL_COUNTERS, record_effect
from ..worker import JobContext

log = logging.getLogger("pipeline.measure")


@dataclass
class Measure:
    adapter_id: str | None = None
    deltas: dict[str, float] = field(default_factory=dict)
    count_job: bool = True

    def add(self, **kw: float) -> None:
        for k, v in kw.items():
            if k not in ALL_COUNTERS:
                raise ValueError(k)
            if v:
                self.deltas[k] = self.deltas.get(k, 0) + v


async def record_failure(adapter_id: str | None, run_id: int | None) -> None:
    """Failure counter from an independent session (safe to call while the job's own transaction is poisoned)."""
    if not adapter_id:
        return
    try:
        async with sessionmaker()() as s:
            await record_effect(s, adapter_id, run_id, failures=1, jobs=1)
            await s.commit()
    except Exception:  # measurement must never mask the real error
        log.warning("could not record failure for %s", adapter_id, exc_info=True)


@asynccontextmanager
async def measured(ctx: JobContext, adapter_id: str | None = None) -> AsyncIterator[Measure]:
    from .. import jobqueue as jq

    m = Measure(adapter_id=adapter_id)
    t0, c0 = time.monotonic(), time.thread_time()

    def usage() -> dict[str, Any]:
        d: dict[str, Any] = {"wall_seconds": round(time.monotonic() - t0, 4), "cpu_seconds": round(max(0.0, time.thread_time() - c0), 4)}
        try:
            d["peak_rss_mb"] = round(ctx.worker.rss_mb(), 1)
        except Exception:
            pass
        return d

    try:
        yield m
    except jq.Requeue:
        # flow control, not a failure: the time was really spent, so keep it (job's own transaction is not committed on Requeue)
        if m.adapter_id:
            try:
                async with sessionmaker()() as s:
                    await record_effect(s, m.adapter_id, ctx.job.run_id, **usage())
                    await s.commit()
            except Exception:
                log.warning("could not record requeue usage for %s", m.adapter_id, exc_info=True)
        raise
    except Exception:
        if m.adapter_id:
            await record_failure(m.adapter_id, ctx.job.run_id)
        raise
    if m.adapter_id:
        extra: dict[str, Any] = dict(m.deltas)
        if m.count_job:
            extra["jobs"] = extra.get("jobs", 0) + 1
        extra.update(usage())
        await record_effect(ctx.session, m.adapter_id, ctx.job.run_id, **extra)
