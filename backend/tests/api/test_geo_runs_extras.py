"""geo detail delegation, UK geography endpoint, RunSummary list fields, run_metrics buckets."""
import pytest
from sqlalchemy import text

from app import db as dbmod
from app import runs
from app.run_config import RunConfig
from app.worker import Worker

pytestmark = pytest.mark.asyncio


async def test_geo_detail_is_populated(viewer_client):
    av = (await viewer_client.get("/api/geo/source-availability?countries=GB")).json()
    assert isinstance(av["detail"]["GB"], list) and av["detail"]["GB"]
    plan = (await viewer_client.get("/api/geo/coverage-plan?countries=GB&regions=north-west")).json()
    assert plan["detail"]["GB"]["strategy"] and "postcode_areas" in plan["detail"]["GB"]
    assert (await viewer_client.get("/api/geo/coverage-plan?countries=DE")).json()["detail"] is None


async def test_uk_geography(viewer_client):
    g = (await viewer_client.get("/api/geo/uk-geography")).json()
    assert {n["id"] for n in g["nations"]} >= {"england", "scotland", "wales"}
    assert g["postcode_areas"] and g["cities"] and g["regions"] and g["uncertainty"]


async def test_run_summary_fields_and_metrics(operator_client):
    cfg = RunConfig(name="sum", countries=["GB", "IE"], sources=["csv_import"], target={"count": 5})
    async with dbmod.sessionmaker()() as s:
        run = await runs.create_run(s, cfg, user_id=None)
        await runs.start_run(s, run.id)
        await s.commit()
        rid = run.id
    lst = (await operator_client.get("/api/runs")).json()["items"]
    row = next(r for r in lst if r["id"] == rid)
    assert row["countries"] == ["GB", "IE"] and row["collected"] == 0 and row["eligible"] == 0 and row["speed"] is None
    w = Worker(concurrency=1)
    async with dbmod.sessionmaker()() as s:
        await w._write_run_metrics(s, [rid], 1e9)
        await s.commit()
    async with dbmod.sessionmaker()() as s:
        m = (await s.execute(text("SELECT metrics FROM run_metrics WHERE run_id=:r"), {"r": rid})).scalar_one()
    assert m["queue_depth"] >= 1 and "jobs_done" in m
    thr = (await operator_client.get(f"/api/runs/{rid}")).json()["throughput"]
    assert thr["metrics"] and thr["metrics"][-1]["queue_depth"] >= 1
