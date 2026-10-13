from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.models import Organisation, OrganisationDomain, OrganisationSource, RunOrganisation, SourcingRun
from app.sources.base import BusinessRecord
from app.sources.persist import canonical_identity, normalise_name, upsert_business


def rec(**kw):
    d = dict(source_ref="r1", country_code="GB", adapter_id="companies_house_bulk", observed_at=datetime.now(UTC))
    d.update(kw)
    return BusinessRecord(**d)


async def new_run(session):
    run = SourcingRun(name="fixture run")
    session.add(run)
    await session.flush()
    return run.id


@pytest.mark.parametrize("a,b", [
    ("The Fixture & Sons Ltd.", "fixture and sons limited"),
    ("FIXTURE   PLUMBING LTD", "Fixture Plumbing"),
    ("Café Fixture LLP", "cafe fixture"),
    ("Fixture's Estate Agents plc", "Fixtures Estate Agents"),
])
def test_normalise_name_equivalences(a, b):
    assert normalise_name(a) == normalise_name(b)


def test_normalise_name_keeps_distinct_names_distinct():
    assert normalise_name("Fixture North Ltd") != normalise_name("Fixture South Ltd")
    assert normalise_name("Ltd") == "ltd"  # never reduces to empty
    assert normalise_name(None) == "" and normalise_name("   ") == ""


def test_canonical_identity_rules():
    assert canonical_identity(rec(company_number=" 0000 0006 ", legal_name="x")) == "GB:cn:00000006"
    a = canonical_identity(rec(legal_name="Fixture Ltd", postal_code="m1 1aa"))
    b = canonical_identity(rec(trading_name="THE FIXTURE LIMITED", postal_code="M1  1AA"))
    assert a == b == "GB:nm:fixture:m11aa"
    assert canonical_identity(rec(legal_name="Fixture", city="Leeds")) == "GB:nm:fixture:leeds"
    assert canonical_identity(rec(legal_name="Fixture")) == "GB:nm:fixture:-"
    assert canonical_identity(rec(legal_name="Fixture", country_code="ie")) != canonical_identity(rec(legal_name="Fixture"))
    with pytest.raises(ValueError):
        canonical_identity(rec())


async def test_upsert_idempotent_and_links_run(session):
    run_id = await new_run(session)
    r = rec(company_number="TEST0001", legal_name="FIXTURE LTD", city="LEEDS", postal_code="ls1 4ap", sic_codes=["68310"],
            status="Active", licence="OGL", source_url="https://example.com/c/1", raw={"k": "v"})
    id1, created1 = await upsert_business(session, r, run_id=run_id, category_id=None)
    id2, created2 = await upsert_business(session, r, run_id=run_id)
    assert (created1, created2) == (True, False) and id1 == id2
    assert (await session.execute(select(func.count()).select_from(Organisation))).scalar_one() == 1
    org = await session.get(Organisation, id1)
    assert org.canonical_identity == "GB:cn:TEST0001" and org.postal_code == "LS1 4AP"
    assert org.discovery_source == "companies_house_bulk" and org.source_ref == "r1" and org.sic_codes == ["68310"]
    links = (await session.execute(select(func.count()).select_from(RunOrganisation).where(RunOrganisation.run_id == run_id))).scalar_one()
    assert links == 1
    srcs = (await session.execute(select(OrganisationSource))).scalars().all()
    assert len(srcs) == 1 and srcs[0].licence == "OGL" and srcs[0].adapter_id == "companies_house_bulk"
    assert srcs[0].raw == {"k": "v"} and srcs[0].run_id == run_id


async def test_identity_merge_by_name_and_postcode_across_sources(session):
    osm = rec(adapter_id="osm_geofabrik", source_ref="node/1", trading_name="Fixture Plumbing", postal_code="M1 1AA", phone="0161 1",
              lat=53.4, lon=-2.2, licence="ODbL-1.0", attribution="© OpenStreetMap contributors")
    csv = rec(adapter_id="csv_import", source_ref="fixture.example.com", legal_name="FIXTURE PLUMBING LTD", postal_code="m11aa",
              website="https://fixture-plumbing.example.com")
    a, c1 = await upsert_business(session, osm)
    b, c2 = await upsert_business(session, csv)
    assert a == b and c1 and not c2
    org = await session.get(Organisation, a)
    assert org.trading_name == "Fixture Plumbing" and org.legal_name == "FIXTURE PLUMBING LTD"  # both filled, none lost
    assert org.phone == "0161 1"
    srcs = (await session.execute(select(OrganisationSource).order_by(OrganisationSource.adapter_id))).scalars().all()
    assert [s.adapter_id for s in srcs] == ["csv_import", "osm_geofabrik"]
    assert srcs[1].attribution.startswith("©") and srcs[1].lat == 53.4


