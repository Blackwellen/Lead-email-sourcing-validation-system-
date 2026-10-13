import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.db import sessionmaker

from .helpers import seed_email, seed_org

CFG = {"name": "UK plumbers", "imported_domains": ["example.com", "example.org"], "countries": ["GB"], "target": {"definition": "distinct_published_emails", "count": 50}}


async def create(c, action="start", **over):
    r = await c.post("/api/runs", json={"config": {**CFG, **over}, "action": action})
    assert r.status_code == 201, r.text
    return r.json()


async def test_full_lifecycle_and_illegal_transitions(operator_client):
    c = operator_client
    run = await create(c, "start")
    rid = run["id"]
    assert run["status"] == "running" and run["target_count"] == 50
    # illegal: start again, resume while running
    r = await c.post(f"/api/runs/{rid}/start")
    assert r.status_code == 409 and r.json()["error"]["code"] == "invalid_run_state"
    assert "running" in r.json()["error"]["message"]
    assert (await c.post(f"/api/runs/{rid}/resume")).status_code == 409
    # pause -> resume -> pause again is illegal twice
    assert (await c.post(f"/api/runs/{rid}/pause")).json()["status"] == "paused"
    assert (await c.post(f"/api/runs/{rid}/pause")).status_code == 409
    assert (await c.post(f"/api/runs/{rid}/resume")).json()["status"] == "running"
    # failed jobs can be retried
    async with sessionmaker()() as s:
        await s.execute(text("UPDATE sourcing_jobs SET state='failed', error_code='http_500', error_detail='boom' WHERE run_id=:r AND id = (SELECT min(id) FROM sourcing_jobs WHERE run_id=:r)"), {"r": rid})
        await s.commit()
    jobs = (await c.get(f"/api/runs/{rid}/jobs")).json()
    assert len(jobs["items"]) == 1 and jobs["items"][0]["error_code"] == "http_500"
    jid = jobs["items"][0]["id"]
    assert (await c.post(f"/api/runs/{rid}/jobs/{jid}/retry")).json()["state"] == "queued"
    assert (await c.post(f"/api/runs/{rid}/jobs/{jid}/retry")).status_code == 409
    async with sessionmaker()() as s:
        await s.execute(text("UPDATE sourcing_jobs SET state='dead' WHERE id=:i"), {"i": jid})
        await s.commit()
    r = (await c.post(f"/api/runs/{rid}/retry-failed")).json()
    assert r["retried_jobs"] == 1
    # stop
    st = (await c.post(f"/api/runs/{rid}/stop")).json()
    assert st["status"] == "stopping" and st["stop_reason"] == "stopped_by_user"
    async with sessionmaker()() as s:
        n = (await s.execute(text("SELECT count(*) FROM sourcing_jobs WHERE run_id=:r AND state='cancelled'"), {"r": rid})).scalar_one()
    assert n == 2
    # cannot archive/pause an unfinished stopping run
    assert (await c.post(f"/api/runs/{rid}/pause")).status_code == 409
    assert (await c.post(f"/api/runs/{rid}/archive")).status_code == 409  # must be stopped first
    async with sessionmaker()() as s:
        await s.execute(text("UPDATE sourcing_runs SET status='completed' WHERE id=:r"), {"r": rid})
        await s.commit()
    assert (await c.post(f"/api/runs/{rid}/archive")).json()["archived"] is True
    assert (await c.post("/api/runs/999999/pause")).status_code == 404


async def test_run_actions_are_audited(admin_client):
    c = admin_client
    run = await create(c, "start")
    await c.post(f"/api/runs/{run['id']}/pause")
    await c.post(f"/api/runs/{run['id']}/resume")
    await c.post(f"/api/runs/{run['id']}/stop")
    acts = [a["action"] for a in (await c.get("/api/audit?action=run.*&limit=50")).json()["items"]]
    assert {"run.create", "run.pause", "run.resume", "run.stop"} <= set(acts)


