"""Adapter output -> persistence (organisations, signals, record links) end to end."""
from sqlalchemy import func, select

from app.models import BusinessSignal, Organisation, OrganisationRecordLink
from app.sources.abn_lookup import AbnLookupAdapter
from app.sources.charity_commission import CharityCommissionAdapter
from app.sources.exhibitors import ExhibitorListImport
from app.sources.gleif import GleifLeiAdapter
from app.sources.ocds import ContractsFinderOCDS
from app.sources.persist import canonical_identity
from app.sources.signals_util import persist_record

from .fixtures import write_abn_zip, write_charity_json_zip, write_lei_csv_zip, write_ocds_package
from .helpers import collect


async def run(session, adapter, query=None):
    n = 0
    for rec, _cp in await collect(adapter, query):
        await persist_record(session, rec)
        n += 1
    return n


async def test_ocds_awards_merge_by_company_number_and_accumulate_signals(session, tmp_path):
    p = write_ocds_package(tmp_path / "p.json")
    assert await run(session, ContractsFinderOCDS({"path": str(p)})) == 7
    assert await run(session, ContractsFinderOCDS({"path": str(p)})) == 7  # idempotent
    org = (await session.execute(select(Organisation).where(Organisation.company_number == "99999991"))).scalar_one()
    assert org.canonical_identity == "GB:cn:99999991" and org.discovery_source == "contracts_finder_ocds"
    sigs = (await session.execute(select(BusinessSignal).where(BusinessSignal.organisation_id == org.id))).scalars().all()
    assert len(sigs) == 3 and all(s.signal_type == "contract_award" and s.source_url.startswith("https://www.contractsfinder") for s in sigs)
    assert (await session.execute(select(func.count()).select_from(OrganisationRecordLink))).scalar_one() == 2  # the two GB-COH suppliers


async def test_identities_join_across_sources(session, tmp_path):
    """charity (company no. 99999991) + OCDS supplier 99999991 + GLEIF RA000585 99999994 resolve onto Companies House identities."""
    await run(session, CharityCommissionAdapter({"path": str(write_charity_json_zip(tmp_path / "c.zip"))}))
    await run(session, ContractsFinderOCDS({"path": str(write_ocds_package(tmp_path / "p.json"))}))
    await run(session, GleifLeiAdapter({"path": str(write_lei_csv_zip(tmp_path / "l.zip"))}))
    rows = (await session.execute(select(Organisation.canonical_identity, Organisation.legal_name).where(Organisation.company_number == "99999991"))).all()
    assert len(rows) == 1  # charity and supplier are ONE organisation
    gl = (await session.execute(select(Organisation).where(Organisation.company_number == "99999994"))).scalar_one()
    assert gl.legal_name == "EXAMPLE HOLDINGS PLC"
    links = (await session.execute(select(OrganisationRecordLink.identifier_type).where(OrganisationRecordLink.organisation_id == gl.id))).scalars().all()
    assert links == ["lei"]


async def test_abn_records_use_abn_as_identity(session, tmp_path):
    await run(session, AbnLookupAdapter({"path": str(write_abn_zip(tmp_path / "a.zip"))}))
    org = (await session.execute(select(Organisation).where(Organisation.company_number == "11000000001"))).scalar_one()
    assert org.canonical_identity == "AU:cn:11000000001" and org.country_code == "AU" and org.postal_code == "2000"


async def test_exhibitor_signals_persist_with_event_detail(session, tmp_path):
    p = tmp_path / "e.csv"
    p.write_text("Company,Website,Stand,Event,Date,Role,Country\nExample Fixings Ltd,www.example-fixings.example.com,A12,Example Expo,2027-03-10,Exhibitor,GB\n")
    await run(session, ExhibitorListImport({"path": str(p), "list_url": "https://events.example.org/expo/exhibitors"}))
    sig = (await session.execute(select(BusinessSignal))).scalar_one()
    assert sig.signal_type == "exhibitor" and sig.detail["stand"] == "A12" and sig.source_url == "https://events.example.org/expo/exhibitors"
    from app.models import OrganisationDomain

    dom = (await session.execute(select(OrganisationDomain))).scalar_one()
    assert dom.verified_match_status == "unknown"  # a candidate website is never auto-confirmed


def test_canonical_identity_for_unnumbered_supplier():
    from app.sources.base import BusinessRecord

    r = BusinessRecord(source_ref="x", legal_name="Placeholder Cleaning Services", country_code="GB", postal_code="BS1 5AH")
    assert canonical_identity(r) == "GB:nm:placeholder cleaning services:bs15ah"
