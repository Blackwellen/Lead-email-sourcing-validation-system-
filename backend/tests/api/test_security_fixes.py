"""Regression tests for the security review findings (numbers refer to the review)."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from app.config import get_settings
from app.db import sessionmaker
from app.sources import access_policy as ap

from .helpers import ApiClient, build_app, create_user, make_client

CFG = {"name": "Sec", "imported_domains": ["example.com"], "countries": ["GB"], "target": {"definition": "distinct_published_emails", "count": 5}}


def uploads() -> Path:
    d = Path(get_settings().data_dir) / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def _draft(c, **over) -> int:
    r = await c.post("/api/runs", json={"config": {**CFG, **over}, "action": "draft"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


# --------------------------------------------------------------------------- 1: draft autosave must not bypass validation
async def test_autosave_runs_full_validation_and_start_refuses_invalid(operator_client):
    c = operator_client
    rid = await _draft(c)
    r = await c.patch(f"/api/runs/{rid}", json={"config": {"sources": ["csv_import"], "source_params": {"csv_import": {"path": "/etc/passwd"}}}})
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is False and any(e["field"] == "source_params.csv_import.path" for e in body["errors"])
    s = await c.post(f"/api/runs/{rid}/start")
    assert s.status_code == 422 and s.json()["error"]["code"] == "invalid_run_config" and "source_params.csv_import.path" in s.json()["error"]["message"]
    d = (await c.get(f"/api/runs/{rid}")).json()
    assert d["status"] == "draft" and d["config_errors"]  # flagged on the detail view too
    async with sessionmaker()() as ses:
        assert (await ses.execute(text("SELECT count(*) FROM sourcing_jobs WHERE run_id=:r"), {"r": rid})).scalar_one() == 0
    # fixing the draft makes it startable again
    good = uploads() / "ok-import.csv"
    good.write_text("name,website\nAcme,acme.example\n")
    r = await c.patch(f"/api/runs/{rid}", json={"config": {"source_params": {"csv_import": {"path": str(good)}}}})
    assert r.json()["valid"] is True, r.text


async def test_autosave_refuses_making_a_scheduled_run_invalid(operator_client):
    from datetime import UTC, datetime, timedelta

    c = operator_client
    when = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    r = await c.post("/api/runs", json={"config": {**CFG, "schedule_at": when}, "action": "start"})
    assert r.status_code == 201 and r.json()["status"] == "scheduled"
    rid = r.json()["id"]
    bad = await c.patch(f"/api/runs/{rid}", json={"config": {"sources": ["csv_import"], "source_params": {"csv_import": {"data_dir": "/"}}}})
    assert bad.status_code == 422
    async with sessionmaker()() as ses:
        snap = (await ses.execute(text("SELECT configuration_snapshot FROM sourcing_runs WHERE id=:r"), {"r": rid})).scalar_one()
    assert "data_dir" not in json.dumps(snap)


async def test_start_refuses_disabled_source_in_draft(operator_client, admin_client):
    rid = await _draft(operator_client)
    assert (await admin_client.post("/api/sources/csv_import/disable")).status_code == 200
    await operator_client.patch(f"/api/runs/{rid}", json={"config": {"sources": ["csv_import"]}})
    r = await operator_client.post(f"/api/runs/{rid}/start")
    assert r.status_code == 422 and "disabled" in r.json()["error"]["message"]


# --------------------------------------------------------------------------- 3: path-like keys (single policy)
@pytest.mark.parametrize("key,value", [
    ("data_dir", "/tmp"), ("spill_dir", str(Path(get_settings().data_dir) / "uploads")), ("work_dir", "/tmp"),
    ("store_index", "/tmp/x.sqlite"), ("current_path", "/etc/passwd"), ("previous_path", "/etc/shadow"),
    ("previous_index", "/etc/passwd"), ("rr_path", "../../../../etc/passwd"), ("paths", ["/etc/passwd"]),
    ("file", "/proc/self/environ"), ("columnar_index_path", "/etc/hosts"),
])
async def test_run_source_params_path_policy(operator_client, key, value):
    v = (await operator_client.post("/api/runs/validate", json={**CFG, "sources": ["csv_import"], "source_params": {"csv_import": {key: value}}})).json()
    assert v["valid"] is False and any(e["field"].startswith(f"source_params.csv_import.{key}") for e in v["errors"]), v


async def test_path_policy_resolves_symlinks_and_checks_list_elements(tmp_path):
    up = uploads()
    ok = up / "fine.csv"
    ok.write_text("a\n")
    link = up / "sneaky.csv"
    if link.is_symlink() or link.exists():
        link.unlink()
    os.symlink("/etc/passwd", link)
    try:
        assert ap.config_problems({"path": str(ok)}) == []
        assert ap.config_problems({"path": str(link)})  # symlink inside uploads pointing outside
        assert ap.config_problems({"paths": [str(ok), "/etc/passwd"]})
        assert ap.config_problems({"nested": {"current_path": "/etc/passwd"}})
        assert ap.config_problems({"path": str(up)})  # the uploads directory itself is not a file
        assert ap.is_path_key("previous_index") and ap.is_path_key("paths") and ap.is_forbidden_key("spill_dir") and ap.is_forbidden_key("store_index")
        assert not ap.is_path_key("country_code")
    finally:
        link.unlink()


async def test_admin_config_rejects_dir_and_outside_paths(admin_client):
    for cfg in ({"path": "/etc/passwd"}, {"data_dir": "/"}, {"spill_dir": "/tmp"}):
        r = await admin_client.put("/api/sources/csv_import/config", json={"config": cfg})
        assert r.status_code == 422, (cfg, r.text)


async def test_monitor_config_refuses_store_index_and_private_urls(operator_client):
    base = {"kind": "new_registrations", "name": "n", "frequency": "weekly"}
    r = await operator_client.post("/api/discovery/monitors", json={**base, "config": {"store_index": str(uploads() / "fp.sqlite")}})
    assert r.status_code == 422
    r = await operator_client.post("/api/discovery/monitors", json={**base, "config": {"url": "http://169.254.169.254/latest/meta-data/"}})
    assert r.status_code == 422


# --------------------------------------------------------------------------- 4: SSRF on adapter / download / search URLs
@pytest.mark.parametrize("url", ["http://169.254.169.254/latest/meta-data/", "http://127.0.0.1:8000/", "http://localhost/", "http://10.0.0.5/x",
                                 "file:///etc/passwd", "http://user:pw@example.com/", "http://example.com:6379/"])
async def test_config_time_url_validation(operator_client, admin_client, url):
    v = (await operator_client.post("/api/runs/validate", json={**CFG, "sources": ["searxng"], "source_params": {"searxng": {"base_url": url}}})).json()
    assert v["valid"] is False and any(e["field"] == "source_params.searxng.base_url" for e in v["errors"]), v
    r = await admin_client.put("/api/sources/searxng/config", json={"config": {"base_url": url}})
    assert r.status_code == 422, r.text


async def test_safe_client_revalidates_every_redirect_hop():
    from app.crawler.safe_http import SSRFBlocked, safe_client

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "files.example.com":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
        return httpx.Response(200, text="secret")

    async with safe_client(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(SSRFBlocked):
            await c.get("https://files.example.com/data.zip")
    assert seen == ["https://files.example.com/data.zip"]  # the metadata hop was never sent


async def test_safe_client_pins_connections_to_public_addresses():
    from app.crawler.safe_http import Guard, SSRFBlocked, safe_client

    async def evil_dns(host, port):
        return ["10.1.2.3"]  # a public-looking name that resolves internally (or rebinds)

    async with safe_client(guard=Guard(resolver=evil_dns)) as c:
        with pytest.raises(SSRFBlocked):
            await c.get("https://data.example.org/file.csv")


async def test_download_and_polite_client_refuse_internal_targets(tmp_path):
    from app.sources._http import HttpFailure, PoliteClient
    from app.sources.download import DownloadError, download_file, make_client

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"location": "http://127.0.0.1:2375/containers/json"})

    cli = make_client(httpx.MockTransport(handler))
    with pytest.raises(DownloadError):
        await download_file("https://example.com/big.zip", tmp_path / "x.zip", client=cli)
    await cli.aclose()
    async with PoliteClient(client=make_client(httpx.MockTransport(handler)), min_interval_s=0) as pc:
        with pytest.raises(HttpFailure):
            await pc.get_json("https://api.example.com/v1/x")


async def test_search_provider_uses_ssrf_safe_client():
    from app.sources.search_providers import SearxngProvider

    client, own = SearxngProvider({"base_url": "https://searx.example.com", "authorised": True})._client()
    try:
        assert own and client.event_hooks["request"] and client.follow_redirects is False
    finally:
        await client.aclose()


async def test_worker_policy_assert_adapter_runnable(session, admin_client):
    from app.sources.base import NotPermitted

    ok = uploads() / "w.csv"
    ok.write_text("a\n")
    await ap.assert_adapter_runnable(session, "csv_import", {"path": str(ok)})
    for params in ({"path": "/etc/passwd"}, {"spill_dir": "/tmp"}, {"url": "http://169.254.169.254/"}, {"paths": ["/etc/passwd"]}):
        with pytest.raises(NotPermitted):
            await ap.assert_adapter_runnable(session, "csv_import", params)
    await admin_client.post("/api/sources/csv_import/disable")
    session.expire_all()
    with pytest.raises(NotPermitted, match="disabled"):
        await ap.assert_adapter_runnable(session, "csv_import", {"path": str(ok)})
    with pytest.raises(NotPermitted):
        await ap.assert_adapter_runnable(session, "no_such_adapter", {})


# --------------------------------------------------------------------------- 6: client IP behind the proxy
def _req(peer: str, xff: str | None, app):
    from starlette.requests import Request

    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers, "client": (peer, 1234), "app": app, "query_string": b""})


async def test_client_ip_uses_rightmost_untrusted_hop():
    from app.security import client_ip

    trusting = build_app(trust_proxy=True, trusted_proxies="172.31.250.0/24")
    assert client_ip(_req("172.31.250.7", "6.6.6.6, 203.0.113.9", trusting)) == "203.0.113.9"  # left-most is client-controlled
    assert client_ip(_req("172.31.250.7", "198.51.100.1, 172.31.250.9", trusting)) == "198.51.100.1"  # skip trusted hops
    assert client_ip(_req("8.8.8.8", "1.1.1.1", trusting)) == "8.8.8.8"  # untrusted peer: header ignored
    assert client_ip(_req("172.31.250.7", "not-an-ip", trusting)) == "172.31.250.7"
    plain = build_app()
    assert client_ip(_req("172.31.250.7", "203.0.113.9", plain)) == "172.31.250.7"  # trust disabled
    defaults = build_app(trust_proxy=True)  # default trusted = private ranges (docker networks)
    assert client_ip(_req("172.18.0.3", "203.0.113.50", defaults)) == "203.0.113.50"


async def test_rate_limit_is_per_forwarded_client():
    app = build_app(trust_proxy=True, trusted_proxies="127.0.0.0/8", rate_limit_per_minute=3)
    t = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
    async with httpx.AsyncClient(transport=t, base_url="http://t") as c:
        a = [(await c.get("/api/me", headers={"X-Forwarded-For": "198.51.100.1"})).status_code for _ in range(4)]
        b = (await c.get("/api/me", headers={"X-Forwarded-For": "198.51.100.2"})).status_code
    assert a == [401, 401, 401, 429] and b == 401  # another client behind the same proxy is not throttled


# --------------------------------------------------------------------------- 7: SSE must not pin DB connections
async def _idle_in_tx() -> int:
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(get_settings().database_url)
    try:
        async with eng.connect() as conn:
            return int((await conn.execute(text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND state = 'idle in transaction' AND pid <> pg_backend_pid()"))).scalar_one())
    finally:
        await eng.dispose()


async def test_many_sse_streams_do_not_exhaust_the_pool():
    app = build_app(sse_max_streams_per_user=100, sse_max_streams=100)
    async with make_client("viewer", app) as v, make_client("operator", app) as o:
        rid = (await o.post("/api/runs", json={"config": CFG, "action": "start"})).json()["id"]

        async def one():
            async with v.stream("GET", f"/api/runs/{rid}/events?max_events=3&interval=0.5") as r:
                assert r.status_code == 200
                return (await r.aread()).decode().count("event: snapshot")

        tasks = [asyncio.create_task(one()) for _ in range(35)]
        await asyncio.sleep(0.8)  # streams are open and sleeping between snapshots
        during = await _idle_in_tx()
        counts = await asyncio.wait_for(asyncio.gather(*tasks), timeout=60)
    assert counts == [3] * 35
    assert during == 0
    assert await _idle_in_tx() == 0
    from app.api.runs import SSE_SLOTS

    assert SSE_SLOTS.total == 0 and SSE_SLOTS.per_user == {}


async def test_sse_streams_capped_per_user_and_end_on_logout():
    app = build_app(sse_max_streams_per_user=1)
    async with make_client("operator", app) as o:
        rid = (await o.post("/api/runs", json={"config": CFG, "action": "start"})).json()["id"]

        async def long():
            async with o.stream("GET", f"/api/runs/{rid}/events?max_events=40&interval=0.2") as r:
                return (await r.aread()).decode()

        t = asyncio.create_task(long())
        await asyncio.sleep(0.4)
        second = await o.get(f"/api/runs/{rid}/events?max_events=1")
        assert second.status_code == 429 and second.json()["error"]["code"] == "too_many_streams"
        async with sessionmaker()() as s:  # revoke every session (e.g. admin deactivated the user / logout elsewhere)
            await s.execute(text("DELETE FROM sessions"))
            await s.commit()
        body = await asyncio.wait_for(t, timeout=30)
    assert '"reason": "session_ended"' in body and body.count("event: snapshot") < 40


# --------------------------------------------------------------------------- 8: request body size
async def test_oversized_json_is_refused_before_parsing(anon_client):
    big = b'{"email": "' + b"a" * (2 * 1024 * 1024) + b'", "password": "x"}'
    r = await anon_client.post("/api/auth/login", content=big, headers={"content-type": "application/json"})
    assert r.status_code == 413 and r.json()["error"]["code"] == "payload_too_large"

    async def chunks():  # no Content-Length: counted while streaming
        for _ in range(40):
            yield b"x" * 65536

    r = await anon_client.post("/api/auth/login", content=chunks(), headers={"content-type": "application/json"})
    assert r.status_code == 413


async def test_uploads_keep_their_own_limit():
    app = build_app(max_upload_mb=2)
    async with make_client("operator", app) as c:
        ok = await c.post("/api/sources/upload", files={"file": ("x.csv", b"name,website\n" + b"Acme,acme.example\n" * 70000, "text/csv")})
        assert ok.status_code in (201, 422) and ok.status_code != 413  # ~1.2 MB multipart passes the 1 MB JSON limit
        big = await c.post("/api/sources/upload", files={"file": ("x.csv", b"a,b\n" + b"x" * (4 * 1024 * 1024), "text/csv")})
        assert big.status_code == 413


# --------------------------------------------------------------------------- 11: secrets never reach viewers
async def test_run_detail_and_jobs_mask_source_secrets(operator_client, viewer_client):
    r = await operator_client.post("/api/runs", json={"config": {**CFG, "sources": ["brave_search"], "source_params": {"brave_search": {"api_key": "sk-live-SECRET"}}}, "action": "start"})
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    d = await viewer_client.get(f"/api/runs/{rid}")
    assert "sk-live-SECRET" not in d.text and d.json()["config"]["source_params"]["brave_search"]["api_key"] == "********"
    async with sessionmaker()() as s:
        await s.execute(text("UPDATE sourcing_jobs SET state='failed' WHERE run_id=:r"), {"r": rid})
        await s.commit()
    jobs = await viewer_client.get(f"/api/runs/{rid}/jobs")
    assert jobs.status_code == 200 and jobs.json()["items"] and "sk-live-SECRET" not in jobs.text
    failed = await viewer_client.get("/api/system/failed-jobs")
    assert "sk-live-SECRET" not in failed.text


async def test_autosave_keeps_masked_secret(operator_client):
    rid = await _draft(operator_client, sources=["brave_search"], source_params={"brave_search": {"api_key": "sk-keep-me"}})
    cfg = (await operator_client.get(f"/api/runs/{rid}")).json()["config"]
    assert cfg["source_params"]["brave_search"]["api_key"] == "********"
    await operator_client.patch(f"/api/runs/{rid}", json={"config": {"source_params": cfg["source_params"]}})  # client echoes the mask
    async with sessionmaker()() as s:
        snap = (await s.execute(text("SELECT configuration_snapshot FROM sourcing_runs WHERE id=:r"), {"r": rid})).scalar_one()
    assert snap["source_params"]["brave_search"]["api_key"] == "sk-keep-me"


def test_secret_key_regex_variants():
    from app.sources.secret_config import is_secret_key, mask_config

    for k in ("api_key", "apikey", "ApiKey", "subscription_key", "guid", "auth", "auth_token", "authorization", "password", "passwd", "pwd",
              "client_secret", "x_api_key", "bearer_token", "access_key"):
        assert is_secret_key(k), k
    for k in ("authority_codes", "authorised", "country_code", "base_url", "page_size", "keywords", "max_keywords", "path"):
        assert not is_secret_key(k), k
    assert mask_config({"guid": "123e4567", "authorised": True}) == {"guid": "********", "authorised": True}


# --------------------------------------------------------------------------- 14: taxonomy import cycles through DB rows
async def test_taxonomy_import_rejects_cycle_with_existing_rows(admin_client):
    a = (await admin_client.post("/api/categories", json={"name": "Alpha", "slug": "alpha"})).json()
    b = (await admin_client.post("/api/categories", json={"name": "Beta", "slug": "beta", "parent_id": a["id"]})).json()
    assert b["parent_id"] == a["id"]
    payload = json.dumps({"categories": [{"slug": "alpha", "name": "Alpha", "parent_slug": "beta"}]}).encode()
    r = await admin_client.post("/api/categories/import", files={"file": ("t.json", payload, "application/json")})
    assert r.status_code == 422 and "cycle" in r.json()["error"]["message"]
    async with sessionmaker()() as s:
        assert (await s.execute(text("SELECT parent_id FROM categories WHERE slug='alpha'"))).scalar_one() is None


# --------------------------------------------------------------------------- 16: deploy hardening
async def test_production_hides_docs_and_readiness_detail():
    app = build_app(env="production", secret_key="p" * 40, cookie_secure=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://t") as c:
        assert (await c.get("/api/docs")).status_code == 404
        assert (await c.get("/api/openapi.json")).status_code == 404
        j = (await c.get("/readyz")).json()
        assert set(k for k, v in j.items() if v is not None) == {"ready"}
    assert app.openapi()["paths"]  # scripts/dump_openapi.py still works
    u = await create_user(role="viewer")
    async with ApiClient(transport=httpx.ASGITransport(app=app), base_url="https://t") as c:
        assert (await c.login(u.email)).status_code == 200
        assert (await c.get("/readyz")).json()["migration"] is not None  # signed in: details


def test_production_requires_secure_cookies_and_real_key():
    with pytest.raises(RuntimeError, match="COOKIE_SECURE"):
        build_app(env="production", secret_key="p" * 40, cookie_secure=False)
    with pytest.raises(RuntimeError, match="LEAD_SECRET_KEY"):
        build_app(env="production", secret_key="change-me-in-production-please-use-32+chars", cookie_secure=True)


def test_development_generates_and_persists_a_secret_key(tmp_path):
    from app.config import Settings, ensure_secret_key

    s1 = ensure_secret_key(Settings(env="development", data_dir=str(tmp_path), secret_key="change-me-in-production-please-use-32+chars"))
    assert not s1.secret_key.startswith("change-me") and len(s1.secret_key) >= 32
    f = tmp_path / ".secret_key"
    assert f.read_text() == s1.secret_key and (f.stat().st_mode & 0o777) == 0o600
    s2 = ensure_secret_key(Settings(env="development", data_dir=str(tmp_path), secret_key="change-me-in-production-please-use-32+chars"))
    assert s2.secret_key == s1.secret_key  # stable across restarts
    t = ensure_secret_key(Settings(env="test", secret_key="change-me-in-production-please-use-32+chars"))
    assert t.secret_key.startswith("change-me")  # tests keep their deterministic key
