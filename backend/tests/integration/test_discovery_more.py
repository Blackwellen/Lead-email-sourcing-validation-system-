"""(b) procurement-winner flow, gap-recovery budget / marginal yield, (c) change detection, (d) scheduler + quotas with the real worker,
(e) run-config backward compatibility, Common Crawl confirmation."""
import asyncio
import csv
import io
import json
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import text

from app import db as dbmod
from app import exports
from app.discovery.monitor import due_monitors
from app.exports import ExportFilters
from app.models import MonitorSchedule, User
from app.pipeline import runtime
from app.run_config import DiscoverySpec, RunConfig
from app.sources.base import AdapterInfo, BusinessRecord, RatePolicy, SourceAdapter, register
from app.system_runs import ensure_system_run
from app.worker import Worker

from .helpers import drain_jobs, make_run, patch_network, q, run_until_done, scalar

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from discovery_sources.fixtures import _party, _release  # noqa: E402
from sources.fixtures import ch_row, write_ch_zip  # noqa: E402

pytestmark = pytest.mark.asyncio


# =========================================================================== (b) procurement winner
async def test_procurement_winner_to_export(tmp_path, web, monkeypatch, session):
    H = "examplesupplies.co.uk"
    web.pages[H] = {
        "/": "<html><head><title>Example Supplies Ltd</title></head><body><h1>Example Supplies Ltd</h1><a href='/contact'>Contact</a>"
             "<footer>Example Supplies Ltd. Registered in England and Wales No. 99999991. M2 2BB</footer></body></html>",
        "/contact": "<html><body><h1>Contact</h1><a href='mailto:orders@examplesupplies.co.uk'>orders@examplesupplies.co.uk</a>"
                    "<footer>Registered in England and Wales No. 99999991</footer></body></html>",
        "/robots.txt": (404, ""),
    }
    await patch_network(monkeypatch, web, [H])
    buyer = _party("GB-CFS-1", "Example District Council", ["buyer"], street="1 Council Road", postcode="M1 1AA")
    sup = _party("GB-CFS-11", "Example Supplies Ltd", ["supplier"], scheme="GB-COH", ident="99999991", street="1 Supply Way, Exampleton, M2 2BB")
    award = {"id": "a1", "status": "active", "date": "2026-05-01T00:00:00+00:00", "value": {"amount": 1000, "currency": "GBP"},
             "suppliers": [{"id": sup["id"], "name": sup["name"]}],
             "documents": [{"id": "1", "documentType": "awardNotice", "url": "https://www.contractsfinder.service.gov.uk/Notice/aaaa1111"}]}
    pkg = {"uri": "https://www.contractsfinder.service.gov.uk/x", "version": "1.1", "publishedDate": "2026-10-01T00:00:00Z", "publisher": {"name": "t"},
           "license": "http://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/", "links": {},
           "releases": [_release("r1", "2026-05-01T10:00:00+00:00", ("GB-CFS-1", "Example District Council"), [buyer, sup], [award])]}
    path = tmp_path / "ocds.json"
    path.write_text(json.dumps(pkg))
    cfg = RunConfig(name="int-b", countries=["GB"], sources=["contracts_finder_ocds"], source_params={"contracts_finder_ocds": {"path": str(path)}},
                    target={"count": 100, "min_confidence": 30},
                    discovery={"source_categories": ["procurement", "company_websites"]})
    rid = await make_run(cfg)
    assert await run_until_done(rid, timeout=90) == "completed"
    org = (await q("select id, company_number, discovery_source from organisations"))[0]
    assert org.company_number == "99999991" and org.discovery_source == "contracts_finder_ocds"
    dom = (await q("select domain, verified_match_status, evidence from organisation_domains where organisation_id=:o and verified_match_status in ('confirmed','high')", o=org.id))[0]
    assert dom.domain == H and any("company" in json.dumps(e).lower() or "footer" in json.dumps(e).lower() for e in dom.evidence)
    sig = (await q("select signal_type, source_url from business_signals where organisation_id=:o", o=org.id))[0]
    assert sig.signal_type == "contract_award" and sig.source_url.endswith("/Notice/aaaa1111")
    assert await scalar("select count(*) from organisation_record_links where organisation_id=:o and identifier_type='company_number'", o=org.id) == 1
    async with dbmod.sessionmaker()() as s:
        user = (await s.execute(text("select id from users limit 1"))).scalar()
        ex = await exports.create_export(s, user=await s.get(User, user), preset="published_business_emails", fmt="csv", filters=ExportFilters(), run_inline=True)
        rows = list(csv.DictReader(io.StringIO(Path(exports.resolve_export_path(ex.file_path)).read_text())))
    assert [r["Email Address"] for r in rows] == ["orders@examplesupplies.co.uk"]
    assert rows[0]["Company Registration Number"] == "99999991" and rows[0]["Discovery Source"] == "contracts_finder_ocds"


