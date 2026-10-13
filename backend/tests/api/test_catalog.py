"""Categories, presets, geo and sources."""
import io
import json
import os

import httpx
from openpyxl import Workbook
from sqlalchemy import text

from app.db import sessionmaker

from .helpers import ApiClient, build_app, make_client, seed_email, seed_org


async def test_category_crud_tree_counts_and_directory(operator_client, admin_client):
    c = operator_client
    root = (await c.post("/api/categories", json={"name": "Trades & Services", "keywords": ["trade", " trade ", "service"]})).json()
    assert root["slug"] == "trades-services" and root["level"] == "category" and root["keywords"] == ["trade", "service"]
    sub = (await c.post("/api/categories", json={"name": "Plumbers", "parent_id": root["id"], "synonyms": ["plumbing"]})).json()
    leaf = (await c.post("/api/categories", json={"name": "Boiler repair", "parent_id": sub["id"]})).json()
    assert sub["level"] == "subcategory" and leaf["level"] == "specialism"
    assert (await c.post("/api/categories", json={"name": "Too deep", "parent_id": leaf["id"]})).status_code == 422
    assert (await c.post("/api/categories", json={"name": "Dup", "slug": "plumbers"})).status_code == 409
    async with sessionmaker()() as s:
        o1 = await seed_org(s, "Pipes Ltd", category_id=sub["id"], country="GB")
        o2 = await seed_org(s, "Boilers Ltd", category_id=leaf["id"], country="IE")
        await seed_email(s, o1, "a@pipes.example.com", marketing="eligible")
        await seed_email(s, o2, "b@boilers.example.com", marketing="needs_review")
        await s.execute(text("INSERT INTO sourcing_runs (name, category_id, started_at) VALUES ('r', :c, now())"), {"c": sub["id"]})
        await s.commit()
    tree = (await c.get("/api/categories")).json()
    assert len(tree) == 1 and tree[0]["name"] == "Trades & Services"
    assert tree[0]["business_count"] == 2 and tree[0]["email_count"] == 2 and tree[0]["published_count"] == 2 and tree[0]["eligible_count"] == 1
    assert tree[0]["countries"] == ["GB", "IE"] and tree[0]["last_run_at"]
    assert tree[0]["children"][0]["children"][0]["name"] == "Boiler repair" and tree[0]["children"][0]["business_count"] == 2
    assert (await c.get(f"/api/categories/{sub['id']}")).json()["business_count"] == 2
    d = (await c.get(f"/api/categories/{root['id']}/contacts")).json()
    assert sorted(i["email"] for i in d["items"]) == ["a@pipes.example.com", "b@boilers.example.com"]
    # edit / synonyms / move
    assert (await c.put(f"/api/categories/{sub['id']}", json={"name": "Plumbers & Heating"})).json()["name"] == "Plumbers & Heating"
    assert (await c.put(f"/api/categories/{sub['id']}/synonyms", json={"synonyms": ["pipes", "pipes", " heating "]})).json()["synonyms"] == ["pipes", "heating"]
    assert (await c.put(f"/api/categories/{root['id']}", json={"set_parent": True, "parent_id": leaf["id"]})).status_code == 422  # cycle
    # destructive operations are admin-only
    assert (await c.post(f"/api/categories/{leaf['id']}/disable")).status_code == 403
    assert (await admin_client.post(f"/api/categories/{sub['id']}/disable")).json()["enabled"] is False
    assert [n["enabled"] for n in (await c.get("/api/categories?include_disabled=false")).json()] == [True]
    assert all(not n["enabled"] for n in (await c.get(f"/api/categories/{sub['id']}")).json()["children"])
    await admin_client.post(f"/api/categories/{sub['id']}/enable")
    # merge leaf into sub
    m = (await admin_client.post(f"/api/categories/{leaf['id']}/merge", json={"target_id": sub["id"]})).json()
    assert m["businesses_moved"] == 1
    got = (await c.get(f"/api/categories/{sub['id']}")).json()
    assert "Boiler repair" in got["synonyms"] and got["business_count"] == 2
    assert (await admin_client.post(f"/api/categories/{sub['id']}/merge", json={"target_id": sub["id"]})).status_code == 422
    assert (await admin_client.post(f"/api/categories/{root['id']}/merge", json={"target_id": sub["id"]})).status_code == 422
    assert (await admin_client.delete(f"/api/categories/{sub['id']}")).status_code == 409  # still in use
    empty = (await c.post("/api/categories", json={"name": "Empty"})).json()
    assert (await admin_client.delete(f"/api/categories/{empty['id']}")).status_code == 200


