import json

import httpx
import pytest

from app.sources._http import RateLimited
from app.sources.base import DiscoveryQuery, get_adapter
from app.sources.cpv_map import cpv_to_industries, cpv_to_industry
from app.sources.ocds import (
    ContractsFinderOCDS,
    FindATenderOCDS,
    build_unit_record,
    iter_award_units,
    iter_releases_from_path,
)
from app.sources.signals_util import MissingSourceUrl, make_signal

from .fixtures import fts_release, ocds_package, ocds_releases, write_ocds_package
from .helpers import assert_resume_no_dup_no_gap, collect


@pytest.fixture
def pkg(tmp_path):
    return write_ocds_package(tmp_path / "package.json")


def cf(path, **cfg):
    return ContractsFinderOCDS({"path": str(path), **cfg})


def test_metadata():
    for aid in ("contracts_finder_ocds", "find_a_tender_ocds"):
        i = get_adapter(aid).info
        assert i.country_coverage == ("GB",) and i.cost == "free" and i.credentials == "none"
        assert "Open Government Licence" in i.licence and i.access_method == "api" and i.live_verified


def test_cpv_mapping_table():
    assert cpv_to_industry("45000000")[0] == "construction"
    assert cpv_to_industry("79950000")[0] == "events"  # 4-digit override beats division 79
    assert cpv_to_industry("79000000")[0] == "business_services"
    assert cpv_to_industry("90910000")[0] == "environmental_cleaning"
    assert cpv_to_industry("99999999") is None and cpv_to_industry("") is None
    assert cpv_to_industries(["45000000", "45100000", "72000000"]) == ["construction", "it_services"]


def test_award_units_skip_cancelled_and_unnamed():
    rels = ocds_releases()
    names = [u.party["name"] for r in rels for u in iter_award_units(r)]
    assert "Never Awarded Ltd" not in names and len(names) == 7


async def test_records_and_signals(pkg):
    got = await collect(cf(pkg))
    recs = [r for r, _ in got]
    assert len(recs) == 7
    r1 = recs[0]
    assert r1.legal_name == "Example Supplies Ltd" and r1.company_number == "99999991" and r1.country_code == "GB"
    assert r1.category_hint == "procurement_winner" and r1.raw["industries"] == ["construction"]
    assert r1.email_published == "sales@example-widgets.co.uk" and r1.website == "https://www.example.com"  # only because the notice publishes them
    assert r1.postal_code == "M2 2BB"  # extracted from the free-text street address
    assert r1.raw["buyer"] == "Example District Council"
    sig = r1.raw["signals"][0]
    assert sig["signal_type"] == "contract_award" and sig["source_url"].endswith("/Notice/aaaa1111")
    assert sig["detail"]["value"] == 1000 and sig["detail"]["cpv"] == ["45000000"]
    by = {r.legal_name: r for r in recs}
    assert by["Placeholder Cleaning Services"].company_number is None and by["Placeholder Cleaning Services"].postal_code == "BS1 5AH"
    assert by["Placeholder Cleaning Services"].raw["industries"] == ["environmental_cleaning"]
    assert by["Overseas Example GmbH"].country_code == "DE"
    # the buyer's contact data never lands on a supplier
    assert all(r.email_published in (None, "sales@example-widgets.co.uk") for r in recs)


async def test_signal_requires_source_url(pkg):
    recs = [r for r, _ in await collect(cf(pkg))]
    no_url = [r for r in recs if r.raw["award_key"].startswith("a8|")][0]
    assert not no_url.raw.get("signals") and no_url.source_url is None  # award a8 has no document, so no signal
    with pytest.raises(MissingSourceUrl):
        make_signal("contract_award", "x", None)
    with pytest.raises(MissingSourceUrl):
        make_signal("contract_award", "x", "   ")


async def test_package_uri_is_the_fallback_source(pkg):
    recs = [r for r, _ in await collect(cf(pkg, source_url="https://example.com/package.json"))]
    last = [r for r in recs if r.raw["award_key"].startswith("a8|")][0]
    assert last.raw["signals"][0]["source_url"] == "https://example.com/package.json"


async def test_resume_file_mode(pkg):
    await assert_resume_no_dup_no_gap(lambda: cf(pkg))


async def test_established_filter_min_awards(pkg):
    got = await collect(cf(pkg, min_awards=2))
    names = {r.legal_name: r for r, _ in got}
    assert set(names) == {"Example Supplies Ltd", "Sample Builders Ltd"}  # 3 awards and 2 awards; one-off winners excluded
    s1 = names["Example Supplies Ltd"]
    assert s1.raw["award_count"] == 3 and s1.raw["award_years"] == 2
    assert len(s1.raw["signals"]) == 3 and all(s["source_url"] for s in s1.raw["signals"])
    assert "construction" in s1.raw["industries"]


