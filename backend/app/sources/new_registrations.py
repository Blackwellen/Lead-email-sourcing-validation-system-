"""New company registrations from Companies House BasicCompanyData snapshots (DISC-08).

Two modes
---------
* **window** (cheap, one snapshot): stream the CSV once and keep rows whose ``IncorporationDate`` is within the last N days.
* **diff** (previous vs current snapshot): detect new incorporations plus status / name / address changes.

Why SQLite for the diff
-----------------------
Each snapshot has ~5 M rows, so neither dict-of-both-snapshots nor ``sorted()`` fits comfortably in RAM, and the CH file is not
guaranteed to be sorted by company number (no sorted-merge join).  A *fingerprint index* in SQLite -- ``(company_number PK,
name_hash, status_hash, address_hash)``, three 64-bit hashes per row, ``WITHOUT ROWID`` -- is disk-backed (flat RAM, a few
tens of bytes per company on disk), gives an indexed anti-/inner join in SQL, and is in the stdlib.  It also doubles as the
"stored fingerprint table": the index of the current snapshot can be kept and used as ``previous`` next month, so the old file
need not be retained (then old values in change details are unknown; with the old file present they are filled in by a
second streaming pass restricted to the changed company numbers).  A PostgreSQL temp table would need a session inside a pure
adapter and a network round-trip per batch; an external merge sort needs both files pre-sorted.

Streaming passes: build(prev) -> build(cur) -> SQL diff -> [read prev once for old values of changed rows] -> read cur once and
emit records for numbers in the diff.  Resume = row offset in the current snapshot.

Signals
-------
* ``new_registration`` (incorporations) -- title ``New company registered: <name>``, ``occurred_at`` = incorporation date.
* ``registry_change`` (additive signal type, not in the original list) -- ``detail.change_kind`` in ``name|status|address``.
Records never carry a website, domain or e-mail (the dataset has none; nothing is assumed).
"""
from __future__ import annotations

import calendar
import hashlib
import os
import re
import sqlite3
import tempfile
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import MonitorSchedule
from .base import (
    AdapterHealth,
    AdapterInfo,
    BusinessRecord,
    Checkpoint,
    DiscoveryQuery,
    NotConfigured,
    RatePolicy,
    SourceAdapter,
    register,
)
from .companies_house_bulk import (
    ATTRIBUTION,
    LICENCE,
    CompaniesHouseBulkAdapter,
    iter_rows,
    row_to_record,
)
from .geo_filters import RecordFilter
from .signals_util import make_signal, persist_record

ADAPTER_ID = "companies_house_new_registrations"
_SNAP_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
FREQUENCIES = ("daily", "weekly", "monthly")


# --------------------------------------------------------------------------- helpers
def parse_ch_date(s: str | None) -> date | None:
    """Companies House dates are ``dd/mm/yyyy`` (ISO tolerated)."""
    if not s:
        return None
    t = s.strip()
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", t)
    try:
        if m:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        return date.fromisoformat(t[:10])
    except ValueError:
        return None


def snapshot_date(path: Path, fallback: date | None = None) -> date | None:
    m = _SNAP_DATE.search(path.name)
    if m:
        try:
            return date.fromisoformat(m.group(1))
        except ValueError:
            pass
    return fallback


def _h(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "big", signed=True)


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().casefold())


def _fingerprint(r: dict[str, str]) -> tuple[int, int, int]:
    addr = "|".join(_norm(r.get(k)) for k in ("care_of", "po_box", "line1", "line2", "town", "county", "country", "postcode"))
    return _h(_norm(r.get("name"))), _h(_norm(r.get("status"))), _h(addr)


