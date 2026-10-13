import json

import httpx
import pytest

from app.sources.base import AdapterError, DiscoveryQuery, NotConfigured, get_adapter
from app.sources.search_providers import (
    AGGREGATOR_DOMAINS,
    BraveSearchProvider,
    CommonCrawlIndexProvider,
    ManualSearchResultsImport,
    QuotaExhausted,
    SearchHit,
    SearxngProvider,
    resolve_official_websites,
)


def client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------- resolver
def test_resolver_drops_aggregators_social_marketplaces():
    hits = [
        SearchHit("https://www.yell.com/biz/fixture-agents-manchester/", "Fixture Agents - Yell", rank=1),
        SearchHit("https://www.facebook.com/fixtureagents", "FB", rank=2),
        SearchHit("https://uk.linkedin.com/company/fixture", "LI", rank=3),
        SearchHit("https://www.rightmove.co.uk/estate-agents/x.html", "RM", rank=4),
        SearchHit("https://www.ebay.co.uk/itm/1", "eBay", rank=5),
        SearchHit("https://find-and-update.company-information.service.gov.uk/company/1", "CH", rank=6),
        SearchHit("https://www.fixture-agents.example.com/contact", "Fixture Agents Manchester | Estate Agents", rank=7),
        SearchHit("https://fixture-agents.example.com/about", "About", rank=9),
        SearchHit("https://council.example.gov.uk/x", "x", rank=8),
        SearchHit("not a url", "", rank=10),
        SearchHit("javascript:alert(1)", "", rank=11),
    ]
    cands, dropped = resolve_official_websites(hits, business_name="Fixture Agents Ltd", locality="Manchester")
    assert [c.registrable for c in cands] == ["example.com"]
    c = cands[0]
    assert c.host.endswith("fixture-agents.example.com") and c.hits == 2 and c.status == "candidate"
    assert c.status != "confirmed" and 0 < c.score <= 1
    reasons = {d["reason"] for d in dropped}
    assert {"aggregator_or_directory", "invalid_url"} <= reasons and reasons & {"public_sector", "aggregator_subdomain"}
    assert len(dropped) == 9


def test_resolver_multi_tenant_hosts_not_collapsed():
    hits = [SearchHit("https://alpha.wixsite.com/site", rank=1), SearchHit("https://beta.wixsite.com/site", rank=2)]
    cands, _ = resolve_official_websites(hits)
    assert len(cands) == 2


def test_blocklist_contents():
    for d in ("yell.com", "thomsonlocal.com", "checkatrade.com", "facebook.com", "linkedin.com", "ebay.co.uk", "tripadvisor.com"):
        assert d in AGGREGATOR_DOMAINS


# ---------------------------------------------------------------- manual
async def test_manual_import(tmp_path):
    p = tmp_path / "res.csv"
    p.write_text("query,url,title,rank\nestate agents manchester,https://a.example.com,A,2\nestate agents manchester,https://b.example.com,B,1\n"
                 "plumbers leeds,https://c.example.com,C,1\n=cmd,=evil,x,1\n", encoding="utf-8")
    prov = ManualSearchResultsImport({"path": str(p)})
    hits = await prov.search("Estate Agents  MANCHESTER")
    assert [h.url for h in hits] == ["https://b.example.com", "https://a.example.com"]
    assert await prov.search("unknown") == []
    assert (await ManualSearchResultsImport({}).health()).status == "not_configured"


async def test_manual_adapter_end_to_end_and_resume(tmp_path, monkeypatch):
    from app.sources import search_providers

    # all synthetic fixtures live under example.com; treat it as multi-tenant so each host stays distinct
    monkeypatch.setattr(search_providers, "MULTI_TENANT_DOMAINS", search_providers.MULTI_TENANT_DOMAINS | {"example.com"})
    p = tmp_path / "res.csv"
    rows = ["query,url,title"] + [f"estate agents leeds,https://fixture-{i}.example.com,Fixture {i}" for i in range(5)]
    rows.append("estate agents leeds,https://www.yell.com/x,Yell")
    p.write_text("\n".join(rows))
    a = get_adapter("manual_search_results", {"path": str(p), "country_code": "GB"})
    q = DiscoveryQuery(keywords=["estate agents"], localities=["Leeds"])
    got = [(r, c) async for r, c in a.iter_businesses(q)]
    assert len(got) == 5
    r = got[0][0]
    assert r.raw["candidate_status"] == "unconfirmed" and r.website.startswith("https://fixture-0") and r.city == "Leeds"
    rest = [r async for r, _ in a.iter_businesses(q, got[2][1])]
    assert [x.source_ref for x in rest] == [g[0].source_ref for g in got[3:]]


# ---------------------------------------------------------------- searxng
async def test_searxng_requires_operator_authorisation():
    h = await SearxngProvider({"base_url": "http://searx.internal"}).health()
    assert h.status == "not_configured" and "authorised" in h.message
    assert (await SearxngProvider({}).health()).status == "not_configured"
    with pytest.raises(NotConfigured):
        await SearxngProvider({}).search("x")


