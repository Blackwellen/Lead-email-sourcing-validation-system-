from sqlalchemy import text

from app.db import sessionmaker

from .helpers import seed_contact, seed_email, seed_org


async def _world():
    async with sessionmaker()() as s:
        root = (await s.execute(text("INSERT INTO categories (slug,name,level) VALUES ('trades','Trades','category') RETURNING id"))).scalar_one()
        plumb = (await s.execute(text("INSERT INTO categories (slug,name,level,parent_id) VALUES ('plumbers','Plumbers','subcategory',:p) RETURNING id"), {"p": root})).scalar_one()
        bakery = (await s.execute(text("INSERT INTO categories (slug,name,level) VALUES ('bakeries','Bakeries','category') RETURNING id"))).scalar_one()
        a = await seed_org(s, "Acme Plumbing Ltd", category_id=plumb)
        b = await seed_org(s, "Acme Baking Ltd", category_id=bakery, country="IE", region="Cork", source="osm_geofabrik")
        c = await seed_org(s, "Zeta Heating", category_id=plumb)
        await s.execute(text("INSERT INTO organisation_domains (organisation_id, domain, registrable_domain, verified_match_status, confidence, website_url) VALUES (:o,'acme-plumbing.example.com','acme-plumbing.example.com','confirmed',0.9,'https://acme-plumbing.example.com')"), {"o": a})
        await s.execute(text("INSERT INTO organisation_domains (organisation_id, domain, registrable_domain, verified_match_status, confidence) VALUES (:o,'zeta.example.com','zeta.example.com','review',0.4)"), {"o": c})
        await seed_email(s, a, "info@acme-plumbing.example.com", role_type="generic", confidence=70)
        await seed_email(s, a, "bob@acme-plumbing.example.com", confidence=95)
        await seed_email(s, b, "x@acmebaking.example.com", origin="pattern_inferred", confidence=30)
    return {"root": root, "plumb": plumb, "bakery": bakery, "a": a, "b": b, "c": c}


async def test_business_list_filters_and_detail(operator_client):
    w = await _world()
    c = operator_client

    async def names(**p):
        r = await c.get("/api/businesses", params=p)
        assert r.status_code == 200, r.text
        return sorted(i["legal_name"] for i in r.json()["items"])

    assert len(await names()) == 3
    assert await names(country="IE") == ["Acme Baking Ltd"]
    assert await names(q="acme") == ["Acme Baking Ltd", "Acme Plumbing Ltd"]
    assert await names(category_id=w["root"]) == ["Acme Plumbing Ltd", "Zeta Heating"]  # includes sub-categories
    assert await names(category_id=w["root"], include_subcategories="false") == []
    assert await names(domain_status="confirmed") == ["Acme Plumbing Ltd"]
    assert await names(domain_status="none") == ["Acme Baking Ltd"]
    assert await names(email_availability="published") == ["Acme Plumbing Ltd"]
    assert await names(email_availability="none") == ["Zeta Heating"]
    assert await names(source="osm_geofabrik") == ["Acme Baking Ltd"]
    assert await names(min_confidence=90) == ["Acme Plumbing Ltd"]
    assert await names(sort="name", direction="asc") == ["Acme Baking Ltd", "Acme Plumbing Ltd", "Zeta Heating"]
    assert (await c.get("/api/businesses?sort=drop")).status_code == 422
    assert (await c.get("/api/businesses?domain_status=bad")).status_code == 422
    row = next(i for i in (await c.get("/api/businesses?q=acme%20plumbing")).json()["items"])
    assert row["domain"] == "acme-plumbing.example.com" and row["email_count"] == 2 and row["published_email_count"] == 2 and row["best_confidence"] == 95
    d = (await c.get(f"/api/businesses/{w['a']}")).json()
    assert d["organisation"]["legal_name"] == "Acme Plumbing Ltd" and d["category_name"] == "Plumbers"
    assert d["domains"][0]["status"] == "confirmed" and len(d["emails"]) == 2
    assert (await c.get("/api/businesses/999999")).status_code == 404
    assert len((await c.get(f"/api/businesses/{w['a']}/contacts")).json()["items"]) == 2