def compute_next_run(frequency: str, now: datetime, anchor: datetime | None = None) -> datetime:
    """Next run strictly after ``now``.  With an ``anchor`` (previous ``next_run_at``) the cadence does not drift."""
    if frequency not in FREQUENCIES:
        raise ValueError(f"unknown frequency {frequency!r}")
    cur = anchor if anchor is not None and anchor <= now else now

    def step(d: datetime) -> datetime:
        if frequency == "daily":
            return d + timedelta(days=1)
        if frequency == "weekly":
            return d + timedelta(days=7)
        month = d.month % 12 + 1
        year = d.year + (1 if d.month == 12 else 0)
        day = min(d.day, calendar.monthrange(year, month)[1])
        return d.replace(year=year, month=month, day=day)

    nxt = step(cur)
    while nxt <= now:
        nxt = step(nxt)
    return nxt


# --------------------------------------------------------------------------- fingerprint index
class SnapshotIndex:
    """Disk-backed fingerprint index of one snapshot (see module docstring)."""

    def __init__(self, path: Path | str | None = None, *, directory: Path | str | None = None):
        self.temp = path is None
        if path is None:
            fd, name = tempfile.mkstemp(prefix="chsnap-", suffix=".sqlite", dir=str(directory) if directory else None)
            os.close(fd)
            path = name
        self.path = Path(path)
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA journal_mode=OFF")
        self.db.execute("PRAGMA synchronous=OFF")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS fp (cn TEXT PRIMARY KEY, nh INTEGER, sh INTEGER, ah INTEGER) WITHOUT ROWID")
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")

    @classmethod
    def build(cls, snapshot: Path, *, path: Path | str | None = None, directory: Path | str | None = None,
              member: str | None = None) -> SnapshotIndex:
        idx = cls(path, directory=directory)
        idx.db.execute("DELETE FROM fp")
        batch: list[tuple[str, int, int, int]] = []
        for _n, r in iter_rows(snapshot, member=member):
            cn = r.get("number", "").strip().upper()
            if not cn:
                continue
            nh, sh, ah = _fingerprint(r)
            batch.append((cn, nh, sh, ah))
            if len(batch) >= 20000:
                idx.db.executemany("INSERT OR REPLACE INTO fp VALUES (?,?,?,?)", batch)
                batch.clear()
        if batch:
            idx.db.executemany("INSERT OR REPLACE INTO fp VALUES (?,?,?,?)", batch)
        idx.set_meta("snapshot", snapshot.name)
        idx.set_meta("built_at", datetime.now(UTC).isoformat())
        idx.db.commit()
        return idx

    def set_meta(self, k: str, v: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, v))

    def meta(self, k: str) -> str | None:
        row = self.db.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
        return row[0] if row else None

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM fp").fetchone()[0]

    def close(self, *, delete: bool | None = None) -> None:
        try:
            self.db.commit()
            self.db.close()
        finally:
            if delete if delete is not None else self.temp:
                self.path.unlink(missing_ok=True)


@dataclass
class DiffResult:
    new: int = 0
    name_changed: int = 0
    status_changed: int = 0
    address_changed: int = 0
    removed: int = 0
    unchanged: int = 0


