"""Suppression list logic (by e-mail, domain or company). Entries are durable and win over every other status."""
from __future__ import annotations

from sqlalchemy import String, and_, cast, exists, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .models import EmailAddress, SuppressionEntry

KINDS = ("email", "domain", "company")
REASONS = ("opt_out", "objection", "bounce", "complaint", "manual", "legal")


def suppressed_expr(e=EmailAddress):
    """SQL boolean: true when any suppression entry applies to the e-mail row ``e`` (correlated EXISTS)."""
    s = SuppressionEntry
    return exists().where(
        or_(
            and_(s.kind == "email", s.value == e.email_normalised),
            and_(s.kind == "domain", s.value.in_([e.registrable_domain, e.domain])),
            and_(s.kind == "company", s.value == cast(e.organisation_id, String)),
        )
    )


def not_suppressed_conds(e=EmailAddress) -> list:
    return [e.suppressed_at.is_(None), e.marketing_status.notin_(("suppressed", "opted_out")), ~suppressed_expr(e)]


def normalise_value(kind: str, value: str) -> str:
    v = value.strip().lower()
    if kind == "company":
        if not v.isdigit():
            raise ValueError("company suppression value must be the numeric organisation id")
        return str(int(v))
    if kind == "domain":
        v = v.removeprefix("@")
    return v


def _where_for(kind: str, value: str):
    if kind == "email":
        return text("email_normalised = :v")
    if kind == "domain":
        return text("(registrable_domain = :v OR domain = :v)")
    return text("organisation_id = CAST(CAST(:v AS text) AS bigint)")


async def suppress(session: AsyncSession, *, kind: str, value: str, reason: str, note: str | None = None, user_id: int | None = None) -> tuple[int, int]:
    """Create (idempotently) the entry and flag every matching e-mail. Returns (entry_id, emails_flagged)."""
    value = normalise_value(kind, value)
    ins = pg_insert(SuppressionEntry).values(kind=kind, value=value, reason=reason, note=note, created_by=user_id).on_conflict_do_nothing(constraint="uq_suppression").returning(SuppressionEntry.id)
    entry_id = (await session.execute(ins)).scalar_one_or_none()
    if entry_id is None:
        entry_id = (await session.execute(select(SuppressionEntry.id).where(SuppressionEntry.kind == kind, SuppressionEntry.value == value))).scalar_one()
    status = "opted_out" if reason in ("opt_out", "objection") else "suppressed"
    res = await session.execute(
        text(f"UPDATE email_addresses SET suppressed_at = COALESCE(suppressed_at, now()), marketing_status = CASE WHEN marketing_status = 'opted_out' THEN 'opted_out' ELSE :st END WHERE {_where_for(kind, value)}"),
        {"v": value, "st": status},
    )
    return entry_id, res.rowcount or 0


async def unsuppress(session: AsyncSession, *, kind: str, value: str) -> tuple[bool, int]:
    """Remove the entry; matching e-mails are un-flagged unless another entry still applies. Returns (removed, emails_released)."""
    value = normalise_value(kind, value)
    res = await session.execute(text("DELETE FROM suppression_entries WHERE kind=:k AND value=:v"), {"k": kind, "v": value})
    removed = (res.rowcount or 0) > 0
    rel = await session.execute(
        text(
            f"""UPDATE email_addresses e SET suppressed_at = NULL,
                   marketing_status = CASE WHEN marketing_status IN ('opted_out','suppressed') THEN 'needs_review' ELSE marketing_status END
                WHERE {_where_for(kind, value)} AND e.suppressed_at IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM suppression_entries s WHERE
                        (s.kind='email' AND s.value=e.email_normalised) OR (s.kind='domain' AND s.value IN (e.registrable_domain, e.domain))
                        OR (s.kind='company' AND s.value = e.organisation_id::text))"""
        ),
        {"v": value},
    )
    return removed, rel.rowcount or 0
