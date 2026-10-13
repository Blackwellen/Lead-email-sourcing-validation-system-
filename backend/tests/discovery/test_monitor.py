from __future__ import annotations

import sys
import types
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.crawler.ratelimit import RateLimiter
from app.discovery.monitor import due_monitors, next_run_after, run_due_monitors
from app.models import MonitorSchedule, Organisation, OrganisationDomain

NOW = datetime(2026, 1, 31, 12, 0, tzinfo=UTC)


def test_next_run_after_frequencies():
    assert next_run_after("daily", NOW) == NOW + timedelta(days=1)
    assert next_run_after("weekly", NOW) == NOW + timedelta(days=7)
    assert next_run_after("monthly", NOW) == datetime(2026, 2, 28, 12, tzinfo=UTC)  # clamped
    assert next_run_after("monthly", datetime(2026, 12, 15, tzinfo=UTC)) == datetime(2027, 1, 15, tzinfo=UTC)
    try:
        next_run_after("hourly", NOW)
    except ValueError:
        pass
    else:
        raise AssertionError


async def test_due_selection(session):
    session.add_all([
        MonitorSchedule(kind="website_changes", name="never run", frequency="daily"),
        MonitorSchedule(kind="website_changes", name="due", frequency="daily", next_run_at=NOW - timedelta(minutes=1)),
        MonitorSchedule(kind="website_changes", name="future", frequency="daily", next_run_at=NOW + timedelta(hours=1)),
        MonitorSchedule(kind="website_changes", name="disabled", frequency="daily", enabled=False),
        MonitorSchedule(kind="website_changes", name="exactly", frequency="weekly", next_run_at=NOW),
    ])
    await session.flush()
    assert [m.name for m in await due_monitors(session, NOW)] == ["never run", "due", "exactly"]


async def test_run_due_monitors_dispatch_and_rescheduling(session, server, fetcher):
    server.add("/robots.txt", "User-agent: *\nAllow: /\n", ctype="text/plain")
    server.add("/", "<html><body><a href='mailto:info@w.co.uk'>x</a></body></html>")
    o = Organisation(legal_name="W Ltd", country_code="GB", canonical_identity="GB:nm:w")
    session.add(o)
    await session.flush()
    session.add(OrganisationDomain(organisation_id=o.id, domain=server.host, registrable_domain="w.co.uk", website_url=server.url("/"),
                                   verified_match_status="confirmed", confidence=0.9))
    ok = MonitorSchedule(kind="website_changes", name="w", frequency="weekly", config={"limit": 5})
    sig = MonitorSchedule(kind="signals", name="s", frequency="daily", config={"limit": 5})
    unk = MonitorSchedule(kind="telepathy", name="u", frequency="daily")
    nr = MonitorSchedule(kind="new_registrations", name="n", frequency="monthly")
    session.add_all([ok, sig, unk, nr])
    await session.flush()
    prev = sys.modules.get("app.sources.new_registrations")
    sys.modules["app.sources.new_registrations"] = None  # simulate the module being absent -> ImportError
    try:
        res = await run_due_monitors(session, fetcher, now=NOW, limiter=RateLimiter(min_interval=0))
    finally:
        if prev is not None:
            sys.modules["app.sources.new_registrations"] = prev
        else:
            sys.modules.pop("app.sources.new_registrations", None)
    by = {r["kind"]: r for r in res}
    assert by["website_changes"]["status"] == "ok" and by["website_changes"]["result"]["domains"] == 1
    assert by["signals"]["status"] == "ok" and by["signals"]["result"]["orgs"] == 1
    assert by["telepathy"]["status"] == "error" and "unknown monitor kind" in by["telepathy"]["result"]["error"]
    assert by["new_registrations"]["status"] == "unavailable"  # the other module is absent: graceful
    await session.refresh(ok)
    await session.refresh(unk)
    await session.refresh(nr)
    assert ok.last_run_at == NOW and ok.next_run_at == NOW + timedelta(days=7) and ok.last_result["status"] == "ok"
    assert unk.next_run_at == NOW + timedelta(hours=1)  # errors retry soon
    assert nr.next_run_at == datetime(2026, 2, 28, 12, tzinfo=UTC)
    # nothing is due right after
    assert await run_due_monitors(session, fetcher, now=NOW + timedelta(minutes=5)) == []


