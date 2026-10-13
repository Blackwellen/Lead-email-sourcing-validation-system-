import re

import httpx
import pytest

from app.sources.base import DiscoveryQuery, NotConfigured
from app.sources.companies_house_bulk import CompaniesHouseBulkAdapter, iter_rows, row_to_record
from app.sources.download import DownloadError, download_file
from tests.sources.fixtures import CH_HEADER, ch_row, small_ch_rows, write_ch_zip


async def collect(adapter, query, checkpoint=None, stop_after=None):
    out = []
    async for rec, cp in adapter.iter_businesses(query, checkpoint):
        out.append((rec, cp))
        if stop_after and len(out) >= stop_after:
            break
    return out


@pytest.fixture
def ch_file(tmp_path):
    return write_ch_zip(tmp_path / "BasicCompanyDataAsOneFile-2026-10-01.zip", small_ch_rows())


def adapter(path, **cfg):
    return CompaniesHouseBulkAdapter({"path": str(path), **cfg})


def test_header_with_leading_space_is_handled(ch_file):
    assert " CompanyNumber" in CH_HEADER
    rows = list(iter_rows(ch_file))
    assert len(rows) == 8
    assert rows[0][1]["number"] == "TEST0001"
    assert rows[0][1]["postcode"] == "M1 1AA"


async def test_map_fields(ch_file):
    got = await collect(adapter(ch_file), DiscoveryQuery(active_only=False))
    by = {r.company_number: r for r, _ in got}
    r = by["TEST0002"]
    assert r.legal_name == "FIXTURE LETTINGS & SONS LIMITED"
    assert r.sic_codes == ["68310", "68320"]
    assert r.city == "LEEDS" and r.region == "WEST YORKSHIRE" and r.postal_code == "LS1 4AP"
    assert r.country_code == "GB" and r.legal_type == "Private Limited Company"
    assert r.licence == "Open Government Licence v3.0" and "Companies House" in r.attribution
    assert r.website is None and r.email_published is None  # the dataset publishes neither; URI is not a website
    assert r.source_url.endswith("/company/TEST0002")
    assert r.adapter_id == "companies_house_bulk"
    assert by["TEST0007"].sic_codes == []  # "None Supplied"
    assert by["SC000008"].status.startswith("Active")


async def test_active_only_default(ch_file):
    got = await collect(adapter(ch_file), DiscoveryQuery())
    nums = {r.company_number for r, _ in got}
    assert "TEST0006" not in nums and "TEST0001" in nums and "SC000008" in nums


async def test_filter_sic(ch_file):
    got = await collect(adapter(ch_file), DiscoveryQuery(sic_codes=["68310"]))
    assert {r.company_number for r, _ in got} == {"TEST0001", "TEST0002"}  # dissolved TEST0006 excluded


async def test_filter_postcode_area_district_and_town(ch_file):
    a = adapter(ch_file)
    m = await collect(a, DiscoveryQuery(postcodes=["M"]))
    assert {r.company_number for r, _ in m} == {"TEST0001", "TEST0005"}
    d = await collect(a, DiscoveryQuery(postcodes=["M2"]))
    assert {r.company_number for r, _ in d} == {"TEST0005"}
    t = await collect(a, DiscoveryQuery(localities=["leeds"]))
    assert {r.company_number for r, _ in t} == {"TEST0002"}
    c = await collect(a, DiscoveryQuery(regions=["greater manchester"]))
    assert {r.company_number for r, _ in c} == {"TEST0001"}


async def test_filter_keyword_and_exclusion(ch_file):
    a = adapter(ch_file)
    k = await collect(a, DiscoveryQuery(keywords=["plumbing"]))
    assert {r.company_number for r, _ in k} == {"TEST0004"}
    k = await collect(a, DiscoveryQuery(keywords=["cleaners"], sic_codes=["99999"]))
    assert {r.company_number for r, _ in k} == {"TEST0007", "TEST0005"}
    k = await collect(a, DiscoveryQuery(sic_codes=["68310"], exclusions=["lettings"]))
    assert {r.company_number for r, _ in k} == {"TEST0001"}
    k = await collect(a, DiscoveryQuery(sic_codes=["68310"], keywords=["lettings"], extra={"category_match": "all"}))
    assert {r.company_number for r, _ in k} == {"TEST0002"}