# =========================================================================== gap recovery: budget + marginal yield
async def test_gap_recovery_budget_and_marginal_yield(tmp_path, web, monkeypatch, session):
    await patch_network(monkeypatch, web, [])
    words = ["alpha", "bravo", "cedar", "delta", "ember", "forge", "grove", "harbor", "iris", "jade", "kilo", "lumen", "maple", "noble", "onyx", "pearl",
             "quartz", "raven", "sable", "tango", "umber", "vivid", "willow", "xenon", "yarrow", "zephyr", "amber", "birch", "coral", "dune"]
    rows = [ch_row(f"{w.upper()} NOSITE TRADING LTD", f"8000{i:04d}", town="LEEDS", postcode=f"LS{i % 9 + 1} 4AP") for i, w in enumerate(words)]
    z = write_ch_zip(tmp_path / "ch.zip", rows)
    cfg = RunConfig(name="int-gap", countries=["GB"], sources=["companies_house_bulk"], source_params={"companies_house_bulk": {"path": str(z)}},
                    target={"count": 1000}, discovery={"strategy": "fast"})
    rid = await make_run(cfg)
    assert await run_until_done(rid, timeout=150) == "completed"
    jobs = await q("select checkpoint from sourcing_jobs where run_id=:r and stage='gap_recovery' and state='done'", r=rid)
    assert len(jobs) == 30
    from app.pipeline.flow import GAP_REQUEST_BUDGET

    assert all(j.checkpoint["requests_used"] <= GAP_REQUEST_BUDGET["fast"] for j in jobs)
    skipped = [s for j in jobs for s in j.checkpoint["skipped"] if s["reason"] == "low_yield"]
    assert skipped, (jobs[-1].checkpoint, (await q("select counters from sourcing_runs where id=:r", r=rid))[0].counters)
    counters = (await q("select counters from sourcing_runs where id=:r", r=rid))[0].counters
    assert "gap_yield" in counters and counters["gap_yield"]
    # nothing was invented: no website, no email -> flags are never 'yes'
    assert await scalar("select count(*) from organisation_completeness where official_website='yes' or published_email='yes'") == 0
    assert await scalar("select count(*) from email_addresses") == 0