def diff_indexes(prev: SnapshotIndex, cur: SnapshotIndex, work: Path | str | None = None) -> tuple[sqlite3.Connection, DiffResult, Path]:
    """Compute the diff table ``d(cn, kinds)`` in a scratch SQLite DB; returns ``(connection, counts, db_path)``."""
    fd, name = tempfile.mkstemp(prefix="chdiff-", suffix=".sqlite", dir=str(work) if work else None)
    os.close(fd)
    db = sqlite3.connect(name)
    db.execute("PRAGMA journal_mode=OFF")
    db.execute("PRAGMA synchronous=OFF")
    prev.db.commit()
    cur.db.commit()
    db.execute("ATTACH DATABASE ? AS p", (str(prev.path),))
    db.execute("ATTACH DATABASE ? AS c", (str(cur.path),))
    db.execute("CREATE TABLE d (cn TEXT PRIMARY KEY, kinds TEXT NOT NULL, old_name TEXT, old_status TEXT, old_addr TEXT) WITHOUT ROWID")
    db.execute("INSERT INTO d(cn, kinds) SELECT c.fp.cn, 'new' FROM c.fp LEFT JOIN p.fp ON p.fp.cn = c.fp.cn WHERE p.fp.cn IS NULL")
    db.execute(
        "INSERT INTO d(cn, kinds) SELECT c.fp.cn, TRIM("
        " CASE WHEN c.fp.nh <> p.fp.nh THEN 'name ' ELSE '' END ||"
        " CASE WHEN c.fp.sh <> p.fp.sh THEN 'status ' ELSE '' END ||"
        " CASE WHEN c.fp.ah <> p.fp.ah THEN 'address' ELSE '' END)"
        " FROM c.fp JOIN p.fp ON p.fp.cn = c.fp.cn WHERE c.fp.nh <> p.fp.nh OR c.fp.sh <> p.fp.sh OR c.fp.ah <> p.fp.ah"
    )
    res = DiffResult()
    res.new = db.execute("SELECT COUNT(*) FROM d WHERE kinds = 'new'").fetchone()[0]
    res.name_changed = db.execute("SELECT COUNT(*) FROM d WHERE kinds <> 'new' AND kinds LIKE '%name%'").fetchone()[0]
    res.status_changed = db.execute("SELECT COUNT(*) FROM d WHERE kinds <> 'new' AND kinds LIKE '%status%'").fetchone()[0]
    res.address_changed = db.execute("SELECT COUNT(*) FROM d WHERE kinds <> 'new' AND kinds LIKE '%address%'").fetchone()[0]
    res.removed = db.execute("SELECT COUNT(*) FROM p.fp LEFT JOIN c.fp ON c.fp.cn = p.fp.cn WHERE c.fp.cn IS NULL").fetchone()[0]
    res.unchanged = cur.count() - res.new - db.execute("SELECT COUNT(*) FROM d WHERE kinds <> 'new'").fetchone()[0]
    db.commit()
    return db, res, Path(name)


def _fill_old_values(db: sqlite3.Connection, prev_snapshot: Path, member: str | None) -> None:
    """One streaming pass over the OLD snapshot recording old name/status/address for changed company numbers only."""
    batch: list[tuple[str, str, str, str]] = []
    for _n, r in iter_rows(prev_snapshot, member=member):
        cn = r.get("number", "").strip().upper()
        if db.execute("SELECT 1 FROM d WHERE cn = ? AND kinds <> 'new'", (cn,)).fetchone():
            addr = ", ".join(r.get(k, "") for k in ("line1", "line2", "town", "county", "postcode") if r.get(k))
            batch.append((r.get("name", ""), r.get("status", ""), addr, cn))
            if len(batch) >= 1000:
                db.executemany("UPDATE d SET old_name = ?, old_status = ?, old_addr = ? WHERE cn = ?", batch)
                batch.clear()
    if batch:
        db.executemany("UPDATE d SET old_name = ?, old_status = ?, old_addr = ? WHERE cn = ?", batch)
    db.commit()


# --------------------------------------------------------------------------- record builders
def _ch_url(cn: str) -> str:
    return f"https://find-and-update.company-information.service.gov.uk/company/{cn}"


def incorporation_record(rec: BusinessRecord, inc: date | None) -> BusinessRecord:
    url = rec.source_url or _ch_url(rec.company_number or rec.source_ref)
    rec.category_hint = "new_registration"
    rec.raw.update(identifiers=[{"type": "company_number", "value": rec.company_number}] if rec.company_number else [])
    rec.raw["signals"] = [make_signal(
        "new_registration", f"New company registered: {rec.legal_name}", url,
        detail={"company_number": rec.company_number, "incorporation_date": inc.isoformat() if inc else None, "status": rec.status,
                "sic_codes": rec.sic_codes, "postcode": rec.postal_code, "legal_type": rec.legal_type, "change_kind": "incorporation"},
        occurred_at=inc, confidence=0.95,
    )]
    return rec