async def test_country_filter_and_limit(ch_file):
    a = adapter(ch_file)
    assert await collect(a, DiscoveryQuery(countries=["FR"])) == []
    assert len(await collect(a, DiscoveryQuery(countries=["GB"], limit=3))) == 3


async def test_checkpoint_resume_no_dups_no_gaps(ch_file):
    a = adapter(ch_file)
    q = DiscoveryQuery()
    full = [r.company_number for r, _ in await collect(a, q)]
    assert len(full) == 7
    for k in range(1, len(full)):
        first = await collect(a, q, stop_after=k)
        cp = first[-1][1]
        rest = await collect(a, q, checkpoint=cp)
        merged = [r.company_number for r, _ in first] + [r.company_number for r, _ in rest]
        assert merged == full, f"stopped after {k}"


async def test_checkpoint_from_other_snapshot_restarts(ch_file):
    a = adapter(ch_file)
    got = await collect(a, DiscoveryQuery(), checkpoint={"row": 5, "file": "OtherFile.zip"})
    assert len(got) == 7


async def test_plain_csv_and_bom(tmp_path):
    p = tmp_path / "plain.csv"
    import csv

    with open(p, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(CH_HEADER)
        w.writerow(ch_row("FIXTURE CSV LTD", "TEST0100", town="LEEDS", postcode="ls1 1aa", sics=["68310 - x"]))
    got = await collect(adapter(p), DiscoveryQuery())
    assert got[0][0].postal_code == "LS1 1AA"


async def test_rejects_non_ch_file(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("a,b\n1,2\n")
    with pytest.raises(NotConfigured):
        await collect(adapter(p), DiscoveryQuery())
    with pytest.raises(NotConfigured):
        await collect(CompaniesHouseBulkAdapter({}), DiscoveryQuery())
    h = await CompaniesHouseBulkAdapter({}).health()
    assert not h.ok and h.status == "not_configured"


def test_row_to_record_skips_blank_identity():
    assert row_to_record({"name": "", "number": "1"}) is None
    assert row_to_record({"name": "X", "number": ""}) is None


# ---------------------------------------------------------------- download (mock server)
PAYLOAD = bytes(range(256)) * 400  # 102 400 bytes


def make_transport(state, payload=PAYLOAD, etag='"v1"', support_range=True):
    def handler(request: httpx.Request) -> httpx.Response:
        state.setdefault("calls", []).append((request.method, request.headers.get("range")))
        hdr = {"etag": etag, "last-modified": "Wed, 01 Oct 2026 00:00:00 GMT", "accept-ranges": "bytes"}
        if request.method == "HEAD":
            return httpx.Response(200, headers={**hdr, "content-length": str(len(payload))})
        rng = request.headers.get("range")
        if rng and support_range:
            start = int(re.match(r"bytes=(\d+)-", rng).group(1))
            body = payload[start:]
            return httpx.Response(206, content=body, headers={**hdr, "content-range": f"bytes {start}-{len(payload)-1}/{len(payload)}"})
        return httpx.Response(200, content=payload, headers=hdr)

    return httpx.MockTransport(handler)


async def test_download_fresh_then_unchanged_skip(tmp_path):
    state: dict = {}
    async with httpx.AsyncClient(transport=make_transport(state)) as c:
        dest = tmp_path / "f.bin"
        r1 = await download_file("https://example.com/f.bin", dest, client=c)
        assert dest.read_bytes() == PAYLOAD and not r1.skipped_unchanged
        r2 = await download_file("https://example.com/f.bin", dest, client=c)
        assert r2.skipped_unchanged and r2.downloaded_bytes == 0
        assert [m for m, _ in state["calls"]].count("GET") == 1


async def test_download_resume_from_partial(tmp_path):
    state: dict = {}
    dest = tmp_path / "f.bin"
    (tmp_path / "f.bin.part").write_bytes(PAYLOAD[:5000])
    (tmp_path / "f.bin.meta.json").write_text('{"partial_for": "\\"v1\\"", "url": "u"}')
    async with httpx.AsyncClient(transport=make_transport(state)) as c:
        r = await download_file("https://example.com/f.bin", dest, client=c)
    assert r.resumed and dest.read_bytes() == PAYLOAD
    assert ("GET", "bytes=5000-") in state["calls"]


async def test_download_restarts_when_server_ignores_range_or_etag_changed(tmp_path):
    dest = tmp_path / "f.bin"
    (tmp_path / "f.bin.part").write_bytes(b"stale-garbage")
    (tmp_path / "f.bin.meta.json").write_text('{"partial_for": "\\"old\\""}')
    async with httpx.AsyncClient(transport=make_transport({})) as c:
        await download_file("https://example.com/f.bin", dest, client=c)
    assert dest.read_bytes() == PAYLOAD
    # server that ignores Range (always 200)
    dest2 = tmp_path / "g.bin"
    (tmp_path / "g.bin.part").write_bytes(PAYLOAD[:100])
    (tmp_path / "g.bin.meta.json").write_text('{"partial_for": "\\"v1\\""}')
    async with httpx.AsyncClient(transport=make_transport({}, support_range=False)) as c:
        await download_file("https://example.com/g.bin", dest2, client=c)
    assert dest2.read_bytes() == PAYLOAD


async def test_download_size_and_disk_sanity(tmp_path, monkeypatch):
    async with httpx.AsyncClient(transport=make_transport({})) as c:
        with pytest.raises(DownloadError, match="refusing"):
            await download_file("https://example.com/f.bin", tmp_path / "a.bin", client=c, max_bytes=1000)
        import shutil
        from collections import namedtuple

        usage = namedtuple("usage", "total used free")
        monkeypatch.setattr(shutil, "disk_usage", lambda p: usage(10, 10, 10))
        with pytest.raises(DownloadError, match="disk"):
            await download_file("https://example.com/f.bin", tmp_path / "b.bin", client=c)


async def test_download_http_error(tmp_path):
    t = httpx.MockTransport(lambda r: httpx.Response(404))
    async with httpx.AsyncClient(transport=t) as c:
        with pytest.raises(DownloadError):
            await download_file("https://example.com/x", tmp_path / "x", client=c)


async def test_adapter_resolves_latest_and_downloads(tmp_path, ch_file):
    zip_bytes = ch_file.read_bytes()
    page = ('<html><a href="BasicCompanyDataAsOneFile-2026-10-01.zip">BasicCompanyDataAsOneFile-2026-10-01.zip</a> '
            '<a href="BasicCompanyData-2026-10-01-part1_7.zip">p</a></html>')
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        if req.url.path.endswith("en_output.html"):
            return httpx.Response(200, text=page)
        if req.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(zip_bytes)), "etag": '"z"'})
        return httpx.Response(200, content=zip_bytes, headers={"etag": '"z"'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        a = CompaniesHouseBulkAdapter({"auto_download": True, "data_dir": str(tmp_path / "dd"), "http_client": c})
        got = [r async for r, _ in a.iter_businesses(DiscoveryQuery())]
    assert len(got) == 7
    assert any(u.endswith("/BasicCompanyDataAsOneFile-2026-10-01.zip") for u in seen)
    assert (tmp_path / "dd" / "companies_house" / "BasicCompanyDataAsOneFile-2026-10-01.zip").exists()
    assert not any("/api." in u or "api.company-information" in u for u in seen)  # no per-company API calls


@pytest.mark.live
async def test_live_product_page_lists_zip():  # pragma: no cover
    a = CompaniesHouseBulkAdapter({})
    url = await a.resolve_latest_url()
    assert url.endswith(".zip")
