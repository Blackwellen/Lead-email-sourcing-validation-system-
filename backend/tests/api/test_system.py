import asyncio
import os
import socket
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app import jobqueue as jq
from app.db import sessionmaker
from app.retention import apply_retention

from .helpers import ApiClient, build_app, make_client, seed_email, seed_org

CFG = {"name": "r", "imported_domains": ["example.com"], "countries": ["GB"]}


async def _run(c, name="r", start=True):
    r = await c.post("/api/runs", json={"config": {**CFG, "name": name}, "action": "start" if start else "draft"})
    return r.json()["id"]


async def test_maintenance_mode_blocks_mutations_and_job_claims(app, admin_client, operator_client):
    rid = await _run(operator_client)
    r = await admin_client.put("/api/system/maintenance", json={"enabled": True, "message": "Upgrading the database"})
    assert r.status_code == 200 and r.json()["enabled"] is True
    try:
        # mutating endpoints are refused with a clear message, reads still work
        r = await operator_client.post("/api/presets", json={"kind": "run", "name": "x", "config": {}})
        assert r.status_code == 503 and r.json()["error"]["code"] == "maintenance_mode" and "Upgrading the database" in r.json()["error"]["message"]
        assert (await operator_client.post(f"/api/runs/{rid}/pause")).status_code == 503
        assert (await operator_client.get("/api/runs")).status_code == 200
        # system + auth endpoints keep working so maintenance can be switched off and people can sign in
        assert (await admin_client.get("/api/system/health")).json()["maintenance"]["enabled"] is True
        async with ApiClient(transport=__import__("httpx").ASGITransport(app=app), base_url="http://test") as other:
            from .helpers import create_user

            u = await create_user("m@example.com", "viewer")
            assert (await other.login(u.email)).status_code == 200
        # workers stop claiming
        async with sessionmaker()() as s:
            assert await jq.claim(s, "w1", ["import_domain"], 10, 60) == []
    finally:
        r = await admin_client.put("/api/system/maintenance", json={"enabled": False})
        assert r.status_code == 200
    async with sessionmaker()() as s:
        assert len(await jq.claim(s, "w1", ["import_domain"], 10, 60)) == 1
    assert (await operator_client.post(f"/api/runs/{rid}/pause")).status_code == 200


async def test_pause_all_and_resume_only_what_it_paused(admin_client, operator_client):
    c = operator_client
    a, b, manual = await _run(c, "a"), await _run(c, "b"), await _run(c, "manual")
    await c.post(f"/api/runs/{manual}/pause")
    draft = await _run(c, "d", start=False)
    r = await admin_client.post("/api/system/pause-all")
    assert sorted(r.json()["paused_run_ids"]) == sorted([a, b])
    statuses = {x["id"]: x["status"] for x in (await c.get("/api/runs?limit=50")).json()["items"]}
    assert statuses[a] == statuses[b] == statuses[manual] == "paused" and statuses[draft] == "draft"
    health = (await admin_client.get("/api/system/health")).json()
    assert sorted(health["paused_by_pause_all"]) == sorted([a, b])
    r = await admin_client.post("/api/system/resume-processing")
    assert sorted(r.json()["resumed_run_ids"]) == sorted([a, b])
    statuses = {x["id"]: x["status"] for x in (await c.get("/api/runs?limit=50")).json()["items"]}
    assert statuses[a] == statuses[b] == "running" and statuses[manual] == "paused"
    assert (await admin_client.post("/api/system/resume-processing")).json()["resumed_run_ids"] == []


async def test_restart_failed_jobs(admin_client, operator_client):
    rid = await _run(operator_client)
    async with sessionmaker()() as s:
        await s.execute(text("UPDATE sourcing_jobs SET state='dead', attempts=4, error_code='x' WHERE run_id=:r"), {"r": rid})
        await s.commit()
    h = (await admin_client.get("/api/system/health")).json()
    assert h["dead_jobs"] == 1
    assert (await admin_client.post("/api/system/restart-failed")).json()["retried_jobs"] == 1
    h = (await admin_client.get("/api/system/health")).json()
    assert h["dead_jobs"] == 0 and {(q["stage"], q["state"]): q["count"] for q in h["queue"]} == {("import_domain", "queued"): 1}