def change_record(rec: BusinessRecord, kinds: list[str], old: dict[str, str | None], when: date | None) -> BusinessRecord:
    url = rec.source_url or _ch_url(rec.company_number or rec.source_ref)
    rec.raw["identifiers"] = [{"type": "company_number", "value": rec.company_number}] if rec.company_number else []
    sigs = []
    for kind in kinds:
        if kind == "name":
            title = f"Company name changed: {old.get('name') or '?'} -> {rec.legal_name}"
            det = {"old": old.get("name"), "new": rec.legal_name}
        elif kind == "status":
            title = f"Company status changed: {old.get('status') or '?'} -> {rec.status}"
            det = {"old": old.get("status"), "new": rec.status}
        else:
            title = f"Registered address changed for {rec.legal_name}"
            det = {"old": old.get("address"), "new": rec.address}
        det.update(change_kind=kind, company_number=rec.company_number)
        sigs.append(make_signal("registry_change", title[:400], url, detail=det, occurred_at=when, confidence=0.95))
    rec.raw["signals"] = sigs
    rec.raw["changes"] = kinds
    return rec


# --------------------------------------------------------------------------- streaming generators
def iter_window(snapshot: Path, days: int, *, today: date | None = None, member: str | None = None, skip: int = 0,
                observed_at: datetime | None = None) -> Iterator[tuple[int, BusinessRecord]]:
    """Companies incorporated in the last ``days`` days (inclusive), one pass over one snapshot."""
    ref = today or snapshot_date(snapshot) or datetime.now(UTC).date()
    cutoff = ref - timedelta(days=days)
    for n, r in iter_rows(snapshot, member=member, skip=skip):
        inc = parse_ch_date(r.get("incorporated"))
        if inc is None or inc < cutoff or inc > ref:
            continue
        rec = row_to_record(r, observed_at)
        if rec is None:
            continue
        yield n, incorporation_record(rec, inc)


def iter_diff(
    prev_snapshot: Path | None, cur_snapshot: Path, *, prev_index: SnapshotIndex | None = None, keep_index: Path | str | None = None,
    work_dir: Path | str | None = None, member: str | None = None, skip: int = 0, kinds: set[str] | None = None,
    observed_at: datetime | None = None, stats: dict[str, Any] | None = None,
) -> Iterator[tuple[int, BusinessRecord]]:
    """Diff generator (see module docstring).  ``keep_index`` stores the CURRENT fingerprint index for next time."""
    stats = stats if stats is not None else {}
    own_prev = False
    if prev_index is None:
        if prev_snapshot is None:
            raise NotConfigured("diff mode needs a previous snapshot file or a stored fingerprint index")
        prev_index = SnapshotIndex.build(prev_snapshot, directory=work_dir, member=member)
        own_prev = True
    cur_index = SnapshotIndex.build(cur_snapshot, path=keep_index, directory=work_dir, member=member)
    db = None
    diff_path: Path | None = None
    try:
        db, res, diff_path = diff_indexes(prev_index, cur_index, work_dir)
        stats.update(res.__dict__)
        if prev_snapshot is not None:
            _fill_old_values(db, prev_snapshot, member)
        when = snapshot_date(cur_snapshot)
        want = kinds
        for n, r in iter_rows(cur_snapshot, member=member, skip=skip):
            cn = r.get("number", "").strip().upper()
            row = db.execute("SELECT kinds, old_name, old_status, old_addr FROM d WHERE cn = ?", (cn,)).fetchone()
            if not row:
                continue
            ks = row[0].split()
            rec = row_to_record(r, observed_at)
            if rec is None:
                continue
            if ks == ["new"]:
                if want and "new" not in want:
                    continue
                yield n, incorporation_record(rec, parse_ch_date(r.get("incorporated")))
            else:
                ks = [k for k in ks if not want or k in want]
                if not ks:
                    continue
                yield n, change_record(rec, ks, {"name": row[1], "status": row[2], "address": row[3]}, when)
    finally:
        cur_index.close(delete=keep_index is None)
        if own_prev:
            prev_index.close()
        if db is not None:
            db.close()
        if diff_path is not None:
            diff_path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- adapter
