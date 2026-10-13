import pytest
from sqlalchemy import text

from app.db import sessionmaker

from .helpers import seed_bulk_emails, seed_contact, seed_email, seed_org


async def _seed_detail():
    async with sessionmaker()() as s:
        org = await seed_org(s, "Acme Plumbing Ltd", company_number="01234567")
        cid = await seed_contact(s, org)
        eid = await seed_email(s, org, "jane@acme-plumbing.example.com", contact_id=cid, confidence=88)
        await s.execute(text("INSERT INTO organisation_domains (organisation_id, domain, registrable_domain, website_url, verified_match_status, confidence) VALUES (:o,'acme-plumbing.example.com','acme-plumbing.example.com','https://acme-plumbing.example.com','confirmed',0.97)"), {"o": org})
        ev = (await s.execute(text("INSERT INTO evidence_sources (source_adapter, source_url, evidence_type, licence) VALUES ('company_websites','https://acme-plumbing.example.com/contact','mailto','robots-ok') RETURNING id"))).scalar_one()
        await s.execute(text("INSERT INTO contact_evidence (email_id, contact_id, organisation_id, evidence_id, extraction_method, context) VALUES (:e,:c,:o,:ev,'mailto_link','{\"name\":\"Jane Smith\"}')"), {"e": eid, "c": cid, "o": org, "ev": ev})
        await s.execute(text("INSERT INTO validation_results (email_id, check_type, result, reason_code, expires_at) VALUES (:e,'V01_syntax','pass','ok', now() + interval '1 day'),(:e,'V03_mx','pass','mx_found', now() - interval '1 day')"), {"e": eid})
        await s.execute(text("INSERT INTO delivery_events (email_id, event_type, campaign, occurred_at) VALUES (:e,'hard_bounce','c1', now())"), {"e": eid})
        await s.commit()
    return org, eid


async def test_contact_detail_has_full_explanation(operator_client):
    org, eid = await _seed_detail()
    d = (await operator_client.get(f"/api/contacts/{eid}")).json()
    assert d["email"] == "jane@acme-plumbing.example.com" and d["published_or_inferred"] == "Published"
    assert d["contact_profile"]["full_name"] == "Jane Smith" and d["organisation"]["company_number"] == "01234567"
    assert d["sources"][0]["source_url"] == "https://acme-plumbing.example.com/contact"
    assert {v["check_type"] for v in d["validation_latest"]} == {"V01_syntax", "V03_mx"}
    assert any(v["expired"] for v in d["validation_latest"])
    assert set(d["statuses"]) == {"syntax", "domain_mail", "recipient_evidence", "overall_reachability", "marketing"}
    assert d["delivery_counts"] == {"hard_bounce": 1}
    assert d["freshness"]["stale"] is False and d["marketing"]["eligible"] is True
    text_ = " ".join(d["classification_explanation"])
    assert "published" in text_ and "https://acme-plumbing.example.com/contact" in text_
    assert "Jane Smith" in text_ and "hard-bounced" in text_ and "not legal advice" in text_
    assert d["domains"][0]["status"] == "confirmed"
    assert (await operator_client.get("/api/contacts/999999")).status_code == 404


async def test_keyset_pagination_over_5000_rows(operator_client):
    async with sessionmaker()() as s:
        await seed_bulk_emails(s, 5000)
    c = operator_client
    for sort, direction in [("id", "desc"), ("id", "asc"), ("confidence", "desc"), ("last_seen_at", "asc"), ("email", "asc")]:
        seen: list[int] = []
        cur = None
        pages = 0
        while True:
            r = await c.get("/api/contacts", params={"limit": 500, "sort": sort, "direction": direction, **({"cursor": cur} if cur else {})})
            assert r.status_code == 200, r.text
            j = r.json()
            if pages == 0:
                assert j["total_estimate"] in (5000, None) or j["total_estimate"] > 0
            seen += [i["id"] for i in j["items"]]
            pages += 1
            cur = j["next_cursor"]
            if not cur:
                break
        assert len(seen) == 5000 and len(set(seen)) == 5000, (sort, direction)
        assert pages == 10
        if sort == "id":
            assert seen == sorted(seen, reverse=direction == "desc")
    # confidence ordering is monotone with the id tiebreak
    j = (await c.get("/api/contacts?sort=confidence&direction=desc&limit=500")).json()
    confs = [i["confidence"] for i in j["items"]]
    assert confs == sorted(confs, reverse=True)
    # cursor from another sort is rejected
    cur = j["next_cursor"]
    assert (await c.get("/api/contacts", params={"cursor": cur, "sort": "id"})).status_code == 422
    assert (await c.get("/api/contacts", params={"cursor": "garbage"})).status_code == 422
    assert (await c.get("/api/contacts?limit=100000")).status_code == 422


