"""Section B: measured source effectiveness.

``record_effect`` atomically increments counters (``UPDATE .. SET x = x + :d`` through an upsert) on two rows: the
(adapter, run) row and the all-time (adapter, NULL) row. ``metrics_for`` derives ratios ONLY from those measured
counters and returns ``None`` for a ratio whose denominator has too few samples; nothing is ever estimated here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import SourceEffectiveness

INT_COUNTERS = (
    "records_seen", "unique_businesses", "duplicates", "confirmed_domains", "published_emails", "eligible_contacts",
    "http_requests", "failures", "jobs",
)
FLOAT_COUNTERS = ("cpu_seconds", "wall_seconds")
MAX_COUNTERS = ("peak_rss_mb",)
ALL_COUNTERS = INT_COUNTERS + FLOAT_COUNTERS + MAX_COUNTERS
DEFAULT_MIN_SAMPLES = 10
MIN_WALL_SECONDS = 60.0


async def record_effect(session: AsyncSession, adapter_id: str, run_id: int | None, **deltas: float) -> None:
    """Atomically add ``deltas`` to the (adapter, run) row and the all-time (adapter) row. Negative deltas are rejected."""
    bad = set(deltas) - set(ALL_COUNTERS)
    if bad:
        raise ValueError(f"unknown effectiveness counters: {sorted(bad)}")
    clean: dict[str, float] = {}
    for k, v in deltas.items():
        if v is None:
            continue
        if v < 0:
            raise ValueError(f"negative delta for {k}")
        clean[k] = int(v) if k in INT_COUNTERS else float(v)
    if not clean:
        return
    targets: list[int | None] = [None] if run_id is None else [run_id, None]
    for rid in targets:
        ins = pg_insert(SourceEffectiveness).values(adapter_id=adapter_id, run_id=rid, **clean)
        tbl = SourceEffectiveness.__table__
        set_ = {}
        for k in clean:
            if k in MAX_COUNTERS:
                set_[k] = func.greatest(tbl.c[k], ins.excluded[k])
            else:
                set_[k] = tbl.c[k] + ins.excluded[k]
        set_["updated_at"] = func.now()
        if rid is None:
            stmt = ins.on_conflict_do_update(index_elements=["adapter_id"], index_where=text("run_id is null"), set_=set_)
        else:
            stmt = ins.on_conflict_do_update(constraint="uq_srceff", set_=set_)
        await session.execute(stmt)


@dataclass
class Metrics:
    adapter_id: str | None
    run_id: int | None
    counters: dict[str, float] = field(default_factory=dict)
    derived: dict[str, float | None] = field(default_factory=dict)
    min_samples: int = DEFAULT_MIN_SAMPLES

    def __getitem__(self, k: str):
        return self.derived[k] if k in self.derived else self.counters[k]

    def get(self, k: str, default=None):
        if k in self.derived:
            return self.derived[k]
        return self.counters.get(k, default)

    def to_dict(self) -> dict[str, Any]:
        return {"adapter_id": self.adapter_id, "run_id": self.run_id, "counters": dict(self.counters),
                "derived": dict(self.derived), "min_samples": self.min_samples}


def derive(c: dict[str, float], min_samples: int = DEFAULT_MIN_SAMPLES) -> dict[str, float | None]:
    """Ratios from raw counters; ``None`` whenever the denominator is below ``min_samples`` (or zero)."""

    def ratio(num: str, den: str, scale: float = 1.0) -> float | None:
        d = c.get(den, 0)
        if d is None or d < max(1, min_samples):
            return None
        return round(c.get(num, 0) * scale / d, 4)

    wall = c.get("wall_seconds", 0.0)
    jobs = c.get("jobs", 0)
    return {
        "unique_businesses_per_1000_records": ratio("unique_businesses", "records_seen", 1000),
        "confirmed_domains_per_1000_businesses": ratio("confirmed_domains", "unique_businesses", 1000),
        "published_emails_per_1000_businesses": ratio("published_emails", "unique_businesses", 1000),
        "eligible_contacts_per_1000_businesses": ratio("eligible_contacts", "unique_businesses", 1000),
        # cost per contact needs at least one real contact AND enough contacts to be meaningful
        "http_requests_per_new_contact": ratio("http_requests", "eligible_contacts") if c.get("http_requests", 0) > 0 else None,
        "cpu_seconds_per_job": round(c.get("cpu_seconds", 0) / jobs, 4) if jobs >= max(1, min_samples) and c.get("cpu_seconds", 0) > 0 else None,
        "peak_rss_mb": c.get("peak_rss_mb") if c.get("peak_rss_mb", 0) > 0 else None,
        "duplicate_pct": ratio("duplicates", "records_seen", 100),
        "failure_pct": ratio("failures", "jobs", 100),
        "new_contacts_per_processing_hour": (round(c.get("eligible_contacts", 0) / (wall / 3600.0), 4)
                                             if wall >= MIN_WALL_SECONDS and jobs >= max(1, min_samples) else None),
    }


def _row_counters(rows) -> dict[str, float]:
    out: dict[str, float] = {k: 0 for k in ALL_COUNTERS}
    for r in rows:
        for k in ALL_COUNTERS:
            v = getattr(r, k) or 0
            out[k] = max(out[k], v) if k in MAX_COUNTERS else out[k] + v
    return out


async def metrics_for(session: AsyncSession, adapter_id: str | None = None, run_id: int | None = None, *,
                      min_samples: int = DEFAULT_MIN_SAMPLES) -> Metrics:
    """Counters + derived ratios for one adapter (or all adapters when None), for one run or all-time (``run_id=None``)."""
    q = select(SourceEffectiveness)
    q = q.where(SourceEffectiveness.run_id.is_(None) if run_id is None else SourceEffectiveness.run_id == run_id)
    if adapter_id is not None:
        q = q.where(SourceEffectiveness.adapter_id == adapter_id)
    rows = list((await session.execute(q)).scalars())
    c = _row_counters(rows)
    return Metrics(adapter_id, run_id, c, derive(c, min_samples), min_samples)


async def metrics_by_adapter(session: AsyncSession, run_id: int | None = None, *,
                             min_samples: int = DEFAULT_MIN_SAMPLES) -> dict[str, Metrics]:
    q = select(SourceEffectiveness)
    q = q.where(SourceEffectiveness.run_id.is_(None) if run_id is None else SourceEffectiveness.run_id == run_id)
    out: dict[str, Metrics] = {}
    for r in (await session.execute(q)).scalars():
        c = _row_counters([r])
        out[r.adapter_id] = Metrics(r.adapter_id, run_id, c, derive(c, min_samples), min_samples)
    return out
