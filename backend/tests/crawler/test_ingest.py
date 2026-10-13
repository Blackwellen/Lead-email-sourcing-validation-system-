from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.crawler.types import EmailObservation as Obs
from app.models import (
    Contact,
    ContactEvidence,
    EmailAddress,
    EvidenceSource,
    Organisation,
    OrganisationDomain,
    SourcingRun,
)
from app.pipeline.ingest import (
    classify_role,
    is_free_provider,
    normalise_email,
    parse_name,
    persist_observations,
)

P = "https://acme.co.uk/team"


async def setup_org(session, cn="1", domain="acme.co.uk", status="confirmed"):
    org = Organisation(legal_name="Acme Ltd", country_code="GB", canonical_identity=f"gb:{cn}")
    session.add(org)
    await session.flush()
    dom = OrganisationDomain(organisation_id=org.id, domain=domain, registrable_domain=domain, verified_match_status=status, confidence=0.9)
    session.add(dom)
    await session.flush()
    return org, dom


async def counts(session):
    return {m.__tablename__: (await session.execute(select(func.count()).select_from(m))).scalar_one()
            for m in (EmailAddress, Contact, EvidenceSource, ContactEvidence)}


# ---------------------------------------------------------------- parse_name
@pytest.mark.parametrize("raw,given,family,prefix,suffix", [
    ("John Smith", "John", "Smith", None, None),
    ("Dr. John A. Smith Jr.", "John A.", "Smith", "Dr", "Jr."),
    ("Smith, John", "John", "Smith", None, None),
    ("Ludwig van Beethoven", "Ludwig", "van Beethoven", None, None),
    ("Prof Jean de la Fontaine", "Jean", "de la Fontaine", "Prof", None),
    ("Anne-Marie O'Neil", "Anne-Marie", "O'Neil", None, None),
    ("María José García", "María José", "García", None, None),
    ("Mary Jane Watson MRICS", "Mary Jane", "Watson", None, "MRICS"),
    ("Mr Sean McDonald", "Sean", "McDonald", "Mr", None),
    ("Jane Smith-Jones", "Jane", "Smith-Jones", None, None),
    ("Jane Doe (Sales)", "Jane", "Doe", None, None),
    ("  Ana   de Souza  ", "Ana", "de Souza", None, None),
    ("Müller, Jürgen", "Jürgen", "Müller", None, None),
])
def test_parse_name(raw, given, family, prefix, suffix):
    p = parse_name(raw)
    assert p is not None, raw
    assert (p.given_name, p.family_name, p.prefix, p.suffix) == (given, family, prefix, suffix)
    assert p.full_name and p.identity_key.startswith("name:")


@pytest.mark.parametrize("raw", [None, "", "John", "Sales Team", "Contact Us", "Mr", "Acme Ltd", "info@acme.co.uk", "Managing Director", "12345"])
def test_parse_name_rejects(raw):
    assert parse_name(raw) is None


def test_parse_name_identity_key_is_stable_across_formats():
    a, b, c = parse_name("Dr. John Smith"), parse_name("Smith, John"), parse_name("JOHN  SMITH Jr")
    assert a.identity_key == b.identity_key == c.identity_key
    assert parse_name("José Núñez").identity_key == parse_name("Jose Nunez").identity_key
    assert parse_name("John Smith").full_name == "John Smith"  # original preserved


def test_role_and_free_provider():
    assert classify_role("info") == "generic" and classify_role("Sales") == "sales" and classify_role("no-reply") in ("no_reply", "noreply", "no-reply")
    assert classify_role("john.smith") is None
    assert is_free_provider("gmail.com") and is_free_provider("BTinternet.com") and not is_free_provider("acme.co.uk")


def test_normalise_email():
    assert normalise_email("Info@ACME.co.UK") == "info@acme.co.uk"
    assert normalise_email("kontakt@bücher.de") == "kontakt@xn--bcher-kva.de"
    assert normalise_email("garbage") is None and normalise_email("a@example.com") is None


