from sqlalchemy import text

from app import jobqueue as jq
from app.db import sessionmaker

from .helpers import seed_email, seed_org


async def _emails():
    async with sessionmaker()() as s:
        org = await seed_org(s, "Verif Ltd", country="GB")
        ids = {
            "ok": await seed_email(s, org, "ok@verif.example.com"),
            "bad": await seed_email(s, org, "bad@verif.example.com", reachability="invalid"),
            "inc": await seed_email(s, org, "inc@verif.example.com", reachability="unknown"),
            "ca": await seed_email(s, org, "ca@verif.example.com", catch_all="confirmed"),
            "never": await seed_email(s, org, "never@verif.example.com", validated=False),
            "stale": await seed_email(s, org, "stale@verif.example.com"),
        }
        await s.execute(text("UPDATE email_addresses SET recipient_evidence_status='inconclusive' WHERE id=:i"), {"i": ids["inc"]})
        await s.execute(text("UPDATE email_addresses SET last_validated_at = now() - interval '90 days' WHERE id=:i"), {"i": ids["stale"]})
        await s.execute(text("INSERT INTO validation_results (email_id, check_type, result, reason_code) VALUES (:e,'V01_syntax','pass','ok'),(:e,'V03_mx','fail','no_mx')"), {"e": ids["bad"]})
        await s.commit()
    return ids


async def test_overview_and_tabs(operator_client):
    ids = await _emails()
    c = operator_client
    o = (await c.get("/api/verification/overview")).json()
    assert o["total_emails"] == 6 and o["invalid"] == 1 and o["inconclusive"] == 1 and o["catch_all"] == 1 and o["never_validated"] == 1
    assert o["needs_recheck"] == 2 and o["smtp_enabled"] is False and o["validity_days"] == 30
    for tab, expect in {"invalid": ["bad"], "inconclusive": ["inc"], "catch_all": ["ca"], "needs_recheck": ["never", "stale"]}.items():
        got = sorted(i["id"] for i in (await c.get(f"/api/verification/emails?tab={tab}")).json()["items"])
        assert got == sorted(ids[k] for k in expect), tab
    assert (await c.get("/api/verification/emails?tab=bogus")).status_code == 422
    h = (await c.get(f"/api/verification/history?email_id={ids['bad']}")).json()
    assert [x["check_type"] for x in h["items"]] == ["V03_mx", "V01_syntax"]
    assert (await c.get("/api/verification/history?check_type=V03_mx&result=fail")).json()["items"][0]["reason_code"] == "no_mx"


async def test_batch_lifecycle_via_filters(operator_client):
    ids = await _emails()
    c = operator_client
    assert (await c.post("/api/verification/batches", json={"name": "all"})).status_code == 422  # explicit scope required
    r = await c.post("/api/verification/batches", json={"name": "Re-check stale", "mode": "fast", "filters": {"freshness": "stale"}})
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["total"] == 1 and b["status"] == "queued" and b["queued"] == 1 and b["percent"] == 0
    async with sessionmaker()() as s:
        j = (await s.execute(text("SELECT j.stage, j.payload, j.state, r.name, r.status FROM sourcing_jobs j JOIN sourcing_runs r ON r.id=j.run_id WHERE r.id=:r"), {"r": b["run_id"]})).one()
    assert j[0] == "verify" and j[1]["email_id"] == ids["stale"] and j[1]["mode"] == "fast" and j[1]["batch_id"] == b["id"] and j[3].startswith("system-verify-batch-") and j[4] == "running"
    assert (await c.get("/api/runs")).json()["items"] == []  # hidden
    p = (await c.post(f"/api/verification/batches/{b['id']}/pause")).json()
    assert p["status"] == "paused"
    async with sessionmaker()() as s:
        assert await jq.claim(s, "w", ["verify"], 5, 60) == []  # paused batch is not claimed
    assert (await c.post(f"/api/verification/batches/{b['id']}/pause")).status_code == 409
    assert (await c.post(f"/api/verification/batches/{b['id']}/resume")).json()["status"] == "running"
    async with sessionmaker()() as s:
        got = await jq.claim(s, "w", ["verify"], 5, 60)
        assert len(got) == 1
        await jq.complete(s, got[0].id)
        await s.commit()
    done = (await c.get(f"/api/verification/batches/{b['id']}")).json()
    assert done["status"] == "done" and done["done"] == 1 and done["percent"] == 100.0
    assert (await c.post(f"/api/verification/batches/{b['id']}/cancel")).status_code == 409
    b2 = (await c.post("/api/verification/batches", json={"name": "ids", "mode": "deep", "email_ids": [ids["ok"], ids["bad"]]})).json()
    assert b2["total"] == 2
    c2 = (await c.post(f"/api/verification/batches/{b2['id']}/cancel")).json()
    assert c2["status"] == "cancelled" and c2["queued"] == 0
    assert [x["id"] for x in (await c.get("/api/verification/batches")).json()["items"]] == [b2["id"], b["id"]]
    assert (await c.post("/api/verification/batches", json={"name": "bad", "mode": "custom", "options": {"nope": 1}, "email_ids": [ids["ok"]]})).status_code == 422
    assert (await c.post("/api/verification/batches", json={"name": "none", "filters": {"country": ["FR"]}})).status_code == 422  # matches nothing


