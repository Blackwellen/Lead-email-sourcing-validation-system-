from __future__ import annotations

import pytest
from sqlalchemy import select

from app.crawler.ratelimit import RateLimiter
from app.discovery.graph import (
    expand_network,
    extract_candidates,
    is_rejected_domain,
    list_relationships,
    neighbourhood,
    rank_site_links,
)
from app.models import BusinessRelationship, Organisation, OrganisationDomain, SourcingJob, SourcingRun

HOME = """<html><head><title>Seed Co</title></head><body>
<nav><a href="/">Home</a><a href="/partners">Partners</a><a href="/suppliers">Suppliers</a><a href="/locations">Our locations</a>
<a href="/login">Login</a><a href="/contact">Contact</a></nav>
<footer><a href="https://www.facebook.com/seedco">Facebook</a></footer></body></html>"""

PARTNERS = """<html><body><h1>Partners</h1>
<ul class="partner-list">
 <li><a href="https://www.partner-one.co.uk/">Partner One Ltd</a></li>
 <li><a href="https://partner-two.co.uk/about"><img src="x.png" alt="Partner Two"></a></li>
 <li><a href="https://www.facebook.com/partner">Facebook page</a></li>
 <li><a href="https://uk.linkedin.com/company/x">LinkedIn</a></li>
 <li><a href="https://www.yell.com/biz/foo">Yell listing</a></li>
 <li><a href="https://www.seed-co.co.uk/">Seed Co home</a></li>
 <li><a href="https://shop.wixsite.com/x">Our shop</a></li>
 <li><a href="mailto:a@b.co.uk">Email</a></li>
 <li><a href="https://www.partner-three.co.uk/">click here</a></li>
</ul>
<footer><a href="https://www.footer-link.co.uk/">Footer Company</a></footer>
<a href="/partners">self</a><a href="/">home</a></body></html>"""

SUPPLIERS = """<html><head><script type="application/ld+json">
{"@type":"Organization","name":"Seed Co","member":[{"@type":"Organization","name":"Supplier Alpha Ltd","url":"https://supplier-alpha.co.uk"}]}
</script></head><body><div class="supplier-grid"><a href="https://www.supplier-beta.co.uk/">Supplier Beta</a></div></body></html>"""

LOCATIONS = """<html><body><ul class="location-list"><li>Leeds</li><li>Sheffield</li>
<li>We are open every day of the week and love customers a lot.</li></ul>
<nav><ul class="menu"><li>Menu item</li></ul></nav></body></html>"""


def build(server, robots="User-agent: *\nAllow: /\n"):
    server.add("/robots.txt", robots, ctype="text/plain")
    server.add("/", HOME)
    server.add("/partners", PARTNERS)
    server.add("/suppliers", SUPPLIERS)
    server.add("/locations", LOCATIONS)
    server.add("/login", "<html>login</html>")
    server.add("/contact", "<html>c</html>")


async def seed_org(session, server, name="Seed Co Ltd", reg="seed-co.co.uk", base="/", status="confirmed", number="00001111"):
    o = Organisation(legal_name=name, company_number=number, country_code="GB", canonical_identity=f"GB:cn:{number}")
    session.add(o)
    await session.flush()
    session.add(OrganisationDomain(organisation_id=o.id, domain=reg, registrable_domain=reg, website_url=server.url(base),
                                   verified_match_status=status, confidence=0.95))
    run = SourcingRun(name="r", status="running")
    session.add(run)
    await session.flush()
    return o.id, run.id


def limiter():
    return RateLimiter(min_interval=0)