# ---------------------------------------------------------------- persistence
async def test_persist_creates_emails_contacts_evidence_with_provenance(session):
    org, dom = await setup_org(session)
    run = SourcingRun(name="r1")
    session.add(run)
    await session.flush()
    obs = [
        Obs("John.Smith@Acme.co.uk", "mailto", P, context="John Smith Director", name="Dr. John Smith", title="Director", phone="+44 20 7946 0001",
            also_methods=("text",)),
        Obs("info@acme.co.uk", "text", "https://acme.co.uk/contact", context="Email info@acme.co.uk", department="Sales"),
        Obs("INFO@acme.co.uk", "mailto", "https://acme.co.uk/about"),
        Obs("not-an-email", "text", P),
        Obs("logo@2x.png", "text", P),
    ]
    out = await persist_observations(session, org.id, dom, obs, run_id=run.id, licence="public web")
    await session.commit()
    assert [c for _, c in out] == [True, True] and len({i for i, _ in out}) == 2
    emails = {e.email_normalised: e for e in (await session.execute(select(EmailAddress))).scalars()}
    assert set(emails) == {"john.smith@acme.co.uk", "info@acme.co.uk"}
    j, i = emails["john.smith@acme.co.uk"], emails["info@acme.co.uk"]
    assert j.email_original == "John.Smith@Acme.co.uk" and j.local_part == "john.smith" and j.domain == "acme.co.uk"
    assert j.registrable_domain == "acme.co.uk" and j.origin == "published" and not j.needs_review and not j.is_free_provider
    assert j.source_type == "mailto" and j.role_type is None and j.first_seen_at and j.last_seen_at
    assert i.role_type == "generic" and i.origin == "published" and i.contact_id is None  # role mailbox: no contact
    # contact only where a name is published next to the address
    c = (await session.execute(select(Contact))).scalar_one()
    assert (c.full_name, c.given_name, c.family_name, c.job_title) == ("Dr John Smith", "John", "Smith", "Director")
    assert j.contact_id == c.id and c.organisation_id == org.id and c.identity_key == "name:john smith"
    # evidence rows per (page, method); ContactEvidence carries run + context
    ev = (await session.execute(select(EvidenceSource))).scalars().all()
    assert {(e.source_url, e.evidence_type) for e in ev} == {
        (P, "mailto"), (P, "text"), ("https://acme.co.uk/contact", "text"), ("https://acme.co.uk/about", "mailto")}
    assert all(e.source_adapter == "website_crawl" and e.licence == "public web" and e.content_hash and e.parser_version for e in ev)
    assert all(0.5 <= e.source_reliability <= 0.95 for e in ev)
    ces = (await session.execute(select(ContactEvidence).where(ContactEvidence.email_id == j.id))).scalars().all()
    assert {ce.extraction_method for ce in ces} == {"mailto", "text"} and all(ce.run_id == run.id and ce.organisation_id == org.id for ce in ces)
    mailto_ce = next(ce for ce in ces if ce.extraction_method == "mailto")
    assert mailto_ce.context["name"] == "Dr. John Smith" and mailto_ce.context["phone"] == "+44 20 7946 0001" and mailto_ce.context["page_url"] == P
    assert mailto_ce.contact_id == c.id
    await session.refresh(dom)
    assert dom.last_crawled_at is not None


async def test_persist_is_idempotent_and_refreshes_last_seen(session):
    org, dom = await setup_org(session)
    obs = [Obs("a@acme.co.uk", "mailto", P, name="Ann Lee", title="Partner"), Obs("b@acme.co.uk", "text", P)]
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    first = await persist_observations(session, org.id, dom, obs, run_id=None, now=t0)
    c1 = await counts(session)
    second = await persist_observations(session, org.id, dom, obs, run_id=None, now=t0 + timedelta(days=30))
    c2 = await counts(session)
    assert c1 == c2 == {"email_addresses": 2, "contacts": 1, "evidence_sources": 2, "contact_evidence": 2}
    assert [i for i, _ in first] == [i for i, _ in second] and all(c for _, c in first) and not any(c for _, c in second)
    rows = (await session.execute(select(EmailAddress).order_by(EmailAddress.id))).scalars().all()
    assert all(r.first_seen_at == t0 and r.last_seen_at == t0 + timedelta(days=30) for r in rows)
    ev = (await session.execute(select(EvidenceSource))).scalars().all()
    assert all(e.observed_at == t0 + timedelta(days=30) for e in ev)


async def test_changed_page_adds_evidence_without_duplicating_email_or_contact(session):
    org, dom = await setup_org(session)
    await persist_observations(session, org.id, dom, [Obs("a@acme.co.uk", "mailto", P)])
    # later the page also shows the person's name and title
    await persist_observations(session, org.id, dom, [Obs("a@acme.co.uk", "mailto", P, name="Ann Lee", title="Partner")])
    await persist_observations(session, org.id, dom, [Obs("a@acme.co.uk", "mailto", P, name="Ann Lee", title="Partner")])
    c = await counts(session)
    assert c["email_addresses"] == 1 and c["contacts"] == 1 and c["evidence_sources"] == 2 and c["contact_evidence"] == 2
    e = (await session.execute(select(EmailAddress))).scalar_one()
    ct = (await session.execute(select(Contact))).scalar_one()
    assert e.contact_id == ct.id and ct.job_title == "Partner"


async def test_same_person_two_addresses_one_contact(session):
    org, dom = await setup_org(session)
    await persist_observations(session, org.id, dom, [
        Obs("ann@acme.co.uk", "mailto", P, name="Ann Lee"), Obs("ann.lee@acme.co.uk", "vcard", "https://acme.co.uk/ann.vcf", name="Lee, Ann", title="Director")])
    c = await counts(session)
    assert c["contacts"] == 1 and c["email_addresses"] == 2
    ct = (await session.execute(select(Contact))).scalar_one()
    assert ct.job_title == "Director" and ct.identity_confidence >= 0.55