async def test_established_filter_award_years_and_resume(pkg):
    got = await collect(cf(pkg, min_award_years=2))
    assert {r.legal_name for r, _ in got} == {"Example Supplies Ltd", "Sample Builders Ltd"}  # B: 2025 + 2026
    got = await collect(cf(pkg, min_award_years=3))
    assert {r.legal_name for r, _ in got} == set()
    await assert_resume_no_dup_no_gap(lambda: cf(pkg, min_awards=2))
    got = await collect(cf(pkg, min_awards=2), DiscoveryQuery(active_only=False, extra={"min_awards": 3}))
    assert [r.legal_name for r, _ in got] == ["Example Supplies Ltd"]  # per-query override


async def test_zip_and_jsonl_inputs(tmp_path):
    import zipfile

    z = tmp_path / "p.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("a.json", json.dumps(ocds_package(ocds_releases()[:2])))
        zf.writestr("b.jsonl", "\n".join(json.dumps(r) for r in ocds_releases()[2:4]))
    ids = [r["id"] for r in iter_releases_from_path(z)]
    assert ids == ["r1", "r2", "r3", "r4"]
    d = tmp_path / "dir"
    d.mkdir()
    write_ocds_package(d / "1.json", ocds_releases()[:1])
    assert [r["id"] for r in iter_releases_from_path(d)] == ["r1"]


# ------------------------------------------------------------------ API mode (MockTransport)
def api_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_api_paging_and_resume(nosleep):
    rels = ocds_releases()
    calls = []

    def handler(req: httpx.Request):
        calls.append(str(req.url))
        if "cursor=2" not in str(req.url):
            return httpx.Response(200, json=ocds_package(rels[:3], nxt="https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search?cursor=2"))
        return httpx.Response(200, json=ocds_package(rels[3:]))

    def mk():
        return ContractsFinderOCDS({"http_client": api_client(handler), "sleep": nosleep, "published_from": "2026-09-01", "published_to": "2026-09-02"})

    full = await assert_resume_no_dup_no_gap(mk)
    assert len(full) == 7
    assert "publishedFrom=2026-09-01" in calls[0] and "stages=award" in calls[0]


async def test_api_429_retry_after_is_honoured(nosleep):
    n = {"c": 0}

    def handler(req):
        n["c"] += 1
        if n["c"] <= 2:
            return httpx.Response(429, text="Rate limit of 12 exceeded. Please retry after 120 seconds.")
        return httpx.Response(200, json=ocds_package(ocds_releases()[:1]))

    a = ContractsFinderOCDS({"http_client": api_client(handler), "sleep": nosleep, "min_interval_s": 0})
    got = await collect(a)
    assert len(got) == 1
    assert [s for s in nosleep.calls if s >= 100] == [120.0, 120.0]  # waited exactly as instructed, twice


async def test_api_429_beyond_bound_stops_cleanly(nosleep):
    a = ContractsFinderOCDS({"http_client": api_client(lambda r: httpx.Response(429, headers={"Retry-After": "900"})), "sleep": nosleep, "min_interval_s": 0})
    with pytest.raises(RateLimited):
        await collect(a)
    assert nosleep.calls == []  # 900s > max_wait_s: stop instead of sleeping


async def test_api_5xx_backoff(nosleep):
    n = {"c": 0}

    def handler(req):
        n["c"] += 1
        return httpx.Response(503) if n["c"] < 3 else httpx.Response(200, json=ocds_package(ocds_releases()[:1]))

    got = await collect(ContractsFinderOCDS({"http_client": api_client(handler), "sleep": nosleep, "min_interval_s": 0}))
    assert len(got) == 1 and nosleep.calls[:2] == [2.0, 4.0]


async def test_find_a_tender_shape(tmp_path):
    pkg = ocds_package([fts_release()], uri="https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages")
    p = tmp_path / "fts.json"
    p.write_text(json.dumps(pkg))
    got = await collect(FindATenderOCDS({"path": str(p)}))
    r = got[0][0]
    assert r.company_number == "99999993" and r.region is None  # NUTS code 'UKI4' is not a region name
    assert r.raw["signals"][0]["source_url"] == "https://www.find-tender.service.gov.uk/Notice/012345-2026"
    assert r.adapter_id == "find_a_tender_ocds"


def test_build_record_without_country_is_dropped():
    rel = {"id": "x", "awards": [{"id": "a", "suppliers": [{"id": "p", "name": "No Country Ltd"}]}],
           "parties": [{"id": "p", "name": "No Country Ltd", "roles": ["supplier"]}]}
    u = next(iter_award_units(rel))
    assert build_unit_record(u, notice_url="https://x.example/n", default_country=None, adapter_id="t", source_name="t") is None