@register
class NewRegistrationsAdapter(SourceAdapter):
    info = AdapterInfo(
        adapter_id=ADAPTER_ID,
        name="UK Companies House -- new registrations & registry changes (snapshot diff)",
        country_coverage=("GB",),
        source_type="bulk_dataset",
        licence=LICENCE,
        source_url="https://download.companieshouse.gov.uk/en_output.html",
        access_requirements=("Config: 'current_path' (snapshot zip/csv) and, for diff mode, 'previous_path' (or 'previous_index' = stored fingerprint DB); "
                             "'mode' window|diff (default: diff when a previous exists else window); 'days' for window; 'kinds' filter."),
        automated_collection_permitted=True,
        rate_policy=RatePolicy(requests_per_minute=None, concurrency=1),
        fields_available=("legal_name", "company_number", "status", "address", "sic_codes", "incorporation_date"),
        incremental_support=True,
        requires_config=True,
        attribution=ATTRIBUTION,
        cost="free", credentials="none", refresh_frequency="monthly (BasicCompanyData) -- window mode is only as fresh as the snapshot",
        access_method="bulk_file", industries=("*", "new_business"),
        allowed_usage="OGL v3.0 with attribution. Records carry no website/e-mail; downstream website discovery must not assume one exists.",
        notes="Uses the same snapshot product as companies_house_bulk (live-checked product page there). Diff correctness and bounded memory are tested on 200k synthetic rows.",
    )

    async def health(self) -> AdapterHealth:
        p = self.config.get("current_path") or self.config.get("path")
        if p:
            ok = Path(p).exists()
            return AdapterHealth(ok, "ok" if ok else "unavailable", f"{'present' if ok else 'missing'}: {p}")
        return AdapterHealth(False, "not_configured", "set 'current_path'")

    async def iter_businesses(
        self, query: DiscoveryQuery, checkpoint: Checkpoint | None = None
    ) -> AsyncIterator[tuple[BusinessRecord, Checkpoint]]:
        if not query.covers_country("GB"):
            return
        cfg = dict(self.config)
        cur_cfg = {k: v for k, v in cfg.items() if k not in ("path",)}
        cur_cfg["path"] = cfg.get("current_path") or cfg.get("path")
        cur = await CompaniesHouseBulkAdapter(cur_cfg).ensure_file(cfg.get("http_client"))
        prev_path = Path(cfg["previous_path"]) if cfg.get("previous_path") else None
        prev_index = SnapshotIndex(cfg["previous_index"]) if cfg.get("previous_index") and Path(cfg["previous_index"]).exists() else None
        mode = cfg.get("mode") or ("diff" if (prev_path or prev_index) else "window")
        flt = RecordFilter(query)
        cp = checkpoint or {}
        skip = int(cp.get("row", -1)) + 1 if cp.get("file") in (None, cur.name) and cp.get("mode") in (None, mode) else 0
        observed = datetime.now(UTC)
        stats: dict[str, Any] = {}
        self.last_stats = stats
        if mode == "window":
            gen = iter_window(cur, int(cfg.get("days", 30)), today=(parse_ch_date(str(cfg["today"])) if cfg.get("today") else None), member=cfg.get("member"), skip=skip, observed_at=observed)
        elif mode == "diff":
            gen = iter_diff(prev_path, cur, prev_index=prev_index, keep_index=cfg.get("store_index"), work_dir=cfg.get("work_dir"),
                            member=cfg.get("member"), skip=skip, kinds=set(cfg["kinds"]) if cfg.get("kinds") else None,
                            observed_at=observed, stats=stats)
        else:
            raise NotConfigured(f"unknown mode {mode!r}")
        emitted = 0
        try:
            for n, rec in gen:
                rec.adapter_id = ADAPTER_ID
                if not flt.accept(rec):
                    continue
                yield rec, {"row": n, "file": cur.name, "mode": mode}
                emitted += 1
                if query.limit and emitted >= query.limit:
                    return
        finally:
            gen.close()
            if prev_index is not None:
                prev_index.close(delete=False)