async def test_batch_from_csv_upload(operator_client):
    await _emails()
    csv = b"email\nOK@verif.example.com\nbad@verif.example.com\nmissing@nowhere.example.com\nnot-an-email\n"
    r = await operator_client.post("/api/verification/batches/upload?name=From%20CSV&mode=fast", files={"file": ("emails.csv", csv, "text/csv")})
    assert r.status_code == 201, r.text
    j = r.json()
    assert j["matched"] == 2 and j["unmatched"] == 1 and j["batch"]["total"] == 2 and j["unmatched_sample"] == ["m***@nowhere.example.com"]
    bad = await operator_client.post("/api/verification/batches/upload?name=x", files={"file": ("e.csv", b"email\nnobody@nowhere.example.com\n", "text/csv")})
    assert bad.status_code == 422
    assert (await operator_client.post("/api/verification/batches/upload?name=x", files={"file": ("e.csv", b"MZ\x90\x00", "text/csv")})).status_code == 422


async def test_estimate_delegates_to_pipeline(operator_client):
    await _emails()
    c = operator_client
    r = (await c.post("/api/verification/estimate", json={"mode": "deep", "filters": {}})).json()
    assert r["addresses"] == 6 and r["cost"]["per_email"]["smtp_connections"] == 2 and r["cost"]["limitations"]
    assert any("SMTP diagnostics are not enabled" in w for w in r["warnings"])
    fast = (await c.post("/api/verification/estimate", json={"mode": "fast", "count": 1000})).json()
    assert fast["addresses"] == 1000 and fast["cost"]["per_email"]["smtp_connections"] == 0 and fast["warnings"] == []
    custom = (await c.post("/api/verification/estimate", json={"mode": "custom", "options": {"smtp": True}, "count": 10})).json()
    assert custom["cost"]["per_email"]["smtp_connections"] == 1
    assert (await c.post("/api/verification/estimate", json={"mode": "custom", "options": {"bogus": 1}, "count": 10})).status_code == 422
    assert (await c.post("/api/verification/estimate", json={"mode": "custom", "options": {"smtp_concurrency": "x"}, "count": 10})).status_code in (200, 422)


async def test_delivery_event_import(operator_client):
    ids = await _emails()
    csv = (b"email,event_type,campaign,occurred_at,detail\n"
           b"bad@verif.example.com,hard_bounce,c1,2026-01-01T10:00:00Z,mailbox full\n"
           b"ok@verif.example.com,accepted,c1,2026-01-01T10:00:00Z,\n"
           b"ca@verif.example.com,complaint,c1,2026-01-02T10:00:00Z,\n"
           b"ghost@nowhere.example.com,hard_bounce,c1,2026-01-01T10:00:00Z,\n"
           b"ok@verif.example.com,bogus_event,c1,2026-01-01T10:00:00Z,\n")
    r = await operator_client.post("/api/verification/delivery-events", files={"file": ("events.csv", csv, "text/csv")})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["received"] == 5 and j["inserted"] == 3 and j["unknown_email"] == 1 and j["invalid"] == 1 and j["suppressions_added"] == 2 and j["emails_affected"] == 3
    again = (await operator_client.post("/api/verification/delivery-events", files={"file": ("events.csv", csv, "text/csv")})).json()
    assert again["inserted"] == 0 and again["duplicates"] == 3
    d = (await operator_client.get(f"/api/contacts/{ids['bad']}")).json()
    assert d["flags"]["previously_bounced"] is True and d["delivery_counts"] == {"hard_bounce": 1} and d["marketing"]["suppressed"] is True
    js = b'[{"email":"inc@verif.example.com","event_type":"unsubscribe","occurred_at":"2026-02-01T00:00:00Z"}]'
    assert (await operator_client.post("/api/verification/delivery-events", files={"file": ("e.json", js, "application/json")})).json()["inserted"] == 1
    assert (await operator_client.post("/api/verification/delivery-events", files={"file": ("e.json", b'{"nope": 1}', "application/json")})).status_code == 422
    assert (await operator_client.post("/api/verification/delivery-events", files={"file": ("e.bin", b"\x7fELF\x00\x00", "text/csv")})).status_code == 422


async def test_smtp_status_and_domain_permissions(operator_client, admin_client):
    s = (await operator_client.get("/api/verification/smtp")).json()
    assert s["enabled"] is False and s["ready"] is False and s["problems"] and s["permission_rows"] == 0
    c = admin_client
    assert (await operator_client.put("/api/verification/domain-permissions/mydomain.example.com", json={"basis": "we own this domain outright"})).status_code == 403
    assert (await c.put("/api/verification/domain-permissions/mydomain.example.com", json={"basis": "short"})).status_code == 422  # basis must explain
    r = await c.put("/api/verification/domain-permissions/MyDomain.Example.com.", json={"basis": "We own this domain and its mail servers"})
    assert r.status_code == 200 and r.json()["domain"] == "mydomain.example.com" and r.json()["granted_by"] == c.user_id
    assert (await c.put("/api/verification/domain-permissions/not_a_domain", json={"basis": "We own this domain and its mail servers"})).status_code == 422
    assert [p["domain"] for p in (await operator_client.get("/api/verification/domain-permissions")).json()] == ["mydomain.example.com"]
    assert (await operator_client.get("/api/verification/smtp")).json()["permission_rows"] == 1
    assert (await c.delete("/api/verification/domain-permissions/mydomain.example.com")).status_code == 200
    assert (await c.delete("/api/verification/domain-permissions/mydomain.example.com")).status_code == 404
    acts = {a["action"] for a in (await c.get("/api/audit?action=smtp.*")).json()["items"]}
    assert acts == {"smtp.permission_set", "smtp.permission_delete"}
