from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.crawler.ratelimit import RateLimiter
from app.crawler.types import EmailObservation
from app.discovery.changes import (
    check_site_for_changes,
    extract_locations,
    extract_members,
    normalised_text,
    page_kind,
    run_monitor,
    schedule_change_checks,
)
from app.models import (
    ContactEvidence,
    EmailAddress,
    EvidenceSource,
    MonitorSchedule,
    Organisation,
    OrganisationDomain,
    SourcingJob,
    SourcingRun,
    WebsiteSnapshot,
)
from app.pipeline.ingest import persist_observations

PAGE_V1 = """<html><head><title>Contact</title><script>var t = 1111;</script><style>.a{}</style></head><body>
<nav><a href="/">Home</a><a href="/news">News 2025</a></nav>
<h1>Contact us</h1><p><a href="mailto:info@acme.co.uk">info@acme.co.uk</a></p><p>Sales: sales@acme.co.uk</p>
<address>1 High Street, Leeds LS1 4AB</address>
<footer>&copy; 2024 Acme Ltd</footer></body></html>"""


def lim():
    return RateLimiter(min_interval=0)


async def setup(session, server, name="Acme Ltd", reg="acme.co.uk", baseline=True, with_run=True):
    o = Organisation(legal_name=name, country_code="GB", canonical_identity=f"GB:nm:{name}")
    session.add(o)
    await session.flush()
    d = OrganisationDomain(organisation_id=o.id, domain=server.host, registrable_domain=reg, website_url=server.url("/"),
                           verified_match_status="confirmed", confidence=0.95)
    session.add(d)
    run = SourcingRun(name="r", status="running")
    session.add(run)
    await session.flush()
    if baseline:  # what an earlier crawl found
        obs = [EmailObservation(email="info@acme.co.uk", method="mailto", page_url=server.url("/contact")),
               EmailObservation(email="sales@acme.co.uk", method="text", page_url=server.url("/contact"))]
        await persist_observations(session, o.id, d, obs, run_id=run.id)
    await session.flush()
    return o, d, run.id


async def snaps(session, domain_id):
    return list((await session.execute(select(WebsiteSnapshot).where(WebsiteSnapshot.domain_id == domain_id)
                                       .order_by(WebsiteSnapshot.id))).scalars())