# --------------------------------------------------------------------------- monitor
_DEFAULT_WINDOW_DAYS = {"daily": 2, "weekly": 8, "monthly": 32}


async def run_monitor(session: AsyncSession, schedule: MonitorSchedule, *, now: datetime | None = None, force: bool = False,
                      query: DiscoveryQuery | None = None) -> dict[str, Any]:
    """Run a ``kind='new_registrations'`` monitor.  Honours ``enabled`` and the daily/weekly/monthly cadence.

    ``schedule.config``: ``current_path`` | ``url``/``auto_download`` (snapshot), optional ``previous_path`` / ``previous_index`` /
    ``store_index`` (fingerprint DB kept between runs), ``mode``, ``days`` (window; default by frequency), ``kinds``,
    ``limit``, filters ``sic_codes`` / ``postcodes`` / ``keywords``.  Updates ``last_run_at``, ``next_run_at`` and ``last_result``.
    The caller commits; records and signals are written with ``persist_record`` (flush only).
    """
    now = now or datetime.now(UTC)
    if schedule.kind != "new_registrations":
        raise ValueError(f"run_monitor handles kind='new_registrations', got {schedule.kind!r}")
    if schedule.frequency not in FREQUENCIES:
        raise ValueError(f"unknown frequency {schedule.frequency!r}")
    if not schedule.enabled and not force:
        return {"status": "skipped_disabled"}
    if not force and schedule.next_run_at is not None and now < schedule.next_run_at:
        return {"status": "skipped_not_due", "next_run_at": schedule.next_run_at.isoformat()}
    cfg = dict(schedule.config or {})
    cfg.setdefault("days", _DEFAULT_WINDOW_DAYS[schedule.frequency])
    q = query or DiscoveryQuery(
        countries=["GB"], sic_codes=list(cfg.get("sic_codes") or []), postcodes=list(cfg.get("postcodes") or []),
        keywords=list(cfg.get("keywords") or []), active_only=bool(cfg.get("active_only", False)), limit=cfg.get("limit"),
    )
    adapter = NewRegistrationsAdapter(cfg)
    result: dict[str, Any] = {"status": "ok", "started_at": now.isoformat(), "records": 0, "created": 0, "signals_new": 0, "signals_seen": 0}
    commit_every = int(cfg.get("commit_every", 0))
    try:
        async for rec, _cp in adapter.iter_businesses(q):
            pr = await persist_record(session, rec)
            result["records"] += 1
            result["created"] += int(pr.created)
            result["signals_new"] += pr.signals_inserted
            result["signals_seen"] += pr.signals_total - pr.signals_inserted
            if commit_every and result["records"] % commit_every == 0:
                await session.commit()
        result["mode"] = cfg.get("mode") or ("diff" if (cfg.get("previous_path") or cfg.get("previous_index")) else "window")
        result["diff"] = {k: v for k, v in getattr(adapter, "last_stats", {}).items()}
    except Exception as exc:  # noqa: BLE001 - recorded on the schedule, run stays resumable next cycle
        result.update(status="error", error=f"{type(exc).__name__}: {exc}"[:500])
    anchor = schedule.next_run_at
    schedule.last_run_at = now
    schedule.next_run_at = compute_next_run(schedule.frequency, now, anchor)
    schedule.last_result = result
    await session.flush()
    return result


__all__ = [
    "NewRegistrationsAdapter", "SnapshotIndex", "iter_window", "iter_diff", "diff_indexes", "run_monitor", "compute_next_run",
    "parse_ch_date", "DiffResult",
]
