"""Hidden 'system' runs that own background jobs not created by a user-visible run (refresh, revalidation, exports)."""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .models import SourcingRun
from .run_config import RunConfig


def is_system_filter():
    """SQL expression true for system runs (so listings can exclude them)."""
    return SourcingRun.configuration_snapshot["system"].astext == "true"


def not_system_filter():
    return SourcingRun.configuration_snapshot["system"].astext.is_distinct_from("true")


async def ensure_system_run(session: AsyncSession, name: str) -> int:
    """Get-or-create the persistent hidden run ``name`` and make sure its jobs are claimable (status running).

    A run an admin paused (pause-all) stays paused: new jobs queue up and are released on resume."""
    await session.execute(text("SELECT pg_advisory_xact_lock(727002)"))
    run = (await session.execute(select(SourcingRun).where(SourcingRun.name == name, is_system_filter()).with_for_update())).scalars().first()
    if run is None:
        snap = RunConfig(name=name, sources=["system"]).model_dump(mode="json") | {"system": True}
        run = SourcingRun(name=name, status="running", configuration_snapshot=snap, started_at=datetime.now(UTC), target_count=1)
        session.add(run)
        await session.flush()
    elif run.status not in ("running", "paused"):
        run.status = "running"
        run.finished_at = None
    return run.id


async def new_system_run(session: AsyncSession, name: str) -> int:
    """A fresh hidden run (e.g. one per verification batch so it can be paused/cancelled on its own)."""
    snap = RunConfig(name=name, sources=["system"]).model_dump(mode="json") | {"system": True}
    run = SourcingRun(name=name, status="running", configuration_snapshot=snap, started_at=datetime.now(UTC), target_count=1)
    session.add(run)
    await session.flush()
    return run.id