async def test_taxonomy_import_export_roundtrip(admin_client):
    c = admin_client
    data = {"categories": [
        {"slug": "food", "name": "Food", "keywords": ["restaurant"], "synonyms": ["eatery"]},
        {"slug": "bakeries", "name": "Bakeries", "parent_slug": "food", "osm_tags": ["shop=bakery"], "sic_codes": ["10710"]},
    ]}
    r = await c.post("/api/categories/import?dry_run=true", files={"file": ("tax.json", json.dumps(data), "application/json")})
    assert r.status_code == 200 and r.json() == {"created": 2, "updated": 0, "dry_run": True, "errors": []}
    assert (await c.get("/api/categories")).json() == []
    r = await c.post("/api/categories/import", files={"file": ("tax.json", json.dumps(data), "application/json")})
    assert r.json()["created"] == 2
    exp = (await c.get("/api/categories/export?format=json")).json()
    assert [x["slug"] for x in exp["categories"]] == ["food", "bakeries"] and exp["categories"][1]["parent_slug"] == "food" and exp["categories"][1]["osm_tags"] == ["shop=bakery"]
    csv_text = (await c.get("/api/categories/export?format=csv")).text
    assert csv_text.splitlines()[0].startswith("slug,name,parent_slug")
    r = await c.post("/api/categories/import", files={"file": ("tax.csv", csv_text, "text/csv")})
    assert r.json() == {"created": 0, "updated": 2, "dry_run": False, "errors": []}
    bad = {"categories": [{"slug": "x", "name": "X", "parent_slug": "missing"}, {"slug": "BAD SLUG", "name": "Y"}, {"name": ""}]}
    r = await c.post("/api/categories/import", files={"file": ("bad.json", json.dumps(bad), "application/json")})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_taxonomy" and len(r.json()["error"]["detail"]) >= 3
    assert (await c.post("/api/categories/import", files={"file": ("x.csv", b"MZ\x90\x00binary", "text/csv")})).status_code == 422
    assert (await c.post("/api/categories/import", files={"file": ("x.json", b"{not json", "application/json")})).status_code == 422


async def test_taxonomy_import_size_limit(admin_client):
    big = json.dumps({"categories": [{"slug": f"c{i}", "name": "x" * 100} for i in range(60_000)]})
    assert len(big) > 5 * 1024 * 1024
    r = await admin_client.post("/api/categories/import", files={"file": ("big.json", big, "application/json")})
    assert r.status_code == 413


async def test_presets_crud_and_ownership(app, operator_client, viewer_client, admin_client):
    c = operator_client
    r = await c.post("/api/presets", json={"kind": "view_contacts", "name": "Hot leads", "config": {"filters": {"min_confidence": 80}, "sort": "confidence"}})
    assert r.status_code == 201
    pid = r.json()["id"]
    assert (await c.post("/api/presets", json={"kind": "view_contacts", "name": "Hot leads", "config": {}})).status_code == 409
    assert (await c.post("/api/presets", json={"kind": "view_contacts", "name": "x" * 300, "config": {}})).status_code == 422
    assert (await c.post("/api/presets", json={"kind": "nope", "name": "n", "config": {}})).status_code == 422
    assert (await c.post("/api/presets", json={"kind": "run", "name": "big", "config": {"x": "y" * 70_000}})).status_code == 413
    assert [p["name"] for p in (await viewer_client.get("/api/presets?kind=view_contacts")).json()] == ["Hot leads"]
    assert (await viewer_client.delete(f"/api/presets/{pid}")).status_code == 403
    async with make_client("operator", app) as other:
        assert (await other.put(f"/api/presets/{pid}", json={"name": "stolen"})).status_code == 403
        assert (await other.delete(f"/api/presets/{pid}")).status_code == 403
    assert (await c.put(f"/api/presets/{pid}", json={"name": "Hotter leads", "config": {"a": 1}})).json()["config"] == {"a": 1}
    assert (await admin_client.delete(f"/api/presets/{pid}")).status_code == 200
    assert (await c.get(f"/api/presets/{pid}")).status_code == 404