# =========================================================================== Common Crawl confirmed by a later live crawl
async def test_historical_contact_counts_only_after_live_confirmation(tmp_path, web, monkeypatch, session):
    from datetime import UTC, datetime

    from app.pipeline import discovery_stages
    from app.sources.commoncrawl import UrlHit, parse_warc_record

    from discovery_sources.fixtures import make_warc_member

    H = "fixtureestateagents.co.uk"
    live = {"/": "<html><head><title>Fixture Estate Agents Ltd</title></head><body><h1>Fixture Estate Agents Ltd</h1><a href='/contact'>c</a>"
                 "<footer>Registered in England and Wales No. 99999991 M1 1AA</footer></body></html>",
            "/contact": "<html><body><a href='mailto:info@fixtureestateagents.co.uk'>info@fixtureestateagents.co.uk</a>"
                        "<footer>Registered in England and Wales No. 99999991</footer></body></html>", "/robots.txt": (404, "")}
    web.pages[H] = dict(live)
    await patch_network(monkeypatch, web, [H])
    hit = UrlHit(url=f"http://{H}/contact", domain=H, crawl_id="CC-MAIN-1", fetch_time=datetime(2026, 8, 1, tzinfo=UTC), mime="text/html", status=200,
                 warc_filename="a.warc.gz", warc_offset=0, warc_length=10)
    arch = "<html><body><a href='mailto:old.boss@fixtureestateagents.co.uk'>Old Boss</a></body></html>"

    async def hits(d, p, n):
        return [hit]

    async def fetch(h):
        return parse_warc_record(make_warc_member(h.url, arch))

    monkeypatch.setattr(discovery_stages, "CC_HITS", hits)
    monkeypatch.setattr(discovery_stages, "CC_FETCH", fetch)
    z = write_ch_zip(tmp_path / "ch.zip", [ch_row("FIXTURE ESTATE AGENTS LTD", "99999991", town="MANCHESTER", postcode="M1 1AA")])
    cfg = RunConfig(name="int-cc", countries=["GB"], sources=["companies_house_bulk"], source_params={"companies_house_bulk": {"path": str(z)}},
                    target={"count": 1000, "min_confidence": 30}, discovery={"source_categories": ["bulk_open_data", "company_websites", "common_crawl"]})
    rid = await make_run(cfg)
    assert await run_until_done(rid, timeout=90) == "completed"
    old = "old.boss@fixtureestateagents.co.uk"
    assert await scalar("select count(*) from email_addresses where email_normalised=:e", e=old) == 1
    assert await scalar("select count(*) from run_yield y join email_addresses e on e.id=y.email_id where e.email_normalised=:e", e=old) == 0
    assert await scalar("select needs_review from email_addresses where email_normalised=:e", e=old) is True
    # the live site now publishes it: a new crawl confirms the archived contact and it becomes countable
    web.pages[H]["/contact"] = live["/contact"].replace("</body>", "<a href='mailto:old.boss@fixtureestateagents.co.uk'>Old Boss</a></body>")
    run2 = await make_run(cfg.model_copy(update={"name": "int-cc-2", "refresh_existing": True}))
    assert await run_until_done(run2, timeout=90) == "completed"
    assert await scalar("select count(*) from run_yield y join email_addresses e on e.id=y.email_id where e.email_normalised=:e and y.run_id=:r", e=old, r=run2) == 1
    assert await scalar("select needs_review from email_addresses where email_normalised=:e", e=old) is False


