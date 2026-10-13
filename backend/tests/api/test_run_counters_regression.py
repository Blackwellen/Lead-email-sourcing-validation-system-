"""Regression: pause/resume bookkeeping must never break readers of sourcing_runs.counters (GET /api/runs, resume, evaluate_run)."""
from sqlalchemy import text

from app import runs
from app.db import sessionmaker

from .test_runs import create


async def test_pause_resume_then_list_and_evaluate(operator_client):
    c = operator_client
    run = await create(c, "start")
    rid = run["id"]
    async with sessionmaker()() as s:  # a freshly started run has empty counters (nothing evaluated yet)
        await s.execute(text("UPDATE sourcing_runs SET counters='{}'::jsonb WHERE id=:r"), {"r": rid})
        await s.commit()
    assert (await c.post(f"/api/runs/{rid}/pause")).status_code == 200
    r = await c.post(f"/api/runs/{rid}/resume")
    assert r.status_code == 200, r.text
    assert (await c.get("/api/runs")).status_code == 200
    assert (await c.get(f"/api/runs/{rid}")).status_code == 200
    async with sessionmaker()() as s:
        counters = (await s.execute(text("SELECT counters FROM sourcing_runs WHERE id=:r"), {"r": rid})).scalar_one()
        assert "companies" in counters and "lifecycle" in counters and "paused_seconds" not in counters
        await runs.evaluate_run(s, rid)
        await s.commit()
        counters = (await s.execute(text("SELECT counters FROM sourcing_runs WHERE id=:r"), {"r": rid})).scalar_one()
        assert "lifecycle" in counters and "companies" in counters
    # a legacy / partial counters dict never makes readers fail
    assert runs.progress_value("distinct_published_emails", {"paused_seconds": 3}) == 0
    async with sessionmaker()() as s:
        await s.execute(text("""UPDATE sourcing_runs SET counters='{"paused_seconds": 5}'::jsonb WHERE id=:r"""), {"r": rid})
        await s.commit()
    assert (await c.get("/api/runs")).status_code == 200
    assert (await c.post(f"/api/runs/{rid}/pause")).status_code == 200
    assert (await c.post(f"/api/runs/{rid}/resume")).status_code == 200
