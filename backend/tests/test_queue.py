import asyncio

from sqlalchemy import text

from app import jobqueue as q
from app.db import sessionmaker
from app.models import SourcingRun


async def _run(session, status="running"):
    r = SourcingRun(name="t", status=status)
    session.add(r)
    await session.commit()
    return r.id


async def test_enqueue_idempotent(session):
    rid = await _run(session)
    a = await q.enqueue(session, run_id=rid, stage="crawl", dedupe_key="x")
    b = await q.enqueue(session, run_id=rid, stage="crawl", dedupe_key="x")
    await session.commit()
    assert a and b is None


async def test_paused_run_jobs_not_claimed(session):
    rid = await _run(session, "paused")
    await q.enqueue(session, run_id=rid, stage="crawl", dedupe_key="x")
    await session.commit()
    assert await q.claim(session, "w1", ["crawl"], 5, 60) == []
    await session.execute(text("update sourcing_runs set status='running'"))
    await session.commit()
    assert len(await q.claim(session, "w1", ["crawl"], 5, 60)) == 1


async def test_skip_locked_no_double_claim(session):
    rid = await _run(session)
    for i in range(50):
        await q.enqueue(session, run_id=rid, stage="crawl", dedupe_key=str(i))
    await session.commit()

    async def worker(name):
        async with sessionmaker()() as s:
            got = []
            while True:
                jobs = await q.claim(s, name, ["crawl"], 7, 60)
                if not jobs:
                    return got
                got += [j.id for j in jobs]

    res = await asyncio.gather(*(worker(f"w{i}") for i in range(5)))
    flat = [i for r in res for i in r]
    assert len(flat) == 50 and len(set(flat)) == 50


async def test_lease_expiry_recovery_and_dead_letter(session):
    rid = await _run(session)
    await q.enqueue(session, run_id=rid, stage="crawl", dedupe_key="x")
    await session.commit()
    (job,) = await q.claim(session, "w1", ["crawl"], 1, 60)
    await session.execute(text("update sourcing_jobs set leased_until = now() - interval '1 second'"))
    await session.commit()
    assert await q.reap_expired(session, max_attempts=3) == 1
    (job,) = await q.claim(session, "w2", ["crawl"], 1, 60)
    assert job.attempts == 2
    await session.execute(text("update sourcing_jobs set leased_until = now() - interval '1 second', attempts=3"))
    await session.commit()
    await q.reap_expired(session, max_attempts=3)
    st = (await session.execute(text("select state from sourcing_jobs"))).scalar()
    assert st == "dead"
    assert await q.retry_jobs(session, run_id=rid) == 1
    await session.commit()
    assert (await session.execute(text("select state from sourcing_jobs"))).scalar() == "queued"
