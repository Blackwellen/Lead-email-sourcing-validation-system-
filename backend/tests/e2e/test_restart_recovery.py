"""Scenario E: kill -9 a real worker process mid-discovery, restart it, prove recovery without duplicates."""
import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path

from sqlalchemy import text

from app import db as dbmod
from app import runs
from app.run_config import RunConfig

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sources.fixtures import ch_row, write_ch_zip  # noqa: E402

N = 4000


def _spawn(env_extra):
    env = {**os.environ, **env_extra}
    return subprocess.Popen([sys.executable, "-m", "app.worker"], env=env, cwd=str(Path(__file__).resolve().parents[2]),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def test_kill_and_recover(tmp_path):
    rows = [ch_row(f"FIXTURE COMPANY {i:05d} LTD", f"9{i:07d}", town="LEEDS", postcode="LS1 4AP", sics=["68310 - Real estate agencies"]) for i in range(N)]
    zpath = write_ch_zip(tmp_path / "ch.zip", rows)
    cfg = RunConfig(name="recovery", countries=["GB"], sources=["companies_house_bulk"], source_params={"companies_house_bulk": {"path": str(zpath)}},
                    target={"count": 10_000_000})
    async with dbmod.sessionmaker()() as s:
        run = await runs.create_run(s, cfg, user_id=None)
        await runs.start_run(s, run.id)
        await s.commit()
        rid = run.id
    env = {"LEAD_WORKER_STAGES": "discover", "LEAD_WORKER_LEASE_SECONDS": "3", "LEAD_WORKER_HEARTBEAT_SECONDS": "1",
           "LEAD_DATABASE_URL": os.environ["LEAD_DATABASE_URL"], "LEAD_ENV": "test"}
    p1 = _spawn(env)

    async def orgs():
        async with dbmod.sessionmaker()() as s:
            return (await s.execute(text("select count(*) from organisations"))).scalar()

    for _ in range(200):  # wait until some, but not all, rows are persisted
        await asyncio.sleep(0.1)
        if 300 < await orgs() < N:
            break
    p1.send_signal(signal.SIGKILL)
    p1.wait()
    killed_at = await orgs()
    assert 0 < killed_at < N, "worker finished before it could be killed; raise N"

    p2 = _spawn(env)
    try:
        for _ in range(300):
            await asyncio.sleep(0.2)
            async with dbmod.sessionmaker()() as s:
                st = (await s.execute(text("select state, attempts from sourcing_jobs where stage='discover' and run_id=:r"), {"r": rid})).one()
            if st[0] == "done":
                break
        assert st[0] == "done" and st[1] >= 2, st  # lease expired, job recovered and re-run
    finally:
        p2.send_signal(signal.SIGTERM)
        p2.wait(timeout=45)
    async with dbmod.sessionmaker()() as s:
        total, distinct = (await s.execute(text("select count(*), count(distinct canonical_identity) from organisations"))).one()
        assert total == distinct == N
        assert (await s.execute(text("select count(*) from run_organisations where run_id=:r"), {"r": rid})).scalar() == N
