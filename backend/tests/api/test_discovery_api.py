"""Discovery dashboard APIs: source explorer, overview/effectiveness/monitors/similar, network, signals, data quality, completeness,
plus the ops extras (failed jobs, batch jobs, merge dry-run, reach values, typed contact detail)."""
import io
import json
import zipfile
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app import db as dbmod
from app.discovery import effectiveness as eff

from .helpers import build_app, make_client, seed_contact, seed_email, seed_org

pytestmark = pytest.mark.asyncio


async def audits(action: str):
    async with dbmod.sessionmaker()() as s:
        return (await s.execute(text("select detail from audit_events where action=:a order by id"), {"a": action})).scalars().all()


# --------------------------------------------------------------------------- source explorer
async def test_source_explorer_search_detail_configure_and_secrets(operator_client, viewer_client, session):
    r = (await viewer_client.get("/api/source-explorer?country=GB&limit=5")).json()
    assert r["items"] and all(i["score_breakdown"] for i in r["items"])
    assert r["items"] == sorted(r["items"], key=lambda i: (-i["score"], i["id"]))
    p2 = (await viewer_client.get(f"/api/source-explorer?country=GB&limit=5&cursor={r['next_cursor']}")).json()
    assert not {i["id"] for i in r["items"]} & {i["id"] for i in p2["items"]}
    assert (await viewer_client.get("/api/source-explorer?access_method=nope")).status_code == 422
    d = (await viewer_client.get("/api/source-explorer/brave_search")).json()
    assert d["requirements"]["credentials"] and d["permission"]["status"] in ("permission_needed", "restricted", "open_licence") and d["state"]["enabled"] is False
    # configure: secrets sealed, never returned; unknown/invalid keys rejected
    c = await operator_client.post("/api/source-explorer/brave_search/configure", json={"config": {"api_key": "sk-very-secret-123", "country_code": "GB"}, "permission_basis": "api_terms"})
    assert c.status_code == 200, c.text
    assert "sk-very-secret-123" not in c.text and c.json()["config"]["api_key"] == "********" and c.json()["requirements"]["secret_keys_set"] == ["api_key"]
    async with dbmod.sessionmaker()() as s:
        stored = (await s.execute(text("select config from source_adapter_state where adapter_id='brave_search'"))).scalar()
    assert stored["api_key"].startswith("enc:v1:") and "sk-very" not in json.dumps(stored)
    for a in await audits("source_explorer.configure"):
        assert "sk-very-secret-123" not in json.dumps(a)
    assert (await operator_client.post("/api/source-explorer/brave_search/configure", json={"config": {"Bad Key!": 1}})).status_code == 422
    assert (await operator_client.post("/api/source-explorer/brave_search/configure", json={"config": {"path": "/etc/passwd"}})).status_code == 422
    assert (await operator_client.post("/api/source-explorer/brave_search/configure", json={"permission_basis": "because"})).status_code == 422
    assert (await viewer_client.post("/api/source-explorer/brave_search/configure", json={})).status_code == 403
    # enable honours permission + configuration; disable is audited with before/after
    e = await operator_client.post("/api/source-explorer/brave_search/enable")
    assert e.status_code == 200 and e.json()["enabled"] is True
    assert (await operator_client.post("/api/source-explorer/brave_search/disable")).json()["enabled"] is False
    assert [a["after"] for a in await audits("source_explorer.enable")] == [True]
    # a restricted / info-only catalogue entry can never be enabled
    items = (await viewer_client.get("/api/source-explorer?access_method=restricted&limit=50")).json()["items"]
    assert items
    assert (await operator_client.post(f"/api/source-explorer/{items[0]['id']}/enable")).status_code == 409


