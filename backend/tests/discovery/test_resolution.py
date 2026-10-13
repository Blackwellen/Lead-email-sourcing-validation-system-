from __future__ import annotations

import pytest
from sqlalchemy import select

from app.discovery.resolution import (
    MergeConflict,
    find_duplicate_candidates,
    merge_organisations,
    resolve_record,
    resolve_review,
    resolving_upsert,
    resolving_upsert_ex,
    split_organisation,
    unlink_record,
)
from app.models import (
    BusinessRelationship,
    BusinessSignal,
    Contact,
    EmailAddress,
    Organisation,
    OrganisationCompleteness,
    OrganisationDomain,
    OrganisationRecordLink,
    RunOrganisation,
    SourcingRun,
    WebsiteSnapshot,
)
from app.sources.base import BusinessRecord


def rec(ref, name, cn=None, pc=None, city=None, adapter="csv_import", **kw):
    return BusinessRecord(source_ref=ref, legal_name=name, company_number=cn, country_code="GB", postal_code=pc, city=city,
                          adapter_id=adapter, **kw)


async def orgs(session):
    return list((await session.execute(select(Organisation).order_by(Organisation.id))).scalars())


async def test_same_company_number_matches(session):
    a, created = await resolving_upsert(session, rec("1", "Acme Plumbing Ltd", "00000006", "SW1A 1AA"), "companies_house_bulk")
    assert created
    r = rec("X9", "ACME PLUMBING LIMITED (trading)", "00000006", "EC1A 1BB")
    res = await resolve_record(session, r, adapter_id="csv_import", source_ref="X9")
    assert res.decision == "matched" and res.org_id == a and res.basis == "identifier:company_number"
    b, created = await resolving_upsert(session, r, "csv_import")
    assert b == a and not created
    assert len(await orgs(session)) == 1


async def test_same_name_different_number_is_rejected(session):
    a, _ = await resolving_upsert(session, rec("1", "Acme Plumbing Ltd", "00000006", "SW1A 1AA"), "companies_house_bulk")
    r = rec("2", "Acme Plumbing Ltd", "00000007", "SW1A 1AA")
    res = await resolve_record(session, r, adapter_id="csv_import", source_ref="2")
    assert res.decision == "rejected" and res.org_id is None and res.detail["conflict"]["type"] == "company_number"
    b, created, _ = await resolving_upsert_ex(session, r, "csv_import")
    assert created and b != a
    link = (await session.execute(select(OrganisationRecordLink).where(OrganisationRecordLink.status == "rejected"))).scalar_one()
    assert link.organisation_id == a and link.candidate_org_id == b


async def test_similar_names_never_merged_go_to_review(session):
    a, _ = await resolving_upsert(session, rec("1", "Acme Plumbing Ltd", None, "SW1A 1AA"), "csv_import")
    r = rec("2", "Acme Plumbers Ltd", None, "M1 1AA")
    res = await resolve_record(session, r, adapter_id="osm_geofabrik", source_ref="2")
    assert res.decision == "review" and res.candidates[0]["org_id"] == a
    b, created, _ = await resolving_upsert_ex(session, r, "osm_geofabrik")
    assert created and b != a
    q = await find_duplicate_candidates(session, limit=10)
    assert len(q["items"]) == 1
    item = q["items"][0]
    assert item["organisation"]["id"] == a and item["candidate"]["id"] == b and item["basis"] == "name_similar"
    # idempotent: second pass resolves to the same org and creates nothing new
    again, created, _ = await resolving_upsert_ex(session, r, "osm_geofabrik")
    assert again == b and not created
    assert len((await find_duplicate_candidates(session))["items"]) == 1


async def test_unrelated_names_created(session):
    await resolving_upsert(session, rec("1", "Acme Plumbing Ltd", None, "SW1A 1AA"), "csv_import")
    res = await resolve_record(session, rec("2", "Zenith Bakery Ltd", None, "SW1A 1AA"), adapter_id="x", source_ref="2")
    assert res.decision == "created" and res.org_id is None


