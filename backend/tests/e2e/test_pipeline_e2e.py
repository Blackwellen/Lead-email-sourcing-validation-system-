"""End-to-end: bulk dataset -> company match -> official-domain discovery -> crawl -> verify -> dedupe -> export.

Everything is synthetic and local: a Companies House-format zip of fictional companies, a local HTTP server standing in
for their websites (hostnames are remapped to 127.0.0.1 via an injected resolver) and a fake DNS resolver for MX checks.
Nothing here claims to be real-world data or a live result.
"""
import asyncio
import csv
import io
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

from app import db as dbmod
from app.config import get_settings
from app.crawler import safe_http
from app.models import User
from app.pipeline import handlers, runtime
from app.run_config import RunConfig
from app.worker import Worker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from crawler.conftest import FixtureServer  # noqa: E402
from sources.fixtures import ch_row, write_ch_zip  # noqa: E402
from verify.fakes import FakeResolver  # noqa: E402

pytestmark = pytest.mark.asyncio

SITE = """<html><head><title>Fixture Estate Agents Ltd</title></head><body><h1>Fixture Estate Agents Ltd</h1>
<p>Manchester lettings.</p><footer>Fixture Estate Agents Ltd. Registered in England and Wales No. {num}. M1 1AA</footer>
<a href="/contact">Contact</a></body></html>"""
CONTACT = """<html><body><h1>Contact</h1><p>Email <a href="mailto:{mail}">{mail}</a></p>
<div class="team"><h3>Jane Example</h3><p>Director</p><a href="mailto:{named}">{named}</a></div>
<footer>Registered in England and Wales No. {num}</footer></body></html>"""


async def _wait(cond, timeout=60):
    t0 = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - t0 < timeout:
        if await cond():
            return True
        await asyncio.sleep(0.3)
    return False


@pytest.fixture
def web(monkeypatch):
    try:
        srv = FixtureServer.__new__(FixtureServer)
        FixtureServer.__init__(srv)
        # bind port 80 on loopback (root in the sandbox) so hostnames without ports work
        from http.server import ThreadingHTTPServer
        handler_cls = srv.httpd.RequestHandlerClass
        srv.httpd.server_close()
        srv.httpd = ThreadingHTTPServer(("127.0.0.1", 80), handler_cls)
    except OSError:
        pytest.skip("cannot bind 127.0.0.1:80")
    import threading
    srv.httpd.daemon_threads = True
    srv.port = 80
    srv.thread = threading.Thread(target=srv.httpd.serve_forever, daemon=True)
    srv.thread.start()
    yield srv
    srv.stop()


