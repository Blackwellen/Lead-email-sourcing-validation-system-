"""Shared machinery for award-based adapters (OCDS, TED, USAspending): the *supplier-established* filter.

A "unit" is one supplier-in-one-award: a :class:`BusinessRecord` carrying exactly one ``contract_award`` signal in
``rec.raw['signals']``.  Two modes:

* **streaming** (default, ``min_awards <= 1`` and ``min_award_years <= 1``): units pass straight through and the
  source's own position checkpoint is used.
* **aggregate** (``min_awards``/``min_award_years`` > 1): every unit of the window is spilled to a temporary SQLite
  table keyed by supplier identity (``canonical_identity``); suppliers meeting the thresholds are then emitted in key
  order with all their awards merged into ONE record (``raw['award_count']``, ``raw['award_years']``, up to
  ``max_signals`` newest signals).  Checkpoint = last supplier key emitted (resume is a ``skey > last`` range scan).
  SQLite is used for the spill because it is in the stdlib, disk-backed (RAM stays flat for millions of units) and
  gives GROUP BY / ordered range scans without needing a Postgres session inside a pure adapter.

``min_award_years`` implements "prior award years": a supplier that won in N distinct calendar years has a track
record; a one-off winner does not pass.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .base import BusinessRecord, Checkpoint
from .geo_filters import RecordFilter
from .persist import canonical_identity

UnitSource = Callable[[Checkpoint | None], AsyncIterator[tuple[BusinessRecord, Checkpoint]]]


@dataclass(frozen=True)
class Established:
    min_awards: int = 1
    min_award_years: int = 1
    max_signals: int = 50

    @classmethod
    def from_config(cls, config: dict[str, Any], extra: dict[str, Any] | None = None) -> Established:
        extra = extra or {}

        def g(k: str, d: int) -> int:
            return max(1, int(extra.get(k, config.get(k, d))))

        return cls(g("min_awards", 1), g("min_award_years", 1), g("max_signals_per_supplier", 50))

    @property
    def aggregate(self) -> bool:
        return self.min_awards > 1 or self.min_award_years > 1


def supplier_key(rec: BusinessRecord) -> str | None:
    try:
        return canonical_identity(rec)
    except ValueError:
        return None


def _year_of(rec: BusinessRecord) -> int | None:
    ad = rec.raw.get("award_date")
    if ad and len(str(ad)) >= 4 and str(ad)[:4].isdigit():
        return int(str(ad)[:4])
    sigs = rec.raw.get("signals") or []
    for s in sigs:
        occ = s.get("occurred_at")
        if occ and len(str(occ)) >= 4 and str(occ)[:4].isdigit():
            return int(str(occ)[:4])
    return None


class AwardSpill:
    """Disk-backed (SQLite) store of award units keyed by supplier."""

    def __init__(self, directory: Path | str | None = None):
        fd, name = tempfile.mkstemp(prefix="awards-", suffix=".sqlite", dir=str(directory) if directory else None)
        os.close(fd)
        self.path = Path(name)
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA journal_mode=OFF")
        self.db.execute("PRAGMA synchronous=OFF")
        self.db.execute(
            "CREATE TABLE units (skey TEXT NOT NULL, akey TEXT NOT NULL, year INTEGER, ts TEXT NOT NULL, payload TEXT NOT NULL,"
            " PRIMARY KEY (skey, akey)) WITHOUT ROWID"
        )
        self._pending = 0

    def add(self, skey: str, akey: str, ts: str, year: int | None, rec: BusinessRecord) -> None:
        d = asdict(rec)
        d["observed_at"] = None
        self.db.execute("INSERT OR IGNORE INTO units VALUES (?,?,?,?,?)", (skey, akey, year, ts, json.dumps(d, default=str)))
        self._pending += 1
        if self._pending >= 5000:
            self.db.commit()
            self._pending = 0

    def suppliers(self, after: str | None, est: Established):
        self.db.commit()
        cur = self.db.execute(
            "SELECT skey, COUNT(*) c, COUNT(DISTINCT year) y FROM units WHERE skey > ? GROUP BY skey "
            "HAVING c >= ? AND (y >= ? OR (y = 0 AND ? <= 1)) ORDER BY skey",
            (after or "", est.min_awards, est.min_award_years, est.min_award_years),
        )
        yield from cur

    def rows(self, skey: str, limit: int) -> list[tuple[str, dict[str, Any]]]:
        cur = self.db.execute("SELECT ts, payload FROM units WHERE skey = ? ORDER BY ts DESC, akey LIMIT ?", (skey, limit))
        return [(ts, json.loads(p)) for ts, p in cur]

    def stats(self) -> dict[str, int]:
        self.db.commit()
        n, s = self.db.execute("SELECT COUNT(*), COUNT(DISTINCT skey) FROM units").fetchone()
        return {"units": n, "suppliers": s}

    def close(self) -> None:
        try:
            self.db.close()
        finally:
            self.path.unlink(missing_ok=True)


def merge_supplier(rows: list[tuple[str, dict[str, Any]]], total: int, years: int) -> BusinessRecord:
    """Newest unit supplies the identity fields; all units' signals are kept (newest first)."""
    newest = rows[0][1]
    signals: list[dict[str, Any]] = []
    industries: list[str] = []
    cpvs: list[str] = []
    for _ts, d in rows:
        signals.extend(d.get("raw", {}).get("signals") or [])
        for i in d.get("raw", {}).get("industries") or []:
            if i not in industries:
                industries.append(i)
        for c in d.get("raw", {}).get("cpv") or []:
            if c not in cpvs:
                cpvs.append(c)
    rec = BusinessRecord(**{**newest, "raw": dict(newest.get("raw") or {})})
    if not rec.source_url:
        rec.source_url = next((d.get("source_url") for _ts, d in rows if d.get("source_url")), None)
    rec.raw.update(signals=signals, industries=industries, cpv=cpvs[:30], award_count=total, award_years=years)
    return rec