async def test_exact_name_and_postcode_matches_but_other_place_is_review(session):
    a, _ = await resolving_upsert(session, rec("1", "Smith and Sons", "00000011", "SW1A 1AA", "London"), "companies_house_bulk")
    res = await resolve_record(session, rec("2", "Smith & Sons Limited", None, "sw1a1aa", "London"), adapter_id="osm", source_ref="2")
    assert res.decision == "matched" and res.org_id == a and res.basis == "name+postcode" and res.confidence >= 0.9
    res = await resolve_record(session, rec("3", "Smith & Sons Limited", None, "M1 1AA", "Manchester"), adapter_id="osm", source_ref="3")
    assert res.decision == "review" and res.basis == "name_only_location_differs"
    res = await resolve_record(session, rec("4", "Smith & Sons", None, None, "London"), adapter_id="osm", source_ref="4")
    assert res.decision == "matched" and res.basis == "name+locality"


async def test_domain_plus_name_matches(session):
    a, _ = await resolving_upsert(session, rec("1", "Bright Dental Care Ltd", "00000022", "SW1A 1AA"), "companies_house_bulk")
    session.add(OrganisationDomain(organisation_id=a, domain="brightdental.example.com", registrable_domain="example.com",
                                   verified_match_status="confirmed", confidence=0.95))
    await session.flush()
    r = rec("9", "Bright Dental", None, "L1 1AA", website="https://www.brightdental.example.com/contact")
    r.trading_name = "Bright Dental Care"
    res = await resolve_record(session, r, adapter_id="yell_like", source_ref="9")
    # registrable of example.com is shared by many tenants in tests; domain match + compatible name -> matched
    assert res.decision == "matched" and res.org_id == a and res.basis == "official_domain+name"
    # incompatible name on the same domain is NOT matched
    r2 = rec("10", "Totally Different Holdings", None, None, website="https://brightdental.example.com")
    res2 = await resolve_record(session, r2, adapter_id="yell_like", source_ref="10")
    assert res2.decision == "created"
    # conflicting numbers on the same domain: rejected
    r3 = rec("11", "Bright Dental Care", "00000099", None, website="https://brightdental.example.com")
    res3 = await resolve_record(session, r3, adapter_id="yell_like", source_ref="11")
    assert res3.decision == "rejected"


async def test_lei_match_and_conflict(session):
    a, _ = await resolving_upsert(session, rec("1", "Globex Corp Ltd", "00000033", raw={"lei": "5493001KJTIIGC8Y1R12"}), "gleif")
    r = rec("L1", "Globex Corporation", None, raw={"lei": "5493001KJTIIGC8Y1R12"})
    res = await resolve_record(session, r, adapter_id="gleif2", source_ref="L1")
    assert res.decision == "matched" and res.org_id == a and res.basis == "identifier:lei"
    r2 = rec("L2", "Globex Corporation", "00000034", raw={"lei": "5493001KJTIIGC8Y1R12"})
    res = await resolve_record(session, r2, adapter_id="gleif2", source_ref="L2")
    assert res.decision == "rejected"


async def test_branch_brand_kept_separate_and_linked(session):
    parent, _ = await resolving_upsert(session, rec("1", "Northern Boilers Ltd", "00000044", "LS1 1AA", "Leeds"), "companies_house_bulk")
    branch = rec("B1", "Northern Boilers Ltd - Sheffield", "00000044", "S1 1AA", "Sheffield",
                 raw={"entity_role": "branch", "parent_company_number": "00000044"})
    b, created, res = await resolving_upsert_ex(session, branch, "osm")
    assert created and b != parent and res.decision == "created"
    brand = rec("BR1", None, None, "LS1 1AA", "Leeds", raw={"entity_role": "brand", "parent_company_number": "00000044"})
    brand.trading_name = "BoilerHero"
    br, created, _ = await resolving_upsert_ex(session, brand, "directory")
    assert created and br not in (parent, b)
    rels = {(r.from_org_id, r.to_org_id, r.relation) for r in (await session.execute(select(BusinessRelationship))).scalars()}
    assert rels == {(b, parent, "branch_of"), (br, parent, "brand_of")}
    # the parent keeps its own company number; branch rows do not claim it
    assert (await session.get(Organisation, b)).company_number is None
    # re-running changes nothing
    b2, created, _ = await resolving_upsert_ex(session, branch, "osm")
    assert b2 == b and not created
    assert len(await orgs(session)) == 3


