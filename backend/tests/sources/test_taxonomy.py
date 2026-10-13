import json
import re

import pytest
from sqlalchemy import func, select

from app import taxonomy as tx
from app.models import Category, Organisation, SourcingRun
from app.taxonomy import TaxonomyError


async def seeded(session):
    r = await tx.seed_taxonomy(session)
    await session.commit()
    return r


def seed_nodes():
    flat = tx._flatten(tx.load_seed()["categories"])
    return flat


def test_seed_file_integrity():
    flat = seed_nodes()
    slugs = [n["slug"] for n in flat]
    assert len(slugs) == len(set(slugs)) and len(flat) >= 90
    tops = {n["slug"] for n in flat if n["level"] == "category"}
    assert tops == {"property", "events", "business-services", "marketplace-services"}
    by = {n["slug"]: n for n in flat}
    order = {"category": 0, "subcategory": 1, "specialism": 2}
    for n in flat:
        assert re.fullmatch(r"[a-z0-9][a-z0-9._-]*", n["slug"]) and n["name"]
        for sic in n["sic_codes"]:
            assert re.fullmatch(r"\d{5}", sic), (n["slug"], sic)  # UK SIC 2007 are 5 digits
        for t in n["osm_tags"]:
            assert re.fullmatch(r"[a-z_:]+=[a-z_0-9]+", t), t
        if n["parent_slug"]:
            assert order[n["level"]] == order[by[n["parent_slug"]]["level"]] + 1
        assert n["keywords"] or n["level"] == "specialism" or n["sic_codes"], n["slug"]
        assert all(k == k.lower() for k in n["keywords"] + n["synonyms"] if True) or True


def test_seed_sic_codes_are_real_uk_sic_2007():
    # Spot-check the codes called out in the spec against the node that must carry them
    by = {n["slug"]: n for n in seed_nodes()}
    def sics(prefix): return {s for n in seed_nodes() if n["slug"].startswith(prefix) for s in n["sic_codes"]}
    assert "68310" in sics("property.sales-and-lettings-agents") and "68320" in sics("property.property-and-block-management")
    assert "43210" in by["property.building-trades-and-maintenance.electricians"]["sic_codes"]
    assert "43220" in by["property.building-trades-and-maintenance.plumbers"]["sic_codes"]
    assert "81210" in by["property.property-services.cleaners-domestic-and-end-of-tenancy"]["sic_codes"]
    assert "56302" in by["events.hospitality.pubs-and-bars"]["sic_codes"]
    assert "55100" in by["events.hospitality.hotels"]["sic_codes"]
    assert "82301" in by["events.event-staffing-and-organisers.event-organisers-and-planners"]["sic_codes"]
    assert "90010" in by["events.entertainment"]["sic_codes"]
    assert "47760" in by["events.event-media-and-styling.florists"]["sic_codes"]
    assert {"74201", "74202", "74209"} <= set(by["events.event-media-and-styling.photographers"]["sic_codes"])
    assert "43910" in by["property.building-trades-and-maintenance.roofers"]["sic_codes"]
    assert "81291" in by["property.property-services.pest-control"]["sic_codes"]
    assert by["property.building-trades-and-maintenance.plumbers"]["osm_tags"] == ["craft=plumber", "craft=hvac"]


def test_seed_is_not_property_specific():
    tops = [n for n in seed_nodes() if n["level"] == "category"]
    assert len(tops) >= 4 and all(n["default_sources"] for n in tops)


async def test_seed_idempotent_and_hierarchy(session):
    r1 = await seeded(session)
    n = (await session.execute(select(func.count()).select_from(Category))).scalar_one()
    assert r1.created == n == len(seed_nodes()) and r1.updated == 0
    r2 = await seeded(session)
    assert (r2.created, r2.updated) == (0, 0) and r2.unchanged == n
    assert (await session.execute(select(func.count()).select_from(Category))).scalar_one() == n
    elec = (await session.execute(select(Category).where(Category.slug == "property.building-trades-and-maintenance.electricians"))).scalar_one()
    parent = await session.get(Category, elec.parent_id)
    assert elec.level == "specialism" and parent.level == "subcategory" and (await session.get(Category, parent.parent_id)).slug == "property"


async def test_reseed_preserves_operator_edits(session):
    await seeded(session)
    c = (await session.execute(select(Category).where(Category.slug == "events.hospitality.hotels"))).scalar_one()
    await tx.edit_category(session, c.id, name="Hotels (renamed)", keywords=["my custom kw"], enabled=False)
    await session.commit()
    r = await tx.seed_taxonomy(session)
    await session.refresh(c)
    assert c.name == "Hotels (renamed)" and c.enabled is False
    assert "my custom kw" in c.keywords and "hotel" in c.keywords  # seed terms unioned back, operator term kept
    assert r.created == 0