async def test_contact_filters(operator_client):
    async with sessionmaker()() as s:
        o1 = await seed_org(s, "Alpha Roofing", country="GB", region="Kent")
        o2 = await seed_org(s, "Beta Bakery", country="IE", region="Cork")
        c1 = await seed_contact(s, o1)
        await seed_email(s, o1, "jane@alpharoofing.example.com", contact_id=c1, confidence=90)
        await seed_email(s, o1, "info@alpharoofing.example.com", role_type="generic", confidence=60, origin="role_speculative", reachability="unknown", catch_all="confirmed")
        await seed_email(s, o2, "sales@betabakery.example.com", role_type="sales", confidence=40, reachability="invalid", marketing="needs_review", validated=False)
    c = operator_client

    async def emails(**p):
        r = await c.get("/api/contacts", params=p)
        assert r.status_code == 200, r.text
        return sorted(i["email"] for i in r.json()["items"])

    assert len(await emails()) == 3
    assert await emails(country="IE") == ["sales@betabakery.example.com"]
    assert await emails(region="Kent", contact_type="named") == ["jane@alpharoofing.example.com"]
    assert await emails(contact_type="role", origin="inferred") == ["info@alpharoofing.example.com"]
    assert await emails(min_confidence=50, max_confidence=95) == ["info@alpharoofing.example.com", "jane@alpharoofing.example.com"]
    assert await emails(reachability="invalid,unknown") == ["info@alpharoofing.example.com", "sales@betabakery.example.com"]
    assert await emails(freshness="never") == ["sales@betabakery.example.com"]
    assert await emails(q="alpha") == ["info@alpharoofing.example.com", "jane@alpharoofing.example.com"]
    assert await emails(q="jane@") == ["jane@alpharoofing.example.com"]
    assert await emails(exclude_catch_all="true", country="GB") == ["jane@alpharoofing.example.com"]
    assert await emails(has_name="true") == ["jane@alpharoofing.example.com"]
    assert await emails(marketing_status="needs_review") == ["sales@betabakery.example.com"]
    assert (await c.get("/api/contacts", params={"min_confidence": 500})).status_code == 422
    assert (await c.get("/api/contacts", params={"origin": "bogus"})).status_code == 422
    # LIKE wildcards in q are literals, not patterns
    assert await emails(q="%") == [] and await emails(q="_lpha") == []


INJECTIONS = ["' OR '1'='1", "'; DROP TABLE email_addresses; --", "1; SELECT pg_sleep(5)--", "\" OR 1=1 --", "%' UNION SELECT password_hash FROM users --", "\\", "' || (SELECT 1) || '"]
ENDPOINT_PARAMS = [
    ("/api/contacts", ["q", "country", "region", "city", "category_id", "origin", "reachability", "syntax_status", "domain_mail_status", "catch_all_status", "marketing_status",
                       "role_type", "source", "domain", "sort", "direction", "cursor", "freshness", "contact_type", "industry", "organisation_id", "run_id", "validated_after"]),
    ("/api/businesses", ["q", "country", "region", "city", "category_id", "domain_status", "company_type", "source", "email_availability", "sort", "direction", "cursor", "updated_after"]),
    ("/api/runs", ["q", "tab", "sort", "direction", "cursor"]),
    ("/api/exports", ["status", "preset", "cursor"]),
    ("/api/verification/emails?tab=invalid", ["q", "sort", "country"]),
    ("/api/verification/history", ["check_type", "result", "cursor", "email_id"]),
    ("/api/geo/localities", ["q", "region", "kind", "country"]),
    ("/api/audit", ["action", "object_type", "object_id", "user_id", "since", "cursor"]),
    ("/api/suppressions", ["q", "kind", "cursor"]),
]