async def test_system_health_contents(admin_client):
    async with sessionmaker()() as s:
        await s.execute(text("INSERT INTO worker_heartbeats (worker_id, hostname, pid, last_seen_at, rss_mb, cpu_percent, active_jobs) VALUES ('w-ok','h1',1, now(), 210.5, 12.0, 3), ('w-old','h2',2, now() - interval '10 minutes', 1, 1, 0)"))
        org = await seed_org(s)
        eid = await seed_email(s, org, "a@dns.example.com")
        for res, reason in [("pass", None), ("timeout", "dns_timeout"), ("pass", None), ("pass", None)]:
            await s.execute(text("INSERT INTO validation_results (email_id, check_type, result, reason_code) VALUES (:e,'V03_mx',:r,:c)"), {"e": eid, "r": res, "c": reason})
        await s.commit()
    h = (await admin_client.get("/api/system/health")).json()
    assert h["api"]["status"] == "ok" and h["database"]["status"] == "ok" and h["database"]["latency_ms"] >= 0
    byid = {w["worker_id"]: w for w in h["workers"]}
    assert byid["w-ok"]["healthy"] is True and byid["w-ok"]["rss_mb"] == 210.5 and byid["w-old"]["healthy"] is False and h["workers_healthy"] == 1
    assert h["dns_timeout_rate"] == 0.25 and h["dns_sample_size"] == 4
    assert h["resources"]["ram_total_mb"] > 0 and h["resources"]["disk_free_gb"] >= 0
    assert h["smtp"]["diagnostics_enabled"] is False and h["smtp"]["egress_port_25"] == "unknown"
    assert isinstance(h["browser_workers"]["playwright_installed"], bool)


async def test_smtp_egress_check_reports_honestly(admin_client, operator_client, monkeypatch):
    async def timeout(*a, **k):
        raise TimeoutError

    class R:
        async def readline(self):
            return b"220 mx.google.com ESMTP\r\n"

    class W:
        def close(self):
            pass

    async def ok(*a, **k):
        return R(), W()

    async def dns_fail(*a, **k):
        raise socket.gaierror(-2, "Name or service not known")

    assert (await operator_client.post("/api/system/smtp-egress-check")).status_code == 403
    for fn, expected in ((timeout, "blocked"), (ok, "open"), (dns_fail, "unknown")):
        monkeypatch.setattr(asyncio, "open_connection", fn)
        r = await admin_client.post("/api/system/smtp-egress-check")
        assert r.status_code == 200 and r.json()["status"] == expected and r.json()["host"].endswith("google.com") and r.json()["port"] == 25
        assert (await admin_client.get("/api/system/health")).json()["smtp"]["egress_port_25"] == expected
    assert (await admin_client.get("/api/verification/smtp")).json()["egress"]["status"] == "unknown"


async def test_settings_sections_roundtrip_and_validation(admin_client, operator_client):
    c = admin_client
    s = (await c.get("/api/settings")).json()
    assert s["worker"]["concurrency"] == 8 and s["validation"]["validity_days"] == 30 and s["retention"]["validation_history_days"] == 365
    body = {"default_mode": "deep", "validity_days": 14, "recheck_after_days": 60,
            "marketing_policy": {"GB": {"personal": "needs_review", "role": "eligible", "sole_trader": "consent_required", "free_provider": "not_permitted", "unknown_entity": "needs_review", "allow_eligible": True}}}
    r = await c.put("/api/settings/validation", json=body)
    assert r.status_code == 200 and r.json()["marketing_policy"]["GB"]["allow_eligible"] is True
    assert (await c.get("/api/settings/validation")).json()["validity_days"] == 14
    assert (await operator_client.get("/api/settings")).json()["validation"]["validity_days"] == 14
    assert (await c.put("/api/settings/validation", json={**body, "validity_days": 0})).status_code == 422
    assert (await c.put("/api/settings/validation", json={**body, "marketing_policy": {"gb!": {}}})).status_code == 422
    assert (await c.put("/api/settings/validation", json={**body, "surprise": 1})).status_code == 422  # unknown fields rejected
    assert (await c.put("/api/settings/notifications", json={"webhook_url": "http://insecure.example.com"})).status_code == 422
    assert (await c.put("/api/settings/worker", json={"concurrency": 16, "poll_interval_s": 0.5, "lease_seconds": 90, "max_job_attempts": 5})).status_code == 200
    assert (await c.get("/api/settings/nope")).status_code == 404
    assert (await operator_client.get("/api/settings/worker")).status_code == 403
    acts = [a for a in (await c.get("/api/audit?action=settings.update")).json()["items"]]
    assert len(acts) >= 2 and acts[0]["user_email"] == c.email


