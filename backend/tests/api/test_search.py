"""Global search: businesses by name prefix / company number / domain, contacts by email prefix, runs by name; viewer access;
LIKE wildcards are literal."""
from sqlalchemy import text

from .helpers import admin_client, app, seed_email, seed_org, viewer_client  # noqa: F401


async def test_global_search(viewer_client, session):  # noqa: F811
    org = await seed_org(session, "Acme Plumbing Ltd", company_number="01234567")
    await session.execute(text("INSERT INTO organisation_domains (organisation_id, domain, registrable_domain, verified_match_status) VALUES (:o, 'acme.example.com', 'acme.example.com', 'confirmed')"), {"o": org})
    await session.commit()
    await seed_email(session, org, "sales@acme.example.com")
    await seed_org(session, "100% Real_Estate Ltd")

    r = await viewer_client.get("/api/search", params={"q": "acme"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert [b["name"] for b in d["businesses"]] == ["Acme Plumbing Ltd"]
    assert d["businesses"][0]["detail"] and "01234567" in d["businesses"][0]["detail"]

    assert (await viewer_client.get("/api/search", params={"q": "01234567"})).json()["businesses"][0]["id"] == org
    assert (await viewer_client.get("/api/search", params={"q": "acme.example.com"})).json()["businesses"][0]["id"] == org
    contacts = (await viewer_client.get("/api/search", params={"q": "sales@acme"})).json()["contacts"]
    assert [c["email"] for c in contacts] == ["sales@acme.example.com"] and contacts[0]["organisation"] == "Acme Plumbing Ltd"

    # wildcards are literal: '%' and '_' do not match everything
    assert (await viewer_client.get("/api/search", params={"q": "%%"})).json()["businesses"] == []
    assert [b["name"] for b in (await viewer_client.get("/api/search", params={"q": "100% real_"})).json()["businesses"]] == ["100% Real_Estate Ltd"]
    assert (await viewer_client.get("/api/search", params={"q": "a"})).status_code == 422  # too short