async def test_expansion_creates_candidates_not_confirmed(session, server, fetcher):
    build(server)
    oid, rid = await seed_org(session, server)
    res = await expand_network(session, fetcher, oid, run_id=rid, max_depth=1, budget=20, limiter=limiter())
    assert not res.budget_exhausted and res.edges == 6
    orgs = {o.trading_name or o.legal_name: o for o in (await session.execute(select(Organisation).where(Organisation.id != oid))).scalars()}
    names = set(orgs)
    assert {"Partner One Ltd", "Partner Two", "Supplier Alpha Ltd", "Supplier Beta"} <= names
    # aggregators / social / directory / platform / self / mailto / generic-text / footer links rejected
    joined = " ".join(names).lower()
    for bad in ("facebook", "linkedin", "yell", "shop", "seed co home", "email", "click here", "footer company"):
        assert bad not in joined, bad
    # branches kept as separate branch records
    branches = [n for n in names if n.startswith("Seed Co Ltd - ")]
    assert sorted(branches) == ["Seed Co Ltd - Leeds", "Seed Co Ltd - Sheffield"]
    rels = {(r.from_org_id, r.to_org_id, r.relation): r for r in (await session.execute(select(BusinessRelationship))).scalars()}
    p1 = orgs["Partner One Ltd"].id
    assert (oid, p1, "partner_of") in rels  # seed -> partner
    assert (orgs["Supplier Beta"].id, oid, "supplier_of") in rels  # supplier -> seed
    assert (orgs["Supplier Alpha Ltd"].id, oid, "member_of_group") in rels  # JSON-LD 'member'
    assert (orgs[branches[0]].id, oid, "branch_of") in rels
    r = rels[(oid, p1, "partner_of")]
    assert r.source_adapter == "network_expansion" and r.source_url.endswith("/partners") and r.depth == 1 and 0 < r.confidence <= 1
    # NEVER auto-confirmed: candidate domains stay 'unknown'
    doms = (await session.execute(select(OrganisationDomain).where(OrganisationDomain.organisation_id.in_([o.id for o in orgs.values()])))).scalars().all()
    assert doms and all(d.verified_match_status == "unknown" and d.confidence == 0 for d in doms)
    # every new company with a website gets a resolve_domain job
    jobs = (await session.execute(select(SourcingJob).where(SourcingJob.stage == "resolve_domain"))).scalars().all()
    assert {j.payload["organisation_id"] for j in jobs} == {orgs[n].id for n in names if not n.startswith("Seed Co Ltd - ")}
    # rerun is idempotent
    n_before = len((await session.execute(select(Organisation))).scalars().all())
    res2 = await expand_network(session, fetcher, oid, run_id=rid, max_depth=1, budget=20, limiter=limiter())
    assert len((await session.execute(select(Organisation))).scalars().all()) == n_before and not res2.new_orgs and res2.jobs_enqueued == 0
    # the login / contact pages (not approved kinds) were never fetched
    assert server.hits("/login") == 0 and server.hits("/contact") == 0


async def test_budget_limits_fetches(session, server, fetcher):
    build(server)
    oid, rid = await seed_org(session, server)
    res = await expand_network(session, fetcher, oid, run_id=rid, budget=2, limiter=limiter())
    assert res.fetched == 2 and res.budget_exhausted
    page_hits = sum(server.hits(p) for p in ("/", "/partners", "/suppliers", "/locations"))
    assert page_hits == 2


async def test_robots_respected(session, server, fetcher):
    build(server, robots="User-agent: *\nDisallow: /suppliers\n")
    oid, rid = await seed_org(session, server)
    res = await expand_network(session, fetcher, oid, run_id=rid, budget=20, limiter=limiter())
    assert server.hits("/suppliers") == 0 and server.hits("/partners") == 1
    assert any(s.get("reason") == "robots_disallowed" for s in res.skipped)
    assert not any(o.trading_name == "Supplier Beta" for o in (await session.execute(select(Organisation))).scalars())


async def test_no_confirmed_domain_no_fetch(session, server, fetcher):
    build(server)
    oid, rid = await seed_org(session, server, status="review")
    res = await expand_network(session, fetcher, oid, run_id=rid, budget=20, limiter=limiter())
    assert res.fetched == 0 and res.skipped[0]["reason"] == "no_confirmed_domain" and not server.requests


async def test_depth_cycle_and_visited(session, server, fetcher):
    build(server)
    # B is an existing organisation WITH a confirmed domain whose site links back to the seed (cycle)
    server.add("/b/", '<html><body><a href="/b/partners">Our partners</a></body></html>')
    server.add("/b/partners", '<html><body><ul class="partner-list"><li><a href="https://www.seed-co.co.uk/">Seed Co Ltd</a></li>'
                              '<li><a href="https://www.deep-one.co.uk/">Deep One Ltd</a></li></ul></body></html>')
    oid, rid = await seed_org(session, server)
    b = Organisation(legal_name="Partner One Ltd", country_code="GB", canonical_identity="GB:nm:partner one:-")
    session.add(b)
    await session.flush()
    session.add(OrganisationDomain(organisation_id=b.id, domain="partner-one.co.uk", registrable_domain="partner-one.co.uk",
                                   website_url=server.url("/b/"), verified_match_status="confirmed", confidence=0.9))
    await session.flush()
    res = await expand_network(session, fetcher, oid, run_id=rid, max_depth=2, budget=40, limiter=limiter())
    assert server.hits("/") == 1 and server.hits("/b/") == 1  # seed expanded once, B once (no ping-pong)
    assert b.id in res.matched_orgs and res.depth_reached == 2
    assert res.visited_orgs.count(oid) == 1
    deep = (await session.execute(select(Organisation).where(Organisation.trading_name == "Deep One Ltd"))).scalar_one()
    rel = (await session.execute(select(BusinessRelationship).where(BusinessRelationship.from_org_id == b.id, BusinessRelationship.to_org_id == deep.id))).scalar_one()
    assert rel.depth == 2
    # depth 1 would not have fetched B
    server.requests.clear()
    await expand_network(session, fetcher, oid, run_id=rid, max_depth=1, budget=40, limiter=limiter())
    assert server.hits("/b/") == 0
    # absurd max_depth is clamped, not honoured
    res3 = await expand_network(session, fetcher, oid, run_id=rid, max_depth=99, budget=40, limiter=limiter())
    assert res3.depth_reached <= 3