@pytest.mark.parametrize("path,params", ENDPOINT_PARAMS)
async def test_sql_injection_in_every_filter_is_harmless(admin_client, path, params):
    async with sessionmaker()() as s:
        org = await seed_org(s, "Victim Ltd")
        await seed_email(s, org, "victim@victim.example.com")
    c = admin_client
    for p in params:
        for payload in INJECTIONS:
            sep = "&" if "?" in path else "?"
            r = await c.get(f"{path}{sep}x=1", params={p: payload})
            assert r.status_code in (200, 422), (path, p, payload, r.status_code, r.text[:200])
            assert r.headers["content-type"].startswith("application/json")
            if r.status_code == 200 and p == "q":
                data = r.json()
                assert (data if isinstance(data, list) else data.get("items", [])) == [], (path, p, payload)  # a prefix match, never "match everything"
    async with sessionmaker()() as s:
        assert (await s.execute(text("SELECT count(*) FROM email_addresses"))).scalar_one() == 1
        assert (await s.execute(text("SELECT count(*) FROM users"))).scalar_one() >= 1


async def test_xss_payload_is_stored_and_returned_as_plain_json(admin_client):
    c = admin_client
    xss = "<script>alert('xss')</script><img src=x onerror=alert(1)>"
    r = await c.post("/api/categories", json={"name": xss})
    assert r.status_code == 201
    r = await c.post("/api/presets", json={"kind": "run", "name": xss, "config": {"note": xss}})
    assert r.status_code == 201
    for path in ("/api/categories", "/api/presets"):
        r = await c.get(path)
        assert r.headers["content-type"].startswith("application/json")
        assert r.headers["x-content-type-options"] == "nosniff"
        assert "default-src 'none'" in r.headers["content-security-policy"]
        assert xss in r.text.replace("\\u003c", "<") or xss in str(r.json())
    assert r.json()[0]["name"] == xss  # returned verbatim as data; escaping is the client's job
    org = None
    async with sessionmaker()() as s:
        org = await seed_org(s, xss)
        eid = await seed_email(s, org, "x@xss.example.com")
    d = await c.get(f"/api/contacts/{eid}")
    assert d.json()["organisation"]["legal_name"] == xss and d.headers["content-type"].startswith("application/json")


async def test_suppression_persists_by_email_domain_company(operator_client, admin_client):
    c = operator_client
    async with sessionmaker()() as s:
        o1 = await seed_org(s, "One Ltd")
        o2 = await seed_org(s, "Two Ltd")
        a = await seed_email(s, o1, "a@one.example.com")
        b = await seed_email(s, o1, "b@one.example.com")
        d = await seed_email(s, o2, "d@two.example.com")
    r = await c.post(f"/api/contacts/{a}/suppress", json={"scope": "email", "reason": "opt_out", "note": "asked to stop"})
    assert r.status_code == 200 and r.json()["emails_affected"] == 1
    det = (await c.get(f"/api/contacts/{a}")).json()
    assert det["marketing"]["suppressed"] is True and det["marketing"]["eligible"] is False and det["statuses"]["marketing"] == "opted_out"
    assert det["suppressions"][0]["reason"] == "opt_out"
    assert [i["email"] for i in (await c.get("/api/contacts?suppressed=true")).json()["items"]] == ["a@one.example.com"]
    # company scope flags every address of the company
    r = await c.post(f"/api/contacts/{b}/suppress", json={"scope": "company", "reason": "manual"})
    assert r.json()["emails_affected"] == 2
    flagged = {i["email"] for i in (await c.get("/api/contacts?suppressed=true")).json()["items"]}
    assert flagged == {"a@one.example.com", "b@one.example.com"} and d not in [0]
    # erasure keeps the suppression entry even though the row is gone
    r = await admin_client.delete(f"/api/contacts/{a}")
    assert r.status_code == 200 and r.json()["suppression_kept"] is True
    assert (await c.get(f"/api/contacts/{a}")).status_code == 404
    entries = (await c.get("/api/suppressions?kind=email")).json()["items"]
    assert any(e["value"] == "a@one.example.com" for e in entries)
    # a rediscovered address is still treated as suppressed (matched by entry, not only by flag)
    async with sessionmaker()() as s:
        again = await seed_email(s, o1, "a@one.example.com", marketing="eligible")
    assert (await c.get("/api/contacts?suppressed=true")).json()["total_estimate"] >= 2
    flagged = {i["id"] for i in (await c.get("/api/contacts?suppressed=true&limit=50")).json()["items"]}
    assert again in flagged
    # operators cannot delete personal data
    assert (await c.delete(f"/api/contacts/{b}")).status_code == 403
    # unsuppress company scope releases only addresses without another reason
    r = await c.post(f"/api/contacts/{b}/unsuppress", json={"scope": "company"})
    assert r.status_code == 200
    assert (await c.post(f"/api/contacts/{b}/unsuppress", json={"scope": "domain"})).status_code == 404