async def test_dataset_to_export(tmp_path, web, monkeypatch, session):
    monkeypatch.setattr(get_settings(), "allow_private_networks", True)
    monkeypatch.setattr(runtime, "MIN_INTERVAL", 0.0)

    async def fake_resolver(host, port):  # every fixture company website lives on the local fixture server
        return ["127.0.0.1"] if host.endswith("example-fixture.co.uk") or host.startswith("fixture") else []
    monkeypatch.setattr(safe_http, "system_resolver", fake_resolver)
    await runtime.close_net()

    dns = FakeResolver()
    for d in ("fixtureestateagents.co.uk",):
        dns.mail_domain(d, [(10, f"mail.{d}")])
    monkeypatch.setattr(handlers, "VERIFY_RESOLVER", dns)

    # two fictional companies in Companies House BasicCompanyData format: only the first has a website
    rows = [
        ch_row("FIXTURE ESTATE AGENTS LTD", "99999991", town="MANCHESTER", postcode="M1 1AA", sics=["68310 - Real estate agencies"]),
        ch_row("FIXTURE NOWEBSITE LETTINGS LTD", "99999992", town="MANCHESTER", postcode="M2 2BB", sics=["68310 - Real estate agencies"]),
        ch_row("FIXTURE ESTATE AGENTS LTD", "99999991", town="MANCHESTER", postcode="M1 1AA", sics=["68310 - Real estate agencies"]),  # duplicate row
    ]
    zpath = write_ch_zip(tmp_path / "ch.zip", rows)

    web.add("/", SITE.format(num="99999991"))
    web.add("/contact", CONTACT.format(mail="info@fixtureestateagents.co.uk", named="jane@fixtureestateagents.co.uk", num="99999991"))
    web.add("/robots.txt", "", status=404)

    cfg = RunConfig(
        name="e2e", countries=["GB"], sources=["companies_house_bulk"], source_params={"companies_house_bulk": {"path": str(zpath)}},
        target={"count": 2, "definition": "distinct_published_emails", "uniqueness": "email", "min_confidence": 30, "exclude_free_providers": True},
    )
    from app import runs
    async with dbmod.sessionmaker()() as s:
        u = User(email="a@example.com", password_hash="x", role="admin")
        s.add(u)
        await s.flush()
        run = await runs.create_run(s, cfg, user_id=u.id)
        await runs.start_run(s, run.id)
        await s.commit()
        run_id, user_id = run.id, u.id

    w = Worker(concurrency=6)
    task = asyncio.create_task(w.run())

    async def done():
        async with dbmod.sessionmaker()() as s:
            return (await s.execute(text("select status from sourcing_runs where id=:r"), {"r": run_id})).scalar() in ("completed", "failed")

    ok = await _wait(done, 90)
    w.request_stop()
    await task
    await runtime.close_net()
    assert ok, "run did not finish"

    async with dbmod.sessionmaker()() as s:
        run = (await s.execute(text("select status, stop_reason, counters from sourcing_runs where id=:r"), {"r": run_id})).one()
        assert run.status == "completed", run
        assert run.stop_reason == "target_reached", run
        orgs = (await s.execute(text("select count(*) from organisations"))).scalar()
        assert orgs == 2  # duplicate dataset row merged by company number
        dom = (await s.execute(text("select domain, verified_match_status from organisation_domains where verified_match_status in ('confirmed','high')"))).all()
        assert dom and dom[0][0] == "fixtureestateagents.co.uk"
        emails = (await s.execute(text("select email_normalised, origin, overall_reachability, recipient_evidence_status, catch_all_status from email_addresses order by 1"))).all()
        assert [e[0] for e in emails] == ["info@fixtureestateagents.co.uk", "jane@fixtureestateagents.co.uk"]
        assert all(e[1] in ("published", "corroborated") for e in emails)
        assert all(e[3] == "untested" for e in emails)  # no SMTP probing happened: never claimed
        assert all(e[2] != "safe" or e[0].startswith("jane") for e in emails)  # role address never 'safe'
        ev = (await s.execute(text("select count(*) from contact_evidence ce join evidence_sources es on es.id=ce.evidence_id where es.source_url like 'http://fixtureestateagents.co.uk/%'"))).scalar()
        assert ev >= 2
        assert (await s.execute(text("select count(*) from run_yield where run_id=:r"), {"r": run_id})).scalar() == 2
        dead = (await s.execute(text("select count(*) from sourcing_jobs where state in ('dead','failed')"))).scalar()
        assert dead == 0

        from app import exports
        from app.exports import ExportFilters
        user = await s.get(User, user_id)
        ex = await exports.create_export(s, user=user, preset="published_business_emails", fmt="csv", filters=ExportFilters(), run_inline=True)
        assert ex.status == "done" and ex.row_count == 2
        data = Path(exports.resolve_export_path(ex.file_path)).read_text()
        rd = list(csv.DictReader(io.StringIO(data)))
        assert {r["Email Address"] for r in rd} == {"info@fixtureestateagents.co.uk", "jane@fixtureestateagents.co.uk"}
        r0 = next(r for r in rd if r["Email Address"].startswith("jane"))
        assert r0["Company Registration Number"] == "99999991"
        assert r0["Source URL"].startswith("http://fixtureestateagents.co.uk/")
        assert r0["Full Name"] == "Jane Example" and r0["Discovery Source"]
