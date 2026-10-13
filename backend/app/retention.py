"""Data-retention job: expire evidence / validation history / exports / sessions per the 'retention' settings."""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .exports import resolve_export_path
from .settings_schema import load_section

log = logging.getLogger("retention")
BATCH = 10_000


async def _batched(session: AsyncSession, sql: str, params: dict[str, Any]) -> int:
    total = 0
    while True:
        res = await session.execute(text(sql), params)
        n = res.rowcount or 0
        await session.commit()
        total += n
        if n < BATCH:
            return total


async def apply_retention(session: AsyncSession) -> dict[str, Any]:
    """Delete expired data according to settings. Idempotent; works in batches. Returns counts per category."""
    cfg = await load_section(session, "retention")
    now = datetime.now(UTC)
    out: dict[str, Any] = {"sessions": 0, "validation_results": 0, "contact_evidence": 0, "evidence_sources": 0, "exports": 0, "audit_events": 0, "emails_flagged_for_review": 0}
    out["sessions"] = await _batched(session, "DELETE FROM sessions WHERE id IN (SELECT id FROM sessions WHERE expires_at < now() LIMIT :n)", {"n": BATCH})
    if cfg.validation_history_days:
        cutoff = now - timedelta(days=cfg.validation_history_days)
        # keep the newest result per (email, check) so current state survives
        out["validation_results"] = await _batched(
            session,
            """DELETE FROM validation_results WHERE id IN (
                   SELECT v.id FROM validation_results v WHERE v.observed_at < :c AND EXISTS (
                       SELECT 1 FROM validation_results v2 WHERE v2.email_id=v.email_id AND v2.check_type=v.check_type
                          AND (v2.observed_at > v.observed_at OR (v2.observed_at = v.observed_at AND v2.id > v.id)))
                   LIMIT :n)""",
            {"c": cutoff, "n": BATCH},
        )
    if cfg.evidence_days:
        cutoff = now - timedelta(days=cfg.evidence_days)
        flagged = 0
        while True:
            rows = (await session.execute(text("DELETE FROM contact_evidence WHERE id IN (SELECT id FROM contact_evidence WHERE observed_at < :c LIMIT :n) RETURNING email_id"), {"c": cutoff, "n": BATCH})).all()
            ids = sorted({r[0] for r in rows if r[0]})
            if ids:
                res = await session.execute(text("UPDATE email_addresses e SET needs_review=true WHERE e.id = ANY(:ids) AND NOT e.needs_review AND NOT EXISTS (SELECT 1 FROM contact_evidence ce WHERE ce.email_id=e.id)"), {"ids": ids})
                flagged += res.rowcount or 0
            await session.commit()
            out["contact_evidence"] += len(rows)
            if len(rows) < BATCH:
                break
        out["emails_flagged_for_review"] = flagged
        out["evidence_sources"] = await _batched(
            session, "DELETE FROM evidence_sources WHERE id IN (SELECT s.id FROM evidence_sources s WHERE s.observed_at < :c AND NOT EXISTS (SELECT 1 FROM contact_evidence ce WHERE ce.evidence_id=s.id) LIMIT :n)", {"c": cutoff, "n": BATCH})
    if cfg.exports_days:
        cutoff = now - timedelta(days=cfg.exports_days)
        rows = (await session.execute(text("SELECT id, file_path FROM exports WHERE created_at < :c AND status IN ('done','failed')"), {"c": cutoff})).all()
        for eid, fp in rows:
            p = resolve_export_path(fp)
            if p:
                p.unlink(missing_ok=True)
            await session.execute(text("DELETE FROM exports WHERE id=:i"), {"i": eid})
        await session.commit()
        out["exports"] = len(rows)
    if cfg.audit_days:
        cutoff = now - timedelta(days=cfg.audit_days)
        out["audit_events"] = await _batched(session, "DELETE FROM audit_events WHERE id IN (SELECT id FROM audit_events WHERE at < :c LIMIT :n)", {"c": cutoff, "n": BATCH})
    log.info("retention applied: %s", {k: v for k, v in out.items() if v})
    return out