async def test_draft_autosave_schedule_duplicate_delete(operator_client):
    c = operator_client
    d = await create(c, "draft")
    assert d["status"] == "draft"
    p = await c.patch(f"/api/runs/{d['id']}", json={"config": {"target": {"definition": "distinct_companies", "count": 7}}, "name": "Renamed"})
    assert p.status_code == 200 and p.json()["valid"] is True
    assert p.json()["run"]["target_count"] == 7 and p.json()["run"]["name"] == "Renamed"
    bad = await c.patch(f"/api/runs/{d['id']}", json={"config": {"countries": []}})
    assert bad.status_code == 200 and bad.json()["valid"] is False and bad.json()["errors"]
    # incomplete draft cannot be started
    r = await c.post(f"/api/runs/{d['id']}/start")
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_run_config"
    await c.patch(f"/api/runs/{d['id']}", json={"config": {"countries": ["GB"]}})
    assert (await c.post(f"/api/runs/{d['id']}/start")).json()["status"] == "running"
    assert (await c.delete(f"/api/runs/{d['id']}")).status_code == 409  # only drafts
    dup = await c.post(f"/api/runs/{d['id']}/duplicate")
    assert dup.status_code == 201 and dup.json()["status"] == "draft" and dup.json()["name"].endswith("(copy)")
    assert (await c.delete(f"/api/runs/{dup.json()['id']}")).status_code == 200
    assert (await c.get(f"/api/runs/{dup.json()['id']}")).status_code == 404
    # scheduling
    when = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    s = await create(c, "start", schedule_at=when)
    assert s["status"] == "scheduled" and s["scheduled_for"]
    r = await c.post("/api/runs", json={"config": {**CFG, "schedule_at": "2001-01-01T00:00:00Z"}, "action": "start"})
    assert r.status_code == 422


async def test_list_tabs_search_and_pagination(operator_client):
    c = operator_client
    for i in range(5):
        await create(c, "draft", name=f"Draft {i}")
    running = await create(c, "start", name="Alpha active")
    paused = await create(c, "start", name="Beta paused")
    await c.post(f"/api/runs/{paused['id']}/pause")
    tabs = (await c.get("/api/runs/tab-counts")).json()
    assert tabs["drafts"] == 5 and tabs["active"] == 1 and tabs["paused"] == 1
    assert [r["id"] for r in (await c.get("/api/runs?tab=active")).json()["items"]] == [running["id"]]
    assert [r["id"] for r in (await c.get("/api/runs?tab=paused")).json()["items"]] == [paused["id"]]
    assert [r["name"] for r in (await c.get("/api/runs?q=alp")).json()["items"]] == ["Alpha active"]
    assert (await c.get("/api/runs?tab=nope")).status_code == 422
    seen, cur = [], None
    while True:
        j = (await c.get("/api/runs", params={"limit": 3, **({"cursor": cur} if cur else {})})).json()
        seen += [r["id"] for r in j["items"]]
        cur = j["next_cursor"]
        if not cur:
            break
    assert len(seen) == len(set(seen)) == 7


async def test_detail_has_tracker_funnel_throughput_and_honest_eta(operator_client):
    c = operator_client
    run = await create(c, "start")
    d = (await c.get(f"/api/runs/{run['id']}")).json()
    assert d["stop_condition"] == {"definition": "distinct_published_emails", "target": 50, "progress": 0, "percent": 0.0, "max_runtime_minutes": None, "max_work_jobs": None, "stop_reason": None}
    assert {(f["stage"], f["state"]): f["count"] for f in d["funnel"]} == {("import_domain", "queued"): 2}
    assert d["queue_backlog"] == {"import_domain": 2}
    assert d["limiting_stage"] == "import_domain"
    assert d["eta"]["status"] == "unknown" and d["eta"]["reason"]  # never invented
    assert d["throughput"]["window_minutes"] == 15 and "effective" in d["effective_concurrency"]
    assert d["rate_limited_sources"] == []