async def test_unlink_prevents_future_matching(session):
    a, _ = await resolving_upsert(session, rec("1", "Acme Plumbing Ltd", None, "SW1A 1AA"), "csv_import")
    r = rec("2", "Acme Plumbers Ltd", None, "M1 1AA")
    await resolving_upsert(session, r, "osm")
    link = (await session.execute(select(OrganisationRecordLink).where(OrganisationRecordLink.status == "review"))).scalar_one()
    out = await resolve_review(session, link.id, accept=False, user_id=None)
    assert out["status"] == "rejected"
    assert (await find_duplicate_candidates(session))["items"] == []
    r3 = rec("3", "Acme Plumbers Ltd", None, "M1 1AA")
    res = await resolve_record(session, r3, adapter_id="osm", source_ref="2")  # same source key -> previous link
    assert res.decision == "matched"  # previously created own org
    assert await unlink_record(session, org_id=a, source_adapter="osm", source_ref="2", user_id=None)


async def _populate(session):
    k, _ = await resolving_upsert(session, rec("1", "Keep Co Ltd", "00000100", "SW1A 1AA"), "companies_house_bulk")
    d, _ = await resolving_upsert(session, rec("2", "Keep Company", None, "M1 1AA", website="https://keep.example.com"), "osm")
    # dependent rows on both
    run = SourcingRun(name="r")
    session.add(run)
    await session.flush()
    for oid in (k, d):
        session.add(RunOrganisation(run_id=run.id, organisation_id=oid))
    c_keep = Contact(organisation_id=k, full_name="Jane Doe", identity_key="name:jane doe")
    c_drop = Contact(organisation_id=d, full_name="Jane Doe", identity_key="name:jane doe", job_title="Director")
    c_only = Contact(organisation_id=d, full_name="Bob Roe", identity_key="name:bob roe")
    session.add_all([c_keep, c_drop, c_only])
    await session.flush()
    emails = [
        EmailAddress(organisation_id=d, contact_id=c_drop.id, email_original="jane@keep.example.com", email_normalised="jane@keep.example.com",
                     local_part="jane", domain="keep.example.com", registrable_domain="example.com", source_type="mailto"),
        EmailAddress(organisation_id=d, contact_id=c_only.id, email_original="bob@keep.example.com", email_normalised="bob@keep.example.com",
                     local_part="bob", domain="keep.example.com", registrable_domain="example.com", source_type="mailto"),
    ]
    session.add_all(emails)
    # same domain row on both (conflict) + a unique one
    kd = OrganisationDomain(organisation_id=k, domain="keep.example.com", registrable_domain="example.com", verified_match_status="unknown")
    session.add(kd)
    await session.flush()
    dd = (await session.execute(select(OrganisationDomain).where(OrganisationDomain.organisation_id == d))).scalar_one()
    dd.verified_match_status, dd.confidence = "confirmed", 0.9
    session.add(WebsiteSnapshot(domain_id=dd.id, url="https://keep.example.com/c", page_kind="contact", content_hash="h"))
    session.add_all([
        BusinessSignal(organisation_id=k, signal_type="hiring", title="t", source_adapter="x", source_url="http://x", dedupe_hash="same"),
        BusinessSignal(organisation_id=d, signal_type="hiring", title="t", source_adapter="x", source_url="http://x", dedupe_hash="same"),
        BusinessSignal(organisation_id=d, signal_type="event", title="e", source_adapter="x", source_url="http://x", dedupe_hash="other"),
    ])
    other, _ = await resolving_upsert(session, rec("3", "Other Ltd", "00000300"), "x")
    session.add_all([
        BusinessRelationship(from_org_id=d, to_org_id=other, relation="partner_of", source_adapter="a"),
        BusinessRelationship(from_org_id=k, to_org_id=other, relation="partner_of", source_adapter="a"),
        BusinessRelationship(from_org_id=d, to_org_id=k, relation="same_group", source_adapter="a"),
        BusinessRelationship(from_org_id=other, to_org_id=d, relation="supplier_of", source_adapter="a"),
    ])
    session.add(OrganisationCompleteness(organisation_id=d, published_email="yes"))
    session.add(OrganisationCompleteness(organisation_id=k, published_email="unknown"))
    await session.flush()
    return k, d, other, run.id


