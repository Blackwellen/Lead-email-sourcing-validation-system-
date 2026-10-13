import pytest

from app.sources.base import DiscoveryQuery, get_adapter
from app.sources.gleif import GleifLeiAdapter, RelationshipIndex

from .fixtures import write_lei_csv_zip, write_lei_json_zip, write_rr_csv_zip, write_rr_json_zip
from .helpers import assert_resume_no_dup_no_gap, collect


@pytest.fixture(params=["csv", "json"])
def lei_file(request, tmp_path):
    return (write_lei_csv_zip if request.param == "csv" else write_lei_json_zip)(tmp_path / f"lei.{request.param}.zip")


def test_metadata():
    i = get_adapter("gleif_lei").info
    assert i.country_coverage == ("*",) and "CC0" in i.licence and i.cost == "free" and i.credentials == "none"
    assert "daily" in i.refresh_frequency


async def test_map_csv_and_json_identically(lei_file):
    got = await collect(GleifLeiAdapter({"path": str(lei_file)}), DiscoveryQuery())
    by = {r.source_ref: r for r, _ in got}
    assert "EXAMPLE00000000000004" not in by  # INACTIVE -> filtered by default
    a = by["EXAMPLE00000000000001"]
    assert a.legal_name == "EXAMPLE HOLDINGS PLC" and a.country_code == "GB" and a.postal_code == "EC2M 4AA" and a.city == "LONDON"
    assert a.company_number == "99999994"  # RA000585 = Companies House
    assert a.status == "Active" and a.licence.startswith("CC0")
    assert a.raw["lei"] == "EXAMPLE00000000000001" and {"type": "lei", "value": a.raw["lei"]} in a.raw["identifiers"]
    assert a.raw["registration_authority"] == "RA000585"
    de = by["EXAMPLE00000000000003"]
    assert de.company_number is None and de.country_code == "DE"  # non-GB registers are not assumed unique


async def test_trading_name_only_in_csv(tmp_path):
    got = await collect(GleifLeiAdapter({"path": str(write_lei_csv_zip(tmp_path / "x.zip"))}))
    assert {r.source_ref: r for r, _ in got}["EXAMPLE00000000000001"].trading_name == "EXAMPLE TRADING"
    got = await collect(GleifLeiAdapter({"path": str(write_lei_json_zip(tmp_path / "y.zip"))}))
    assert {r.source_ref: r for r, _ in got}["EXAMPLE00000000000001"].trading_name == "EXAMPLE TRADING"


async def test_extra_company_number_registers(lei_file):
    got = await collect(GleifLeiAdapter({"path": str(lei_file), "company_number_ras": {"RA000100": "DE"}}))
    assert {r.source_ref: r for r, _ in got}["EXAMPLE00000000000003"].company_number == "HRB000000"


async def test_country_filter_and_inactive(lei_file):
    got = await collect(GleifLeiAdapter({"path": str(lei_file)}), DiscoveryQuery(countries=["DE"]))
    assert [r.source_ref for r, _ in got] == ["EXAMPLE00000000000003"]
    got = await collect(GleifLeiAdapter({"path": str(lei_file)}), DiscoveryQuery(active_only=False, countries=["GB"]))
    assert len(got) == 3


async def test_resume(lei_file):
    await assert_resume_no_dup_no_gap(lambda: GleifLeiAdapter({"path": str(lei_file)}))


@pytest.mark.parametrize("writer", [write_rr_csv_zip, write_rr_json_zip])
async def test_relationship_hints(tmp_path, writer):
    lei = write_lei_csv_zip(tmp_path / "l.zip")
    rr = writer(tmp_path / "rr.zip")
    got = await collect(GleifLeiAdapter({"path": str(lei), "rr_path": str(rr)}))
    by = {r.source_ref: r for r, _ in got}
    rels = by["EXAMPLE00000000000002"].raw["relationships"]
    assert {(x["relation"], x["ultimate"], x["counterparty"]["identifier_value"]) for x in rels} == {
        ("subsidiary_of", False, "EXAMPLE00000000000001"), ("subsidiary_of", True, "EXAMPLE00000000000009")}
    assert all(x["counterparty"]["identifier_type"] == "lei" and x["source_url"] for x in rels)
    assert by["EXAMPLE00000000000003"].raw["relationships"] == []  # INACTIVE relationship ignored
    assert by["EXAMPLE00000000000001"].raw["relationships"] == []


def test_relationship_index_cleans_up(tmp_path):
    idx = RelationshipIndex(write_rr_csv_zip(tmp_path / "rr.zip"))
    p = idx.path
    assert p.exists() and len(idx.parents("EXAMPLE00000000000002")) == 2
    idx.close()
    assert not p.exists()