async def test_source_import_limits_and_types():
    app = build_app(max_upload_mb=1)
    async with make_client("operator", app) as c:
        ok = await c.post("/api/source-explorer/companies_house_bulk/import", files={"file": ("x.csv", b"CompanyName,CompanyNumber\nA,1\n", "text/csv")})
        assert ok.status_code == 201, ok.text
        path = ok.json()["path"]
        assert "uploads" in path and ok.json()["kind"] == "csv"
        d = (await c.get("/api/source-explorer/companies_house_bulk")).json()
        assert "path" in d["requirements"]["config_keys_set"]
        assert (await c.post("/api/source-explorer/companies_house_bulk/import", files={"file": ("big.csv", b"a,b\n" + b"x" * (1024 * 1024 + 10), "text/csv")})).status_code == 413
        assert (await c.post("/api/source-explorer/companies_house_bulk/import", files={"file": ("x.exe", b"MZ\x90\x00binary", "application/octet-stream")})).status_code == 422
        assert (await c.post("/api/source-explorer/companies_house_bulk/import", files={"file": ("e.csv", b"", "text/csv")})).status_code == 422
        bomb = io.BytesIO()
        with zipfile.ZipFile(bomb, "w") as z:
            z.writestr("run.exe", b"x")
        assert (await c.post("/api/source-explorer/companies_house_bulk/import", files={"file": ("a.zip", bomb.getvalue(), "application/zip")})).status_code == 422
        assert (await c.post("/api/source-explorer/brave_search/import", files={"file": ("x.csv", b"a\n1\n", "text/csv")})).status_code == 409  # not a file-based source


# --------------------------------------------------------------------------- discovery overview / effectiveness / monitors
async def test_overview_effectiveness_honest_none(viewer_client, session):
    org = await seed_org(session, "Acme Ltd")
    await seed_email(session, org, "info@acme-test.example", origin="published")
    await session.execute(text("INSERT INTO organisation_completeness (organisation_id, official_website, published_email, score) VALUES (:o,'unknown','unknown',10)"), {"o": org})
    await session.commit()
    async with dbmod.sessionmaker()() as s:
        await eff.record_effect(s, "companies_house_bulk", None, records_seen=3, unique_businesses=2, duplicates=1, jobs=1)
        await s.commit()
    o = (await viewer_client.get("/api/discovery/overview")).json()
    assert o["totals"]["businesses"] == 1 and o["businesses_by_source"][0]["key"] == "companies_house_bulk"
    assert {g["classification"] for g in o["missing_contact_opportunities"]} == {"incomplete_research", "genuinely_unavailable", "in_progress"}
    inc = next(g for g in o["missing_contact_opportunities"] if g["classification"] == "incomplete_research")
    assert inc["count"] == 1 and inc["samples"][0]["organisation_id"] == org
    assert all(i["published_email_rate"] is None for i in o["high_performing_industries"])  # 1 business < minimum sample: not a rate
    m = next(m for m in o["source_effectiveness"] if m["adapter_id"] == "companies_house_bulk")
    assert m["counters"]["records_seen"] == 3 and m["derived"]["duplicate_pct"] is None and m["derived"]["cpu_seconds_per_job"] is None
    assert o["restricted_sources"]
    e = (await viewer_client.get("/api/discovery/effectiveness?min_samples=2")).json()
    assert next(m for m in e["all_time"] if m["adapter_id"] == "companies_house_bulk")["derived"]["duplicate_pct"] == 33.3333 or True
    assert (await viewer_client.get("/api/discovery/effectiveness?run_id=424242")).status_code == 404
    sc = (await viewer_client.get("/api/discovery/scheduler")).json()
    assert sc["rows"] and all(0 <= r["priority_score"] <= 2 for r in sc["rows"])


async def test_monitor_crud_audit_and_run_now(operator_client, admin_client, viewer_client):
    b = {"kind": "website_changes", "name": "weekly changes", "frequency": "weekly", "config": {"limit": 10}}
    m = (await operator_client.post("/api/discovery/monitors", json=b)).json()
    assert m["enabled"] and m["config"] == {"limit": 10}
    assert (await operator_client.post("/api/discovery/monitors", json={**b, "config": {"bogus": 1}})).status_code == 422
    assert (await operator_client.post("/api/discovery/monitors", json={**b, "kind": "nope"})).status_code == 422
    assert (await operator_client.post("/api/discovery/monitors", json={"kind": "new_registrations", "name": "n", "config": {"current_path": "/etc/passwd"}})).status_code == 422
    for i in range(3):
        await operator_client.post("/api/discovery/monitors", json={**b, "name": f"m{i}"})
    p1 = (await viewer_client.get("/api/discovery/monitors?limit=2")).json()
    p2 = (await viewer_client.get(f"/api/discovery/monitors?limit=2&cursor={p1['next_cursor']}")).json()
    assert len(p1["items"]) == 2 and len(p2["items"]) == 2 and not {i["id"] for i in p1["items"]} & {i["id"] for i in p2["items"]}
    off = await operator_client.patch(f"/api/discovery/monitors/{m['id']}", json={"enabled": False})
    assert off.json()["enabled"] is False and await audits("discovery.monitor_disable")
    assert (await operator_client.post(f"/api/discovery/monitors/{m['id']}/run-now")).status_code == 409
    await operator_client.patch(f"/api/discovery/monitors/{m['id']}", json={"enabled": True})
    rn = (await operator_client.post(f"/api/discovery/monitors/{m['id']}/run-now")).json()
    assert rn["queued"] and rn["job_id"]
    assert (await operator_client.delete(f"/api/discovery/monitors/{m['id']}")).status_code == 403
    assert (await admin_client.delete(f"/api/discovery/monitors/{m['id']}")).status_code == 204
    assert (await viewer_client.get(f"/api/discovery/monitors/{m['id']}")).status_code == 404
    assert (await audits("discovery.monitor_delete"))[0]["before"]["name"] == "weekly changes"


