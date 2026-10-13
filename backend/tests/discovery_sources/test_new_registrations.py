import csv
import io
import tracemalloc
import zipfile
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.models import BusinessSignal, MonitorSchedule, Organisation, OrganisationDomain
from app.sources.base import DiscoveryQuery, NotConfigured, get_adapter
from app.sources.new_registrations import (
    NewRegistrationsAdapter,
    SnapshotIndex,
    compute_next_run,
    iter_diff,
    iter_window,
    parse_ch_date,
    run_monitor,
)
from tests.sources.fixtures import CH_HEADER, ch_row

from .helpers import assert_resume_no_dup_no_gap, collect

INC = CH_HEADER.index("IncorporationDate")


def mk(number, name, *, status="Active", postcode="M1 1AA", line1="1 Fixture Street", inc="01/01/2010", town="MANCHESTER", sic="62012 - Business software"):
    r = ch_row(name, number, town=town, postcode=postcode, status=status, line1=line1, sics=[sic])
    r[INC] = inc
    return r


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
        w.writerow(CH_HEADER)
        w.writerows(rows)
    return path


PREV = [mk("99990001", "EXAMPLE ALPHA LTD"), mk("99990002", "EXAMPLE BETA LTD"), mk("99990003", "EXAMPLE GAMMA LTD"),
        mk("99990004", "EXAMPLE DELTA LTD"), mk("99990005", "EXAMPLE EPSILON LTD")]
CUR = [mk("99990001", "EXAMPLE ALPHA LTD"),
       mk("99990002", "EXAMPLE BETA RENAMED LTD"),
       mk("99990003", "EXAMPLE GAMMA LTD", status="Liquidation"),
       mk("99990004", "EXAMPLE DELTA LTD", postcode="LS1 4AP", line1="9 New Road", town="LEEDS"),
       # 99990005 removed from the snapshot
       mk("99990006", "EXAMPLE ZETA LTD", inc="20/09/2026", postcode="BS1 5AH", town="BRISTOL"),
       mk("99990007", "EXAMPLE ETA LTD", inc="25/09/2026", sic="68310 - Real estate agencies"),
       mk("99990008", "EXAMPLE THETA LTD", name_status := "Active", inc="01/06/2026") if False else mk("99990008", "EXAMPLE THETA LTD", inc="01/06/2026")]


@pytest.fixture
def snaps(tmp_path):
    return (write_csv(tmp_path / "BasicCompanyDataAsOneFile-2026-09-01.csv", PREV),
            write_csv(tmp_path / "BasicCompanyDataAsOneFile-2026-10-01.csv", CUR))


def sig_types(rec):
    return [(s["signal_type"], s["detail"].get("change_kind")) for s in rec.raw["signals"]]


def test_metadata():
    i = get_adapter("companies_house_new_registrations").info
    assert i.country_coverage == ("GB",) and i.cost == "free" and "Open Government Licence" in i.licence and i.credentials == "none"


def test_parse_dates():
    assert parse_ch_date("20/09/2026") == date(2026, 9, 20) and parse_ch_date("2026-09-20") == date(2026, 9, 20)
    assert parse_ch_date("") is None and parse_ch_date("31/02/2026") is None


# ------------------------------------------------------------------------------------------------ window mode
def test_window_mode_filters_by_incorporation_date(snaps):
    _, cur = snaps
    got = {r.company_number: r for _n, r in iter_window(cur, 30, today=date(2026, 10, 1))}
    assert set(got) == {"99990006", "99990007"}
    r = got["99990006"]
    assert r.website is None and r.email_published is None  # a new company has no website: nothing is assumed
    s = r.raw["signals"][0]
    assert s["signal_type"] == "new_registration" and s["occurred_at"].startswith("2026-09-20") and s["source_url"].endswith("/company/99990006")
    assert s["title"] == "New company registered: EXAMPLE ZETA LTD" and s["detail"]["incorporation_date"] == "2026-09-20"
    wide = {r.company_number for _n, r in iter_window(cur, 200, today=date(2026, 10, 1))}
    assert "99990008" in wide and "99990001" not in wide
    assert {r.company_number for _n, r in iter_window(cur, 30)} == {"99990006", "99990007"}  # reference date taken from the file name