async def test_eta_range_from_observed_rate(operator_client):
    c = operator_client
    run = await create(c, "start", target={"definition": "distinct_published_emails", "count": 100})
    async with sessionmaker()() as s:
        org = await seed_org(s)
        for i in range(12):
            eid = await seed_email(s, org, f"p{i}@acme{i}.example.com")
            await s.execute(text("INSERT INTO run_yield (run_id, unique_key, email_id, organisation_id, qualified, created_at) VALUES (:r, :k, :e, :o, true, now() - make_interval(mins => :m))"),
                            {"r": run["id"], "k": f"email:{i}", "e": eid, "o": org, "m": i * 3})
        await s.execute(text("UPDATE sourcing_runs SET started_at = now() - interval '2 hours' WHERE id=:r"), {"r": run["id"]})
        await s.commit()
    eta = (await c.get(f"/api/runs/{run['id']}")).json()["eta"]
    assert eta["status"] == "estimated" and eta["remaining"] == 88
    assert 0 < eta["eta_seconds_min"] <= eta["eta_seconds_max"] and eta["rate_per_minute_min"] <= eta["rate_per_minute_max"]
    # contacts of the run
    async with sessionmaker()() as s:
        ev = (await s.execute(text("INSERT INTO evidence_sources (source_adapter, source_url, evidence_type) VALUES ('company_websites','https://acme.example.com/contact','mailto') RETURNING id"))).scalar_one()
        await s.execute(text("INSERT INTO contact_evidence (email_id, evidence_id, run_id) SELECT id, :ev, :r FROM email_addresses LIMIT 3"), {"ev": ev, "r": run["id"]})
        await s.commit()
    cs = (await c.get(f"/api/runs/{run['id']}/contacts")).json()
    assert len(cs["items"]) == 3
    assert len((await c.get(f"/api/runs/{run['id']}/contacts?qualified_only=true")).json()["items"]) == 12


async def test_validate_and_estimate(operator_client):
    c = operator_client
    ok = (await c.post("/api/runs/validate", json=CFG)).json()
    assert ok["valid"] is True
    bad = (await c.post("/api/runs/validate", json={"name": "", "sources": ["x"]})).json()
    assert bad["valid"] is False and "name" in " ".join(e["field"] for e in bad["errors"])
    bad2 = (await c.post("/api/runs/validate", json={"name": "x", "countries": ["ZZ"], "imported_domains": ["http://bad_domain", "ok.example.com"], "sources": ["company_websites"],
                                                     "source_params": {"company_websites": {"path": "/etc/passwd"}}, "category_ids": [9999]})).json()
    msgs = " | ".join(e["message"] for e in bad2["errors"])
    assert "ISO" in msgs and "valid domain" in msgs and "uploaded" in msgs and "does not exist" in msgs
    est = (await c.post("/api/runs/estimate", json={**CFG, "verification": {"mode": "deep"}, "performance": {"preset": "advanced", "browser_workers": 2}})).json()
    assert est["valid"] is True and est["infra"]["effective_http_concurrency"] == 128
    codes = {r["code"] for r in est["restrictions"]}
    assert "smtp_not_enabled" in codes
    assert any(s["resource"] == "worker_processes" for s in est["shortages"])  # no workers running in tests
    assert est["assumptions"] and est["estimated_businesses_needed"] is None
    assert (await c.post("/api/runs/estimate", json={"name": ""})).json()["valid"] is False


async def test_sse_stream_emits_snapshots(operator_client):
    c = operator_client
    run = await create(c, "start")
    async with c.stream("GET", f"/api/runs/{run['id']}/events?max_events=2&interval=0.2") as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        body = (await r.aread()).decode()
    events = [b for b in body.split("\n\n") if "event: snapshot" in b]
    assert len(events) == 2
    snap = json.loads(events[0].split("data: ", 1)[1])
    assert snap["run_id"] == run["id"] and snap["status"] == "running" and snap["job_states"] == {"queued": 2} and "throughput" in snap
    assert (await c.get("/api/runs/999999/events")).status_code == 404


async def test_sse_requires_auth(anon_client):
    assert (await anon_client.get("/api/runs/1/events")).status_code == 401


async def test_system_runs_are_hidden(operator_client):
    c = operator_client
    async with sessionmaker()() as s:
        from app.system_runs import ensure_system_run

        rid = await ensure_system_run(s, "system-refresh")
        await s.commit()
    assert (await c.get("/api/runs")).json()["items"] == []
    assert (await c.get(f"/api/runs/{rid}")).status_code == 404