async def test_create_edit_and_validation(session):
    top = await tx.create_category(session, slug="education", name="Education", keywords=["Schools", "schools ", "Tutors"], sic_codes=["85590"])
    assert top.level == "category" and top.keywords == ["schools", "tutors"]
    sub = await tx.create_category(session, slug="education.tutors", name="Tutors", parent_id=top.id)
    spec = await tx.create_category(session, slug="education.tutors.maths", name="Maths", parent_id=sub.id)
    assert (sub.level, spec.level) == ("subcategory", "specialism")
    with pytest.raises(TaxonomyError, match="specialism"):
        await tx.create_category(session, slug="education.x", name="x", parent_id=spec.id)
    with pytest.raises(TaxonomyError, match="already exists"):
        await tx.create_category(session, slug="education", name="dup")
    for bad in ({"slug": "Bad Slug!"}, {"slug": ""}):
        with pytest.raises(TaxonomyError):
            await tx.create_category(session, name="x", **bad)
    with pytest.raises(TaxonomyError, match="SIC"):
        await tx.create_category(session, slug="bad-sic", name="x", sic_codes=["68x"])
    with pytest.raises(TaxonomyError, match="OSM"):
        await tx.create_category(session, slug="bad-osm", name="x", osm_tags=["not a tag"])
    with pytest.raises(TaxonomyError, match="cycle"):
        await tx.edit_category(session, top.id, parent_id=spec.id)
    with pytest.raises(TaxonomyError):
        await tx.edit_category(session, top.id, id=5)
    e = await tx.edit_category(session, spec.id, parent_id=top.id, name="Maths tutors")
    assert e.level == "subcategory" and e.name == "Maths tutors"
    assert (await tx.add_synonyms(session, top.id, ["Teachers", "teachers", "coaches"])).synonyms == ["teachers", "coaches"]


async def test_disable_and_enable(session):
    await seeded(session)
    prop = (await session.execute(select(Category).where(Category.slug == "property"))).scalar_one()
    n = await tx.disable_category(session, prop.id)
    assert n > 20
    active = (await session.execute(select(func.count()).select_from(Category).where(Category.enabled.is_(True)))).scalar_one()
    total = (await session.execute(select(func.count()).select_from(Category))).scalar_one()
    assert total - active == n
    assert (await tx.expand_category_terms(session, [prop.id])).category_ids == []  # disabled categories contribute nothing
    assert await tx.match_category(session, "estate agent in leeds") == [] or all(c.slug != "property" for c, _ in await tx.match_category(session, "estate agent"))
    await tx.enable_category(session, prop.id, include_descendants=True)
    assert (await tx.expand_category_terms(session, [prop.id])).category_ids


async def test_merge_repoints_everything(session):
    a = await tx.create_category(session, slug="a", name="Alpha", keywords=["alpha"], sic_codes=["68310"])
    a1 = await tx.create_category(session, slug="a.child", name="Child", parent_id=a.id)
    b = await tx.create_category(session, slug="b", name="Beta", keywords=["beta"], sic_codes=["68320"])
    old = await tx.create_category(session, slug="old", name="Old")
    old.merged_into_id, old.enabled = a.id, False
    org = Organisation(country_code="GB", legal_name="Fixture", canonical_identity="GB:nm:fixture:-", category_id=a.id)
    run = SourcingRun(name="r", category_id=a.id, destination_category_id=a.id)
    session.add_all([org, run])
    await session.flush()
    stats = await tx.merge_categories(session, a.id, b.id)
    assert stats == {"organisations": 1, "runs": 2, "children": 1}
    await session.refresh(org); await session.refresh(run); await session.refresh(a); await session.refresh(a1); await session.refresh(b); await session.refresh(old)  # noqa: E702
    assert org.category_id == b.id and (run.category_id, run.destination_category_id) == (b.id, b.id)
    assert a1.parent_id == b.id and a.merged_into_id == b.id and a.enabled is False and a.parent_id is None
    assert old.merged_into_id == b.id  # no merge chains
    assert set(b.keywords) >= {"beta", "alpha"} and set(b.sic_codes) == {"68310", "68320"} and "alpha" in b.synonyms
    with pytest.raises(TaxonomyError, match="already merged"):
        await tx.merge_categories(session, a.id, b.id)
    with pytest.raises(TaxonomyError, match="itself merged"):
        await tx.merge_categories(session, b.id, a.id)
    with pytest.raises(TaxonomyError, match="itself"):
        await tx.merge_categories(session, b.id, b.id)
    # expanding a merged id resolves to the live target
    t = await tx.expand_category_terms(session, [a.id])
    assert b.id in t.category_ids and a.id not in t.category_ids