# ------------------------------------------------------------------------------------------------ diff mode
def test_diff_detects_new_and_changes_with_old_values(snaps):
    prev, cur = snaps
    stats = {}
    out = {r.company_number: r for _n, r in iter_diff(prev, cur, stats=stats)}
    assert set(out) == {"99990002", "99990003", "99990004", "99990006", "99990007", "99990008"}
    assert stats["new"] == 3 and stats["name_changed"] == 1 and stats["status_changed"] == 1 and stats["address_changed"] == 1
    assert stats["removed"] == 1 and stats["unchanged"] == 1
    assert sig_types(out["99990006"]) == [("new_registration", "incorporation")]
    n = out["99990002"].raw["signals"][0]
    assert n["signal_type"] == "registry_change" and n["detail"] == {"old": "EXAMPLE BETA LTD", "new": "EXAMPLE BETA RENAMED LTD", "change_kind": "name", "company_number": "99990002"}
    s = out["99990003"].raw["signals"][0]
    assert s["detail"]["old"] == "Active" and s["detail"]["new"] == "Liquidation" and s["occurred_at"].startswith("2026-10-01")
    a = out["99990004"].raw["signals"][0]
    assert "M1 1AA" in a["detail"]["old"] and "LS1 4AP" in a["detail"]["new"] and a["detail"]["change_kind"] == "address"
    assert all(s["source_url"] for r in out.values() for s in r.raw["signals"])
    assert all(r.website is None and r.email_published is None for r in out.values())


def test_diff_kind_filter_and_multiple_changes(tmp_path):
    prev = write_csv(tmp_path / "BasicCompanyDataAsOneFile-2026-09-01.csv", [mk("99990001", "OLD NAME LTD"), mk("99990002", "STAYS LTD")])
    cur = write_csv(tmp_path / "BasicCompanyDataAsOneFile-2026-10-01.csv", [mk("99990001", "NEW NAME LTD", status="Dissolved", postcode="LS1 4AP"), mk("99990002", "STAYS LTD")])
    r = next(iter(iter_diff(prev, cur)))[1]
    assert [s["detail"]["change_kind"] for s in r.raw["signals"]] == ["name", "status", "address"] and r.raw["changes"] == ["name", "status", "address"]
    assert [x.company_number for _n, x in iter_diff(prev, cur, kinds={"new"})] == []
    only = next(iter(iter_diff(prev, cur, kinds={"status"})))[1]
    assert [s["detail"]["change_kind"] for s in only.raw["signals"]] == ["status"]


def test_whitespace_and_case_noise_is_not_a_change(tmp_path):
    prev = write_csv(tmp_path / "BasicCompanyDataAsOneFile-2026-09-01.csv", [mk("99990001", "Example  Name Ltd", line1="1 Fixture Street")])
    cur = write_csv(tmp_path / "BasicCompanyDataAsOneFile-2026-10-01.csv", [mk("99990001", "EXAMPLE NAME LTD", line1="1  fixture street ")])
    assert list(iter_diff(prev, cur)) == []


def test_stored_fingerprint_index_replaces_previous_file(snaps, tmp_path):
    prev, cur = snaps
    idx_path = tmp_path / "fp.sqlite"
    SnapshotIndex.build(prev, path=idx_path).close(delete=False)
    stored = SnapshotIndex(idx_path)
    assert stored.count() == 5 and stored.meta("snapshot").endswith("2026-09-01.csv")
    out = {r.company_number: r for _n, r in iter_diff(None, cur, prev_index=stored, keep_index=tmp_path / "next.sqlite")}
    stored.close(delete=False)
    assert set(out) == {"99990002", "99990003", "99990004", "99990006", "99990007", "99990008"}
    assert out["99990002"].raw["signals"][0]["detail"]["old"] is None  # old values unknown without the old file ...
    assert out["99990002"].raw["signals"][0]["detail"]["new"] == "EXAMPLE BETA RENAMED LTD"  # ... but the change is detected
    assert (tmp_path / "next.sqlite").exists() and SnapshotIndex(tmp_path / "next.sqlite").count() == 7


def test_diff_cleans_up_temp_files(snaps, tmp_path, monkeypatch):
    import tempfile

    work = tmp_path / "work"
    work.mkdir()
    prev, cur = snaps
    list(iter_diff(prev, cur, work_dir=work))
    assert list(work.iterdir()) == []
    gen = iter_diff(prev, cur, work_dir=work)
    next(gen)
    gen.close()
    assert list(work.iterdir()) == []
    _ = tempfile


def test_diff_requires_a_previous(snaps):
    with pytest.raises(NotConfigured):
        list(iter_diff(None, snaps[1]))