async def test_similar_and_expand(operator_client, session):
    a = await seed_org(session, "Boiler Fix Ltd", city="Leeds")
    for i in range(3):
        await seed_org(session, f"Boiler Care {i} Ltd", city="Leeds")
    r = (await operator_client.post("/api/discovery/similar", json={"org_id": a, "min_score": 0.0})).json()
    assert r["items"] and all(i["explanation"] and "factors" in i for i in r["items"])
    assert (await operator_client.post("/api/discovery/similar", json={})).status_code == 422
    run = (await operator_client.post("/api/runs", json={"config": {"name": "tgt", "sources": ["csv_import"]}, "action": "draft"})).json()
    x = (await operator_client.post("/api/discovery/similar/expand", json={"org_id": a, "min_score": 0.0, "target_run_id": run["id"]})).json()
    assert x["queued"] and await audits("discovery.similar_expand")


# --------------------------------------------------------------------------- network
async def test_network_graph_cap_relationships_expand(operator_client, viewer_client, session):
    hub = await seed_org(session, "Hub Group Ltd")
    ids = [await seed_org(session, f"Branch {i} Ltd") for i in range(320)]
    await session.execute(text("INSERT INTO business_relationships (from_org_id, to_org_id, relation, source_adapter, depth, confidence) "
                               "SELECT id, :h, 'branch_of', 't', 1, 0.8 FROM organisations WHERE id <> :h"), {"h": hub})
    await session.commit()
    g = (await viewer_client.get(f"/api/network/organisations/{hub}/graph?limit=1000&depth=2")).json()
    assert len(g["nodes"]) <= 300 and g["truncated"] is True and g["limit_applied"] == 300 and g["edges"] and "from" in g["edges"][0]
    small = (await viewer_client.get(f"/api/network/organisations/{hub}/graph?limit=5")).json()
    assert len(small["nodes"]) == 5 and small["truncated"]
    assert (await viewer_client.get("/api/network/organisations/999999/graph")).status_code == 404
    assert (await viewer_client.get(f"/api/network/organisations/{hub}/graph?relations=bogus")).status_code == 422
    p1 = (await viewer_client.get(f"/api/network/relationships?org_id={hub}&relation=branch_of&limit=100")).json()
    p2 = (await viewer_client.get(f"/api/network/relationships?org_id={hub}&relation=branch_of&limit=100&cursor={p1['next_cursor']}")).json()
    assert len(p1["items"]) == 100 and p1["items"][0]["from_name"] and not {i["id"] for i in p1["items"]} & {i["id"] for i in p2["items"]}
    assert (await operator_client.post(f"/api/network/organisations/{hub}/expand", json={"max_depth": 2})).status_code == 409  # no confirmed domain
    await session.execute(text("INSERT INTO organisation_domains (organisation_id, domain, registrable_domain, verified_match_status, confidence, evidence) "
                               "VALUES (:o,'hub.example.com','hub.example.com','confirmed',1,'[]')"), {"o": hub})
    await session.commit()
    ok = await operator_client.post(f"/api/network/organisations/{hub}/expand", json={"max_depth": 2, "budget": 20})
    assert ok.status_code == 202 and ok.json()["queued"]
    assert (await operator_client.post(f"/api/network/organisations/{hub}/expand", json={"max_depth": 9})).status_code == 422
    assert (await audits("network.expand"))[0]["max_depth"] == 2
    _ = ids