async def drive_awards(
    source: UnitSource, *, established: Established, flt: RecordFilter, checkpoint: Checkpoint | None, limit: int | None,
    adapter_id: str, spill_dir: Path | str | None = None, stats: dict[str, int] | None = None,
) -> AsyncIterator[tuple[BusinessRecord, Checkpoint]]:
    """Apply the established-supplier policy to a unit source (see module docstring)."""
    stats = stats if stats is not None else {}
    emitted = 0
    if not established.aggregate:
        async for rec, cp in source(checkpoint if checkpoint and not checkpoint.get("agg") else None):
            rec.adapter_id = adapter_id
            stats["units"] = stats.get("units", 0) + 1
            if not flt.accept(rec):
                continue
            yield rec, cp
            emitted += 1
            if limit and emitted >= limit:
                return
        return
    spill = AwardSpill(spill_dir)
    try:
        async for rec, _cp in source(None):
            rec.adapter_id = adapter_id
            sk = supplier_key(rec)
            if sk is None:
                continue
            sigs = rec.raw.get("signals") or []
            ts = str(rec.raw.get("award_date") or (sigs[0].get("occurred_at") if sigs else "") or "")
            akey = str(rec.raw.get("award_key") or (sigs[0].get("source_url") if sigs else "") or ts)
            spill.add(sk, akey, ts, _year_of(rec), rec)
            stats["units"] = stats.get("units", 0) + 1
        after = (checkpoint or {}).get("skey") if checkpoint and checkpoint.get("agg") else None
        for sk, cnt, yrs in spill.suppliers(after, established):
            rows = spill.rows(sk, established.max_signals)
            rec = merge_supplier(rows, cnt, yrs)
            rec.adapter_id = adapter_id
            rec.observed_at = datetime.now(UTC)
            if not flt.accept(rec):
                continue
            yield rec, {"agg": True, "skey": sk}
            emitted += 1
            if limit and emitted >= limit:
                return
        stats.update(spill.stats())
    finally:
        spill.close()


__all__ = ["Established", "AwardSpill", "drive_awards", "merge_supplier", "supplier_key"]
