import json

import httpx

from app.sources.base import DiscoveryQuery, get_adapter
from app.sources.eu_ted import TedAdapter, notice_to_records, winners_of_notice

from .fixtures import ted_notice, ted_notices, ted_pages
from .helpers import assert_resume_no_dup_no_gap, collect


def mock_client(pages, seen=None):
    def handler(req: httpx.Request):
        body = json.loads(req.content)
        if seen is not None:
            seen.append(body)
        tok = body.get("iterationNextToken")
        idx = 0 if not tok else int(tok[3:])
        return httpx.Response(200, json=pages[idx])

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def adapter(pages=None, seen=None, **cfg):
    return TedAdapter({"http_client": mock_client(pages or ted_pages(), seen), "min_interval_s": 0, "sleep": _nosleep, **cfg})


async def _nosleep(_s):
    return None


def test_metadata():
    i = get_adapter("eu_ted").info
    assert "DE" in i.country_coverage and "FR" in i.country_coverage and "US" not in i.country_coverage
    assert i.cost == "free" and i.credentials == "none" and i.access_method == "api" and i.refresh_frequency


def test_winner_inference_rules():
    ns = {n["publication-number"]: n for n in ted_notices()}
    w, inf = winners_of_notice(ns["100001-2026"])
    assert inf == "winner-identifier" and [x["name"] for x in w] == ["Beispiel Bau GmbH"]
    w, inf = winners_of_notice(ns["100002-2026"])
    assert inf == "winner-identifier" and [x["name"] for x in w] == ["Muster AG"] and w[0]["country"] == "CHE" and w[0]["city"] == "Zurich"
    w, inf = winners_of_notice(ns["100003-2026"])
    assert inf == "winner-country-count" and len(w) == 2
    w, inf = winners_of_notice(ns["100004-2026"])
    assert w == [] and inf == "ambiguous"
    assert winners_of_notice(ns["100005-2026"]) == ([], "no_tenderers")


def test_misaligned_attribute_lists_are_not_guessed():
    n = ted_notice("1-2026", names=["A Ltd", "B Ltd"], ids=["X1", "X2"], countries=["FRA"], emails=["only.one@example.com"], winner_ids=["X2"])
    w, _ = winners_of_notice(n)
    assert w[0]["name"] == "B Ltd" and w[0]["country"] is None and w[0]["email"] is None  # shorter lists are ignored, never mis-assigned


async def test_records_and_signals():
    got = await collect(adapter())
    recs = {r.legal_name: r for r, _ in got}
    assert set(recs) == {"Beispiel Bau GmbH", "Muster AG", "Voorbeeld BV", "Esempio SRL"}  # ambiguous + empty notices skipped
    r = recs["Beispiel Bau GmbH"]
    assert r.country_code == "DE" and r.city == "Berlin" and r.email_published == "kontakt@example.com" and r.website == "http://www.example.com"
    assert r.raw["industries"] == ["engineering_architecture"] and r.category_hint == "procurement_winner"
    sig = r.raw["signals"][0]
    assert sig["signal_type"] == "contract_award" and sig["source_url"] == "https://ted.europa.eu/en/notice/-/detail/100001-2026"
    assert sig["detail"]["buyer"] == "Beispiel Behoerde" and sig["occurred_at"].startswith("2026-08-18")
    assert recs["Muster AG"].country_code == "CH" and recs["Muster AG"].raw["industries"] == ["events"]
    assert recs["Voorbeeld BV"].raw["signals"][0]["confidence"] < r.raw["signals"][0]["confidence"]  # inferred winners are less certain
    assert recs["Muster AG"].website is None  # websites only attached for single-winner notices with a single published address


async def test_query_construction_and_country_filter():
    seen = []
    await collect(adapter(seen=seen, days_back=3), DiscoveryQuery(countries=["DE", "FR"], active_only=False))
    q = seen[0]["query"]
    assert "notice-type IN (can-standard can-social can-desg)" in q and "winner-country IN (DEU FRA)" in q and "publication-date>=" in q
    assert seen[0]["paginationMode"] == "ITERATION" and seen[0]["limit"] == 100


async def test_paging_and_resume_across_pages():
    full = await assert_resume_no_dup_no_gap(lambda: adapter(ted_pages(size=2)))
    assert len(full) == 4
    full1 = await assert_resume_no_dup_no_gap(lambda: adapter(ted_pages(size=1)))
    assert [r.source_ref for r, _ in full1] == [r.source_ref for r, _ in full]


async def test_established_filter_over_ted():
    pages = [{"notices": [ted_notice(f"{i}-2026", names=["Repeat Winner SA"], ids=["FR1"], countries=["FRA"], winner_ids=["FR1"],
                                     date=f"2026-0{i}-01+02:00") for i in range(1, 4)]
                       + [ted_notice("9-2026", names=["One Off SA"], ids=["FR2"], countries=["FRA"], winner_ids=["FR2"])],
              "iterationNextToken": None}]
    got = await collect(adapter(pages), DiscoveryQuery(active_only=False, extra={"min_awards": 3}))
    assert [r.legal_name for r, _ in got] == ["Repeat Winner SA"] and got[0][0].raw["award_count"] == 3


def test_notice_without_url_has_no_signal():
    n = ted_notice("5-2026", names=["No Link Ltd"], countries=["IRL"])
    n.pop("links")
    n.pop("publication-number")
    recs, _ = notice_to_records(n)
    assert recs[0].source_url is None and "signals" not in recs[0].raw


async def test_rate_limit_backoff(nosleep):
    n = {"c": 0}

    def handler(req):
        n["c"] += 1
        if n["c"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json=ted_pages()[0] | {"iterationNextToken": None})

    a = TedAdapter({"http_client": httpx.AsyncClient(transport=httpx.MockTransport(handler)), "sleep": nosleep, "min_interval_s": 0})
    assert len(await collect(a)) == 2 and 7.0 in nosleep.calls