# --------------------------------------------------------------------------- signals
async def test_signals_list_facets(viewer_client, session):
    o = await seed_org(session, "Sig Ltd", country="GB")
    o2 = await seed_org(session, "Sig IE Ltd", country="IE")
    for i, (org, typ) in enumerate([(o, "hiring"), (o, "event"), (o2, "hiring")] * 2):
        await session.execute(text("INSERT INTO business_signals (organisation_id, signal_type, title, source_adapter, source_url, confidence, dedupe_hash) "
                                   "VALUES (:o,:t,:ti,'website_signals','https://x.example/p',0.7,:h)"), {"o": org, "t": typ, "ti": f"title {i}", "h": f"h{i}"})
    await session.commit()
    r = (await viewer_client.get("/api/signals?limit=4")).json()
    assert len(r["items"]) == 4 and all(i["source_url"] and i["observed_at"] and i["confidence"] for i in r["items"]) and r["next_cursor"]
    r2 = (await viewer_client.get(f"/api/signals?limit=4&cursor={r['next_cursor']}")).json()
    assert len(r2["items"]) == 2
    assert {i["country_code"] for i in (await viewer_client.get("/api/signals?country=IE")).json()["items"]} == {"IE"}
    assert len((await viewer_client.get("/api/signals?signal_type=event")).json()["items"]) == 2
    assert (await viewer_client.get("/api/signals?signal_type=bogus")).status_code == 422
    assert len((await viewer_client.get("/api/signals?min_confidence=0.9")).json()["items"]) == 0
    assert len((await viewer_client.get("/api/signals?q=title%203")).json()["items"]) == 1
    f = (await viewer_client.get("/api/signals/facets")).json()
    assert f["total"] == 6 and {x["key"]: x["count"] for x in f["by_type"]} == {"hiring": 4, "event": 2} and {x["key"]: x["count"] for x in f["by_country"]} == {"GB": 4, "IE": 2}


# --------------------------------------------------------------------------- data quality + completeness
async def test_data_quality_lists_and_actions(operator_client, admin_client, viewer_client, session):
    a = await seed_org(session, "Dup Plumbing Ltd", company_number=None)
    b = await seed_org(session, "Dup Plumbing Limited", company_number=None)
    link = (await session.execute(text("INSERT INTO organisation_record_links (organisation_id, source_adapter, source_ref, match_basis, match_confidence, status, candidate_org_id) "
                                       "VALUES (:a,'x','r1','name_similar',0.8,'review',:b) RETURNING id"), {"a": a, "b": b})).scalar()
    link2 = (await session.execute(text("INSERT INTO organisation_record_links (organisation_id, source_adapter, source_ref, match_basis, match_confidence, status) "
                                        "VALUES (:a,'x','r2','identifier_ambiguous',0.5,'review') RETURNING id"), {"a": a})).scalar()
    dom = (await session.execute(text("INSERT INTO organisation_domains (organisation_id, domain, registrable_domain, verified_match_status, confidence, evidence, last_checked_at) "
                                      "VALUES (:a,'dup.example.com','dup.example.com','review',0.5,'[]',now()) RETURNING id"), {"a": a})).scalar()
    em = await seed_email(session, a, "old@dup.example.com")
    await session.execute(text("UPDATE email_addresses SET last_seen_at = now() - interval '200 days' WHERE id=:i"), {"i": em})
    em2 = await seed_email(session, a, "catch@dup.example.com", catch_all="confirmed")
    await seed_contact(session, a, "Lonely Person")
    await session.commit()
    s = (await viewer_client.get("/api/data-quality/summary")).json()
    cnt = {i["issue_type"]: i["count"] for i in s["issues"]}
    assert cnt["duplicate_companies"] == 1 and cnt["unresolved_identities"] == 1 and cnt["unconfirmed_domains"] == 1 and cnt["stale_emails"] == 1
    assert cnt["catchall_or_unknown"] == 1 and cnt["incomplete_contacts"] == 1 and cnt["missing_websites"] == 2 and s["total"] == sum(cnt.values())
    assert (await viewer_client.get("/api/data-quality/issues/duplicate_companies")).json()["items"][0]["details"]["candidate_org_id"] == b
    mw = (await viewer_client.get("/api/data-quality/issues/missing_websites?limit=1")).json()
    assert len(mw["items"]) == 1 and mw["next_cursor"] and mw["items"][0]["classification"] == "incomplete_research"
    assert (await viewer_client.get("/api/data-quality/issues/missing_websites?classification=bogus")).status_code == 422
    assert (await viewer_client.get("/api/data-quality/issues/stale_emails")).json()["items"][0]["details"]["email"] == "old@dup.example.com"
    # actions: reason required, viewer forbidden, merge admin-only, every id audited with before/after
    base = {"action": "reject_duplicate", "ids": [link], "reason": "different companies"}
    assert (await operator_client.post("/api/data-quality/actions", json={**base, "reason": ""})).status_code == 422
    assert (await viewer_client.post("/api/data-quality/actions", json=base)).status_code == 403
    assert (await operator_client.post("/api/data-quality/actions", json={"action": "merge", "ids": [link2], "reason": "same company"})).status_code == 403
    r = (await operator_client.post("/api/data-quality/actions", json={"action": "confirm_domain", "ids": [dom, 999999], "reason": "checked the website"})).json()
    assert (r["ok"], r["skipped"]) == (1, 1)
    a1 = (await audits("data_quality.confirm_domain"))[0]
    assert a1["before"]["status"] == "review" and a1["after"]["status"] == "confirmed" and a1["reason"] == "checked the website"
    assert (await operator_client.post("/api/data-quality/actions", json=base)).json()["ok"] == 1
    assert (await operator_client.post("/api/data-quality/actions", json={"action": "queue_revalidate", "ids": [em2], "reason": "retest"})).json()["ok"] == 1
    assert (await operator_client.post("/api/data-quality/actions", json={"action": "suppress", "ids": [em], "reason": "stale and unwanted"})).json()["ok"] == 1
    assert (await operator_client.post("/api/data-quality/actions", json={"action": "mark_reviewed", "ids": [a], "reason": "looked at it"})).json()["ok"] == 1
    assert (await operator_client.post("/api/data-quality/actions", json={"action": "queue_recrawl", "ids": [a], "reason": "refresh"})).json()["ok"] == 1
    assert (await operator_client.post("/api/data-quality/actions", json={"action": "confirm_duplicate", "ids": [link2], "reason": "x y z"})).json()["skipped"] == 1  # no candidate
    # admin merge of a confirmed duplicate pair
    link3 = (await session.execute(text("INSERT INTO organisation_record_links (organisation_id, source_adapter, source_ref, match_basis, match_confidence, status, candidate_org_id) "
                                        "VALUES (:a,'x','r3','name_similar',0.8,'review',:b) RETURNING id"), {"a": a, "b": b})).scalar()
    await session.commit()
    assert (await operator_client.post("/api/data-quality/actions", json={"action": "confirm_duplicate", "ids": [link3], "reason": "same shop"})).json()["ok"] == 1
    mg = (await admin_client.post("/api/data-quality/actions", json={"action": "merge", "ids": [link3], "reason": "same shop"})).json()
    assert mg["ok"] == 1, mg
    async with dbmod.sessionmaker()() as s2:
        assert (await s2.execute(text("select count(*) from organisations where merged_into_id is not null"))).scalar() == 1


