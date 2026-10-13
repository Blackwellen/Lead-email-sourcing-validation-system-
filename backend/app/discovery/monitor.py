"""Recurring monitors (``MonitorSchedule``): which are due, and dispatch by kind.

* ``website_changes``   -> :func:`app.discovery.changes.run_monitor`
* ``signals``           -> :func:`app.discovery.signals.run_monitor`
* ``new_registrations`` -> ``app.sources.new_registrations.run_monitor`` (another module; imported lazily, absence is tolerated)

Each monitor runs inside its own SAVEPOINT, so one failing monitor never poisons the others or the caller's transaction.
"""
from __future__ import annotations

import calendar
import importlib
import inspect
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..crawler.ratelimit import RateLimiter
from ..crawler.robots import RobotsCache
from ..crawler.safe_http import SafeFetcher
from ..models import MonitorSchedule

log = logging.getLogger("discovery.monitor")
RETRY_AFTER_ERROR = timedelta(hours=1)
KINDS = ("website_changes", "signals", "new_registrations")


def next_run_after(frequency: str, base: datetime) -> datetime:
    """daily +1 day, weekly +7 days, monthly = same day next calendar month (clamped to the month's length)."""
    if frequency == "daily":
        return base + timedelta(days=1)
    if frequency == "weekly":
        return base + timedelta(days=7)
    if frequency == "monthly":
        y, m = (base.year + 1, 1) if base.month == 12 else (base.year, base.month + 1)
        return base.replace(year=y, month=m, day=min(base.day, calendar.monthrange(y, m)[1]))
    raise ValueError(f"unknown frequency {frequency!r}")


async def due_monitors(session: AsyncSession, now: datetime | None = None) -> list[MonitorSchedule]:
    """Enabled monitors whose ``next_run_at`` has passed (or was never set), oldest first."""
    now = now or datetime.now(UTC)
    q = (select(MonitorSchedule).where(MonitorSchedule.enabled.is_(True),
                                       or_(MonitorSchedule.next_run_at.is_(None), MonitorSchedule.next_run_at <= now))
         .order_by(MonitorSchedule.next_run_at.asc().nulls_first(), MonitorSchedule.id))
    return list((await session.execute(q)).scalars())


SERVER_DERIVED_KEYS = ("store_index",)  # never taken from the stored (user) configuration; derived per monitor below


def monitor_store_index(mon: MonitorSchedule) -> str:
    """Server-side fingerprint store of a new-registrations monitor (persists between runs so diffs work)."""
    from pathlib import Path

    from ..config import get_settings

    d = Path(get_settings().data_dir) / "monitors"
    d.mkdir(parents=True, exist_ok=True)
    return str(d / f"{mon.id}.sqlite")


class _ScheduleView:
    """The schedule as handed to an adapter's ``run_monitor``: every attribute is the real row's, except ``config``, which is the
    policy-checked configuration plus server-derived keys (never written back to the row)."""

    def __init__(self, mon: MonitorSchedule, config: dict[str, Any]):
        object.__setattr__(self, "_mon", mon)
        object.__setattr__(self, "config", config)

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_mon"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "config":
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "_mon"), name, value)


def checked_config(mon: MonitorSchedule) -> dict[str, Any]:
    """The monitor's stored configuration after the single source-configuration policy (raises ``NotPermitted``)."""
    from ..sources.access_policy import config_problems
    from ..sources.base import NotPermitted

    cfg = {k: v for k, v in dict(mon.config or {}).items() if k not in SERVER_DERIVED_KEYS}
    problems = config_problems(cfg)
    if problems:
        field, msg = problems[0]
        raise NotPermitted(f"monitor configuration refused: {field}: {msg}")
    return cfg


async def _dispatch(session: AsyncSession, fetcher: SafeFetcher, mon: MonitorSchedule, now: datetime, limiter: RateLimiter,
                    robots: RobotsCache) -> dict[str, Any]:
    from ..sources.base import NotPermitted

    try:
        cfg = checked_config(mon)
    except NotPermitted as e:
        return {"status": "error", "error": str(e)[:300], "code": "not_permitted"}
    if mon.kind == "website_changes":
        from . import changes
        return await changes.run_monitor(session, fetcher, mon, limiter=limiter, robots=robots)
    if mon.kind == "signals":
        from . import signals
        return await signals.run_monitor(session, fetcher, mon, limiter=limiter, robots=robots)
    if mon.kind == "new_registrations":
        try:
            mod = importlib.import_module("app.sources.new_registrations")
            fn = mod.run_monitor
        except (ImportError, AttributeError):
            return {"status": "unavailable", "reason": "app.sources.new_registrations.run_monitor is not installed"}
        cfg["store_index"] = monitor_store_index(mon)
        view = _ScheduleView(mon, cfg)
        avail = {"session": session, "fetcher": fetcher, "monitor": view, "schedule": view, "config": cfg, "now": now,
                 "limiter": limiter, "robots": robots}
        params = inspect.signature(fn).parameters
        missing = [n for n, p in params.items() if p.default is inspect.Parameter.empty and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
                   and n not in avail]
        if missing:
            return {"status": "error", "error": f"incompatible run_monitor signature (needs {missing})"}
        res = fn(**{n: v for n, v in avail.items() if n in params})
        if inspect.isawaitable(res):
            res = await res
        return res if isinstance(res, dict) else {"result": str(res)}
    return {"status": "error", "error": f"unknown monitor kind {mon.kind!r}"}


async def run_due_monitors(
    session: AsyncSession, fetcher: SafeFetcher, *, now: datetime | None = None, limiter: RateLimiter | None = None,
    robots: RobotsCache | None = None, limit: int = 20,
) -> list[dict[str, Any]]:
    """Run every due monitor (at most ``limit``), update ``last_run_at`` / ``next_run_at`` / ``last_result``; returns one summary each."""
    now = now or datetime.now(UTC)
    limiter = limiter or RateLimiter(min_interval=1.0)
    robots = robots or RobotsCache(fetcher)
    out: list[dict[str, Any]] = []
    for mon in (await due_monitors(session, now))[:limit]:
        status, result = "ok", {}
        try:
            async with session.begin_nested():
                result = await _dispatch(session, fetcher, mon, now, limiter, robots)
        except Exception as e:
            log.exception("monitor %s failed", mon.id)
            status, result = "error", {"status": "error", "error": f"{type(e).__name__}: {e}"[:300]}
        if result.get("status") in ("error",):
            status = "error"
        elif result.get("status") == "unavailable":
            status = "unavailable"
        mon.last_run_at = now
        mon.next_run_at = now + RETRY_AFTER_ERROR if status == "error" else next_run_after(mon.frequency, now)
        mon.last_result = {"status": status, "at": now.isoformat(), **{k: v for k, v in result.items() if k != "status"}}
        out.append({"id": mon.id, "kind": mon.kind, "status": status, "next_run_at": mon.next_run_at, "result": mon.last_result})
    await session.flush()
    return out