async def test_objection_workflow(operator_client):
    c = operator_client
    async with sessionmaker()() as s:
        org = await seed_org(s, "Obj Ltd")
        cid = await seed_contact(s, org, "Sam Objector", "CEO")
        eid = await seed_email(s, org, "sam@obj.example.com", contact_id=cid)
        ev = (await s.execute(text("INSERT INTO evidence_sources (source_adapter, source_url, evidence_type) VALUES ('w','https://obj.example.com/team','text') RETURNING id"))).scalar_one()
        await s.execute(text("INSERT INTO contact_evidence (email_id, contact_id, evidence_id, context) VALUES (:e,:c,:ev,'{\"name\":\"Sam Objector\",\"phone\":\"0123\"}')"), {"e": eid, "c": cid, "ev": ev})
        await s.commit()
    r = await c.post("/api/privacy/objection", json={"email": "Sam@Obj.example.com", "delete_personal_data": True, "note": "Art. 21 objection"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["matched_emails"] == 1 and j["personal_fields_cleared"] is True
    async with sessionmaker()() as s:
        row = (await s.execute(text("SELECT full_name, job_title FROM contacts WHERE id=:c"), {"c": cid})).one()
        ctx = (await s.execute(text("SELECT context FROM contact_evidence WHERE email_id=:e"), {"e": eid})).scalar_one()
        sup = (await s.execute(text("SELECT reason FROM suppression_entries WHERE kind='email' AND value='sam@obj.example.com'"))).scalar_one()
        st = (await s.execute(text("SELECT marketing_status, suppressed_at IS NOT NULL FROM email_addresses WHERE id=:e"), {"e": eid})).one()
    assert row == (None, None) and ctx == {} and sup == "objection" and st == ("opted_out", True)
    # unknown address: still recorded so it is never marketed to if discovered later
    r = await c.post("/api/privacy/objection", json={"email": "ghost@nowhere.example.com", "suppress_domain": True})
    assert r.json()["matched_emails"] == 0 and r.json()["domain_suppressed"] is True
    assert (await c.post("/api/privacy/objection", json={"email": "not-an-email"})).status_code == 422
    # audit record never contains the plain address
    from app.models import AuditEvent

    async with sessionmaker()() as s:
        details = [str(a.detail) for a in (await s.execute(text("SELECT detail FROM audit_events WHERE action='privacy.objection'"))).mappings().all()]
        _ = AuditEvent
    assert details and all("sam@obj.example.com" not in d.lower() for d in details)


async def test_contact_actions_and_bulk(operator_client):
    c = operator_client
    async with sessionmaker()() as s:
        org = await seed_org(s)
        ids = [await seed_email(s, org, f"p{i}@bulk{i}.example.com") for i in range(4)]
    assert (await c.post(f"/api/contacts/{ids[0]}/needs-review", json={"value": True})).status_code == 200
    assert (await c.get(f"/api/contacts/{ids[0]}")).json()["flags"]["needs_review"] is True
    assert (await c.post(f"/api/contacts/{ids[0]}/revalidate", json={"mode": "fast"})).status_code == 200
    async with sessionmaker()() as s:
        j = (await s.execute(text("SELECT stage, payload, r.name, r.status FROM sourcing_jobs j JOIN sourcing_runs r ON r.id=j.run_id WHERE stage='verify'"))).one()
    assert j[1]["email_id"] == ids[0] and j[2] == "system-verification" and j[3] == "running"
    r = (await c.post("/api/contacts/bulk", json={"action": "revalidate", "ids": ids})).json()
    assert r["affected"] == 4
    assert (await c.post("/api/contacts/bulk", json={"action": "mark_needs_review", "ids": ids})).json()["affected"] == 4
    assert (await c.post("/api/contacts/bulk", json={"action": "suppress", "ids": ids[:2], "reason": "manual"})).json()["affected"] == 2
    assert (await c.post("/api/contacts/bulk", json={"action": "unsuppress", "ids": ids[:1]})).json()["affected"] == 1
    ex = (await c.post("/api/contacts/bulk", json={"action": "export", "ids": ids})).json()
    assert ex["export_id"]
    assert (await c.post("/api/contacts/bulk", json={"action": "revalidate", "ids": []})).status_code == 422
    assert (await c.post("/api/contacts/bulk", json={"action": "drop_table", "ids": [1]})).status_code == 422