async def test_baseline_then_304_then_change(session, server, fetcher):
    server.add("/robots.txt", "User-agent: *\nAllow: /\n", ctype="text/plain")
    server.add("/contact", PAGE_V1, etag='"v1"')
    o, d, rid = await setup(session, server)
    rep = await check_site_for_changes(session, fetcher, d, run_id=rid, limiter=lim())
    assert [p.url for p in rep.pages] == [server.url("/contact")] and rep.pages[0].first_observation and not rep.changed
    s1 = (await snaps(session, d.id))[0]
    assert s1.etag == '"v1"' and sorted(s1.emails) == ["info@acme.co.uk", "sales@acme.co.uk"] and s1.page_kind == "contact"
    assert s1.content_hash and s1.emails_hash and not s1.changed
    # ---- 304 path
    rep = await check_site_for_changes(session, fetcher, d, run_id=rid, limiter=lim())
    assert rep.pages[0].not_modified and rep.pages[0].status == 304 and not rep.changed and rep.requests == 1
    hdrs = [h for p, h in server.requests if p == "/contact"][-1]
    assert hdrs.get("if-none-match") == '"v1"'
    all_s = await snaps(session, d.id)
    assert len(all_s) == 2 and all_s[1].content_hash == s1.content_hash and all_s[1].change_summary["diff"]["not_modified"]
    assert all_s[0].id == s1.id and all_s[0].observed_at == s1.observed_at  # prior snapshot untouched
    # ---- content change: email removed + email added + team member + location
    before = (await session.execute(select(EmailAddress).where(EmailAddress.email_normalised == "sales@acme.co.uk"))).scalar_one()
    seen_before = before.last_seen_at
    v2 = PAGE_V1.replace('<p>Sales: sales@acme.co.uk</p>', '<p>New: hello@acme.co.uk</p>').replace(
        "<address>1 High Street, Leeds LS1 4AB</address>", "<address>2 Low Road, York YO1 1AA</address>")
    v2 = v2.replace("</footer>", '</footer><div class="team-member"><h3>Jane Doe</h3><p class="role">Director</p></div>')
    server.add("/contact", v2, etag='"v2"')
    rep = await check_site_for_changes(session, fetcher, d, run_id=rid, limiter=lim())
    pc = rep.pages[0]
    assert pc.content_changed and pc.emails_added == ["hello@acme.co.uk"] and pc.emails_removed == ["sales@acme.co.uk"]
    assert pc.members_added == ["jane doe|director"] and pc.locations_added and pc.locations_removed
    assert rep.changed and rep.emails_added == ["hello@acme.co.uk"] and rep.emails_removed == ["sales@acme.co.uk"]
    # new email persisted with provenance + verify job enqueued
    new = (await session.execute(select(EmailAddress).where(EmailAddress.email_normalised == "hello@acme.co.uk"))).scalar_one()
    assert new.origin == "published"
    ev = (await session.execute(select(EvidenceSource).join(ContactEvidence, ContactEvidence.evidence_id == EvidenceSource.id)
                                .where(ContactEvidence.email_id == new.id))).scalar_one()
    assert ev.source_adapter == "website_changes" and ev.source_url == server.url("/contact")
    jobs = (await session.execute(select(SourcingJob).where(SourcingJob.stage == "verify"))).scalars().all()
    assert [j.payload["email_id"] for j in jobs] == [new.id]
    # removed email: record kept, last_seen_at NOT refreshed, removal evidence added, nothing deleted
    kept = (await session.execute(select(EmailAddress).where(EmailAddress.email_normalised == "sales@acme.co.uk"))).scalar_one()
    assert kept.last_seen_at == seen_before and kept.suppressed_at is None
    rem = (await session.execute(select(ContactEvidence).where(ContactEvidence.email_id == kept.id, ContactEvidence.extraction_method == "removed"))).scalar_one()
    assert rem.context["no_longer_published_on_page"] is True and rem.context["page_url"] == server.url("/contact")
    assert rem.context["still_published_elsewhere_on_site"] is False
    prior_ev = (await session.execute(select(func.count()).select_from(ContactEvidence).where(
        ContactEvidence.email_id == kept.id, ContactEvidence.extraction_method != "removed"))).scalar_one()
    assert prior_ev >= 1  # original observations preserved
    all_s = await snaps(session, d.id)
    assert len(all_s) == 3 and all_s[2].changed and all_s[2].change_summary["diff"]["emails_removed"] == ["sales@acme.co.uk"]
    assert all_s[0].emails == s1.emails
    # idempotent re-check of the same content: unchanged (etag matches v2 now -> 304), no extra verify jobs / removal rows
    await check_site_for_changes(session, fetcher, d, run_id=rid, limiter=lim())
    assert len((await session.execute(select(SourcingJob).where(SourcingJob.stage == "verify"))).scalars().all()) == 1
    assert (await session.execute(select(func.count()).select_from(ContactEvidence).where(ContactEvidence.extraction_method == "removed"))).scalar_one() == 1


async def test_noise_is_ignored(session, server, fetcher):
    server.add("/robots.txt", "User-agent: *\nAllow: /\n", ctype="text/plain")
    server.add("/contact", PAGE_V1)  # no etag -> always a full 200
    o, d, rid = await setup(session, server)
    await check_site_for_changes(session, fetcher, d, run_id=rid, limiter=lim())
    noisy = (PAGE_V1.replace("var t = 1111;", "var t = 99999; track('x');").replace("&copy; 2024", "&copy; 2026")
             .replace('<a href="/news">News 2025</a>', '<a href="/news">News 2026</a><a href="/x">New nav</a>')
             .replace(".a{}", ".b{color:red}"))
    server.add("/contact", noisy)
    rep = await check_site_for_changes(session, fetcher, d, run_id=rid, limiter=lim())
    pc = rep.pages[0]
    assert not pc.not_modified and not pc.content_changed and not rep.changed and not pc.emails_added
    s = await snaps(session, d.id)
    assert len(s) == 2 and s[0].content_hash == s[1].content_hash and not s[1].changed
    # a real text change IS detected
    server.add("/contact", PAGE_V1.replace("Contact us", "Contact our new team"))
    assert (await check_site_for_changes(session, fetcher, d, run_id=rid, limiter=lim())).pages[0].content_changed