async def test_geo_endpoints(operator_client):
    c = operator_client
    async with sessionmaker()() as s:
        await seed_org(s, "L1", country="GB", region="Greater London", city="London")
        await seed_org(s, "L2", country="GB", region="Greater London", city="London")
        await seed_org(s, "M1", country="GB", region="Greater Manchester", city="Manchester")
        o = await seed_org(s, "P1", country="GB", region="Kent", city="Canterbury")
        await s.execute(text("UPDATE organisations SET postal_code='CT1 2AB' WHERE id=:o"), {"o": o})
        await s.execute(text("UPDATE organisations SET postal_code='SW1A 1AA' WHERE country_code='GB' AND city='London'"))
        await seed_org(s, "D1", country="DE", region="Bayern", city="Munich")
    top = (await c.get("/api/geo/countries")).json()
    assert top[0]["code"] == "GB" and top[0]["businesses"] == 4 and top[1]["code"] == "DE"
    assert [r["code"] for r in (await c.get("/api/geo/countries?with_data=true")).json()] == ["GB", "DE"]
    assert [r["code"] for r in (await c.get("/api/geo/countries?q=ireland")).json()] == ["IE"]
    subs = (await c.get("/api/geo/countries/GB/subdivisions?q=kent")).json()
    assert subs and subs[0]["name"] == "Kent" and subs[0]["businesses"] == 1
    assert (await c.get("/api/geo/countries/ZZ/subdivisions")).status_code == 404
    loc = (await c.get("/api/geo/localities?country=GB&kind=city")).json()
    assert loc[0] == {"kind": "city", "name": "London", "count": 2}
    assert [x["name"] for x in (await c.get("/api/geo/localities?country=GB&kind=city&q=man")).json()] == ["Manchester"]
    assert (await c.get("/api/geo/localities?country=GB&kind=postcode_area")).json()[0] == {"kind": "postcode_area", "name": "SW", "count": 2}
    assert (await c.get("/api/geo/localities?country=GB&kind=hacker")).status_code == 422
    av = (await c.get("/api/geo/source-availability?countries=GB,DE")).json()
    assert av["registry_loaded"] is True and {x["country"] for x in av["countries"]} == {"GB", "DE"}
    gb = next(x for x in av["countries"] if x["country"] == "GB")
    assert any(s["adapter_id"] == "companies_house_bulk" for s in gb["sources"])
    plan = (await c.get("/api/geo/coverage-plan?countries=GB")).json()
    assert plan["countries"][0]["existing_businesses"] == 4 and plan["countries"][0]["country"] == "GB"
    assert (await c.get("/api/geo/coverage-plan?countries=gb;drop")).status_code == 422


async def test_sources_listing_state_and_admin_controls(operator_client, admin_client):
    c = operator_client
    async with sessionmaker()() as s:
        o = await seed_org(s, "Src Ltd", source="companies_house_bulk")
        await seed_email(s, o, "a@src.example.com")
        await seed_email(s, o, "b@src.example.com", origin="pattern_inferred")
    srcs = (await c.get("/api/sources")).json()
    by = {s["adapter_id"]: s for s in srcs}
    assert "companies_house_bulk" in by and "csv_import" in by and by["companies_house_bulk"]["registered"] is True
    ch = by["companies_house_bulk"]
    assert ch["total_businesses"] == 1 and ch["total_published_emails"] == 1 and ch["licence"] and ch["country_coverage"]
    assert (await c.post("/api/sources/csv_import/enable")).status_code == 403
    r = (await admin_client.post("/api/sources/csv_import/enable")).json()
    assert r["enabled"] is True
    assert (await admin_client.post("/api/sources/csv_import/disable")).json()["enabled"] is False
    assert (await admin_client.put("/api/sources/csv_import/priority", json={"priority": 7})).json()["priority"] == 7
    assert (await admin_client.put("/api/sources/csv_import/priority", json={"priority": -1})).status_code == 422
    assert (await admin_client.post("/api/sources/does_not_exist/enable")).status_code == 404
    r = await admin_client.put("/api/sources/csv_import/config", json={"config": {"country_code": "GB", "api_key": "sk-supersecret", "mapping": {"token": "abc"}}})
    assert r.status_code == 200 and r.json()["config"] == {"country_code": "GB", "api_key": "********", "mapping": {"token": "********"}}
    r = await admin_client.put("/api/sources/csv_import/config", json={"config": {"country_code": "IE", "api_key": "********"}})
    async with sessionmaker()() as s:
        cfg = (await s.execute(text("SELECT config FROM source_adapter_state WHERE adapter_id='csv_import'"))).scalar_one()
    from app.sources.secret_config import open_config

    assert cfg["api_key"].startswith("enc:v1:") and "sk-supersecret" not in json.dumps(cfg)  # sealed at rest (finding 11)
    assert open_config(cfg) == {"country_code": "IE", "api_key": "sk-supersecret"}  # masked placeholder keeps the stored secret
    assert (await admin_client.put("/api/sources/csv_import/config", json={"config": {"path": "/etc/passwd"}})).status_code == 422
    # a disabled source cannot be used by a run
    v = (await c.post("/api/runs/validate", json={"name": "n", "sources": ["csv_import"]})).json()
    assert v["valid"] is False and "disabled" in v["errors"][0]["message"]
    h = (await c.post("/api/sources/csv_import/health")).json()
    assert h["status"] in ("not_configured", "ok", "unavailable") and h["checked_at"]
    assert (await c.get("/api/sources/csv_import")).json()["health"]["status"] == h["status"]