def test_zip_snapshots(tmp_path):
    def z(name, rows):
        buf = io.StringIO(newline="")
        w = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
        w.writerow(CH_HEADER)
        w.writerows(rows)
        p = tmp_path / name
        with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(name.replace(".zip", ".csv"), buf.getvalue())
        return p

    prev, cur = z("BasicCompanyDataAsOneFile-2026-09-01.zip", PREV), z("BasicCompanyDataAsOneFile-2026-10-01.zip", CUR)
    assert len(list(iter_diff(prev, cur))) == 6


# ------------------------------------------------------------------------------------------------ adapter
async def test_adapter_modes_and_resume(snaps):
    prev, cur = snaps
    mk_diff = lambda: NewRegistrationsAdapter({"current_path": str(cur), "previous_path": str(prev)})  # noqa: E731
    full = await assert_resume_no_dup_no_gap(mk_diff)
    assert len(full) == 6 and all(cp["mode"] == "diff" for _, cp in full)
    mk_win = lambda: NewRegistrationsAdapter({"current_path": str(cur), "mode": "window", "days": 30, "today": "2026-10-01"})  # noqa: E731
    win = await assert_resume_no_dup_no_gap(mk_win)
    assert [r.company_number for r, _ in win] == ["99990006", "99990007"]
    default = await collect(NewRegistrationsAdapter({"current_path": str(cur), "days": 30}), DiscoveryQuery(active_only=False))
    assert len(default) == 2  # no previous -> window mode
    assert full[0][1]["row"] < full[-1][1]["row"]


async def test_adapter_filters_apply(snaps):
    prev, cur = snaps
    got = await collect(NewRegistrationsAdapter({"current_path": str(cur), "previous_path": str(prev), "kinds": ["new"]}), DiscoveryQuery(sic_codes=["68310"], active_only=False))
    assert [r.company_number for r, _ in got] == ["99990007"]
    got = await collect(NewRegistrationsAdapter({"current_path": str(cur), "previous_path": str(prev)}), DiscoveryQuery(postcodes=["BS"], active_only=False))
    assert [r.company_number for r, _ in got] == ["99990006"]


# ------------------------------------------------------------------------------------------------ bounded memory
@pytest.mark.slow
def test_diff_of_200k_rows_has_bounded_memory(tmp_path):
    n = 200_000

    def write(path, mutate):
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
            w.writerow(CH_HEADER)
            for i in range(n + (2000 if mutate else 0)):
                num = f"8{i:07d}"
                name, status, pc = f"EXAMPLE COMPANY {i} LTD", "Active", "M1 1AA"
                if mutate and i < n:
                    if i % 20000 == 1:
                        name += " RENAMED"
                    elif i % 20000 == 2:
                        status = "Dissolved"
                    elif i % 20000 == 3:
                        pc = "LS1 4AP"
                w.writerow(ch_row(name, num, town="TESTTOWN", postcode=pc, status=status, sics=["62012 - Business software"]))
        return path

    prev = write(tmp_path / "BasicCompanyDataAsOneFile-2026-09-01.csv", False)
    cur = write(tmp_path / "BasicCompanyDataAsOneFile-2026-10-01.csv", True)
    size_mb = (prev.stat().st_size + cur.stat().st_size) / 1e6
    stats: dict = {}
    count = 0
    tracemalloc.start()
    for _n, _r in iter_diff(prev, cur, work_dir=tmp_path, stats=stats):
        count += 1
    peak_mb = tracemalloc.get_traced_memory()[1] / 1e6
    tracemalloc.stop()
    assert stats["new"] == 2000 and stats["name_changed"] == 10 and stats["status_changed"] == 10 and stats["address_changed"] == 10
    assert stats["removed"] == 0 and count == 2030
    assert size_mb > 100 and peak_mb < 30, f"peak python heap {peak_mb:.1f} MB for {size_mb:.0f} MB of input: the diff must not hold snapshots in RAM"


# ------------------------------------------------------------------------------------------------ scheduling
def test_compute_next_run_frequencies_and_anchor():
    now = datetime(2026, 1, 31, 9, 0, tzinfo=UTC)
    assert compute_next_run("daily", now) == now + timedelta(days=1)
    assert compute_next_run("weekly", now) == now + timedelta(days=7)
    assert compute_next_run("monthly", now) == datetime(2026, 2, 28, 9, 0, tzinfo=UTC)  # clamps to month end
    assert compute_next_run("monthly", datetime(2026, 12, 15, tzinfo=UTC)) == datetime(2027, 1, 15, tzinfo=UTC)
    anchor = datetime(2026, 1, 31, 6, 0, tzinfo=UTC)
    assert compute_next_run("daily", now, anchor) == datetime(2026, 2, 1, 6, 0, tzinfo=UTC)  # cadence does not drift with run time
    assert compute_next_run("daily", now, datetime(2026, 1, 1, 6, 0, tzinfo=UTC)) > now  # catches up after downtime
    with pytest.raises(ValueError):
        compute_next_run("hourly", now)


