from __future__ import annotations

import pytest
from sqlalchemy import select

from app.discovery.signals import record_signal
from app.discovery.similar import (
    category_score,
    expand_from_seed,
    find_similar,
    postal_area,
    sic_pair,
    soft_jaccard,
)
from app.models import (
    BusinessSignal,
    Category,
    Organisation,
    OrganisationDomain,
    OrganisationSource,
    RunOrganisation,
    SourcingJob,
    SourcingRun,
)


async def org(session, name, sic=(), cat=None, pc=None, city=None, region=None, cc="GB", **kw):
    o = Organisation(legal_name=name, country_code=cc, canonical_identity=f"{cc}:nm:{name}:{pc}", sic_codes=list(sic), category_id=cat,
                     postal_code=pc, city=city, region=region, **kw)
    session.add(o)
    await session.flush()
    return o


def test_primitives():
    assert sic_pair("43220", "43220") == 1.0 and sic_pair("43220", "43221") == 0.75 and sic_pair("43220", "43290") == 0.5
    assert sic_pair("43220", "43100") == 0.3 and sic_pair("43220", "49100") == 0.0 and sic_pair("4322", "43229") == 0.75
    assert soft_jaccard({"43220"}, {"43220"}) == 1.0 and soft_jaccard({"43220"}, {"10110"}) == 0.0
    assert soft_jaccard({"43220", "43210"}, {"43220"}) == pytest.approx(0.5 + 0.0, abs=0.35)  # partial credit, between disjoint and equal
    assert 0 < soft_jaccard({"43220"}, {"43221"}) < 1
    assert postal_area("LS1 4AB") == "LS" and postal_area("M1 1AA") == "M" and postal_area("nonsense") is None
    tree = {1: None, 2: 1, 3: 1, 4: 2, 5: None}
    assert category_score(tree, {4}, 4) == 1.0
    assert category_score(tree, {4}, 2) == 0.5 and category_score(tree, {2}, 4) == 0.5  # parent / child
    assert category_score(tree, {2}, 3) == 0.5  # siblings
    assert category_score(tree, {4}, 5) == 0.0 and category_score(tree, {4}, None) == 0.0
    assert category_score(tree, {4}, 1) == pytest.approx(1 / 3)


async def _world(session):
    parent = Category(slug="trades", name="Trades")
    session.add(parent)
    await session.flush()
    heat = Category(slug="heating", name="Heating", parent_id=parent.id)
    elec = Category(slug="electrical", name="Electrical", parent_id=parent.id)
    food = Category(slug="food", name="Food")
    session.add_all([heat, elec, food])
    await session.flush()
    seed = await org(session, "Northern Boiler Services", ["43220"], heat.id, "LS1 4AB", "Leeds", "Yorkshire", legal_type="Private limited company",
                     industry="heating engineers", subcategory="boiler installation")
    session.add(OrganisationSource(organisation_id=seed.id, adapter_id="osm", source_ref="s", lat=53.80, lon=-1.55, raw={"employees": 20}))
    near = await org(session, "Leeds Boiler Installers", ["43220"], heat.id, "LS2 8JB", "Leeds", "Yorkshire", legal_type="Private limited company",
                     industry="heating engineers", subcategory="boiler installation")
    session.add(OrganisationSource(organisation_id=near.id, adapter_id="osm", source_ref="n", lat=53.81, lon=-1.54, raw={"employees": 25}))
    far = await org(session, "Southern Boiler Services", ["43220"], heat.id, "BN1 1AA", "Brighton", "South East", legal_type="Private limited company",
                    industry="heating engineers")
    session.add(OrganisationSource(organisation_id=far.id, adapter_id="osm", source_ref="f", lat=50.82, lon=-0.14, raw={}))
    related = await org(session, "Leeds Electrical Contractors", ["43210"], elec.id, "LS3 1AA", "Leeds", "Yorkshire", industry="electricians")
    other = await org(session, "Tasty Bakery", ["10710"], food.id, "LS1 1AA", "Leeds", "Yorkshire", industry="bakery")
    foreign = await org(session, "Boiler Services Dublin", ["43220"], heat.id, None, "Dublin", None, cc="IE", industry="heating engineers")
    merged = await org(session, "Leeds Boiler Dupe", ["43220"], heat.id, "LS2 8JB", "Leeds", "Yorkshire")
    merged.merged_into_id = near.id
    await session.flush()
    return dict(seed=seed, near=near, far=far, related=related, other=other, foreign=foreign, merged=merged, heat=heat, parent=parent)


