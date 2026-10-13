import gzip
import json
import tracemalloc
from datetime import UTC, datetime

import httpx
import pytest

from app.sources._http import HttpFailure, RateLimited
from app.sources.base import AdapterUnavailable, DiscoveryQuery, get_adapter
from app.sources.commoncrawl import (
    CDXClient,
    ColumnarIndexReader,
    CommonCrawlContactPageFinder,
    UrlHit,
    extract_archived_observations,
    fetch_warc_record,
    hit_from_cdx,
    parse_cc_timestamp,
    parse_warc_record,
)

from .fixtures import CC_CONTACT_HTML, make_warc_member, write_cc_parquet
from .helpers import collect

pa = pytest.importorskip("pyarrow")


# ------------------------------------------------------------------------------------------------ CDX client
def cdx_handler(calls, *, pages=2, fail_first=0, status_first=429, headers=None):
    state = {"n": 0}

    def handler(req: httpx.Request):
        calls.append(req)
        state["n"] += 1
        if state["n"] <= fail_first:
            return httpx.Response(status_first, headers=headers or {}, text="SlowDown")
        path = req.url.path
        if path.endswith("collinfo.json"):
            return httpx.Response(200, json=[{"id": "CC-MAIN-2026-33", "name": "Aug 2026"}, {"id": "CC-MAIN-2026-26", "name": "Jun 2026"}])
        p = req.url.params
        if p.get("showNumPages"):
            return httpx.Response(200, text=json.dumps({"pages": pages, "pageSize": 5, "blocks": 9}) + "\n")
        page = int(p.get("page", 0))
        lines = [json.dumps({"urlkey": "com,example)/c", "timestamp": f"2026081{page}120000", "url": f"https://www.example.com/contact-{page}-{i}",
                             "mime": "text/html", "status": "200", "digest": "AAA", "length": "500", "offset": str(1000 * (i + 1)),
                             "filename": "crawl-data/CC-MAIN-2026-33/segments/1/warc/x.warc.gz"}) for i in range(2)]
        return httpx.Response(200, text="\n".join(lines) + "\n")

    return handler


def cdx(handler, nosleep, **kw):
    return CDXClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), sleep=nosleep, min_interval_s=0, **kw)


async def test_cdx_list_and_query_pagination(nosleep):
    calls = []
    async with cdx(cdx_handler(calls), nosleep) as c:
        assert (await c.list_crawls())[0]["id"] == "CC-MAIN-2026-33"
        hits = [(h, p) async for h, p in c.query("example.com")]
    assert len(hits) == 4 and [p for _, p in hits] == [0, 0, 1, 1]
    h = hits[0][0]
    assert h.crawl_id == "CC-MAIN-2026-33" and h.fetch_time == datetime(2026, 8, 10, 12, 0, tzinfo=UTC) and h.status == 200
    assert h.warc_offset == 1000 and h.warc_length == 500 and h.warc_url.startswith("https://data.commoncrawl.org/crawl-data/") and h.has_warc
    q = [r for r in calls if not r.url.path.endswith("collinfo.json")]
    assert q[0].url.params["matchType"] == "domain" and q[0].url.params["output"] == "json" and q[0].url.params["showNumPages"] == "true"
    assert "User-Agent" in q[0].headers and q[0].headers["user-agent"]


async def test_cdx_resume_from_page_and_limit(nosleep):
    async with cdx(cdx_handler([]), nosleep) as c:
        assert [p async for _h, p in c.query("example.com", crawl_id="CC-MAIN-2026-33", start_page=1)] == [1, 1]
        assert len([x async for x in c.query("example.com", crawl_id="CC-MAIN-2026-33", limit=3)]) == 3


async def test_cdx_429_backoff_then_success(nosleep):
    calls = []
    async with cdx(cdx_handler(calls, fail_first=2, status_first=429, headers={"Retry-After": "5"}), nosleep) as c:
        crawls = await c.list_crawls()
    assert crawls and nosleep.calls == [5.0, 5.0] and c.stats["rate_limited"] == 2


async def test_cdx_503_exponential_backoff(nosleep):
    async with cdx(cdx_handler([], fail_first=3, status_first=503), nosleep) as c:
        await c.list_crawls()
    assert nosleep.calls == [2.0, 4.0, 8.0]


async def test_cdx_gives_up_after_bounded_attempts(nosleep):
    async with cdx(cdx_handler([], fail_first=99, status_first=503), nosleep, max_attempts=3) as c:
        with pytest.raises(HttpFailure):
            await c.list_crawls()
    async with cdx(cdx_handler([], fail_first=99, status_first=429, headers={"Retry-After": "9999"}), nosleep) as c:
        with pytest.raises(RateLimited):
            await c.list_crawls()


async def test_cdx_no_captures_is_empty(nosleep):
    def handler(req):
        if req.url.params.get("showNumPages"):
            return httpx.Response(404, text="No Captures found for: nothing.example")
        return httpx.Response(404)

    async with cdx(handler, nosleep) as c:
        assert [x async for x in c.query("nothing.example", crawl_id="CC-MAIN-2026-33")] == []