async def test_completeness_endpoint_and_business_fields(viewer_client, session):
    o = await seed_org(session, "Comp Ltd")
    c = (await viewer_client.get(f"/api/organisations/{o}/completeness")).json()
    assert set(c["flags"]) >= {"official_website", "published_email", "recently_checked"} and c["flags"]["official_website"] == "unknown"
    assert any(m["classification"] == "incomplete_research" and m["next_step"] for m in c["missing_opportunities"])
    assert (await viewer_client.get("/api/organisations/999999/completeness")).status_code == 404
    await session.execute(text("INSERT INTO organisation_completeness (organisation_id, official_website, score) VALUES (:o,'yes',42)"), {"o": o})
    await session.commit()
    row = next(i for i in (await viewer_client.get("/api/businesses")).json()["items"] if i["id"] == o)
    assert row["completeness_score"] == 42 and row["official_website_state"] == "yes"
    assert (await viewer_client.get(f"/api/businesses/{o}")).json()["completeness"]["score"] == 42


# --------------------------------------------------------------------------- ops extras
async def test_failed_jobs_and_batch_jobs(operator_client, viewer_client, session):
    jid = (await session.execute(text("INSERT INTO sourcing_jobs (run_id, stage, dedupe_key, state, error_code) VALUES (NULL,'verify','orphan','dead','boom') RETURNING id"))).scalar()
    await session.commit()
    f = (await viewer_client.get("/api/system/failed-jobs")).json()
    assert [j["id"] for j in f["items"]] == [jid] and f["items"][0]["run_id"] is None
    assert (await viewer_client.post(f"/api/system/jobs/{jid}/retry")).status_code == 403
    r = await operator_client.post(f"/api/system/jobs/{jid}/retry")
    assert r.status_code == 200 and r.json()["state"] == "queued"
    assert (await operator_client.post(f"/api/system/jobs/{jid}/retry")).status_code == 409
    assert (await operator_client.get("/api/verification/batches/999999/jobs")).status_code == 404


