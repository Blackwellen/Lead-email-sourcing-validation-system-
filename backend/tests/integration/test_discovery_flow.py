"""(a) public dataset -> company match -> official domain -> crawl -> published emails -> verify -> dedupe -> attributed export, with the
discovery stages enabled (signals, documents, network expansion, Common Crawl historical, gap recovery, completeness)."""
import csv
import io
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app import exports
from app.discovery import effectiveness as eff
from app.exports import ExportFilters
from app.models import User
from app.pipeline import discovery_stages
from app.run_config import RunConfig
from app.sources.commoncrawl import UrlHit, parse_warc_record

from .helpers import make_run, patch_network, q, run_until_done, scalar

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from discovery_sources.fixtures import make_warc_member  # noqa: E402
from sources.fixtures import ch_row, write_ch_zip  # noqa: E402

pytestmark = pytest.mark.asyncio
H1, H2 = "fixtureestateagents.co.uk", "partnerwidgets.co.uk"

HOME = """<html><head><title>Fixture Estate Agents Ltd</title>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"JobPosting","title":"Lettings Negotiator","datePosted":"2026-09-20","validThrough":"2027-12-31"}</script>
</head><body><h1>Fixture Estate Agents Ltd</h1><p>Manchester lettings.</p>
<footer>Fixture Estate Agents Ltd. Registered in England and Wales No. 99999991. M1 1AA</footer>
<a href="/contact">Contact</a> <a href="/partners">Partners</a> <a href="/files/staff.csv">Staff directory</a></body></html>"""
CONTACT = """<html><body><h1>Contact</h1><p>Email <a href="mailto:info@fixtureestateagents.co.uk">info@fixtureestateagents.co.uk</a></p>
<div class="team"><h3>Jane Example</h3><p>Director</p><a href="mailto:jane@fixtureestateagents.co.uk">jane@fixtureestateagents.co.uk</a></div>
<footer>Registered in England and Wales No. 99999991</footer></body></html>"""
PARTNERS = f"""<html><body><h1>Our partners</h1><div class="partners"><ul><li><a href="http://{H2}/">Partner Widgets Ltd</a></li></ul></div></body></html>"""
STAFF_CSV = "name,email,role\nSam Sample,sam@fixtureestateagents.co.uk,Lettings manager\n"
PARTNER_HOME = "<html><head><title>Partner Widgets Ltd</title></head><body><h1>Partner Widgets Ltd</h1><p>Widgets for everyone.</p></body></html>"
ARCHIVED = """<html><body><h1>Contact</h1><a href="mailto:info@fixtureestateagents.co.uk">info</a>
<a href="mailto:old.manager@fixtureestateagents.co.uk">Old Manager</a></body></html>"""


