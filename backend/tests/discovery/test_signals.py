from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.discovery.signals import (
    SIGNAL_TYPES,
    dedupe_hash,
    detect_and_record,
    detect_signals_from_html,
    is_fresh,
    parse_date,
    query_signals,
    record_signal,
    run_monitor,
)
from app.models import BusinessSignal, MonitorSchedule, Organisation, OrganisationDomain

NOW = datetime(2026, 6, 1, tzinfo=UTC)


async def mk_org(session, name="Acme Ltd", cc="GB"):
    o = Organisation(legal_name=name, country_code=cc, canonical_identity=f"{cc}:nm:{name}")
    session.add(o)
    await session.flush()
    return o


def test_parse_date_formats():
    assert parse_date("2026-03-04T10:30:00Z") == datetime(2026, 3, 4, 10, 30, tzinfo=UTC)
    assert parse_date("2026-03-04T10:30:00+01:00") == datetime(2026, 3, 4, 9, 30, tzinfo=UTC)
    assert parse_date("Posted 4th March 2026") == datetime(2026, 3, 4, tzinfo=UTC)
    assert parse_date("Mar 4, 2026") == datetime(2026, 3, 4, tzinfo=UTC)
    assert parse_date("Sept 9 2025") == datetime(2025, 9, 9, tzinfo=UTC)
    assert parse_date("04/03/2026") == datetime(2026, 3, 4, tzinfo=UTC)  # UK day-first
    assert parse_date("25/12/2025") == datetime(2025, 12, 25, tzinfo=UTC)
    assert parse_date("12/25/2025") == datetime(2025, 12, 25, tzinfo=UTC)  # unambiguous month-first
    assert parse_date("31 February 2026") is None and parse_date("1850-01-01") is None and parse_date("no date") is None
    assert parse_date("2099-01-01", now=NOW) is None


JOBS_LD = """<html><head><script type="application/ld+json">
{"@context":"https://schema.org","@graph":[
 {"@type":"JobPosting","title":"Gas Engineer","datePosted":"2026-05-20","validThrough":"2026-12-31"},
 {"@type":"JobPosting","title":"Old Role","datePosted":"2025-01-01","validThrough":"2025-02-01"},
 {"@type":"Event","name":"Open Day","startDate":"2026-06-20T10:00:00Z"}]}
</script></head><body>x</body></html>"""

NEWS = """<html><body><h1>News</h1>
<article><h2>We are opening a new branch in Leeds</h2><time datetime="2026-05-15">15 May 2026</time><p>Our new showroom opens.</p></article>
<article><h2>Launching our boiler servicing plans</h2><p>Published 2 April 2026. We introduce a new service range.</p></article>
<article><h2>Company picnic</h2><time datetime="2026-05-01">1 May</time></article>
<article><h2>Great expansion plans for the team</h2><p>No date anywhere</p></article>
<article><h2>Very old news: acquires rival</h2><time datetime="2020-01-01">x</time></article>
</body></html>"""


def test_jsonld_signals_and_confidence():
    sigs = detect_signals_from_html(JOBS_LD, "https://acme.example.com/careers", None, now=NOW)
    by = {(s.signal_type, s.title): s for s in sigs}
    hire = by[("hiring", "Hiring: Gas Engineer")]
    assert hire.confidence == 0.9 and hire.occurred_at == datetime(2026, 5, 20, tzinfo=UTC)
    assert ("hiring", "Hiring: Old Role") not in by  # expired posting
    ev = by[("event", "Event: Open Day")]
    assert ev.confidence == 0.9 and ev.occurred_at.day == 20
    assert hire.detail["parser_version"] and hire.source_url == "https://acme.example.com/careers"


def test_news_keyword_rules_and_confidence_levels():
    sigs = {s.title: s for s in detect_signals_from_html(NEWS, "https://acme.example.com/news", None, now=NOW)}
    loc = sigs["We are opening a new branch in Leeds"]
    assert loc.signal_type == "new_location" and loc.confidence == 0.7 and loc.occurred_at == datetime(2026, 5, 15, tzinfo=UTC)
    svc = sigs["Launching our boiler servicing plans"]
    assert svc.signal_type == "new_service" and svc.confidence == 0.6 and svc.occurred_at == datetime(2026, 4, 2, tzinfo=UTC)
    exp = sigs["Great expansion plans for the team"]
    assert exp.signal_type == "expansion" and exp.confidence == 0.35 and exp.occurred_at is None
    assert "Company picnic" not in sigs  # no keyword rule
    assert loc.confidence > svc.confidence > exp.confidence
    fresh = detect_signals_from_html(NEWS, "https://acme.example.com/news", None, now=NOW, freshness_days=90)
    titles = {s.title for s in fresh}
    assert "Very old news: acquires rival" not in titles and "Great expansion plans for the team" in titles  # undated kept
    stale = detect_signals_from_html(NEWS, "https://acme.example.com/news", None, now=NOW)
    assert "Very old news: acquires rival" in {s.title for s in stale}
    only = detect_signals_from_html(NEWS, "https://acme.example.com/news", None, now=NOW, types=["expansion"])
    assert {s.signal_type for s in only} == {"expansion"}