async def test_scoring_ranking_explanations_deterministic(session):
    w = await _world(session)
    r1 = await find_similar(session, {"org_id": w["seed"].id}, limit=10)
    r2 = await find_similar(session, {"org_id": w["seed"].id}, limit=10)
    assert [(c.org_id, c.score) for c in r1] == [(c.org_id, c.score) for c in r2]  # deterministic
    ids = [c.org_id for c in r1]
    assert w["seed"].id not in ids  # seed never returned
    assert w["merged"].id not in ids and w["foreign"].id not in ids  # merged duplicates and other countries excluded
    assert ids[0] == w["near"].id and ids.index(w["near"].id) < ids.index(w["far"].id) < ids.index(w["other"].id) if w["other"].id in ids else True
    near = r1[0]
    assert near.score > 0.8
    f = near.factors
    assert f["sic"]["score"] == 1.0 and f["category"]["score"] == 1.0 and f["geography"]["detail"].endswith("km from nearest seed")
    assert "boiler" in f["keywords"]["detail"] and f["size"]["used"]
    assert f["accreditations"]["used"] is False  # seed has none -> factor dropped, weights renormalised
    assert sum(v["weight"] for v in f.values()) == pytest.approx(1.0, abs=1e-3)
    assert "sic=" in near.explain()
    far = next(c for c in r1 if c.org_id == w["far"].id)
    assert far.factors["geography"]["score"] < near.factors["geography"]["score"]
    rel = next(c for c in r1 if c.org_id == w["related"].id)
    assert 0 < rel.factors["category"]["score"] < 1 and 0 < rel.factors["sic"]["score"] < 1
    # country filter
    ie = await find_similar(session, {"org_id": w["seed"].id}, countries=["IE"])
    assert [c.org_id for c in ie] == [w["foreign"].id]
    # limit & min_score
    assert len(await find_similar(session, {"org_id": w["seed"].id}, limit=2)) == 2
    assert all(c.score >= 0.6 for c in await find_similar(session, {"org_id": w["seed"].id}, min_score=0.6))
    with pytest.raises(ValueError):
        await find_similar(session, {})


async def test_category_and_run_seeds_and_accreditations(session):
    w = await _world(session)
    res = await find_similar(session, {"category_id": w["heat"].id}, limit=10)
    ids = {c.org_id for c in res}
    # seed set = every org in the category (and its descendants); candidates are the rest
    assert w["seed"].id not in ids and w["near"].id not in ids and w["far"].id not in ids
    assert w["related"].id in ids or w["other"].id in ids
    run = SourcingRun(name="r")
    session.add(run)
    await session.flush()
    session.add(RunOrganisation(run_id=run.id, organisation_id=w["seed"].id))
    await session.flush()
    byrun = await find_similar(session, {"run_id": run.id}, limit=10)
    assert byrun[0].org_id == w["near"].id
    # accreditations: seed + near share Gas Safe -> accreditation factor activates and favours near
    for o in (w["seed"], w["near"]):
        await record_signal(session, o.id, "accreditation", "Accreditation: Gas Safe", "t", "https://x.example.com/a", None, 0.6,
                            {"accreditation": "Gas Safe"})
    r = await find_similar(session, {"org_id": w["seed"].id}, limit=10)
    top = r[0]
    assert top.org_id == w["near"].id and top.factors["accreditations"]["used"] and top.factors["accreditations"]["score"] == 1.0
    assert (await session.execute(select(BusinessSignal))).scalars().all()


async def test_expand_from_seed_enqueues(session):
    w = await _world(session)
    run = SourcingRun(name="r", status="running")
    session.add(run)
    await session.flush()
    session.add(OrganisationDomain(organisation_id=w["near"].id, domain="near.co.uk", registrable_domain="near.co.uk",
                                   verified_match_status="confirmed", confidence=0.9))
    await session.flush()
    out = await expand_from_seed(session, {"org_id": w["seed"].id}, run_id=run.id, limit=3, min_score=0.3)
    assert out and out[0]["org_id"] == w["near"].id and out[0]["stage"] == "crawl" and "=" in out[0]["explanation"]
    assert any(o["stage"] == "resolve_domain" for o in out)
    jobs = (await session.execute(select(SourcingJob).where(SourcingJob.run_id == run.id))).scalars().all()
    assert len(jobs) == len(out) and all(j.dedupe_key.startswith("similar:") and j.payload["source"] == "lookalike" for j in jobs)
    assert {r.organisation_id for r in (await session.execute(select(RunOrganisation))).scalars()} >= {o["org_id"] for o in out}
    # second call: candidates already in the run are not proposed again
    again = await expand_from_seed(session, {"org_id": w["seed"].id}, run_id=run.id, limit=3, min_score=0.3)
    assert {o["org_id"] for o in again}.isdisjoint({o["org_id"] for o in out})