async def test_full_discovery_flow(tmp_path, web, monkeypatch, session):
    web.pages[H1] = {"/": HOME, "/contact": CONTACT, "/partners": PARTNERS, "/files/staff.csv": (200, STAFF_CSV, "text/csv"), "/robots.txt": (404, "")}
    web.pages[H2] = {"/": PARTNER_HOME, "/robots.txt": (404, "")}
    await patch_network(monkeypatch, web, [H1, H2])

    hit = UrlHit(url=f"http://{H1}/contact", domain=H1, crawl_id="CC-MAIN-2026-33", fetch_time=datetime(2026, 8, 1, tzinfo=UTC), mime="text/html", status=200,
                 warc_filename="crawl-data/x.warc.gz", warc_offset=0, warc_length=100)

    async def cc_hits(domain, params, max_hits):
        return [hit] if domain == H1 else []

    async def cc_fetch(h):
        return parse_warc_record(make_warc_member(h.url, ARCHIVED, warc_date="2026-08-01T00:00:00Z"))

    monkeypatch.setattr(discovery_stages, "CC_HITS", cc_hits)
    monkeypatch.setattr(discovery_stages, "CC_FETCH", cc_fetch)

    rows = [
        ch_row("FIXTURE ESTATE AGENTS LTD", "99999991", town="MANCHESTER", postcode="M1 1AA", sics=["68310 - Real estate agencies"]),
        ch_row("FIXTURE NOWEBSITE LETTINGS LTD", "99999992", town="MANCHESTER", postcode="M2 2BB", sics=["68310 - Real estate agencies"]),
        ch_row("FIXTURE ESTATE AGENTS LTD", "99999991", town="MANCHESTER", postcode="M1 1AA", sics=["68310 - Real estate agencies"]),  # duplicate row
    ]
    zpath = write_ch_zip(tmp_path / "ch.zip", rows)
    cfg = RunConfig(
        name="int-a", countries=["GB"], sources=["companies_house_bulk"], source_params={"companies_house_bulk": {"path": str(zpath)}},
        target={"count": 1000, "min_confidence": 30},
        discovery={"strategy": "balanced", "source_categories": ["bulk_open_data", "company_websites", "documents", "common_crawl"],
                   "network": {"enabled": True, "max_depth": 1, "source_budget": 20}, "signals": {"enabled": True}},
    )
    rid = await make_run(cfg)
    assert await run_until_done(rid, timeout=150) == "completed"

    # ---- company matching: the duplicate dataset row never became a second business, nor a second crawl
    assert await scalar("select count(*) from organisations where discovery_source='companies_house_bulk'") == 2
    assert await scalar("select count(*) from sourcing_jobs where run_id=:r and stage='resolve_domain' and payload->>'source_adapter'='companies_house_bulk'", r=rid) == 2
    assert web.hits_for(H1, "/contact") == 1, web.hits

    # ---- measured source effectiveness (counted, never invented)
    ms = await eff.metrics_by_adapter(None) if False else None  # noqa: F841
    row = (await q("select records_seen, unique_businesses, duplicates, http_requests, jobs, failures, wall_seconds from source_effectiveness "
                   "where adapter_id='companies_house_bulk' and run_id=:r", r=rid))[0]
    assert (row.records_seen, row.unique_businesses, row.duplicates) == (3, 2, 1)
    assert row.http_requests > 0 and row.jobs >= 3 and row.failures == 0 and row.wall_seconds > 0
    assert (await q("select eligible_contacts, published_emails, confirmed_domains from source_effectiveness where adapter_id='companies_house_bulk' and run_id=:r", r=rid))[0].eligible_contacts >= 2
    derived = eff.derive({"records_seen": 3, "duplicates": 1})
    assert derived["duplicate_pct"] is None  # 3 samples are not enough to quote a percentage

    # ---- emails: crawled + document + historical
    emails = {r.email_normalised: r for r in await q("select email_normalised, origin, needs_review from email_addresses")}
    assert {"info@fixtureestateagents.co.uk", "jane@fixtureestateagents.co.uk", "sam@fixtureestateagents.co.uk", "old.manager@fixtureestateagents.co.uk"} <= set(emails)
    assert await scalar("select count(*) from document_records") >= 1
    # ---- signals persisted with their source URL
    sig = await q("select signal_type, source_url, confidence from business_signals")
    assert sig and all(s.source_url.startswith(f"http://{H1}") for s in sig) and any(s.signal_type == "hiring" for s in sig)
    # ---- Common Crawl: archived-only contact is stored but NOT counted / exported; the one also seen live is counted
    yielded = {r[0] for r in await q("select e.email_normalised from run_yield y join email_addresses e on e.id=y.email_id where y.run_id=:r", r=rid)}
    assert "old.manager@fixtureestateagents.co.uk" not in yielded
    assert {"info@fixtureestateagents.co.uk", "jane@fixtureestateagents.co.uk"} <= yielded
    # ---- network expansion: related organisation + relationship, NOT auto-confirmed
    partner = await q("select o.id, d.verified_match_status from organisations o left join organisation_domains d on d.organisation_id=o.id where o.discovery_source='network_expansion'")
    assert partner and all(p.verified_match_status != "confirmed" for p in partner)
    assert await scalar("select count(*) from business_relationships") >= 1
    # ---- completeness is truthful (tri-state)
    c1 = (await q("select c.* from organisation_completeness c join organisations o on o.id=c.organisation_id where o.company_number='99999991'"))[0]
    assert (c1.official_website, c1.published_email, c1.contact_page_inspected) == ("yes", "yes", "yes")
    c3 = (await q("select c.*, c.gap_log from organisation_completeness c join organisations o on o.id=c.organisation_id where o.company_number='99999992'"))[0]
    assert c3.published_email != "yes" and c3.campaign_eligibility != "yes" and c3.official_website in ("no", "unknown")
    if c3.official_website == "no":
        assert c3.gap_log, "'no' is only allowed after the gap-recovery ladder actually ran"
    # ---- attributed export: archived-only address excluded, crawled + document addresses present with attribution
    async with __import__("app.db", fromlist=["x"]).sessionmaker()() as s:
        user = (await s.execute(__import__("sqlalchemy").select(User).limit(1))).scalar_one()
        ex = await exports.create_export(s, user=user, preset="published_business_emails", fmt="csv", filters=ExportFilters(), run_inline=True)
        assert ex.status == "done"
        rd = list(csv.DictReader(io.StringIO(Path(exports.resolve_export_path(ex.file_path)).read_text())))
    got = {r["Email Address"] for r in rd}
    assert "old.manager@fixtureestateagents.co.uk" not in got
    assert {"info@fixtureestateagents.co.uk", "jane@fixtureestateagents.co.uk"} <= got
    jane = next(r for r in rd if r["Email Address"].startswith("jane"))
    assert jane["Company Registration Number"] == "99999991" and jane["Source URL"].startswith(f"http://{H1}/") and jane["Discovery Source"]