def _xlsx_bytes(rows):
    wb = Workbook()
    for r in rows:
        wb.active.append(r)
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


async def test_source_file_upload_limits_and_malicious_files(operator_client):
    c = operator_client
    good = b"company name,website,country\nAcme Ltd,acme.example.com,GB\nBeta Ltd,beta.example.com,GB\n"
    r = await c.post("/api/sources/upload", files={"file": ("../../../etc/evil.csv", good, "text/csv")})
    assert r.status_code == 201, r.text
    j = r.json()
    uploads = os.path.join(os.environ["LEAD_DATA_DIR"], "uploads")
    assert os.path.dirname(j["path"]) == os.path.realpath(uploads) and os.path.basename(j["path"]) == f"{j['upload_id']}.csv" and "evil" not in j["path"]
    assert oct(os.stat(j["path"]).st_mode & 0o777) == "0o600" and open(j["path"], "rb").read() == good
    assert j["report"]["accepted"] == 2
    # the stored path is accepted by run validation; any other path is not
    ok = (await c.post("/api/runs/validate", json={"name": "n", "sources": ["csv_import"], "source_params": {"csv_import": j["suggested_source_params"]["csv_import"]}})).json()
    assert not any("uploaded" in e["message"] for e in ok["errors"])
    x = await c.post("/api/sources/upload", files={"file": ("book.xlsx", _xlsx_bytes([["name", "domain"], ["Acme", "acme.example.com"]]), "application/octet-stream")})
    assert x.status_code == 201 and x.json()["kind"] == "xlsx"
    # rejected content
    exe = b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 200
    elf = b"\x7fELF\x02\x01\x01" + b"\x00" * 200
    zipbomb = b"PK\x03\x04" + b"\x00" * 100
    html = b"<html><script>alert(1)</script></html>"
    pdf = b"%PDF-1.7 \n1 0 obj"
    nul = b"name,domain\nAcme\x00,acme.example.com\n"
    legacy = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100
    php = b"<?php system($_GET['c']); ?>"
    for name, data in [("a.csv", exe), ("b.csv", elf), ("c.xlsx", zipbomb), ("d.csv", html), ("e.csv", pdf), ("f.csv", nul), ("g.xls", legacy), ("h.csv", php), ("i.csv", b"")]:
        r = await c.post("/api/sources/upload", files={"file": (name, data, "text/csv")})
        assert r.status_code == 422, (name, r.status_code, r.text)
    # xlsx with a macro payload
    import zipfile

    macro = io.BytesIO()
    with zipfile.ZipFile(macro, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("xl/workbook.xml", "<workbook/>")
        z.writestr("xl/vbaProject.bin", b"macro")
    assert (await c.post("/api/sources/upload", files={"file": ("m.xlsx", macro.getvalue(), "application/octet-stream")})).status_code == 422
    assert (await c.post("/api/sources/upload", files={"file": ("j.csv", b'{"a": 1}', "text/csv")})).status_code == 422
    # nothing malicious was left on disk
    assert len(os.listdir(uploads)) >= 2
    for f in os.listdir(uploads):
        assert f.endswith((".csv", ".xlsx"))


async def test_upload_size_limit_is_enforced():
    app = build_app(max_upload_mb=1)
    async with make_client("operator", app) as c:
        big = b"name,domain\n" + b"Acme Ltd,acme.example.com\n" * 60_000  # ~1.5 MB
        r = await c.post("/api/sources/upload", files={"file": ("big.csv", big, "text/csv")})
        assert r.status_code == 413 and r.json()["error"]["code"] == "payload_too_large"
        r = await c.post("/api/verification/delivery-events", files={"file": ("big.csv", big, "text/csv")})
        assert r.status_code == 413
    _ = (ApiClient, httpx)