# =========================================================================== (c) change detection end to end
async def test_change_detection_end_to_end(tmp_path, web, monkeypatch, session):
    H = "fixtureestateagents.co.uk"
    base = {"/": "<html><head><title>Fixture Estate Agents Ltd</title></head><body><h1>Fixture Estate Agents Ltd</h1><a href='/contact'>c</a>"
                 "<footer>Registered in England and Wales No. 99999991 M1 1AA</footer></body></html>",
            "/contact": "<html><body><a href='mailto:info@fixtureestateagents.co.uk'>info@fixtureestateagents.co.uk</a>"
                        "<footer>Registered in England and Wales No. 99999991</footer></body></html>", "/robots.txt": (404, "")}
    web.pages[H] = dict(base)
    await patch_network(monkeypatch, web, [H])
    z = write_ch_zip(tmp_path / "ch.zip", [ch_row("FIXTURE ESTATE AGENTS LTD", "99999991", town="MANCHESTER", postcode="M1 1AA")])
    cfg = RunConfig(name="int-chg", countries=["GB"], sources=["companies_house_bulk"], source_params={"companies_house_bulk": {"path": str(z)}},
                    target={"count": 1000, "min_confidence": 30})
    rid = await make_run(cfg)
    assert await run_until_done(rid, timeout=90) == "completed"
    dom_id = await scalar("select id from organisation_domains where verified_match_status in ('confirmed','high')")
    async with dbmod.sessionmaker()() as s:
        sysrun = await ensure_system_run(s, "int-change-checks")
        from app import jobqueue as jq

        await jq.enqueue(s, run_id=sysrun, stage="change_check", dedupe_key="c1", payload={"domain_id": dom_id})
        await s.commit()
    await drain_jobs(sysrun)
    n_before = await scalar("select count(*) from website_snapshots where domain_id=:d", d=dom_id)
    assert n_before >= 1
    old_hash = await scalar("select content_hash from website_snapshots where domain_id=:d and url like '%/contact' order by id limit 1", d=dom_id)
    # the site changes: a new person is published
    web.pages[H]["/contact"] = base["/contact"].replace("</body>", "<p>New: <a href='mailto:newhire@fixtureestateagents.co.uk'>newhire@fixtureestateagents.co.uk</a></p></body>")
    async with dbmod.sessionmaker()() as s:
        await jq.enqueue(s, run_id=sysrun, stage="change_check", dedupe_key="c2", payload={"domain_id": dom_id})
        await s.commit()
    await drain_jobs(sysrun)
    assert await scalar("select count(*) from email_addresses where email_normalised='newhire@fixtureestateagents.co.uk'") == 1
    assert await scalar("select count(*) from sourcing_jobs where run_id=:r and stage='verify' and state='done'", r=sysrun) >= 1  # verify job queued AND run
    assert await scalar("select last_validated_at is not null from email_addresses where email_normalised='newhire@fixtureestateagents.co.uk'") is True
    snaps = await q("select content_hash, changed from website_snapshots where domain_id=:d and url like '%/contact' order by id", d=dom_id)
    assert len(snaps) >= 2 and snaps[0].content_hash == old_hash != snaps[-1].content_hash and snaps[-1].changed  # prior snapshot preserved
    # the recurring monitor tick drives the same machinery
    async with dbmod.sessionmaker()() as s:
        s.add(MonitorSchedule(kind="website_changes", name="m", frequency="daily", config={"older_than_days": 0, "limit": 5}, enabled=True))
        await s.commit()
        w = Worker(concurrency=1)
        await w._enqueue_monitor_tick(s)
        await s.commit()
    mon_run = await scalar("select run_id from sourcing_jobs where stage='monitor'")
    assert mon_run
    await drain_jobs(mon_run)
    async with dbmod.sessionmaker()() as s:
        assert (await s.execute(text("select last_run_at is not null from monitor_schedules"))).scalar() is True
        assert not await due_monitors(s)


# =========================================================================== (d) scheduler + quotas + rate policy with the real worker
def _fake(aid: str, n: int, rpm: int | None, prefix: str):
    @register
    class _F(SourceAdapter):
        info = AdapterInfo(adapter_id=aid, name=aid, country_coverage=("GB",), source_type="open_data", licence="test", source_url="http://t",
                           access_requirements="none", automated_collection_permitted=True, rate_policy=RatePolicy(requests_per_minute=rpm, concurrency=1))

        async def iter_businesses(self, query, checkpoint=None):
            start = int((checkpoint or {}).get("i", -1)) + 1
            for i in range(start, n):
                yield BusinessRecord(source_ref=f"{aid}-{i}", legal_name=f"{prefix}{i:03d} {'abcdefghij'[i % 10] * 5} Industries Ltd", company_number=f"{aid[-1].upper()}{i:07d}",
                                     country_code="GB", postal_code="LS1 4AP", adapter_id=aid, source_url=f"http://t/{i}"), {"i": i}
    return _F


_FAKES = {}


def _ensure_fakes():
    if not _FAKES:
        _FAKES["a"] = _fake("int_fake_a", 100, 6, "Zq")
        _FAKES["b"] = _fake("int_fake_b", 60, None, "Wx")
        _FAKES["c"] = _fake("int_fake_c", 100, None, "Vy")