async def test_manual_domain_correction_is_audited(operator_client, admin_client):
    w = await _world()
    c = operator_client
    async with sessionmaker()() as s:
        did = (await s.execute(text("SELECT id FROM organisation_domains WHERE domain='zeta.example.com'"))).scalar_one()
    r = await c.post(f"/api/businesses/{w['c']}/domains/{did}/decision", json={"decision": "confirm", "note": "checked manually"})
    assert r.status_code == 200 and r.json()["status"] == "confirmed" and r.json()["manual_override"] is True
    assert r.json()["evidence"][-1]["type"] == "manual" and r.json()["evidence"][-1]["decision"] == "confirm"
    r = await c.post(f"/api/businesses/{w['c']}/domains/{did}/decision", json={"decision": "reject"})
    assert r.json()["status"] == "mismatch"
    r = await c.post(f"/api/businesses/{w['c']}/domains/0/decision", json={"decision": "set", "domain": "https://www.Zeta-Heating.co.uk/about?x=1"})
    assert r.status_code == 200 and r.json()["domain"] == "zeta-heating.co.uk" and r.json()["status"] == "confirmed"
    assert (await c.post(f"/api/businesses/{w['c']}/domains/0/decision", json={"decision": "set", "domain": "not a domain!"})).status_code == 422
    assert (await c.post(f"/api/businesses/{w['c']}/domains/999999/decision", json={"decision": "confirm"})).status_code == 404
    audit = (await admin_client.get("/api/audit?action=business.domain_override")).json()["items"]
    assert len(audit) == 3 and audit[0]["detail"]["decision"] == "set"


async def test_bulk_actions_and_refresh_queue(operator_client):
    w = await _world()
    c = operator_client
    r = await c.post("/api/businesses/bulk", json={"action": "add_to_category", "ids": [w["a"], w["c"]], "category_id": w["bakery"]})
    assert r.json()["affected"] == 2
    assert (await c.post("/api/businesses/bulk", json={"action": "add_to_category", "ids": [w["a"]], "category_id": 99999})).status_code == 422
    r = (await c.post("/api/businesses/bulk", json={"action": "queue_refresh", "ids": [w["a"], w["c"], w["b"]]})).json()
    assert r["jobs_queued"] == 3
    async with sessionmaker()() as s:
        rows = (await s.execute(text("SELECT j.stage, r.name, r.status FROM sourcing_jobs j JOIN sourcing_runs r ON r.id=j.run_id ORDER BY j.stage"))).all()
    assert [x[0] for x in rows] == ["crawl", "resolve_domain", "resolve_domain"] and {x[1] for x in rows} == {"system-refresh"} and {x[2] for x in rows} == {"running"}
    # idempotent within the hour
    assert (await c.post("/api/businesses/bulk", json={"action": "queue_refresh", "ids": [w["a"]]})).json()["jobs_queued"] == 0
    ex = (await c.post("/api/businesses/bulk", json={"action": "export", "ids": [w["a"]]})).json()
    assert ex["export_id"]
    assert (await c.post("/api/businesses/bulk", json={"action": "nuke", "ids": [1]})).status_code == 422


async def test_merge_duplicates(admin_client, operator_client):
    async with sessionmaker()() as s:
        p = await seed_org(s, "Dup Co Ltd")
        d = await seed_org(s, "Dup Company Limited", company_number="999")
        cp = await seed_contact(s, p, "Jane Smith", key="jane smith")
        cd = await seed_contact(s, d, "Jane Smith", key="jane smith")
        co = await seed_contact(s, d, "Other Person", key="other person")
        await seed_email(s, p, "jane@dup.example.com", contact_id=cp)
        await seed_email(s, d, "j.smith@dup.example.com", contact_id=cd)
        await seed_email(s, d, "other@dup.example.com", contact_id=co)
        await s.execute(text("INSERT INTO organisation_domains (organisation_id, domain, registrable_domain) VALUES (:p,'dup.example.com','dup.example.com'),(:d,'dup.example.com','dup.example.com'),(:d,'dup-co.example.com','dup-co.example.com')"), {"p": p, "d": d})
        await s.commit()
    assert (await operator_client.post("/api/businesses/merge", json={"primary_id": p, "duplicate_ids": [d]})).status_code == 403
    r = await admin_client.post("/api/businesses/merge", json={"primary_id": p, "duplicate_ids": [d]})
    assert r.status_code == 200, r.text
    assert r.json()["emails_moved"] == 2 and r.json()["domains_moved"] == 1
    async with sessionmaker()() as s:
        merged = (await s.execute(text("SELECT merged_into_id FROM organisations WHERE id=:d"), {"d": d})).scalar_one()
        emails = (await s.execute(text("SELECT count(*) FROM email_addresses WHERE organisation_id=:p"), {"p": p})).scalar_one()
        contacts = (await s.execute(text("SELECT count(*) FROM contacts WHERE organisation_id=:p"), {"p": p})).scalar_one()
        doms = (await s.execute(text("SELECT count(*) FROM organisation_domains WHERE organisation_id=:p"), {"p": p})).scalar_one()
        num = (await s.execute(text("SELECT company_number FROM organisations WHERE id=:p"), {"p": p})).scalar_one()
    assert merged == p and emails == 3 and contacts == 2 and doms == 2 and num == "999"
    # merged organisations drop out of default listings; cannot merge again
    assert [i["id"] for i in (await admin_client.get("/api/businesses")).json()["items"]] == [p]
    assert (await admin_client.post("/api/businesses/merge", json={"primary_id": p, "duplicate_ids": [d]})).status_code == 404
    assert (await admin_client.post("/api/businesses/merge", json={"primary_id": p, "duplicate_ids": [p]})).status_code == 422