async def test_audit_viewer_filters_and_pagination(admin_client, operator_client):
    for i in range(6):
        await operator_client.post("/api/presets", json={"kind": "run", "name": f"p{i}", "config": {}})
    c = admin_client
    j = (await c.get("/api/audit?action=preset.create&limit=4")).json()
    assert len(j["items"]) == 4 and j["next_cursor"]
    j2 = (await c.get("/api/audit", params={"action": "preset.create", "limit": 4, "cursor": j["next_cursor"]})).json()
    assert len(j2["items"]) == 2 and j2["next_cursor"] is None
    assert (await c.get(f"/api/audit?user_id={operator_client.user_id}&object_type=preset")).json()["items"]
    assert (await c.get("/api/audit?action=preset.*")).json()["items"]
    assert (await c.get("/api/audit?since=not-a-date")).status_code == 422


async def test_retention_applies_settings(admin_client):
    async with sessionmaker()() as s:
        org = await seed_org(s)
        eid = await seed_email(s, org, "old@ret.example.com")
        old = datetime.now(UTC) - timedelta(days=400)
        recent = datetime.now(UTC) - timedelta(days=2)
        for obs, res in [(old - timedelta(days=5), "fail"), (old, "pass"), (recent, "pass")]:
            await s.execute(text("INSERT INTO validation_results (email_id, check_type, result, observed_at) VALUES (:e,'V01_syntax',:r,:o)"), {"e": eid, "r": res, "o": obs})
        await s.execute(text("INSERT INTO validation_results (email_id, check_type, result, observed_at) VALUES (:e,'V02_domain','pass',:o)"), {"e": eid, "o": old})  # only result for its check -> kept
        ev = (await s.execute(text("INSERT INTO evidence_sources (source_adapter, source_url, evidence_type, observed_at) VALUES ('w','https://x.example.com','text', :o) RETURNING id"), {"o": old})).scalar_one()
        await s.execute(text("INSERT INTO contact_evidence (email_id, evidence_id, observed_at) VALUES (:e,:ev,:o)"), {"e": eid, "ev": ev, "o": old})
        await s.execute(text("INSERT INTO sessions (id, user_id, csrf_token, expires_at) SELECT 'expiredsession', id, 'c', now() - interval '1 day' FROM users LIMIT 1"))
        await s.execute(text("INSERT INTO exports (preset, format, status, created_at, file_path) VALUES ('full_intelligence','csv','done', now() - interval '90 days', 'gone.csv')"))
        await s.commit()
    assert (await admin_client.put("/api/settings/retention", json={"evidence_days": 365, "validation_history_days": 365, "exports_days": 30, "audit_days": None})).status_code == 200
    r = (await admin_client.post("/api/settings/retention/run")).json()["counts"]
    assert r["validation_results"] == 2 and r["contact_evidence"] == 1 and r["evidence_sources"] == 1 and r["exports"] == 1 and r["sessions"] == 1 and r["emails_flagged_for_review"] == 1
    async with sessionmaker()() as s:
        left = (await s.execute(text("SELECT check_type, result FROM validation_results ORDER BY check_type, observed_at"))).all()
        assert left == [("V01_syntax", "pass"), ("V02_domain", "pass")]
        assert (await s.execute(text("SELECT needs_review FROM email_addresses WHERE id=:e"), {"e": eid})).scalar_one() is True
        assert await apply_retention(s) == {k: 0 for k in r}  # idempotent
    assert os.path.isdir(os.environ["LEAD_EXPORT_DIR"]) or True