def test_careers_page_and_accreditations():
    html = ("<html><body><h1>Careers</h1><p>We're hiring! Apply now for full-time roles. Salary £30k.</p>"
            "<p>We are Gas Safe registered and ISO 9001 certified. NICEIC approved contractor.</p></body></html>")
    sigs = detect_signals_from_html(html, "https://acme.example.com/careers", None, now=NOW)
    kinds = {(s.signal_type, s.detail.get("accreditation")) for s in sigs}
    assert ("hiring", None) in kinds
    assert {("accreditation", "Gas Safe"), ("accreditation", "ISO 9001"), ("accreditation", "NICEIC")} <= kinds
    hire = next(s for s in sigs if s.signal_type == "hiring")
    assert hire.confidence == 0.55
    acc = next(s for s in sigs if s.signal_type == "accreditation")
    assert acc.confidence <= 0.55 < 0.8
    # a bare mention without an accreditation cue is not a signal
    assert not detect_signals_from_html("<html><body><p>Our van is a Gas Safe sticker colour</p></body></html>", "https://a.example.com/", None)


async def test_record_signal_validation_and_dedupe(session):
    o = await mk_org(session)
    with pytest.raises(ValueError):
        await record_signal(session, o.id, "hiring", "t", "x", "", None, 0.5)
    with pytest.raises(ValueError):
        await record_signal(session, o.id, "hiring", "t", "x", "ftp://nope", None, 0.5)
    with pytest.raises(ValueError):
        await record_signal(session, o.id, "not_a_type", "t", "x", "https://a.example.com/", None, 0.5)
    when = datetime(2026, 5, 1, tzinfo=UTC)
    i1, c1 = await record_signal(session, o.id, "hiring", "Hiring: Engineer", "w", "https://a.example.com/jobs", when, 0.5, {"k": 1})
    i2, c2 = await record_signal(session, o.id, "hiring", "hiring:   engineer!", "w", "https://a.example.com/other", when, 0.8, {"k2": 2})
    assert c1 and not c2 and i1 == i2
    row = (await session.execute(select(BusinessSignal))).scalar_one()
    assert row.confidence == 0.8 and row.detail["k"] == 1 and row.detail["k2"] == 2 and row.detail["parser_version"]
    i3, c3 = await record_signal(session, o.id, "hiring", "Hiring: Engineer", "w", "https://a.example.com/jobs", when + timedelta(days=3), 0.5)
    assert c3 and i3 != i1  # different day = different signal
    assert dedupe_hash(1, "hiring", "A b", None) == dedupe_hash(1, "hiring", "a-b", None)
    assert set(SIGNAL_TYPES) >= {"new_location", "sponsor", "exhibitor", "new_registration"}


async def test_query_signals_keyset_and_filters(session):
    a, b = await mk_org(session, "A Ltd"), await mk_org(session, "B Ltd", "IE")
    base = datetime.now(UTC)
    for i in range(7):
        await record_signal(session, a.id if i % 2 else b.id, "event" if i % 3 else "hiring", f"sig {i}", "x",
                            f"https://x.example.com/{i}", base - timedelta(days=i * 40), 0.3 + i / 20)
    seen, cur = [], None
    while True:
        page = await query_signals(session, {}, cur, limit=3)
        seen += [i["title"] for i in page["items"]]
        cur = page["next_cursor"]
        if not cur:
            break
    assert seen == [f"sig {i}" for i in range(7)]  # newest first, no gaps, no duplicates
    assert [i["title"] for i in (await query_signals(session, {"types": ["hiring"]}))["items"]] == ["sig 0", "sig 3", "sig 6"]
    assert {i["country_code"] for i in (await query_signals(session, {"country_code": "ie"}))["items"]} == {"IE"}
    fresh = await query_signals(session, {"freshness_days": 100})
    assert [i["title"] for i in fresh["items"]] == ["sig 0", "sig 1", "sig 2"]
    assert all(i["confidence"] >= 0.5 for i in (await query_signals(session, {"min_confidence": 0.5}))["items"])
    assert (await query_signals(session, {"org_id": a.id}))["items"][0]["organisation"] == "A Ltd"


async def test_detect_and_record_and_monitor(session, server, fetcher):
    o = await mk_org(session)
    ids = await detect_and_record(session, o, "https://acme.example.com/news", NEWS, now=NOW, freshness_days=100000)
    assert ids and all(c for _i, c in ids)
    again = await detect_and_record(session, o, "https://acme.example.com/news", NEWS, now=NOW, freshness_days=100000)
    assert not any(c for _i, c in again)
    # monitor over a local site
    server.add("/robots.txt", "User-agent: *\nAllow: /\n", ctype="text/plain")
    server.add("/", '<html><body><a href="/careers">Careers</a><a href="/news">News</a></body></html>')
    server.add("/careers", JOBS_LD)
    server.add("/news", NEWS)
    o2 = await mk_org(session, "Local Co")
    session.add(OrganisationDomain(organisation_id=o2.id, domain=server.host, registrable_domain="localhost",
                                   website_url=server.url("/"), verified_match_status="confirmed", confidence=0.9))
    mon = MonitorSchedule(kind="signals", name="s", frequency="weekly", config={"limit": 5, "freshness_days": 100000})
    session.add(mon)
    await session.flush()
    from app.crawler.ratelimit import RateLimiter
    stats = await run_monitor(session, fetcher, mon, limiter=RateLimiter(min_interval=0))
    assert stats["orgs"] == 1 and stats["pages"] == 3 and stats["signals_new"] >= 3
    got = await query_signals(session, {"org_id": o2.id})
    assert {i["signal_type"] for i in got["items"]} >= {"hiring", "new_location"}
    assert all(i["source_url"].startswith("http://127.0.0.1") for i in got["items"])


def test_is_fresh():
    assert is_fresh(None, 10) and is_fresh(NOW - timedelta(days=5), 10, NOW) and not is_fresh(NOW - timedelta(days=50), 10, NOW)