async def test_merge_organisations_integrity_and_split(session):
    k, d, other, run_id = await _populate(session)
    out = await merge_organisations(session, k, d, None, "dupe")
    assert out["keep_id"] == k and out["audit_id"]
    drop = await session.get(Organisation, d)
    assert drop.merged_into_id == k
    # nothing left pointing at the dropped org
    for table, col in [("contacts", "organisation_id"), ("email_addresses", "organisation_id"), ("organisation_domains", "organisation_id"),
                       ("business_signals", "organisation_id"), ("run_organisations", "organisation_id"),
                       ("organisation_sources", "organisation_id"), ("organisation_completeness", "organisation_id")]:
        n = (await session.execute(__import__("sqlalchemy").text(f"select count(*) from {table} where {col}={d}"))).scalar_one()
        assert n == 0, table
    contacts = (await session.execute(select(Contact).where(Contact.organisation_id == k))).scalars().all()
    assert sorted(c.full_name for c in contacts) == ["Bob Roe", "Jane Doe"]
    jane = next(c for c in contacts if c.full_name == "Jane Doe")
    assert jane.job_title == "Director"
    em = (await session.execute(select(EmailAddress).order_by(EmailAddress.id))).scalars().all()
    assert {e.organisation_id for e in em} == {k}
    assert {e.contact_id for e in em} == {c.id for c in contacts}
    doms = (await session.execute(select(OrganisationDomain).where(OrganisationDomain.organisation_id == k))).scalars().all()
    assert len(doms) == 1 and doms[0].verified_match_status == "confirmed"
    snaps = (await session.execute(select(WebsiteSnapshot))).scalars().all()
    assert snaps and snaps[0].domain_id == doms[0].id
    sig = (await session.execute(select(BusinessSignal).where(BusinessSignal.organisation_id == k))).scalars().all()
    assert sorted(s.dedupe_hash for s in sig) == ["other", "same"]
    rels = {(r.from_org_id, r.to_org_id, r.relation) for r in (await session.execute(select(BusinessRelationship))).scalars()}
    assert rels == {(k, other, "partner_of"), (other, k, "supplier_of")}
    comp = await session.get(OrganisationCompleteness, k)
    assert comp.published_email == "yes"
    assert (await session.get(RunOrganisation, (run_id, k))) is not None
    # merging again is refused; so is merging into a merged org
    with pytest.raises(ValueError):
        await merge_organisations(session, k, d, None, "again")
    # undo
    sp = await split_organisation(session, d, None, "oops")
    assert (await session.get(Organisation, d)).merged_into_id is None
    assert sp["restored"]["email_addresses"] == 2
    assert (await session.execute(select(EmailAddress).where(EmailAddress.organisation_id == d))).scalars().all()


async def test_merge_conflicting_company_numbers_refused_and_atomic(session):
    a, _ = await resolving_upsert(session, rec("1", "A Ltd", "00000501"), "x")
    b, _ = await resolving_upsert(session, rec("2", "B Ltd", "00000502"), "x")
    with pytest.raises(MergeConflict):
        await merge_organisations(session, a, b, None, "no")
    assert (await session.get(Organisation, b)).merged_into_id is None
    out = await merge_organisations(session, a, b, None, "forced", allow_identifier_conflict=True)
    assert out["drop_id"] == b


async def test_resolving_upsert_follows_merge(session):
    k, d, *_ = await _populate(session)
    await merge_organisations(session, k, d, None, "x")
    # a record for the dropped org's canonical identity lands on the survivor
    org_id, created = await resolving_upsert(session, rec("2", "Keep Company", None, "M1 1AA"), "osm")
    assert org_id == k and not created