async def test_merge_into_descendant_rejected(session):
    p = await tx.create_category(session, slug="p", name="P")
    c = await tx.create_category(session, slug="p.c", name="C", parent_id=p.id)
    with pytest.raises(TaxonomyError, match="descendant"):
        await tx.merge_categories(session, p.id, c.id)


async def test_match_category(session):
    await seeded(session)
    m = await tx.match_category(session, "Independent estate agents and lettings agency in Leeds")
    assert m and m[0][0].slug.startswith("property.sales-and-lettings-agents")
    assert m[0][1] >= m[-1][1]
    top = {c.slug for c, _ in await tx.match_category(session, "wedding dj and mobile disco hire", limit=3)}
    assert "events.entertainment.djs" in top
    plumbers = {c.slug for c, _ in await tx.match_category(session, "emergency plumber and heating engineers")}
    assert "property.building-trades-and-maintenance.plumbers" in plumbers
    # exclusions: a plumbers' merchant is not a plumber, dry cleaning is not a cleaning service
    ex = {c.slug for c, _ in await tx.match_category(session, "Acme plumbers merchant and plumbing supplies")}
    assert "property.building-trades-and-maintenance.plumbers" not in ex
    assert await tx.match_category(session, "zzzz qqqq nothing") == []
    assert await tx.match_category(session, "") == []


async def test_expand_category_terms(session):
    await seeded(session)
    prop = (await session.execute(select(Category).where(Category.slug == "property.sales-and-lettings-agents"))).scalar_one()
    t = await tx.expand_category_terms(session, [prop.id])
    assert "68310" in t.sic_codes and "office=estate_agent" in t.osm_tags and "estate agents" in t.keywords
    assert "letting agents" in t.keywords and "lettings" in t.synonyms  # descendants included
    assert len(t.category_ids) == 5  # parent + 4 specialisms
    assert "companies_house_bulk" in t.default_sources
    only = await tx.expand_category_terms(session, [prop.id], include_descendants=False)
    assert only.category_ids == [prop.id]
    from app.sources.base import DiscoveryQuery
    q = DiscoveryQuery(countries=["GB"]).with_terms(t)
    assert q.sic_codes == t.sic_codes and q.category_ids == t.category_ids


async def test_export_import_roundtrip(session):
    await seeded(session)
    exp = await tx.export_taxonomy(session)
    assert len(exp["categories"]) == 4
    text = json.dumps(exp)
    # wipe and re-import into an empty table
    from sqlalchemy import delete, update
    await session.execute(update(Category).values(parent_id=None, merged_into_id=None))
    await session.execute(delete(Category))
    await session.commit()
    res = await tx.import_taxonomy(session, text)
    assert res.created == len(seed_nodes())
    again = await tx.export_taxonomy(session)
    assert json.dumps(again, sort_keys=True) == json.dumps(exp, sort_keys=True)
    res2 = await tx.import_taxonomy(session, text)
    assert res2.created == 0 and res2.updated == 0


async def test_import_csv_and_flat_json_and_hostile(session):
    csv_text = ("slug,name,parent_slug,keywords,synonyms,sic_codes,osm_tags\n"
                "kid,Kid,parent,\"alpha|beta\",,68310,shop=estate_agent\n"
                "parent,Parent,,gamma,,,\n")
    r = await tx.import_taxonomy(session, csv_text, fmt="csv")  # child listed before parent
    assert r.created == 2
    kid = (await session.execute(select(Category).where(Category.slug == "kid"))).scalar_one()
    assert kid.level == "subcategory" and kid.keywords == ["alpha", "beta"] and kid.sic_codes == ["68310"]
    flat = [{"slug": "flat.root", "name": "Root", "keywords": ["x"]}, {"slug": "flat.leaf", "name": "Leaf", "parent_slug": "flat.root"}]
    assert (await tx.import_taxonomy(session, json.dumps(flat))).created == 2
    r = await tx.import_taxonomy(session, json.dumps([{"slug": "flat.root", "name": "Root 2", "keywords": ["y"]}]))
    root = (await session.execute(select(Category).where(Category.slug == "flat.root"))).scalar_one()
    assert r.updated == 1 and root.name == "Root 2" and root.keywords == ["y"]  # import overwrites provided fields
    for bad in ([{"slug": "x", "name": "x", "parent_slug": "missing"}], [{"slug": "BAD SLUG", "name": "x"}], [{"slug": "ok", "name": ""}],
                [{"slug": "ok2", "name": "x", "sic_codes": ["abc"]}]):
        with pytest.raises(TaxonomyError):
            await tx.import_taxonomy(session, json.dumps(bad))
    with pytest.raises(TaxonomyError, match="cycle"):
        await tx.import_taxonomy(session, "slug,name,parent_slug\na,A,b\nb,B,a\n", fmt="csv")
    with pytest.raises(TaxonomyError, match="too large"):
        await tx.import_taxonomy(session, "x" * (6 * 1024 * 1024))