async def test_origin_rules_and_source_reliability(session):
    org, dom = await setup_org(session)
    obs = [
        Obs("own@acme.co.uk", "mailto", P),
        Obs("owner.gmail@gmail.com", "text", P),  # free provider on the organisation's own page
        Obs("agency@other-agency.com", "text", P),  # third-party domain, single source
        Obs("partner@partner-co.com", "text", P), Obs("partner@partner-co.com", "mailto", "https://acme.co.uk/partners"),  # two pages
    ]
    await persist_observations(session, org.id, dom, obs)
    e = {x.email_normalised: x for x in (await session.execute(select(EmailAddress))).scalars()}
    assert (e["own@acme.co.uk"].origin, e["own@acme.co.uk"].needs_review) == ("published", False)
    assert (e["owner.gmail@gmail.com"].origin, e["owner.gmail@gmail.com"].is_free_provider, e["owner.gmail@gmail.com"].needs_review) == ("published", True, False)
    assert (e["agency@other-agency.com"].origin, e["agency@other-agency.com"].needs_review) == ("published", True)
    assert (e["partner@partner-co.com"].origin, e["partner@partner-co.com"].needs_review) == ("corroborated", False)
    ev = {(x.source_url, x.evidence_type): x for x in (await session.execute(select(EvidenceSource))).scalars()}
    # group mixing own + third-party addresses is less reliable than a pure own-domain group
    own_only = (await session.execute(select(EvidenceSource.source_reliability).join(ContactEvidence, ContactEvidence.evidence_id == EvidenceSource.id)
                                      .where(ContactEvidence.email_id == e["own@acme.co.uk"].id, EvidenceSource.evidence_type == "mailto"))).scalar_one()
    assert own_only > ev[(P, "text")].source_reliability


async def test_corroboration_across_later_crawl_upgrades_origin_never_downgrades(session):
    org, dom = await setup_org(session)
    o1 = Obs("x@partner-co.com", "text", P)
    await persist_observations(session, org.id, dom, [o1])
    e = (await session.execute(select(EmailAddress))).scalar_one()
    assert e.origin == "published" and e.needs_review
    await persist_observations(session, org.id, dom, [Obs("x@partner-co.com", "mailto", "https://acme.co.uk/other")])
    await session.refresh(e)
    assert e.origin == "corroborated" and not e.needs_review
    await persist_observations(session, org.id, dom, [o1])
    await session.refresh(e)
    assert e.origin == "corroborated"


async def test_email_belongs_to_first_org_but_second_org_gets_evidence(session):
    org1, dom1 = await setup_org(session, cn="1")
    org2, dom2 = await setup_org(session, cn="2", domain="sister.co.uk")
    shared = Obs("shared@acme.co.uk", "text", P)
    (id1, c1), = await persist_observations(session, org1.id, dom1, [shared])
    (id2, c2), = await persist_observations(session, org2.id, dom2, [Obs("shared@acme.co.uk", "text", "https://sister.co.uk/c")])
    assert id1 == id2 and c1 and not c2
    e = (await session.execute(select(EmailAddress))).scalar_one()
    assert e.organisation_id == org1.id
    orgs = {ce.organisation_id for ce in (await session.execute(select(ContactEvidence))).scalars()}
    assert orgs == {org1.id, org2.id}


async def test_unicode_and_placeholder_handling(session):
    org, dom = await setup_org(session)
    out = await persist_observations(session, org.id, dom, [Obs("kontakt@bücher.de", "text", P), Obs("x@example.com", "text", P), Obs("", "text", P)])
    assert len(out) == 1
    e = (await session.execute(select(EmailAddress))).scalar_one()
    assert e.email_normalised == "kontakt@xn--bcher-kva.de" and e.email_original == "kontakt@bücher.de"


async def test_empty_input_and_missing_domain_row(session):
    org, dom = await setup_org(session)
    assert await persist_observations(session, org.id, dom, []) == []
    out = await persist_observations(session, org.id, None, [Obs("a@acme.co.uk", "text", P)])
    # without a domain row the verified org domains still define "own": acme.co.uk is confirmed
    assert len(out) == 1 and (await session.execute(select(EmailAddress))).scalar_one().origin == "published"


async def test_no_contact_created_from_local_part_or_junk_names(session):
    org, dom = await setup_org(session)
    await persist_observations(session, org.id, dom, [
        Obs("john.smith@acme.co.uk", "mailto", P),  # no name published
        Obs("jane.doe2@acme.co.uk", "mailto", P, name="Sales Team"),
        Obs("sales@acme.co.uk", "mailto", P, name="Bob Jones"),  # role mailbox
    ])
    assert (await counts(session))["contacts"] == 0


async def test_pdf_evidence_records_document_name(session):
    org, dom = await setup_org(session)
    await persist_observations(session, org.id, dom, [Obs("p@acme.co.uk", "pdf", "https://acme.co.uk/files/brochure.pdf")])
    ev = (await session.execute(select(EvidenceSource))).scalar_one()
    assert ev.evidence_type == "pdf" and ev.source_document == "brochure.pdf"