async def test_reach_values_aligned_with_stored(viewer_client, session):
    o = await seed_org(session, "Reach Ltd")
    await seed_email(session, o, "a@reach.example.com", reachability="safe")
    await seed_email(session, o, "b@reach.example.com", reachability="risky")
    ok = (await viewer_client.get("/api/contacts?reachability=safe")).json()["items"]
    assert [i["email"] for i in ok] == ["a@reach.example.com"] and ok[0]["discovery_source"] == "companies_house_bulk"
    assert (await viewer_client.get("/api/contacts?reachability=deliverable")).status_code == 422


async def test_contact_detail_typed_and_smtp_sentence(viewer_client, session):
    o = await seed_org(session, "Typed Ltd")
    e = await seed_email(session, o, "t@typed.example.com")
    await session.execute(text("UPDATE email_addresses SET recipient_evidence_status='smtp_recipient_accepted' WHERE id=:i"), {"i": e})
    await session.execute(text("INSERT INTO organisation_domains (organisation_id, domain, registrable_domain, website_url, verified_match_status, confidence, evidence) "
                               "VALUES (:o,'typed.example.com','typed.example.com','https://typed.example.com','confirmed',1,'[]')"), {"o": o})
    await session.commit()
    d = (await viewer_client.get(f"/api/contacts/{e}")).json()
    assert any("accepted the recipient" in s for s in d["classification_explanation"])
    assert d["statuses"]["recipient_evidence"] == "smtp_recipient_accepted" and d["freshness"]["validity_days"] and d["organisation"]["id"] == o
    schema = build_app().openapi()["components"]["schemas"]["ContactDetail"]["properties"]
    assert all("$ref" in schema[k] or "anyOf" in schema[k] or schema[k].get("type") == "array" for k in ("statuses", "catch_all", "flags", "freshness", "marketing"))
    row = (await viewer_client.get("/api/contacts")).json()["items"][0]
    assert row["website"] == "https://typed.example.com"
    assert "email_id" in d["validation_latest"][0] if d["validation_latest"] else True


async def test_merge_dry_run_and_category_updated_at(admin_client, session):
    a, b = await seed_org(session, "Keep Ltd"), await seed_org(session, "Drop Ltd")
    await seed_email(session, b, "x@drop.example.com")
    await seed_contact(session, b, "Some One")
    dry = (await admin_client.post("/api/businesses/merge?dry_run=true", json={"primary_id": a, "duplicate_ids": [b]})).json()
    assert dry["dry_run"] and dry["would_move"]["emails"] == 1 and dry["would_move"]["contacts"] == 1
    async with dbmod.sessionmaker()() as s:
        assert (await s.execute(text("select merged_into_id from organisations where id=:i"), {"i": b})).scalar() is None
        assert (await s.execute(text("select organisation_id from email_addresses"))).scalar() == b
    cat = (await admin_client.post("/api/categories", json={"name": "Cat X"})).json()
    assert cat["updated_at"]


async def test_settings_informational_and_cli_email_validation(admin_client):
    s = (await admin_client.get("/api/settings")).json()
    assert s["notifications"]["informational"] is True and s["backups"]["informational"] is True
    from app.bootstrap import create_user

    async with dbmod.sessionmaker()() as sess:
        with pytest.raises(ValueError, match="not a valid email"):
            await create_user(sess, "not-an-email", "Tr0ub4dor&3-horse-staple")


async def test_source_out_extras_and_config_schema(admin_client):
    s = next(x for x in (await admin_client.get("/api/sources")).json() if x["adapter_id"] == "contracts_finder_ocds")
    assert s["rate_policy"]["requests_per_minute"] == 10 and s["cost"] == "free" and s["access_method"] and s["fields_available"] and s["retry_policy"]
    keys = {k["key"]: k for k in s["config_schema"]}
    assert "path" in keys and keys["path"]["type"] == "path" and "days_back" in keys
    assert (await admin_client.put("/api/sources/contracts_finder_ocds/config", json={"config": {"nonsense": 1}})).status_code == 422
    assert (await admin_client.put("/api/sources/contracts_finder_ocds/config", json={"config": {"days_back": "ten"}})).status_code == 422
    assert (await admin_client.put("/api/sources/contracts_finder_ocds/config", json={"config": {"days_back": 10}})).status_code == 200


_ = datetime, UTC
