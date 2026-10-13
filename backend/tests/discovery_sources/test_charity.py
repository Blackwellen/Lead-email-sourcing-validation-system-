import pytest

from app.sources.base import DiscoveryQuery, NotConfigured, get_adapter
from app.sources.charity_commission import CharityCommissionAdapter, iter_extract, row_to_record

from .fixtures import write_charity_json_zip, write_charity_txt_zip
from .helpers import assert_resume_no_dup_no_gap, collect


@pytest.fixture(params=["json", "txt"])
def charity_file(request, tmp_path):
    w = write_charity_json_zip if request.param == "json" else write_charity_txt_zip
    return w(tmp_path / f"publicextract.charity.{request.param}.zip")


def test_registered_with_honest_metadata():
    i = get_adapter("charity_commission_ew").info
    assert i.country_coverage == ("GB",) and i.cost == "free" and i.credentials == "none" and "Open Government Licence" in i.licence
    assert i.refresh_frequency and i.access_method == "bulk_file" and i.automated_collection_permitted


async def test_map_fields_json_and_txt(charity_file):
    got = await collect(CharityCommissionAdapter({"path": str(charity_file)}), DiscoveryQuery())
    by = {r.source_ref: r for r, _ in got}
    r = by["9999001-0"]
    assert r.legal_name == "EXAMPLE COMMUNITY HALL TRUST" and r.country_code == "GB"
    assert r.company_number == "99999991"  # GB-COH number joins the Companies House identity
    assert r.postal_code == "M1 1AA" and "1 Example Road" in r.address
    assert r.website == "www.example.com" and r.email_published == "office@example.com" and r.phone == "0161 000 0001"
    assert r.raw["charity_number"] == "9999001" and r.raw["identifiers"][0] == {"type": "charity_no", "value": "9999001"}
    assert r.licence == "Open Government Licence v3.0" and "Charity Commission" in r.attribution
    assert by["9999001-1"].company_number is None
    assert by["9999004-0"].company_number == "00000123"  # zero-padded
    assert "9999002-0" not in by  # Removed charities are inactive (active_only default)


async def test_active_only_false_includes_removed(charity_file):
    got = await collect(CharityCommissionAdapter({"path": str(charity_file)}), DiscoveryQuery(active_only=False))
    assert "9999002-0" in {r.source_ref for r, _ in got}


async def test_postcode_filter_and_limit(charity_file):
    got = await collect(CharityCommissionAdapter({"path": str(charity_file)}), DiscoveryQuery(postcodes=["LS"]))
    assert [r.source_ref for r, _ in got] == ["9999003-0"]
    got = await collect(CharityCommissionAdapter({"path": str(charity_file)}), DiscoveryQuery(limit=2))
    assert len(got) == 2


async def test_resume_no_dup_no_gap(charity_file):
    await assert_resume_no_dup_no_gap(lambda: CharityCommissionAdapter({"path": str(charity_file)}))


async def test_not_configured():
    with pytest.raises(NotConfigured):
        await collect(CharityCommissionAdapter({}))


def test_iter_extract_skip(tmp_path):
    p = write_charity_json_zip(tmp_path / "c.zip")
    assert [i for i, _r, _m in iter_extract(p, skip=3)] == [3, 4]


def test_row_without_name_is_dropped():
    assert row_to_record({"registered_charity_number": 1, "charity_name": None}) is None