def test_timestamp_and_cdx_row():
    assert parse_cc_timestamp("20260810120000") == datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    assert parse_cc_timestamp("garbage") is None and parse_cc_timestamp(None) is None
    assert hit_from_cdx({"url": ""}, "c") is None


# ------------------------------------------------------------------------------------------------ columnar index
def test_columnar_predicate_pushdown_and_streaming(tmp_path):
    p = write_cc_parquet(tmp_path / "part-00000.parquet", row_group_size=2)
    r = ColumnarIndexReader(p, registered_domains=["example.com"], batch_size=4)
    hits = list(r.iter_hits())
    assert {h.domain for h in hits} == {"example.com"} and all(h.status == 200 for h in hits)  # status filter pushed down too
    assert len(hits) == 6 and all(isinstance(h, UrlHit) and h.source == "parquet" for h in hits)
    assert r.stats["rows_scanned"] < 11, "row-group statistics should prune the other domains"  # 11 rows in the file
    h = next(x for x in hits if x.url.endswith("/contact-us"))
    assert h.crawl_id == "CC-MAIN-2026-33" and h.fetch_time == datetime(2026, 8, 10, 12, 0, tzinfo=UTC) and h.mime == "text/html"
    assert h.warc_offset is not None and h.warc_length is not None and h.warc_filename.endswith(".warc.gz") and h.has_warc


def test_columnar_tld_host_status_and_mime(tmp_path):
    p = write_cc_parquet(tmp_path / "idx.parquet")
    assert {h.domain for h in ColumnarIndexReader(p, tlds=["de"]).iter_hits()} == {"other.de"}
    assert [h.url for h in ColumnarIndexReader(p, hosts=["shop.sample.co.uk"]).iter_hits()] == ["https://shop.sample.co.uk/find-us"]
    allst = list(ColumnarIndexReader(p, registered_domains=["example.com"], status=None).iter_hits())
    assert any(h.status == 404 for h in allst)
    pdfs = list(ColumnarIndexReader(p, registered_domains=["example.com"], mime_prefixes=["application/pdf"]).iter_hits())
    assert [h.url for h in pdfs] == ["https://www.example.com/brochure.pdf"]


def test_columnar_directory_of_files_and_bounded_memory(tmp_path):
    import pyarrow as pa_
    import pyarrow.parquet as pq

    d = tmp_path / "cc-index"
    d.mkdir()
    for k in range(3):  # 3 files x 20k rows
        n = 20000
        pq.write_table(pa_.table({
            "url": [f"https://site{k}-{i % 500}.example.com/contact" for i in range(n)],
            "url_host_registered_domain": [f"site{k}-{i % 500}.example.com" for i in range(n)],
            "url_host_name": [f"site{k}-{i % 500}.example.com" for i in range(n)], "url_host_tld": ["com"] * n,
            "fetch_status": pa_.array([200] * n, pa_.int16()), "content_mime_type": ["text/html"] * n, "warc_filename": ["w.warc.gz"] * n,
            "warc_record_offset": pa_.array(range(n), pa_.int64()), "warc_record_length": pa_.array([100] * n, pa_.int32()),
            "crawl": ["CC-MAIN-2026-33"] * n,
        }), d / f"part-{k}.parquet", row_group_size=2000)
    tracemalloc.start()
    count = sum(1 for _ in ColumnarIndexReader(d, batch_size=1024).iter_hits())
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert count == 60000
    assert peak < 20 * 1024 * 1024, f"peak {peak / 1e6:.1f} MB: the index must be streamed in batches"
    sel = list(ColumnarIndexReader(d, registered_domains=["site1-7.example.com"]).iter_hits())
    assert len(sel) == 40 and {h.domain for h in sel} == {"site1-7.example.com"}


def test_non_cc_parquet_is_rejected(tmp_path):
    import pyarrow.parquet as pq

    p = tmp_path / "x.parquet"
    pq.write_table(pa.table({"a": [1]}), p)
    with pytest.raises(AdapterUnavailable):
        list(ColumnarIndexReader(p).iter_hits())


# ------------------------------------------------------------------------------------------------ contact page finder
def H(url, status=200, mime="text/html", t=None):
    from urllib.parse import urlsplit

    return UrlHit(url=url, domain=urlsplit(url).hostname, crawl_id="c", fetch_time=t, mime=mime, status=status)


