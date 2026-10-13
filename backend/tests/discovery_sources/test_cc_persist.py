from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.crawler.types import EmailObservation
from app.models import ContactEvidence, EmailAddress, EvidenceSource, Organisation, OrganisationDomain
from app.sources.cc_persist import (
    HISTORICAL_RELIABILITY_CAP,
    confirm_against_live,
    persist_historical_observations,
    unconfirmed_emails,
)

T0 = datetime(2025, 3, 4, 10, 0, tzinfo=UTC)


async def make_org(session, domain="example-widgets.co.uk", confirmed=True):
    org = Organisation(legal_name="Example Ltd", country_code="GB", canonical_identity="GB:cn:99999991")
    session.add(org)
    await session.flush()
    dom = OrganisationDomain(organisation_id=org.id, domain=domain, registrable_domain=domain, website_url=f"https://{domain}/",
                             verified_match_status="confirmed" if confirmed else "unknown", confidence=0.9)
    session.add(dom)
    await session.flush()
    return org, dom


def ob(email, url="https://www.example-widgets.co.uk/contact", method="mailto", name=None, title=None):
    return EmailObservation(email=email, method=method, page_url=url, context="ctx", name=name, title=title)


async def test_historical_flags_and_reliability(session):
    org, dom = await make_org(session)
    res = await persist_historical_observations(
        session, org.id, [ob("sales@example-widgets.co.uk"), ob("jane.smithers@example-widgets.co.uk", name="Jane Smithers", title="Director", method="text")],
        archive_timestamp=T0, crawl_id="CC-MAIN-2025-10")
    assert {r.email for r in res} == {"sales@example-widgets.co.uk", "jane.smithers@example-widgets.co.uk"} and all(r.unconfirmed for r in res)
    evs = (await session.execute(select(EvidenceSource))).scalars().all()
    assert evs and all(e.source_adapter == "commoncrawl" and e.historical and e.archive_timestamp == T0 and e.observed_at == T0 for e in evs)
    assert all(e.source_reliability <= HISTORICAL_RELIABILITY_CAP for e in evs)
    emails = (await session.execute(select(EmailAddress))).scalars().all()
    assert all(e.origin == "published" and e.needs_review for e in emails)  # even on the organisation's own domain
    ces = (await session.execute(select(ContactEvidence))).scalars().all()
    assert ces and all(c.context["historical"] is True and c.context["unconfirmed"] is True and c.context["crawl_id"] == "CC-MAIN-2025-10" for c in ces)
    assert await unconfirmed_emails(session, org.id) == ["jane.smithers@example-widgets.co.uk", "sales@example-widgets.co.uk"]


async def test_two_archive_pages_do_not_corroborate(session):
    org, dom = await make_org(session)
    await persist_historical_observations(session, org.id, [
        ob("x@other-widgets.co.uk", url="https://www.example-widgets.co.uk/contact"),
        ob("x@other-widgets.co.uk", url="https://www.example-widgets.co.uk/about")], archive_timestamp=T0)
    e = (await session.execute(select(EmailAddress))).scalar_one()
    assert e.origin == "published" and e.needs_review  # would be 'corroborated' for two live pages


async def test_archive_only_contact_has_capped_confidence(session):
    from app.models import Contact

    org, _ = await make_org(session)
    await persist_historical_observations(session, org.id, [ob("jane.smithers@example-widgets.co.uk", name="Jane Smithers", title="Director", method="jsonld")], archive_timestamp=T0)
    c = (await session.execute(select(Contact))).scalar_one()
    assert c.identity_confidence <= 0.4


async def test_confirm_against_live_clears_only_matching(session):
    org, dom = await make_org(session)
    await persist_historical_observations(session, org.id, [ob("sales@example-widgets.co.uk"), ob("former.staff@example-widgets.co.uk")], archive_timestamp=T0)
    now = datetime(2026, 10, 1, tzinfo=UTC)
    confirmed = await confirm_against_live(session, org.id, dom, [ob("sales@example-widgets.co.uk", url="https://www.example-widgets.co.uk/contact")], now=now)
    assert len(confirmed) == 1
    assert await unconfirmed_emails(session, org.id) == ["former.staff@example-widgets.co.uk"]
    e_sales = (await session.execute(select(EmailAddress).where(EmailAddress.email_normalised == "sales@example-widgets.co.uk"))).scalar_one()
    e_old = (await session.execute(select(EmailAddress).where(EmailAddress.email_normalised == "former.staff@example-widgets.co.uk"))).scalar_one()
    assert e_sales.id == confirmed[0] and not e_sales.needs_review and e_old.needs_review
    ces = (await session.execute(select(ContactEvidence).where(ContactEvidence.email_id == e_sales.id))).scalars().all()
    hist = [c for c in ces if c.context.get("historical")]
    live = [c for c in ces if not c.context.get("historical")]
    assert hist and all(c.context["unconfirmed"] is False and c.context["confirmed_live_at"] for c in hist) and live
    evs = (await session.execute(select(EvidenceSource).where(EvidenceSource.source_adapter == "commoncrawl"))).scalars().all()
    assert all(e.historical for e in evs)  # the archive evidence stays historical, only the unconfirmed flag clears


async def test_confirm_requires_live_source_and_same_email(session):
    org, dom = await make_org(session)
    await persist_historical_observations(session, org.id, [ob("sales@example-widgets.co.uk")], archive_timestamp=T0)
    with pytest.raises(ValueError):
        await confirm_against_live(session, org.id, dom, [ob("sales@example-widgets.co.uk")], source_adapter="commoncrawl")
    assert await confirm_against_live(session, org.id, dom, [ob("other@example-widgets.co.uk")]) == []
    assert await confirm_against_live(session, org.id, dom, []) == []
    assert await unconfirmed_emails(session, org.id) == ["sales@example-widgets.co.uk"]


async def test_repersisting_archive_does_not_reset_live_confirmation_or_raise_reliability(session):
    org, dom = await make_org(session)
    await persist_historical_observations(session, org.id, [ob("sales@example-widgets.co.uk")], archive_timestamp=T0)
    await confirm_against_live(session, org.id, dom, [ob("sales@example-widgets.co.uk")])
    res = await persist_historical_observations(session, org.id, [ob("sales@example-widgets.co.uk")], archive_timestamp=T0)
    e = (await session.execute(select(EmailAddress))).scalar_one()
    assert not res[0].unconfirmed and not e.needs_review  # live evidence exists, so the address is not downgraded
    evs = (await session.execute(select(EvidenceSource).where(EvidenceSource.historical.is_(True)))).scalars().all()
    assert all(x.source_reliability <= HISTORICAL_RELIABILITY_CAP for x in evs)