async def test_merge_does_not_clobber_better_data(session):
    ch = rec(company_number="TEST0002", legal_name="FIXTURE LTD", city="LEEDS", address="1 Fixture Street, LEEDS", status="Active",
             sic_codes=["68310"])
    oid, _ = await upsert_business(session, ch)
    weaker = rec(adapter_id="csv_import", source_ref="x", company_number="TEST0002", legal_name="fixture", city="Wrong City",
                 address="somewhere else", status="Dissolved", sic_codes=["68320"], phone="0113 000")
    oid2, created = await upsert_business(session, weaker)
    assert oid2 == oid and not created
    org = await session.get(Organisation, oid)
    await session.refresh(org)
    assert (org.legal_name, org.city, org.address, org.status) == ("FIXTURE LTD", "LEEDS", "1 Fixture Street, LEEDS", "Active")
    assert org.phone == "0113 000"  # null in the better record -> filled
    assert org.sic_codes == ["68310", "68320"] and org.discovery_source == "companies_house_bulk"
    # a null incoming value never erases
    await upsert_business(session, rec(company_number="TEST0002", legal_name=None, city=None, status=None))
    await session.refresh(org)
    assert org.city == "LEEDS" and org.status == "Active"


async def test_better_or_equal_source_refreshes_values(session):
    oid, _ = await upsert_business(session, rec(adapter_id="csv_import", source_ref="x", company_number="TEST0003", legal_name="FIXTURE LTD",
                                                  status="active", city="Guess"))
    await upsert_business(session, rec(company_number="TEST0003", legal_name="FIXTURE LIMITED", status="Dissolved", city="LEEDS"))
    org = await session.get(Organisation, oid)
    await session.refresh(org)
    assert (org.legal_name, org.status, org.city) == ("FIXTURE LIMITED", "Dissolved", "LEEDS")
    assert org.discovery_source == "companies_house_bulk"
    # equal rank: newest wins (monthly snapshot refresh)
    await upsert_business(session, rec(company_number="TEST0003", status="Active"))
    await session.refresh(org)
    assert org.status == "Active"


async def test_candidate_domain_is_never_confirmed(session):
    oid, _ = await upsert_business(session, rec(adapter_id="osm_geofabrik", source_ref="n/1", trading_name="Fixture Cafe",
                                                  postal_code="M1 1AA", website="http://WWW.Fixture-Cafe.example.com/menu?x=1#frag"))
    d = (await session.execute(select(OrganisationDomain).where(OrganisationDomain.organisation_id == oid))).scalar_one()
    assert d.verified_match_status == "unknown" and d.confidence == 0.0 and d.manual_override is False
    assert d.domain == "www.fixture-cafe.example.com" and d.registrable_domain == "example.com"
    assert d.website_url == "http://www.fixture-cafe.example.com/menu?x=1"
    assert d.evidence[0]["type"] == "source_candidate" and "NOT verified" in d.evidence[0]["note"]
    # the identity engine confirms it; a re-run of the discovery must not downgrade or touch the verdict
    d.verified_match_status, d.confidence = "confirmed", 0.97
    await session.flush()
    await upsert_business(session, rec(adapter_id="osm_geofabrik", source_ref="n/1", trading_name="Fixture Cafe", postal_code="M1 1AA",
                                         website="http://www.fixture-cafe.example.com/other"))
    await session.refresh(d)
    assert d.verified_match_status == "confirmed" and d.confidence == 0.97
    n = (await session.execute(select(func.count()).select_from(OrganisationDomain))).scalar_one()
    assert n == 1


async def test_invalid_website_creates_no_domain(session):
    oid, _ = await upsert_business(session, rec(legal_name="Fixture Ltd", website="javascript:alert(1)"))
    assert (await session.execute(select(func.count()).select_from(OrganisationDomain))).scalar_one() == 0
    assert oid


async def test_published_email_and_coordinates_recorded_as_provenance_only(session):
    oid, _ = await upsert_business(session, rec(adapter_id="osm_geofabrik", source_ref="n/2", trading_name="Fixture Pub", city="Leeds",
                                                  email_published="bar@fixture-pub.example.com", lat=53.8, lon=-1.5))
    s = (await session.execute(select(OrganisationSource).where(OrganisationSource.organisation_id == oid))).scalar_one()
    assert s.email_published == "bar@fixture-pub.example.com" and (s.lat, s.lon) == (53.8, -1.5)


async def test_requires_country_and_identity(session):
    with pytest.raises(ValueError):
        await upsert_business(session, rec(country_code="", legal_name="X"))
    with pytest.raises(ValueError):
        await upsert_business(session, rec())


async def test_bulk_upsert_many_is_idempotent(session):
    recs = [rec(company_number=f"TEST{i:04d}", legal_name=f"FIXTURE {i} LTD", source_ref=str(i)) for i in range(60)]
    for r in recs:
        await upsert_business(session, r)
    for r in recs:
        _, created = await upsert_business(session, r)
        assert not created
    assert (await session.execute(select(func.count()).select_from(Organisation))).scalar_one() == 60