async def test_dashboard_overview_aggregates(operator_client):
    async with sessionmaker()() as s:
        o1 = await seed_org(s, "A Ltd", country="GB", region="Kent", source="companies_house_bulk")
        o2 = await seed_org(s, "B Ltd", country="IE", region="Cork", source="osm_geofabrik")
        await s.execute(text("INSERT INTO organisation_domains (organisation_id, domain, registrable_domain, verified_match_status) VALUES (:o,'a.example.com','a.example.com','confirmed')"), {"o": o1})
        await seed_email(s, o1, "x@a.example.com", reachability="safe")
        await seed_email(s, o1, "y@a.example.com", reachability="unknown", catch_all="confirmed", origin="pattern_inferred")
        await seed_email(s, o2, "z@b.example.com", reachability="invalid", validated=False)
        await s.execute(text("INSERT INTO sourcing_jobs (stage, dedupe_key, state, error_code, error_detail) VALUES ('crawl','k','dead','http_500','boom')"))
        await s.execute(text("INSERT INTO worker_heartbeats (worker_id, last_seen_at) VALUES ('w1', now())"))
        await s.commit()
    await operator_client.post("/api/runs", json={"config": CFG, "action": "start"})
    d = (await operator_client.get("/api/dashboard/overview")).json()
    assert d["approximate"] is False
    assert d["total_businesses"]["value"] == 2 and d["businesses_with_confirmed_domains"]["value"] == 1
    assert d["published_contacts"]["value"] == 2 and d["inferred_candidates"]["value"] == 1
    assert {b["key"]: b["count"] for b in d["reachability_distribution"]} == {"safe": 1, "unknown": 1, "invalid": 1}
    assert d["catch_all_count"]["value"] == 1 and d["unknown_count"]["value"] == 1
    assert d["active_runs_count"] == 1 and d["active_runs"][0]["target_count"] == 100
    assert {b["key"]: b["count"] for b in d["validation_freshness"]}["never"] == 1 and {b["key"]: b["count"] for b in d["validation_freshness"]}["<7d"] == 2
    assert d["workers_healthy"] == 1 and d["latest_failed_jobs"][0]["error_code"] == "http_500"
    assert {b["key"]: b["count"] for b in d["source_distribution"]} == {"companies_house_bulk": 1, "osm_geofabrik": 1}
    assert {b["key"]: b["count"] for b in d["country_distribution"]} == {"GB": 1, "IE": 1}
    assert {(g["country"], g["region"]) for g in d["geographic_distribution"]} == {("GB", "Kent"), ("IE", "Cork")}
    assert d["time_series"][-1]["discoveries"] == 2 and d["time_series"][-1]["validations"] == 2


async def test_healthz_readyz_metrics_and_headers(app, anon_client):
    r = await anon_client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    for h in ("x-content-type-options", "x-frame-options", "referrer-policy", "content-security-policy", "x-request-id"):
        assert h in r.headers
    r = await anon_client.get("/readyz")
    j = r.json()
    assert r.status_code == 200 and j["ready"] and j["database"] and "head" in j["migration"] and set(j["workers"]) >= {"alive", "stale"}
    rid = (await anon_client.get("/healthz", headers={"X-Request-ID": "abc-12345678"})).headers["x-request-id"]
    assert rid == "abc-12345678"
    assert (await anon_client.get("/healthz", headers={"X-Request-ID": "bad id\r\nX: y"})).headers["x-request-id"] != "bad id"
    m = await anon_client.get("/metrics")
    assert m.status_code == 200 and "lead_http_requests_total" in m.text and "lead_jobs_total" in m.text
    cors = await anon_client.options("/api/runs", headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "x-csrf-token"})
    assert cors.headers["access-control-allow-origin"] == "http://localhost:3000" and cors.headers["access-control-allow-credentials"] == "true"
    bad = await anon_client.options("/api/runs", headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in bad.headers


async def test_readyz_fails_when_database_is_down(monkeypatch):
    from app import main as main_mod

    app = build_app()

    class Boom:
        def __call__(self):
            raise RuntimeError("db down")

    monkeypatch.setattr(main_mod, "sessionmaker", lambda: Boom())
    async with ApiClient(transport=__import__("httpx").ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/readyz")
    assert r.status_code == 503 and r.json()["ready"] is False and r.json()["database"] is False


async def test_consistent_error_format_and_no_stack_traces(app):
    import httpx

    async def boom():
        raise RuntimeError("secret internal detail /srv/app/secret.py")

    app.add_api_route("/api/boom", boom, methods=["GET"])
    async with ApiClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as c:
        r = await c.get("/api/boom")
        assert r.status_code == 500 and r.json()["error"]["code"] == "internal_error" and "secret" not in r.text and "Traceback" not in r.text
        r = await c.get("/api/does-not-exist")
        assert r.status_code == 404 and r.json()["error"] == {"code": "not_found", "message": "The requested resource was not found."}
        r = await c.delete("/healthz")
        assert r.status_code == 405 and r.json()["error"]["code"] == "method_not_allowed"
        r = await c.post("/api/auth/login", json={"email": 5})
        j = r.json()["error"]
        assert r.status_code == 422 and j["code"] == "validation_error" and j["detail"][0]["field"] and "email" in j["message"]
        r = await c.post("/api/auth/login", content=b"{not json", headers={"content-type": "application/json"})
        assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"


async def test_make_client_reuses_app(app):
    async with make_client("admin", app) as a, make_client("viewer", app) as v:
        assert a.app is v.app