def test_finder_classification():
    f = CommonCrawlContactPageFinder()
    kinds = {u: (c.kind if (c := f.classify(H(u))) else None) for u in [
        "https://a.example.com/contact", "https://a.example.com/contact-us/", "https://a.example.com/en/about-us", "https://a.example.com/our-team",
        "https://a.example.com/locations", "https://a.example.com/impressum", "https://a.example.com/products/widget", "https://a.example.com/",
        "https://a.example.com/wp-json/contact", "https://a.example.com/static/contact.js", "https://a.example.com/a/b/c/d/e/contact",
        "https://a.example.com/members-directory.pdf", "https://a.example.com/random.pdf", "https://a.example.com/blog/contact-lens-guide"]}
    assert kinds["https://a.example.com/contact"] == "contact" and kinds["https://a.example.com/contact-us/"] == "contact"
    assert kinds["https://a.example.com/en/about-us"] == "about" and kinds["https://a.example.com/our-team"] == "team"
    assert kinds["https://a.example.com/locations"] == "locations" and kinds["https://a.example.com/impressum"] == "legal"
    assert kinds["https://a.example.com/members-directory.pdf"] == "document" and kinds["https://a.example.com/random.pdf"] == "document"
    for none in ("products/widget", "", "wp-json/contact", "static/contact.js", "a/b/c/d/e/contact", "blog/contact-lens-guide"):
        assert kinds["https://a.example.com/" + none] is None, none
    assert f.classify(H("https://a.example.com/contact", status=404)) is None
    assert f.classify(H("https://a.example.com/contact", mime="application/octet-stream")) is None
    assert CommonCrawlContactPageFinder(include_documents=False).classify(H("https://a.example.com/x.pdf", mime="application/pdf")) is None
    assert CommonCrawlContactPageFinder(kinds={"team"}).classify(H("https://a.example.com/contact")) is None


def test_finder_on_parquet_hits_and_dedup(tmp_path):
    p = write_cc_parquet(tmp_path / "i.parquet")
    hits = list(ColumnarIndexReader(p).iter_hits())
    best = CommonCrawlContactPageFinder().best_per_domain(hits, per_domain=2)
    by_dom = {}
    for c in best:
        by_dom.setdefault(c.hit.domain, []).append(c.kind)
    assert by_dom["example.com"] == ["contact", "team"]  # top 2 by score; about/pdf trimmed
    assert by_dom["other.de"] == ["legal"] and by_dom["sample.co.uk"] == ["locations"]
    assert best == CommonCrawlContactPageFinder().best_per_domain(list(reversed(hits)), per_domain=2)  # deterministic regardless of input order
    old, new = H("https://x.example.com/contact", t=datetime(2025, 1, 1, tzinfo=UTC)), H("http://www.x.example.com/contact/", t=datetime(2026, 1, 1, tzinfo=UTC))
    assert CommonCrawlContactPageFinder().best_per_domain([old, new])[0].hit is new


# ------------------------------------------------------------------------------------------------ WARC
def test_parse_warc_member_plain_and_chunked():
    for chunked in (False, True):
        page = parse_warc_record(make_warc_member("https://www.example.com/contact", CC_CONTACT_HTML, chunked=chunked))
        assert page.url == "https://www.example.com/contact" and page.status == 200 and "sales@example-widgets.co.uk" in page.text()
        assert page.fetch_time == datetime(2026, 8, 10, 12, 0, tzinfo=UTC) and page.content_type.startswith("text/html")


def test_parse_warc_rejects_garbage():
    with pytest.raises(ValueError):
        parse_warc_record(gzip.compress(b"not a warc"))


def test_parse_warc_body_cap():
    big = make_warc_member("https://www.example.com/", "<p>" + "x" * 100000 + "</p>")
    page = parse_warc_record(big, max_body=1000)
    assert len(page.body) <= 1000


async def test_fetch_warc_record_uses_range_and_slices_correctly(nosleep):
    m1 = make_warc_member("https://www.example.com/other", "<p>other</p>")
    m2 = make_warc_member("https://www.example.com/contact-us", CC_CONTACT_HTML)
    blob = b"\x00" * 50 + m1 + m2
    off2 = 50 + len(m1)
    seen = []

    def handler(req: httpx.Request):
        seen.append(req.headers.get("range"))
        a, b = req.headers["range"].removeprefix("bytes=").split("-")
        return httpx.Response(206, content=blob[int(a): int(b) + 1])

    hit = UrlHit(url="https://www.example.com/contact-us", domain="example.com", crawl_id="c", fetch_time=None, mime="text/html", status=200,
                 warc_filename="crawl-data/x.warc.gz", warc_offset=off2, warc_length=len(m2))
    page = await fetch_warc_record(hit, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert seen == [f"bytes={off2}-{off2 + len(m2) - 1}"] and "sales@example-widgets.co.uk" in page.text()
    ex = extract_archived_observations(page)
    assert {o.email for o in ex.emails} >= {"sales@example-widgets.co.uk"}
    with pytest.raises(ValueError):
        await fetch_warc_record(UrlHit("u", "d", "c", None, None, 200), client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(ValueError):
        await fetch_warc_record(UrlHit("u", "d", "c", None, None, 200, "f", 0, 99_999_999), client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_fetch_warc_when_server_ignores_range():
    m = make_warc_member("https://www.example.com/contact", CC_CONTACT_HTML)
    blob = b"\x00" * 10 + m
    hit = UrlHit("https://www.example.com/contact", "example.com", "c", None, None, 200, "f.warc.gz", 10, len(m))
    page = await fetch_warc_record(hit, client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=blob))))
    assert page.status == 200


def test_registry_entry_is_info_only():
    a = get_adapter("commoncrawl_archive")
    assert a.info.info_only and a.info.source_type == "web_archive"


async def test_registry_entry_yields_no_businesses():
    with pytest.raises(AdapterUnavailable):
        await collect(get_adapter("commoncrawl_archive"), DiscoveryQuery())