async def test_searxng_json_api():
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        return httpx.Response(200, json={"results": [{"url": "https://a.example.com", "title": "A", "content": "snip"},
                                                     {"url": "https://b.example.com", "title": "B"}]})

    async with client(handler) as c:
        p = SearxngProvider({"base_url": "http://searx.internal:8080/", "authorised": True, "http_client": c})
        hits = await p.search("plumbers leeds", country="GB", count=10)
    assert "format=json" in seen["url"] and seen["url"].startswith("http://searx.internal:8080/search")
    assert [h.url for h in hits] == ["https://a.example.com", "https://b.example.com"] and hits[0].snippet == "snip"


async def test_searxng_429_and_json_disabled():
    async with client(lambda r: httpx.Response(429)) as c:
        with pytest.raises(QuotaExhausted):
            await SearxngProvider({"base_url": "http://s", "authorised": True, "http_client": c}).search("x")
    async with client(lambda r: httpx.Response(403)) as c:
        with pytest.raises(AdapterError, match="JSON"):
            await SearxngProvider({"base_url": "http://s", "authorised": True, "http_client": c}).search("x")


# ---------------------------------------------------------------- brave
async def test_brave_not_configured_and_headers():
    h = await BraveSearchProvider({}).health()
    assert h.status == "not_configured" and "not configured" in h.message
    seen = {}

    def handler(req):
        seen["token"] = req.headers.get("x-subscription-token")
        seen["url"] = str(req.url)
        return httpx.Response(200, json={"web": {"results": [{"url": "https://a.example.com", "title": "A", "description": "d"}]}},
                              headers={"x-ratelimit-remaining": "1, 1999", "x-ratelimit-limit": "1, 2000"})

    async with client(handler) as c:
        p = BraveSearchProvider({"api_key": "KEY", "http_client": c})
        hits = await p.search("plumbers leeds", country="gb")
    assert seen["token"] == "KEY" and "country=GB" in seen["url"]
    assert hits[0].snippet == "d" and p.remaining == [1, 1999] and not p.exhausted


async def test_brave_stops_on_429_without_retry():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, headers={"retry-after": "1"})

    async with client(handler) as c:
        p = BraveSearchProvider({"api_key": "K", "http_client": c})
        with pytest.raises(QuotaExhausted):
            await p.search("x")
        with pytest.raises(QuotaExhausted):
            await p.search("y")  # latched: no further HTTP request at all
    assert len(calls) == 1 and p.stop_reason == "quota_exhausted"


async def test_brave_exhausted_window_latches_after_response():
    def handler(req):
        return httpx.Response(200, json={"web": {"results": []}}, headers={"x-ratelimit-remaining": "0, 1500"})

    async with client(handler) as c:
        p = BraveSearchProvider({"api_key": "K", "http_client": c})
        await p.search("x")
        assert p.exhausted
        with pytest.raises(QuotaExhausted):
            await p.search("y")


async def test_brave_run_request_cap():
    async with client(lambda r: httpx.Response(200, json={"web": {"results": []}})) as c:
        p = BraveSearchProvider({"api_key": "K", "http_client": c, "max_requests": 2})
        await p.search("a")
        await p.search("b")
        with pytest.raises(QuotaExhausted):
            await p.search("c")


async def test_brave_adapter_stops_gracefully_on_quota():
    state = {"n": 0}

    def handler(req):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(200, json={"web": {"results": [{"url": "https://fixture-1.example.com", "title": "t"}]}})
        return httpx.Response(429)

    async with client(handler) as c:
        a = get_adapter("brave_search", {"api_key": "K", "http_client": c, "country_code": "GB"})
        got = [r async for r, _ in a.iter_businesses(DiscoveryQuery(keywords=["a", "b", "c"]))]
    assert len(got) == 1 and a.stop_reason == "quota_exhausted" and state["n"] == 2  # stopped, no retries


# ---------------------------------------------------------------- common crawl
async def test_commoncrawl_cdx():
    seen = []

    def handler(req):
        seen.append(str(req.url))
        if req.url.path.endswith("collinfo.json"):
            return httpx.Response(200, json=[{"id": "CC-MAIN-2026-05"}])
        lines = "\n".join(json.dumps({"url": f"https://fixture-cc.example.com/p{i}", "timestamp": "20260101000000"}) for i in range(3))
        return httpx.Response(200, text=lines)

    async with client(handler) as c:
        p = CommonCrawlIndexProvider({"http_client": c})
        hits = await p.search("fixture-cc.example.com", count=2)
    assert len(hits) == 2 and "CC-MAIN-2026-05-index" in seen[1] and "url=fixture-cc.example.com%2F%2A" in seen[1]
    with pytest.raises(AdapterError):
        await CommonCrawlIndexProvider({"http_client": c}).search("free text query")


async def test_commoncrawl_404_empty_and_503_stop():
    async with client(lambda r: httpx.Response(200, json=[{"id": "X"}]) if r.url.path.endswith("json") else httpx.Response(404)) as c:
        assert await CommonCrawlIndexProvider({"http_client": c}).search("fixture.example.com") == []
    async with client(lambda r: httpx.Response(200, json=[{"id": "X"}]) if r.url.path.endswith("json") else httpx.Response(503)) as c:
        with pytest.raises(QuotaExhausted):
            await CommonCrawlIndexProvider({"http_client": c}).search("fixture.example.com")


def test_adapter_infos_declare_url_discovery_and_disabled():
    for aid in ("searxng", "brave_search", "commoncrawl_index", "manual_search_results"):
        i = get_adapter(aid).info
        assert i.requires_config
    assert "URL discovery" in get_adapter("commoncrawl_index").info.notes
