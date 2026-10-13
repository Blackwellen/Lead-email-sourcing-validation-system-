"""V10 -- imported delivery feedback from a real sending platform.

``accepted`` means the receiving server accepted a message for processing -- it is **never** treated
as inbox placement and never upgrades reachability to "delivered".
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import DeliveryEvent, EmailAddress, SuppressionEntry
from .syntax import check_syntax

EVENT_ALIASES: dict[str, str] = {
    "hard_bounce": "hard_bounce", "hardbounce": "hard_bounce", "bounce": "hard_bounce", "bounced": "hard_bounce",
    "permanent_bounce": "hard_bounce", "permanent": "hard_bounce", "hard": "hard_bounce", "invalid": "hard_bounce",
    "soft_bounce": "soft_bounce", "softbounce": "soft_bounce", "soft": "soft_bounce", "temporary": "soft_bounce",
    "accepted": "accepted", "delivered": "accepted", "sent": "accepted", "delivery": "accepted",
    "complaint": "complaint", "spam": "complaint", "spamreport": "complaint", "spam_complaint": "complaint", "abuse": "complaint",
    "unsubscribe": "unsubscribe", "unsubscribed": "unsubscribe", "opt_out": "unsubscribe", "optout": "unsubscribe",
    "deferral": "deferral", "deferred": "deferral", "delayed": "deferral",
}
SUPPRESSION_REASON = {"hard_bounce": "bounce", "complaint": "complaint", "unsubscribe": "opt_out"}


@dataclass(slots=True)
class ImportSummary:
    received: int = 0
    inserted: int = 0
    duplicates: int = 0
    unknown_email: int = 0
    invalid: int = 0
    suppressions_added: int = 0
    errors: list[str] = field(default_factory=list)
    affected_email_ids: set[int] = field(default_factory=set)


def _parse_dt(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    if isinstance(v, str) and v.strip():
        try:
            dt = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return None


def normalise_event_type(raw: str | None) -> str | None:
    return EVENT_ALIASES.get((raw or "").strip().lower().replace(" ", "_").replace("-", "_"))


async def import_delivery_events(session: AsyncSession, rows: Iterable[Mapping[str, Any]]) -> ImportSummary:
    """Idempotently import ``email,event_type,campaign,occurred_at,detail`` rows.

    Re-importing the same rows inserts nothing. Hard bounce -> ``previously_bounced`` + suppression
    ('bounce'); complaint -> suppression ('complaint'); unsubscribe -> suppression ('opt_out').
    Addresses we do not hold are counted in ``unknown_email`` and skipped.
    """
    summary = ImportSummary()
    for n, row in enumerate(rows, start=1):
        summary.received += 1
        et = normalise_event_type(row.get("event_type"))
        when = _parse_dt(row.get("occurred_at"))
        syn = check_syntax(str(row.get("email") or ""), strip_display=True)
        if et is None or when is None or syn.status != "valid" or not syn.normalised:
            summary.invalid += 1
            summary.errors.append(f"row {n}: invalid event_type/occurred_at/email")
            continue
        email = (await session.execute(select(EmailAddress).where(EmailAddress.email_normalised == syn.normalised))).scalar_one_or_none()
        if email is None:
            summary.unknown_email += 1
            continue
        campaign = (str(row.get("campaign") or "")).strip()[:200]  # '' not NULL so the unique key dedupes
        stmt = pg_insert(DeliveryEvent).values(
            email_id=email.id, event_type=et, campaign=campaign, occurred_at=when,
            detail=(str(row["detail"])[:2000] if row.get("detail") else None),
        ).on_conflict_do_nothing(constraint="uq_delivery_event").returning(DeliveryEvent.id)
        inserted = (await session.execute(stmt)).scalar_one_or_none()
        if inserted is None:
            summary.duplicates += 1
        else:
            summary.inserted += 1
        summary.affected_email_ids.add(email.id)
        # side effects are idempotent too, so a re-import still repairs missing suppressions
        if et in SUPPRESSION_REASON:
            sup = pg_insert(SuppressionEntry).values(
                kind="email", value=syn.normalised, reason=SUPPRESSION_REASON[et], note=f"delivery feedback ({campaign or 'no campaign'})",
            ).on_conflict_do_nothing(constraint="uq_suppression").returning(SuppressionEntry.id)
            if (await session.execute(sup)).scalar_one_or_none() is not None:
                summary.suppressions_added += 1
            values: dict[str, Any] = {"suppressed_at": func.coalesce(EmailAddress.suppressed_at, func.now())}
            if et == "hard_bounce":
                values["previously_bounced"] = True
            await session.execute(update(EmailAddress).where(EmailAddress.id == email.id).values(**values))
    await session.commit()
    return summary


async def delivery_history(session: AsyncSession, email_id: int) -> dict[str, int]:
    """Counts of imported event types for one address."""
    rows = (await session.execute(
        select(DeliveryEvent.event_type, func.count()).where(DeliveryEvent.email_id == email_id).group_by(DeliveryEvent.event_type)
    )).all()
    return {t: c for t, c in rows}


async def domain_accept_then_bounce(session: AsyncSession, domain: str) -> bool:
    """Passive catch-all hint: an address on this domain was 'accepted' and later hard-bounced."""
    acc = DeliveryEvent.__table__.alias("acc")
    bnc = DeliveryEvent.__table__.alias("bnc")
    q = (
        select(func.count())
        .select_from(acc.join(bnc, (acc.c.email_id == bnc.c.email_id) & (bnc.c.occurred_at > acc.c.occurred_at)))
        .join(EmailAddress, EmailAddress.id == acc.c.email_id)
        .where(acc.c.event_type == "accepted", bnc.c.event_type == "hard_bounce", EmailAddress.domain == domain.lower())
    )
    return ((await session.execute(q)).scalar_one() or 0) > 0