async def test_robots_blocks_check(session, server, fetcher):
    server.add("/robots.txt", "User-agent: *\nDisallow: /contact\n", ctype="text/plain")
    server.add("/contact", PAGE_V1)
    o, d, rid = await setup(session, server)
    rep = await check_site_for_changes(session, fetcher, d, run_id=rid, limiter=lim())
    assert rep.skipped and rep.skipped[0]["reason"] == "robots_disallowed" and server.hits("/contact") == 0 and not await snaps(session, d.id)


async def test_error_pages_do_not_write_snapshots(session, server, fetcher):
    server.add("/robots.txt", "User-agent: *\nAllow: /\n", ctype="text/plain")
    server.add("/contact", "gone", status=404)
    o, d, rid = await setup(session, server)
    rep = await check_site_for_changes(session, fetcher, d, run_id=rid, limiter=lim())
    assert rep.pages[0].error and not await snaps(session, d.id)


async def test_schedule_and_monitor(session, server, fetcher):
    server.add("/robots.txt", "User-agent: *\nAllow: /\n", ctype="text/plain")
    server.add("/contact", PAGE_V1)
    server.add("/", "<html><body>home</body></html>")
    o1, d1, rid = await setup(session, server)  # has published emails
    o2 = Organisation(legal_name="NoMail Ltd", country_code="GB", canonical_identity="GB:nm:nomail")
    o3 = Organisation(legal_name="Fresh Ltd", country_code="GB", canonical_identity="GB:nm:fresh")
    o4 = Organisation(legal_name="Unconfirmed Ltd", country_code="GB", canonical_identity="GB:nm:unconf")
    session.add_all([o2, o3, o4])
    await session.flush()
    d2 = OrganisationDomain(organisation_id=o2.id, domain="nomail.co.uk", registrable_domain="nomail.co.uk", verified_match_status="high")
    d3 = OrganisationDomain(organisation_id=o3.id, domain="fresh.co.uk", registrable_domain="fresh.co.uk", verified_match_status="confirmed")
    d4 = OrganisationDomain(organisation_id=o4.id, domain="unconf.co.uk", registrable_domain="unconf.co.uk", verified_match_status="review")
    session.add_all([d2, d3, d4])
    await session.flush()
    session.add(WebsiteSnapshot(domain_id=d3.id, url="https://fresh.co.uk/", page_kind="home", content_hash="x", observed_at=datetime.now(UTC)))
    session.add(WebsiteSnapshot(domain_id=d2.id, url="https://nomail.co.uk/", page_kind="home", content_hash="x",
                                observed_at=datetime.now(UTC) - timedelta(days=60)))
    await session.flush()
    due = await schedule_change_checks(session, older_than_days=30, limit=10)
    assert due == [d1.id, d2.id]  # published-email domain first; fresh and unconfirmed excluded
    assert await schedule_change_checks(session, older_than_days=30, limit=1) == [d1.id]
    qd = await schedule_change_checks(session, older_than_days=30, limit=10, run_id=rid)
    await schedule_change_checks(session, older_than_days=30, limit=10, run_id=rid)  # idempotent per day
    jobs = (await session.execute(select(SourcingJob).where(SourcingJob.stage == "change_check"))).scalars().all()
    assert sorted(j.payload["domain_id"] for j in jobs) == sorted(qd)
    # monitor: runs the check inline for the due domains that are reachable
    mon = MonitorSchedule(kind="website_changes", name="w", frequency="daily", config={"limit": 1})
    session.add(mon)
    await session.flush()
    stats = await run_monitor(session, fetcher, mon, limiter=lim())
    assert stats["domains"] == 1 and stats["pages"] == 1 and stats["requests"] == 1
    assert len(await snaps(session, d1.id)) == 1


def test_helpers():
    assert page_kind("https://a.co.uk/contact-us") == "contact" and page_kind("https://a.co.uk/") == "home"
    assert page_kind("https://a.co.uk/our-team") == "team" and page_kind("https://a.co.uk/x") == "other"
    a = normalised_text("<html><body><script>1</script><nav>menu</nav><p>Hello  World 12:30</p>&copy; 2020 X</body></html>")
    b = normalised_text("<html><body><p>Hello World 09:15</p>&copy; 2026 X<style>x</style></body></html>")
    assert a == b == "hello world x"
    assert extract_members('<div class="team"><h3>Ann Smith</h3><p>CEO</p></div><div class="team"><h3>Our values</h3></div>') == ["ann smith|ceo"]
    assert extract_locations("<address>1 A St,   Leeds</address>") == ["1 a st, leeds"]