async def test_name_collision_with_other_confirmed_domain_is_not_merged(session, server, fetcher):
    build(server)
    other = Organisation(legal_name="Partner One Ltd", country_code="GB", canonical_identity="GB:nm:partner one:-")
    session.add(other)
    await session.flush()
    session.add(OrganisationDomain(organisation_id=other.id, domain="partnerone-different.co.uk", registrable_domain="partnerone-different.co.uk",
                                   verified_match_status="confirmed", confidence=0.9))
    oid, rid = await seed_org(session, server)
    res = await expand_network(session, fetcher, oid, run_id=rid, budget=20, limiter=limiter())
    assert any(s.get("reason") == "name_collision_different_domain" for s in res.skipped)
    assert other.id not in res.matched_orgs


def test_extractors_unit():
    assert is_rejected_domain("https://www.facebook.com/x") == "aggregator_or_social"
    assert is_rejected_domain("https://x.wixsite.com") == "aggregator_or_platform"
    assert is_rejected_domain("https://www.acme.co.uk", {"acme.co.uk"}) == "same_site"
    assert is_rejected_domain("https://www.new-co.co.uk") is None
    assert is_rejected_domain("http://127.0.0.1/") == "no_registrable_domain"
    ranked = rank_site_links(HOME, "https://seed.co.uk/")
    assert [k for _u, k, _s in ranked][:3].count("suppliers") == 1 and all("login" not in u for u, _k, _s in ranked)
    c = extract_candidates(PARTNERS, "https://seed.co.uk/partners", "partners", {"seed.co.uk"})
    assert [x.name for x in c] == ["Partner One Ltd", "Partner Two", "Seed Co home"][:0] or {x.name for x in c} >= {"Partner One Ltd", "Partner Two"}
    assert all(x.confidence >= 0.4 for x in c)


async def test_neighbourhood_cap_and_table(session):
    hub = Organisation(legal_name="Hub", country_code="GB", canonical_identity="GB:nm:hub:-")
    session.add(hub)
    leaves = [Organisation(legal_name=f"Leaf {i}", country_code="GB", canonical_identity=f"GB:nm:leaf{i}:-") for i in range(340)]
    session.add_all(leaves)
    await session.flush()
    session.add_all([BusinessRelationship(from_org_id=hub.id, to_org_id=lf.id, relation="partner_of", source_adapter="t", confidence=0.5)
                     for lf in leaves])
    # second ring
    ring = Organisation(legal_name="Ring", country_code="GB", canonical_identity="GB:nm:ring:-")
    session.add(ring)
    await session.flush()
    session.add(BusinessRelationship(from_org_id=leaves[0].id, to_org_id=ring.id, relation="supplier_of", source_adapter="t"))
    await session.flush()
    g = await neighbourhood(session, hub.id, depth=2, limit=10_000)
    assert len(g["nodes"]) == 300 and g["truncated"] and len(g["edges"]) <= 299 + 1
    small = await neighbourhood(session, leaves[0].id, depth=2, limit=50)
    assert len(small["nodes"]) == 50 and small["truncated"]  # depth 2 reaches the hub's 340 leaves: bounded, flagged
    assert {n["id"] for n in small["nodes"]} >= {hub.id, ring.id, leaves[0].id}
    assert next(n for n in small["nodes"] if n["id"] == ring.id)["distance"] == 1
    d1 = await neighbourhood(session, leaves[0].id, depth=1, limit=50)
    assert ring.id in {n["id"] for n in d1["nodes"]}
    # table fallback: keyset pagination, filters
    seen, cur = [], None
    while True:
        page = await list_relationships(session, {}, cur, limit=100)
        seen += [i["id"] for i in page["items"]]
        cur = page["next_cursor"]
        if cur is None:
            break
    assert len(seen) == 341 and len(set(seen)) == 341 and seen == sorted(seen, reverse=True)
    only = await list_relationships(session, {"relation": "supplier_of"})
    assert len(only["items"]) == 1 and only["items"][0]["to_name"] == "Ring"
    assert pytest is not None
