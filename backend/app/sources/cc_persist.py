"""Persist contacts seen in Common Crawl archives as HISTORICAL, UNCONFIRMED evidence -- and confirm them against live crawls.

Rules (enforced here, not left to callers):

* Every evidence row is ``source_adapter='commoncrawl'``, ``historical=True``, ``archive_timestamp=<capture time>``,
  ``observed_at=<capture time>`` and ``source_reliability <= 0.3`` (:data:`HISTORICAL_RELIABILITY_CAP`).  The verification
  scorer multiplies by ``0.7 + 0.3 * reliability``, so archive-only evidence contributes the least weight.
* ``ContactEvidence.context`` carries ``{"historical": true, "unconfirmed": true, "archive_timestamp": ...}``.
* An e-mail that has ONLY historical evidence stays ``origin='published'`` and ``needs_review=True`` -- archive pages can
  never "corroborate" each other into a higher origin, and contacts created from archive-only names have capped confidence.
* :func:`confirm_against_live` clears ``unconfirmed`` ONLY for e-mails that a *current live crawl* observation (a source other
  than ``commoncrawl``) actually contains; the historical evidence row itself stays historical.

The heavy lifting reuses :func:`app.pipeline.ingest.persist_observations` (read, not modified).
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from ..crawler.types import EmailObservation
from ..models import Contact, ContactEvidence, EmailAddress, EvidenceSource, OrganisationDomain
from ..pipeline.ingest import normalise_email, persist_observations

ADAPTER = "commoncrawl"
LICENCE = "Common Crawl Terms of Use (archived content remains the property of its owners)"
HISTORICAL_RELIABILITY_CAP = 0.3
HISTORICAL_CONTACT_CONFIDENCE_CAP = 0.4


@dataclass
class HistoricalResult:
    email_id: int
    email: str
    created: bool
    unconfirmed: bool = True


def _merge_context(patch: dict[str, Any]):
    return func.coalesce(ContactEvidence.context, literal({}, JSONB)).op("||")(literal(patch, JSONB))


async def _has_live_evidence(session: AsyncSession, email_id: int) -> bool:
    q = (select(func.count()).select_from(ContactEvidence)
         .join(EvidenceSource, EvidenceSource.id == ContactEvidence.evidence_id)
         .where(ContactEvidence.email_id == email_id, EvidenceSource.historical.is_(False)))
    return (await session.execute(q)).scalar_one() > 0


async def persist_historical_observations(
    session: AsyncSession, org_id: int, observations: Iterable[EmailObservation], *, archive_timestamp: datetime,
    crawl_id: str | None = None, run_id: int | None = None,
) -> list[HistoricalResult]:
    """Write archived observations as historical/unconfirmed evidence.  ``archive_timestamp`` is the capture time."""
    obs = list(observations)
    if not obs:
        return []
    if archive_timestamp.tzinfo is None:
        archive_timestamp = archive_timestamp.replace(tzinfo=UTC)
    results = await persist_observations(
        session, org_id, None, obs, run_id=run_id, source_adapter=ADAPTER, licence=LICENCE, now=datetime.now(UTC),
    )
    urls = sorted({o.page_url for o in obs})
    ev_ids = list((await session.execute(
        select(EvidenceSource.id).where(EvidenceSource.source_adapter == ADAPTER, EvidenceSource.source_url.in_(urls))
    )).scalars())
    if ev_ids:
        await session.execute(
            update(EvidenceSource).where(EvidenceSource.id.in_(ev_ids)).values(
                historical=True, archive_timestamp=archive_timestamp, observed_at=archive_timestamp,
                source_reliability=func.least(EvidenceSource.source_reliability, HISTORICAL_RELIABILITY_CAP),
            )
        )
        patch = {"historical": True, "unconfirmed": True, "archive_timestamp": archive_timestamp.isoformat()}
        if crawl_id:
            patch["crawl_id"] = crawl_id
        email_ids = [e for e, _ in results]
        await session.execute(
            update(ContactEvidence)
            .where(ContactEvidence.evidence_id.in_(ev_ids), ContactEvidence.email_id.in_(email_ids))
            .values(context=_merge_context(patch), observed_at=archive_timestamp)
        )
    out: list[HistoricalResult] = []
    for email_id, created in results:
        em = (await session.execute(select(EmailAddress).where(EmailAddress.id == email_id))).scalar_one()
        live = await _has_live_evidence(session, email_id)
        if not live:
            await session.execute(update(EmailAddress).where(EmailAddress.id == email_id).values(origin="published", needs_review=True))
            if em.contact_id is not None:
                await session.execute(
                    update(Contact).where(Contact.id == em.contact_id).values(
                        identity_confidence=func.least(Contact.identity_confidence, HISTORICAL_CONTACT_CONFIDENCE_CAP))
                )
        out.append(HistoricalResult(email_id, em.email_normalised, created, unconfirmed=not live))
    await session.flush()
    return out


async def _own_registrables(session: AsyncSession, org_id: int, domain_row: OrganisationDomain | None) -> set[str]:
    own: set[str] = set()
    if domain_row is not None and domain_row.registrable_domain:
        own.add(domain_row.registrable_domain)
    rows = (await session.execute(select(OrganisationDomain).where(OrganisationDomain.organisation_id == org_id))).scalars().all()
    for r in rows:
        if r.manual_override or r.verified_match_status in ("confirmed", "high"):
            own.add(r.registrable_domain)
    return own


async def confirm_against_live(
    session: AsyncSession, org_id: int, domain_row: OrganisationDomain | None, live_observations: Iterable[EmailObservation],
    *, run_id: int | None = None, now: datetime | None = None, source_adapter: str = "website_crawl",
) -> list[int]:
    """Persist ``live_observations`` (a CURRENT crawl) and clear ``unconfirmed`` on matching historical evidence.

    Returns the ids of e-mails whose historical evidence was confirmed.  E-mails that were not seen live stay unconfirmed.
    """
    if source_adapter == ADAPTER:
        raise ValueError("live confirmation must come from a live crawl, not from Common Crawl")
    obs = list(live_observations)
    if not obs:
        return []
    now = now or datetime.now(UTC)
    results = await persist_observations(
        session, org_id, domain_row, obs, run_id=run_id, source_adapter=source_adapter, now=now)
    live_ids = {e for e, _ in results}
    if not live_ids:
        return []
    # which of them have unconfirmed historical evidence?
    rows = (await session.execute(
        select(ContactEvidence.email_id).join(EvidenceSource, EvidenceSource.id == ContactEvidence.evidence_id)
        .where(ContactEvidence.email_id.in_(live_ids), EvidenceSource.historical.is_(True),
               ContactEvidence.context["unconfirmed"].astext == "true").distinct()
    )).scalars().all()
    confirmed = sorted(set(rows))
    if not confirmed:
        return []
    hist_ev = select(EvidenceSource.id).where(EvidenceSource.historical.is_(True))
    await session.execute(
        update(ContactEvidence)
        .where(ContactEvidence.email_id.in_(confirmed), ContactEvidence.evidence_id.in_(hist_ev))
        .values(context=_merge_context({"unconfirmed": False, "confirmed_live_at": now.isoformat()}))
    )
    own = await _own_registrables(session, org_id, domain_row)
    for email_id in confirmed:
        em = (await session.execute(select(EmailAddress).where(EmailAddress.id == email_id))).scalar_one()
        if em.registrable_domain in own:
            await session.execute(update(EmailAddress).where(EmailAddress.id == email_id).values(needs_review=False))
    await session.flush()
    return confirmed


async def unconfirmed_emails(session: AsyncSession, org_id: int) -> list[str]:
    """Normalised e-mails of the organisation that still rest on unconfirmed historical evidence only."""
    q = (select(EmailAddress.email_normalised).join(ContactEvidence, ContactEvidence.email_id == EmailAddress.id)
         .where(EmailAddress.organisation_id == org_id, ContactEvidence.context["unconfirmed"].astext == "true").distinct())
    return sorted((await session.execute(q)).scalars().all())


__all__ = [
    "persist_historical_observations", "confirm_against_live", "unconfirmed_emails", "HistoricalResult",
    "HISTORICAL_RELIABILITY_CAP", "normalise_email",
]