async def make_schedule(session, frequency, cfg, **kw):
    s = MonitorSchedule(kind="new_registrations", name=f"new regs {frequency}", frequency=frequency, config=cfg, **kw)
    session.add(s)
    await session.flush()
    return s


@pytest.mark.parametrize("freq,delta", [("daily", timedelta(days=1)), ("weekly", timedelta(days=7))])
async def test_run_monitor_window_persists_and_schedules(session, snaps, freq, delta):
    _, cur = snaps
    now = datetime(2026, 10, 2, 8, 0, tzinfo=UTC)
    s = await make_schedule(session, freq, {"current_path": str(cur), "mode": "window", "days": 30, "today": "2026-10-01"})
    res = await run_monitor(session, s, now=now)
    assert res["status"] == "ok" and res["records"] == 2 and res["created"] == 2 and res["signals_new"] == 2 and res["mode"] == "window"
    assert s.last_run_at == now and s.next_run_at == now + delta and s.last_result == res
    assert (await session.execute(select(func.count()).select_from(OrganisationDomain))).scalar_one() == 0  # no website is ever assumed
    sigs = (await session.execute(select(BusinessSignal))).scalars().all()
    assert {x.signal_type for x in sigs} == {"new_registration"} and all(x.source_url for x in sigs)
    orgs = (await session.execute(select(Organisation))).scalars().all()
    assert {o.company_number for o in orgs} == {"99990006", "99990007"}


async def test_run_monitor_not_due_disabled_force(session, snaps):
    _, cur = snaps
    now = datetime(2026, 10, 2, 8, 0, tzinfo=UTC)
    cfg = {"current_path": str(cur), "mode": "window", "days": 30, "today": "2026-10-01"}
    s = await make_schedule(session, "weekly", cfg, next_run_at=now + timedelta(days=3))
    r = await run_monitor(session, s, now=now)
    assert r["status"] == "skipped_not_due" and s.last_run_at is None and s.next_run_at == now + timedelta(days=3)
    assert (await session.execute(select(func.count()).select_from(Organisation))).scalar_one() == 0
    d = await make_schedule(session, "daily", cfg, enabled=False)
    assert (await run_monitor(session, d, now=now))["status"] == "skipped_disabled"
    r = await run_monitor(session, s, now=now, force=True)
    assert r["status"] == "ok" and s.next_run_at == compute_next_run("weekly", now, now + timedelta(days=3))
    due = await make_schedule(session, "daily", cfg, next_run_at=now - timedelta(hours=1))
    r = await run_monitor(session, due, now=now)
    assert r["status"] == "ok" and r["records"] == 2 and r["created"] == 0 and r["signals_new"] == 0 and r["signals_seen"] == 2  # idempotent re-run
    assert due.next_run_at > now


async def test_run_monitor_diff_with_stored_index_and_monthly(session, snaps, tmp_path):
    prev, cur = snaps
    now = datetime(2026, 10, 31, 3, 0, tzinfo=UTC)
    s = await make_schedule(session, "monthly", {"current_path": str(cur), "previous_path": str(prev), "store_index": str(tmp_path / "fp.sqlite")})
    res = await run_monitor(session, s, now=now)
    assert res["mode"] == "diff" and res["records"] == 6 and res["diff"]["new"] == 3 and res["diff"]["name_changed"] == 1
    assert s.next_run_at == datetime(2026, 11, 30, 3, 0, tzinfo=UTC)
    types = {x.signal_type for x in (await session.execute(select(BusinessSignal))).scalars()}
    assert types == {"new_registration", "registry_change"}
    assert SnapshotIndex(tmp_path / "fp.sqlite").count() == 7


async def test_run_monitor_records_errors_and_still_reschedules(session, tmp_path):
    now = datetime(2026, 10, 2, 8, 0, tzinfo=UTC)
    s = await make_schedule(session, "daily", {"current_path": str(tmp_path / "missing.zip")})
    res = await run_monitor(session, s, now=now)
    assert res["status"] == "error" and "NotConfigured" in res["error"] and s.last_result["status"] == "error" and s.next_run_at == now + timedelta(days=1)


async def test_run_monitor_rejects_other_kinds(session):
    s = MonitorSchedule(kind="website_changes", name="x", frequency="daily", config={})
    session.add(s)
    await session.flush()
    with pytest.raises(ValueError):
        await run_monitor(session, s)