async def test_scheduler_rate_policy_quotas_and_fairness(monkeypatch, session):
    _ensure_fakes()
    from app.pipeline import handlers

    monkeypatch.setattr(handlers, "CHECKPOINT_EVERY", 10)
    runtime.reset_gates()
    cfg = RunConfig(name="int-d", countries=["GB"], sources=["int_fake_a", "int_fake_b", "int_fake_c"], target={"count": 100000},
                    discovery={"source_categories": ["bulk_open_data"], "source_quotas": {"int_fake_c": 25}, "overall_max_work": 100000})
    rid = await make_run(cfg)
    w = Worker(concurrency=4, stages=["discover"])
    task = asyncio.create_task(w.run())
    t0 = time.monotonic()
    await asyncio.sleep(9)
    elapsed = time.monotonic() - t0
    w.request_stop()
    await task
    gate = runtime.adapter_gate(__import__("app.sources.base", fromlist=["x"]).get_adapter("int_fake_a").info)
    # the rate policy (6 requests/min, bucket of 5) cannot have been exceeded: grants <= burst + refill
    assert gate.work_used <= 5 + elapsed * 6 / 60 + 1, gate.work_used
    seen = {r.adapter_id: r.records_seen for r in await q("select adapter_id, records_seen from source_effectiveness where run_id=:r", r=rid)}
    assert seen["int_fake_b"] == 60  # unthrottled source finished: it is not starved by the throttled one
    assert seen["int_fake_c"] == 25  # hard per-source quota
    assert seen.get("int_fake_a", 0) < 100  # still throttled: remaining work waits instead of exceeding the policy
    c_cp = (await q("select checkpoint from sourcing_jobs where run_id=:r and payload->>'adapter_id'='int_fake_c'", r=rid))[0].checkpoint
    assert c_cp.get("stopped") == "source_quota"
    assert await scalar("select count(*) from organisations where discovery_source='int_fake_c'") == 25
    # resume of the throttled source: no duplicates, no gaps
    assert await scalar("select count(*) from organisations where discovery_source='int_fake_a'") == seen["int_fake_a"]


# =========================================================================== (e) backward compatibility
async def test_run_config_backward_compatibility(tmp_path, web, monkeypatch, session):
    old = {"name": "legacy", "countries": ["GB"], "sources": ["companies_house_bulk"], "target": {"count": 5}}
    cfg = RunConfig.model_validate(old)
    assert cfg.discovery == DiscoverySpec()  # defaults fill in
    H = "fixtureestateagents.co.uk"
    web.pages[H] = {"/": "<html><head><title>Fixture Estate Agents Ltd</title></head><body><h1>Fixture Estate Agents Ltd</h1><a href='/contact'>c</a>"
                         "<footer>Registered in England and Wales No. 99999991 M1 1AA</footer></body></html>",
                    "/contact": "<html><body><a href='mailto:info@fixtureestateagents.co.uk'>info@fixtureestateagents.co.uk</a>"
                                "<footer>Registered in England and Wales No. 99999991</footer></body></html>", "/robots.txt": (404, "")}
    await patch_network(monkeypatch, web, [H])
    z = write_ch_zip(tmp_path / "ch.zip", [ch_row("FIXTURE ESTATE AGENTS LTD", "99999991", town="MANCHESTER", postcode="M1 1AA"),
                                           ch_row("FIXTURE NOSITE LTD", "99999992", town="LEEDS", postcode="LS1 4AP")])
    cfg = RunConfig(name="legacy", countries=["GB"], sources=["companies_house_bulk"], source_params={"companies_house_bulk": {"path": str(z)}},
                    target={"count": 1000, "min_confidence": 30})
    rid = await make_run(cfg, start=False)
    async with dbmod.sessionmaker()() as s:  # a snapshot written before the discovery layer existed has no 'discovery' key
        await s.execute(text("UPDATE sourcing_runs SET configuration_snapshot = configuration_snapshot - 'discovery' WHERE id=:r"), {"r": rid})
        from app import runs

        await runs.start_run(s, rid)
        await s.commit()
    assert await run_until_done(rid, timeout=90) == "completed"
    assert await scalar("select count(*) from run_yield where run_id=:r", r=rid) == 1
    # legacy behaviour: no gap-recovery / network / cc jobs are invented for old runs
    assert await scalar("select count(*) from sourcing_jobs where run_id=:r and stage in ('gap_recovery','network_expand','cc_lookup')", r=rid) == 0
    # a NEW run (explicit discovery layer) refuses a source whose category it did not select, visibly
    z2 = Path(z)
    cfg2 = RunConfig(name="strict", countries=["GB"], sources=["contracts_finder_ocds"], source_params={"contracts_finder_ocds": {"path": str(z2)}}, target={"count": 5})
    r2 = await make_run(cfg2)
    await run_until_done(r2, timeout=30)
    assert await scalar("select error_code from sourcing_jobs where run_id=:r and stage='discover'", r=r2) == "category_not_selected"
