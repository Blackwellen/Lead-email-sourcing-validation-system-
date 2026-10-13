"""Record business signals and persist adapter records together with their signals / identifiers.

Adapters cannot touch the database (they only yield :class:`BusinessRecord`), so signals travel inside
``rec.raw["signals"]`` as plain dicts built with :func:`make_signal` / :func:`attach_signal`.  Persisting goes
through :func:`persist_record`, which strips the bulky ``signals`` list before ``upsert_business`` (the
``organisation_sources.raw`` column is size-capped) and writes the signals with
``INSERT .. ON CONFLICT (organisation_id, dedupe_hash) DO NOTHING``.

Rules enforced here: **no source URL, no signal** (:class:`MissingSourceUrl`), and a deterministic
``dedupe_hash = sha256(type | source_url | title | date)``.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import BusinessSignal, OrganisationRecordLink
from .base import AdapterError, BusinessRecord
from .persist import upsert_business

SIGNAL_TYPES = (
    "new_location", "new_service", "expansion", "hiring", "contract_award", "supplier_partnership", "event",
    "accreditation", "new_registration", "exhibitor", "sponsor",
)


class MissingSourceUrl(AdapterError):
    """A signal was built without a source URL (forbidden: no source URL, no signal)."""


def _date_part(occurred_at: datetime | date | str | None) -> str:
    if occurred_at is None:
        return ""
    if isinstance(occurred_at, datetime):
        return occurred_at.date().isoformat()
    if isinstance(occurred_at, date):
        return occurred_at.isoformat()
    return str(occurred_at)[:10]


def signal_dedupe_hash(signal_type: str, source_url: str, title: str, occurred_at: datetime | date | str | None) -> str:
    key = "\x1f".join((signal_type, source_url.strip(), title.strip(), _date_part(occurred_at)))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def make_signal(
    signal_type: str, title: str, source_url: str | None, *, detail: dict[str, Any] | None = None,
    occurred_at: datetime | date | str | None = None, confidence: float = 0.7,
) -> dict[str, Any]:
    """Build a JSON-safe signal dict.  Raises :class:`MissingSourceUrl` without a (non-blank) source URL."""
    if signal_type not in SIGNAL_TYPES:
        raise ValueError(f"unknown signal type {signal_type!r}")
    if not source_url or not str(source_url).strip():
        raise MissingSourceUrl(f"signal {signal_type!r} {title[:60]!r} has no source URL")
    if not title or not title.strip():
        raise ValueError("signal title is required")
    occ = None
    if isinstance(occurred_at, datetime):
        occ = occurred_at.isoformat()
    elif isinstance(occurred_at, date):
        occ = occurred_at.isoformat()
    elif occurred_at:
        occ = str(occurred_at)
    return {
        "signal_type": signal_type, "title": title.strip()[:400], "source_url": str(source_url).strip(),
        "detail": detail or {}, "occurred_at": occ, "confidence": float(confidence),
    }


def attach_signal(rec: BusinessRecord, signal_type: str, title: str, source_url: str | None, **kw: Any) -> dict[str, Any]:
    sig = make_signal(signal_type, title, source_url, **kw)
    rec.raw.setdefault("signals", []).append(sig)
    return sig


def _parse_occurred(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


async def record_signal(session: AsyncSession, org_id: int, source_adapter: str, sig: dict[str, Any]) -> bool:
    """Insert one signal; returns True when a new row was written (False = duplicate)."""
    url = sig.get("source_url")
    if not url or not str(url).strip():
        raise MissingSourceUrl(f"signal {sig.get('signal_type')!r} has no source URL")
    occ = _parse_occurred(sig.get("occurred_at"))
    h = signal_dedupe_hash(sig["signal_type"], url, sig["title"], occ or sig.get("occurred_at"))
    stmt = (
        pg_insert(BusinessSignal)
        .values(
            organisation_id=org_id, signal_type=sig["signal_type"], title=sig["title"][:400], detail=sig.get("detail") or {},
            source_adapter=source_adapter, source_url=url, occurred_at=occ, confidence=float(sig.get("confidence", 0.5)),
            dedupe_hash=h,
        )
        .on_conflict_do_nothing(index_elements=["organisation_id", "dedupe_hash"])
        .returning(BusinessSignal.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


@dataclass
class PersistResult:
    organisation_id: int
    created: bool
    signals_inserted: int = 0
    signals_total: int = 0
    link_written: bool = False
    relationships: list[dict[str, Any]] = field(default_factory=list)


async def persist_record(session: AsyncSession, rec: BusinessRecord, *, run_id: int | None = None,
                         category_id: int | None = None) -> PersistResult:
    """``upsert_business`` + signals (``rec.raw['signals']``) + one :class:`OrganisationRecordLink` for
    ``rec.raw['identifiers'][0]`` (``{'type','value'}``).  Relationship hints (``rec.raw['relationships']``) stay in
    ``organisation_sources.raw`` for the graph engine and are echoed in the result."""
    signals = list(rec.raw.get("signals") or [])
    slim = replace(rec, raw={k: v for k, v in rec.raw.items() if k != "signals"})
    org_id, created = await upsert_business(session, slim, run_id=run_id, category_id=category_id)
    inserted = 0
    for sig in signals:
        if await record_signal(session, org_id, rec.adapter_id or "unknown", sig):
            inserted += 1
    link = False
    idents = rec.raw.get("identifiers") or []
    if idents and rec.source_ref:
        first = idents[0]
        res = await session.execute(
            pg_insert(OrganisationRecordLink)
            .values(
                organisation_id=org_id, source_adapter=rec.adapter_id or "unknown", source_ref=rec.source_ref[:300],
                identifier_type=str(first.get("type"))[:30], identifier_value=str(first.get("value"))[:120],
                match_basis=str(first.get("basis", "registration_number"))[:40],
                match_confidence=float(first.get("confidence", 0.9)),
            )
            .on_conflict_do_nothing(constraint="uq_orglink")
            .returning(OrganisationRecordLink.id)
        )
        link = res.scalar_one_or_none() is not None
    return PersistResult(org_id, created, inserted, len(signals), link, list(rec.raw.get("relationships") or []))


__all__ = [
    "SIGNAL_TYPES", "MissingSourceUrl", "make_signal", "attach_signal", "record_signal", "signal_dedupe_hash",
    "persist_record", "PersistResult",
]