async def test_new_registrations_dispatch_to_other_module(session, fetcher):
    calls = {}

    async def run_monitor(session, monitor, now):  # a plausible signature in the other module
        calls["m"] = (monitor.name, now)
        return {"new": 3}

    prev = sys.modules.get("app.sources.new_registrations")
    mod = types.ModuleType("app.sources.new_registrations")
    mod.run_monitor = run_monitor
    sys.modules["app.sources.new_registrations"] = mod
    try:
        mon = MonitorSchedule(kind="new_registrations", name="nr", frequency="daily")
        session.add(mon)
        await session.flush()
        res = await run_due_monitors(session, fetcher, now=NOW)
        assert res[0]["status"] == "ok" and res[0]["result"]["new"] == 3 and calls["m"] == ("nr", NOW)
        # a failing monitor is isolated in a savepoint
        async def bad(session, monitor):
            session.add(MonitorSchedule(kind="signals", name="ghost", frequency="daily"))
            raise RuntimeError("boom")
        mod.run_monitor = bad
        mon.next_run_at = None
        await session.flush()
        res = await run_due_monitors(session, fetcher, now=NOW + timedelta(days=2))
        assert res[0]["status"] == "error" and "boom" in res[0]["result"]["error"]
        assert not (await session.execute(select(MonitorSchedule).where(MonitorSchedule.name == "ghost"))).scalars().all()
    finally:
        if prev is not None:
            sys.modules["app.sources.new_registrations"] = prev
        else:
            sys.modules.pop("app.sources.new_registrations", None)


async def test_monitor_config_policy_and_server_side_store_index(session, fetcher):
    """Monitor configuration passes the source-configuration policy before any adapter sees it; the new-registrations fingerprint
    store is derived server-side (per monitor, under data_dir/monitors) and a user-supplied store_index is ignored."""
    from pathlib import Path

    from app.config import get_settings

    seen = {}

    async def run_monitor(session, schedule, now):
        seen["store_index"] = schedule.config.get("store_index")
        seen["days"] = schedule.config.get("days")
        schedule.last_result = {"touched": True}  # attribute writes go to the real row
        return {"new": 1}

    prev = sys.modules.get("app.sources.new_registrations")
    mod = types.ModuleType("app.sources.new_registrations")
    mod.run_monitor = run_monitor
    sys.modules["app.sources.new_registrations"] = mod
    try:
        mon = MonitorSchedule(kind="new_registrations", name="nr", frequency="daily", config={"store_index": "/etc/evil.sqlite", "days": 3})
        bad = MonitorSchedule(kind="new_registrations", name="bad", frequency="daily", config={"current_path": "/etc/passwd"})
        session.add_all([mon, bad])
        await session.flush()
        res = {r["id"]: r for r in await run_due_monitors(session, fetcher, now=NOW)}
        expected = Path(get_settings().data_dir) / "monitors" / f"{mon.id}.sqlite"
        assert res[mon.id]["status"] == "ok" and seen == {"store_index": str(expected), "days": 3}
        assert expected.parent.is_dir()
        assert mon.config == {"store_index": "/etc/evil.sqlite", "days": 3}  # the derived value is never written back
        assert res[bad.id]["status"] == "error" and "refused" in res[bad.id]["result"]["error"]
        assert bad.last_result["status"] == "error"
    finally:
        if prev is not None:
            sys.modules["app.sources.new_registrations"] = prev
        else:
            sys.modules.pop("app.sources.new_registrations", None)
