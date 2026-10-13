import httpx
import pytest

from app.sources.base import AdapterError, DiscoveryQuery, NotConfigured
from app.sources.wikidata import WikidataAdapter, binding_to_record, build_query


def binding(i, label=None, site="https://fixture-wd.example.com", hq="Fixture City", coord="Point(-2.24 53.48)", email=None):
    b = {"item": {"value": f"http://www.wikidata.org/entity/Q{i}"}, "label": {"value": label or f"Fixture Org {i}"},
         "website": {"value": f"{site}/{i}"}, "hq": {"value": hq}, "coords": {"value": coord}}
    if email:
        b["email"] = {"value": email}
    return b


def mk_transport(pages, log):
    def handler(req: httpx.Request) -> httpx.Response:
        q = req.url.params["query"]
        log.append((q, req.headers.get("user-agent")))
        import re

        off = int(re.search(r"OFFSET (\d+)", q).group(1))
        lim = int(re.search(r"LIMIT (\d+)", q).group(1))
        rows = pages[off: off + lim]
        return httpx.Response(200, json={"results": {"bindings": rows}})

    return httpx.MockTransport(handler)


async def collect(a, q, cp=None, stop_after=None):
    out = []
    async for r, c in a.iter_businesses(q, cp):
        out.append((r, c))
        if stop_after and len(out) >= stop_after:
            break
    return out


def cfg(client, **kw):
    return {"http_client": client, "user_agent": "FixtureBot/1.0 (+contact: test@example.com)", "min_interval_s": 0, "page_size": 3, **kw}


async def test_pagination_mapping_and_ua():
    rows = [binding(i, email="mailto:Info@Fixture-wd.example.com" if i == 1 else None) for i in range(1, 8)]
    log: list = []
    async with httpx.AsyncClient(transport=mk_transport(rows, log)) as c:
        got = await collect(WikidataAdapter(cfg(c)), DiscoveryQuery(countries=["GB"]))
    assert [r.source_ref for r, _ in got] == [f"Q{i}" for i in range(1, 8)]
    assert len(log) == 3 and all(ua.startswith("FixtureBot") for _, ua in log)
    r = got[0][0]
    assert r.licence == "CC0 1.0" and r.trading_name == "Fixture Org 1" and r.city == "Fixture City"
    assert r.website == "https://fixture-wd.example.com/1"
    assert r.email_published == "info@fixture-wd.example.com"
    assert abs(r.lat - 53.48) < 1e-6 and abs(r.lon + 2.24) < 1e-6
    assert r.source_url == "https://www.wikidata.org/wiki/Q1"


async def test_checkpoint_resume_across_pages():
    rows = [binding(i) for i in range(1, 11)]
    async with httpx.AsyncClient(transport=mk_transport(rows, [])) as c:
        a = WikidataAdapter(cfg(c))
        q = DiscoveryQuery(countries=["GB"])
        full = [r.source_ref for r, _ in await collect(a, q)]
        for k in (1, 3, 4, 9):
            first = await collect(a, q, stop_after=k)
            rest = await collect(a, q, cp=first[-1][1])
            assert [r.source_ref for r, _ in first] + [r.source_ref for r, _ in rest] == full


async def test_backoff_then_stop_on_persistent_429():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, headers={"retry-after": "0"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(AdapterError, match="back off"):
            await collect(WikidataAdapter(cfg(c, max_waits=2)), DiscoveryQuery(countries=["GB"]))
    assert len(calls) == 3  # 1 try + 2 honoured waits, then stop


async def test_requires_contact_and_country(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "contact_email", "")
    a = WikidataAdapter({})
    assert (await a.health()).status == "not_configured"
    with pytest.raises(NotConfigured):
        await collect(a, DiscoveryQuery(countries=["GB"]))
    with pytest.raises(NotConfigured):
        await collect(WikidataAdapter({"user_agent": "x/1 (+a@b.c)"}), DiscoveryQuery())


def test_query_building_is_injection_safe():
    q = build_query("GB", ["Q4830453", "Q783794"], limit=10, offset=20)
    assert 'wdt:P297 "GB"' in q and "wd:Q4830453 wd:Q783794" in q and "LIMIT 10 OFFSET 20" in q
    assert "FILTER NOT EXISTS" in q
    assert "FILTER NOT EXISTS" not in build_query("GB", ["Q1"], limit=1, offset=0, active_only=False)
    with pytest.raises(ValueError):
        build_query('GB" } DROP', ["Q1"], limit=1, offset=0)
    with pytest.raises(ValueError):
        build_query("GB", ["Q1 } SERVICE <http://evil/>"], limit=1, offset=0)


def test_binding_rejects_bad_rows():
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    assert binding_to_record(binding(1, site="javascript:alert(1)"), "GB", now) is None
    assert binding_to_record({"item": {"value": "http://x/notaqid"}, "label": {"value": "a"}}, "GB", now) is None
    assert binding_to_record(binding(2, email="not-an-email"), "GB", now).email_published is None
