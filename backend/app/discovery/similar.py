"""DISC-07: deterministic "similar businesses" (lookalike) discovery - no ML, fully explainable.

``find_similar`` scores candidate organisations against a seed (an organisation, a category or a run) on six factors:

====================  ======  ==========================================================================
factor                weight  how
====================  ======  ==========================================================================
sic                   0.30    soft Jaccard over SIC codes (same code 1.0, 4/3/2-digit prefix 0.75/0.5/0.3)
category              0.20    equal 1.0, ancestor/descendant 1/(1+distance), siblings 0.5
keywords              0.20    cosine of smoothed TF-IDF over names / industry / subcategory text
geography             0.15    haversine when coordinates exist, else postcode area / locality / region
size                  0.05    legal type, SIC-count and employee-count similarity (where present)
accreditations        0.10    Jaccard of ``accreditation`` signals
====================  ======  ==========================================================================

A factor the seed has no data for is dropped and the remaining weights renormalised, so a missing field never counts
against a candidate. Every result carries the per-factor breakdown. Candidates come from bounded, index-friendly SQL
pre-filters (category, postcode range, region, exact SIC via GIN) and never include the seed set itself.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .. import jobqueue as jq
from ..models import (
    BusinessSignal,
    Category,
    Organisation,
    OrganisationDomain,
    OrganisationSource,
    RunOrganisation,
)
from .textutil import clamp, cosine, haversine_km, tfidf_vectors, tokens

WEIGHTS = {"sic": 0.30, "category": 0.20, "keywords": 0.20, "geography": 0.15, "size": 0.05, "accreditations": 0.10}
MAX_SEED = 500
DEFAULT_MAX_CANDIDATES = 2000
_UK_AREA = re.compile(r"^([A-Z]{1,2})\d")
_EMP_KEYS = ("employees", "employee_count", "number_of_employees", "headcount", "staff")


@dataclass
class SimilarCandidate:
    org_id: int
    name: str | None
    score: float
    factors: dict[str, dict[str, Any]] = field(default_factory=dict)
    country_code: str | None = None
    city: str | None = None
    postal_code: str | None = None

    def explain(self) -> str:
        parts = [f"{k}={v['score']:.2f}x{v['weight']:.2f}" for k, v in self.factors.items() if v.get("used")]
        return f"{self.score:.3f} = " + " + ".join(parts)


@dataclass
class _Profile:
    org_ids: set[int]
    sic: set[str]
    category_ids: set[int]
    tokens_: list[str]
    areas: set[str]
    regions: set[str]
    cities: set[str]
    countries: set[str]
    coords: list[tuple[float, float]]
    legal_types: set[str]
    sic_count: float | None
    employees: float | None
    accreditations: set[str]


def sic_pair(a: str, b: str) -> float:
    if a == b:
        return 1.0
    for n, v in ((4, 0.75), (3, 0.5), (2, 0.3)):
        if len(a) >= n and len(b) >= n and a[:n] == b[:n]:
            return v
    return 0.0


def soft_jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard with hierarchical partial credit: inter = mean of the best-match credit from each side."""
    if not a or not b:
        return 0.0
    inter = (sum(max(sic_pair(x, y) for y in b) for x in a) + sum(max(sic_pair(y, x) for x in a) for y in b)) / 2.0
    union = len(a) + len(b) - inter
    return clamp(inter / union) if union > 0 else 0.0


def postal_area(pc: str | None) -> str | None:
    m = _UK_AREA.match((pc or "").upper().replace(" ", ""))
    return m.group(1) if m else None


def _org_text(o: Organisation) -> list[str]:
    return tokens(" ".join(x for x in (o.legal_name, o.trading_name, o.industry, o.subcategory) if x))


def _employees(raw: dict[str, Any] | None) -> float | None:
    for k in _EMP_KEYS:
        v = (raw or {}).get(k)
        try:
            f = float(str(v).replace(",", ""))
            if f > 0:
                return f
        except (TypeError, ValueError):
            continue
    return None


def _accred_name(sig: BusinessSignal) -> str:
    d = sig.detail or {}
    n = d.get("accreditation") or re.sub(r"^accreditation:\s*", "", sig.title or "", flags=re.I)
    return re.sub(r"\W+", " ", str(n).casefold()).strip()


async def _category_tree(session: AsyncSession) -> dict[int, int | None]:
    return {i: p for i, p in (await session.execute(select(Category.id, Category.parent_id))).all()}


def _ancestors(tree: dict[int, int | None], cid: int) -> list[int]:
    out, seen = [], {cid}
    cur = tree.get(cid)
    while cur is not None and cur not in seen and len(out) < 10:
        out.append(cur)
        seen.add(cur)
        cur = tree.get(cur)
    return out


def category_score(tree: dict[int, int | None], seed_cats: set[int], cand: int | None) -> float:
    if cand is None or not seed_cats:
        return 0.0
    best = 0.0
    ca = _ancestors(tree, cand)
    for s in seed_cats:
        if s == cand:
            return 1.0
        sa = _ancestors(tree, s)
        if s in ca:
            best = max(best, 1.0 / (1 + ca.index(s) + 1))
        elif cand in sa:
            best = max(best, 1.0 / (1 + sa.index(cand) + 1))
        elif ca and sa and ca[0] == sa[0]:
            best = max(best, 0.5)
        elif len(ca) > 1 and len(sa) > 1 and ca[1] == sa[1]:
            best = max(best, 0.25)
    return best


async def _seed_orgs(session: AsyncSession, seed: dict[str, Any]) -> tuple[list[Organisation], set[int]]:
    cat_ids: set[int] = set()
    if seed.get("org_id"):
        q = select(Organisation).where(Organisation.id == int(seed["org_id"]))
    elif seed.get("run_id"):
        q = (select(Organisation).join(RunOrganisation, RunOrganisation.organisation_id == Organisation.id)
             .where(RunOrganisation.run_id == int(seed["run_id"]), Organisation.merged_into_id.is_(None)).order_by(Organisation.id).limit(MAX_SEED))
    elif seed.get("category_id"):
        cid = int(seed["category_id"])
        tree = await _category_tree(session)
        cat_ids = {cid} | {c for c in tree if cid in _ancestors(tree, c)}
        q = select(Organisation).where(Organisation.category_id.in_(cat_ids), Organisation.merged_into_id.is_(None)).order_by(Organisation.id).limit(MAX_SEED)
    else:
        raise ValueError("seed needs one of org_id, category_id, run_id")
    orgs = list((await session.execute(q)).scalars())
    if seed.get("category_id"):
        cat_ids = cat_ids or {int(seed["category_id"])}
    return orgs, cat_ids


async def _build_profile(session: AsyncSession, seeds: list[Organisation], seed_cats: set[int]) -> _Profile:
    ids = {o.id for o in seeds}
    srcs = (await session.execute(select(OrganisationSource.organisation_id, OrganisationSource.lat, OrganisationSource.lon, OrganisationSource.raw)
                                  .where(OrganisationSource.organisation_id.in_(ids)))).all() if ids else []
    coords = sorted({(round(lat, 5), round(lon, 5)) for _o, lat, lon, _r in srcs if lat is not None and lon is not None})[:200]
    emps = [e for e in (_employees(r) for _o, _la, _lo, r in srcs) if e]
    sigs = (await session.execute(select(BusinessSignal).where(BusinessSignal.organisation_id.in_(ids), BusinessSignal.signal_type == "accreditation"))).scalars().all() if ids else []
    cats = set(seed_cats) | {o.category_id for o in seeds if o.category_id}
    tok: list[str] = []
    for o in seeds:
        tok += _org_text(o)
    sic_counts = [len(o.sic_codes or []) for o in seeds if o.sic_codes]
    return _Profile(
        org_ids=ids, sic={s for o in seeds for s in (o.sic_codes or [])}, category_ids=cats, tokens_=tok,
        areas={a for a in (postal_area(o.postal_code) for o in seeds) if a}, regions={o.region for o in seeds if o.region},
        cities={o.city.casefold() for o in seeds if o.city}, countries={o.country_code for o in seeds},
        coords=[(a, b) for a, b in coords], legal_types={o.legal_type.casefold() for o in seeds if o.legal_type},
        sic_count=(sum(sic_counts) / len(sic_counts)) if sic_counts else None, employees=(sum(emps) / len(emps)) if emps else None,
        accreditations={a for a in (_accred_name(s) for s in sigs) if a})


async def _candidate_ids(session: AsyncSession, prof: _Profile, countries: list[str], tree: dict[int, int | None], max_candidates: int) -> list[int]:
    """Union of bounded, index-friendly pre-filters (each capped), deterministic order."""
    per = max(50, max_candidates // 4)
    base = "o.merged_into_id IS NULL AND o.country_code = ANY(:cc) AND NOT (o.id = ANY(:seed))"
    params: dict[str, Any] = {"cc": countries, "seed": sorted(prof.org_ids), "n": per}
    ids: set[int] = set()
    fam: set[int] = set(prof.category_ids)
    for c in list(prof.category_ids):
        fam |= set(_ancestors(tree, c))
        parent = tree.get(c)
        fam |= {k for k, p in tree.items() if p == c or (parent is not None and p == parent)}
    if fam:
        r = await session.execute(text(f"SELECT o.id FROM organisations o WHERE {base} AND o.category_id = ANY(:cats) ORDER BY o.id LIMIT :n"),
                                  {**params, "cats": sorted(fam)})
        ids |= {x[0] for x in r}
    if prof.sic:
        r = await session.execute(text(f"SELECT o.id FROM organisations o WHERE {base} AND jsonb_exists_any(o.sic_codes, CAST(:sic AS text[])) ORDER BY o.id LIMIT :n"),
                                  {**params, "sic": sorted(prof.sic)})
        ids |= {x[0] for x in r}
    for area in sorted(prof.areas)[:20]:
        nxt = area[:-1] + chr(ord(area[-1]) + 1)
        r = await session.execute(text(f"SELECT o.id FROM organisations o WHERE {base} AND o.postal_code >= :lo AND o.postal_code < :hi ORDER BY o.id LIMIT :n"),
                                  {**params, "lo": area, "hi": nxt, "n": max(25, per // max(1, len(prof.areas)))})
        ids |= {x[0] for x in r}
    for region in sorted(prof.regions)[:10]:
        r = await session.execute(text(f"SELECT o.id FROM organisations o WHERE {base} AND o.region = :rg ORDER BY o.id LIMIT :n"),
                                  {**params, "rg": region, "n": max(25, per // max(1, len(prof.regions)))})
        ids |= {x[0] for x in r}
    return sorted(ids)[:max_candidates]


async def find_similar(
    session: AsyncSession, seed: dict[str, Any], *, limit: int = 50, countries: list[str] | None = None,
    min_score: float = 0.0, exclude_run_id: int | None = None, max_candidates: int = DEFAULT_MAX_CANDIDATES,
) -> list[SimilarCandidate]:
    """Rank organisations similar to ``seed`` = ``{org_id | category_id | run_id}``; see module docstring for the factors."""
    seeds, seed_cats = await _seed_orgs(session, seed)
    if not seeds and not seed_cats:
        return []
    prof = await _build_profile(session, seeds, seed_cats)
    tree = await _category_tree(session)
    cc = [c.upper() for c in countries] if countries else sorted(prof.countries) or ["GB"]
    cand_ids = await _candidate_ids(session, prof, cc, tree, max_candidates)
    if exclude_run_id:
        in_run = set((await session.execute(select(RunOrganisation.organisation_id).where(RunOrganisation.run_id == exclude_run_id))).scalars())
        cand_ids = [i for i in cand_ids if i not in in_run]
    if not cand_ids:
        return []
    cands = {o.id: o for o in (await session.execute(select(Organisation).where(Organisation.id.in_(cand_ids)))).scalars()}
    src: dict[int, list[tuple[float | None, float | None, dict]]] = {}
    for oid, lat, lon, raw in (await session.execute(select(OrganisationSource.organisation_id, OrganisationSource.lat, OrganisationSource.lon,
                                                            OrganisationSource.raw).where(OrganisationSource.organisation_id.in_(cand_ids)))).all():
        src.setdefault(oid, []).append((lat, lon, raw))
    acc: dict[int, set[str]] = {}
    if prof.accreditations:
        for s in (await session.execute(select(BusinessSignal).where(BusinessSignal.organisation_id.in_(cand_ids), BusinessSignal.signal_type == "accreditation"))).scalars():
            acc.setdefault(s.organisation_id, set()).add(_accred_name(s))

    docs: dict[Any, list[str]] = {"__seed__": prof.tokens_}
    for i in sorted(cands):
        docs[i] = _org_text(cands[i])
    vecs = tfidf_vectors(docs) if prof.tokens_ else {}
    seed_vec = vecs.get("__seed__", {})

    # which factors can be evaluated for this seed
    avail = {
        "sic": bool(prof.sic), "category": bool(prof.category_ids), "keywords": bool(seed_vec),
        "geography": bool(prof.coords or prof.areas or prof.cities or prof.regions or prof.countries),
        "size": bool(prof.legal_types or prof.sic_count or prof.employees), "accreditations": bool(prof.accreditations),
    }
    total_w = sum(w for k, w in WEIGHTS.items() if avail[k]) or 1.0
    out: list[SimilarCandidate] = []
    for oid in sorted(cands):
        o = cands[oid]
        raw_scores: dict[str, tuple[float, str]] = {}
        # sic
        if avail["sic"]:
            sc = soft_jaccard(prof.sic, set(o.sic_codes or []))
            raw_scores["sic"] = (sc, f"{len(set(o.sic_codes or []) & prof.sic)} shared of {len(o.sic_codes or [])} codes")
        if avail["category"]:
            sc = category_score(tree, prof.category_ids, o.category_id)
            raw_scores["category"] = (sc, "same category" if sc == 1.0 else ("related category" if sc else "unrelated category"))
        if avail["keywords"]:
            cv = vecs.get(oid, {})
            sc = cosine(seed_vec, cv)
            shared = sorted(set(seed_vec) & set(cv), key=lambda t: (-(seed_vec[t] * cv[t]), t))[:5]
            raw_scores["keywords"] = (sc, "shared terms: " + ", ".join(shared) if shared else "no shared terms")
        if avail["geography"]:
            raw_scores["geography"] = _geo(prof, o, src.get(oid, []))
        if avail["size"]:
            raw_scores["size"] = _size(prof, o, src.get(oid, []))
        if avail["accreditations"]:
            ca = acc.get(oid, set())
            sc = len(ca & prof.accreditations) / len(ca | prof.accreditations) if (ca | prof.accreditations) else 0.0
            raw_scores["accreditations"] = (sc, "shared: " + ", ".join(sorted(ca & prof.accreditations)) if ca & prof.accreditations else "none shared")
        factors: dict[str, dict[str, Any]] = {}
        total = 0.0
        for k, w in WEIGHTS.items():
            if not avail[k]:
                factors[k] = {"score": 0.0, "weight": 0.0, "used": False, "detail": "seed has no data for this factor"}
                continue
            sc, detail = raw_scores[k]
            nw = w / total_w
            total += sc * nw
            factors[k] = {"score": round(sc, 4), "weight": round(nw, 4), "used": True, "detail": detail}
        if total >= min_score:
            out.append(SimilarCandidate(oid, o.legal_name or o.trading_name, round(total, 5), factors, o.country_code, o.city, o.postal_code))
    out.sort(key=lambda c: (-c.score, c.org_id))
    return out[:max(1, min(limit, 1000))]


def _geo(prof: _Profile, o: Organisation, srcs: list) -> tuple[float, str]:
    pts = [(la, lo) for la, lo, _r in srcs if la is not None and lo is not None]
    if pts and prof.coords:
        d = min(haversine_km(a, b, c, e) for a, b in prof.coords for c, e in pts)
        sc = 1.0 if d <= 5 else max(0.0, 1.0 - (d - 5) / 95.0)
        return sc, f"{d:.1f} km from nearest seed"
    area = postal_area(o.postal_code)
    if area and area in prof.areas:
        return 0.8, f"same postcode area {area}"
    if o.city and o.city.casefold() in prof.cities:
        return 0.7, "same city"
    if o.region and o.region in prof.regions:
        return 0.5, "same region"
    if o.country_code in prof.countries:
        return 0.1, "same country"
    return 0.0, "different country"


def _size(prof: _Profile, o: Organisation, srcs: list) -> tuple[float, str]:
    parts: list[float] = []
    notes: list[str] = []
    if prof.legal_types and o.legal_type:
        same = o.legal_type.casefold() in prof.legal_types
        parts.append(1.0 if same else 0.0)
        notes.append("same legal type" if same else "different legal type")
    if prof.sic_count and o.sic_codes:
        r = min(prof.sic_count, len(o.sic_codes)) / max(prof.sic_count, len(o.sic_codes))
        parts.append(r)
        notes.append(f"sic count ratio {r:.2f}")
    if prof.employees:
        e = next((x for x in (_employees(r) for _la, _lo, r in srcs) if x), None)
        if e:
            r = math.exp(-abs(math.log(e / prof.employees)))
            parts.append(r)
            notes.append(f"employees ratio {r:.2f}")
    return (sum(parts) / len(parts), "; ".join(notes)) if parts else (0.0, "no size indicators")


async def expand_from_seed(
    session: AsyncSession, seed: dict[str, Any], *, run_id: int, limit: int = 20, countries: list[str] | None = None,
    min_score: float = 0.35, priority: int = 70,
) -> list[dict[str, Any]]:
    """Queue discovery/resolution work for the best lookalikes. Candidates already in the run are skipped; each gets a
    ``resolve_domain`` job (or ``crawl`` when it already has a confirmed domain) and joins the run as ``discovered``."""
    found = await find_similar(session, seed, limit=limit, countries=countries, min_score=min_score, exclude_run_id=run_id)
    if not found:
        return []
    confirmed = set((await session.execute(select(OrganisationDomain.organisation_id).where(
        OrganisationDomain.organisation_id.in_([c.org_id for c in found]),
        OrganisationDomain.verified_match_status.in_(("confirmed", "high")), OrganisationDomain.parked.is_(False)))).scalars())
    out = []
    for c in found:
        stage = "crawl" if c.org_id in confirmed else "resolve_domain"
        payload = {"organisation_id": c.org_id, "source": "lookalike", "similarity": c.score, "seed": {k: v for k, v in seed.items() if v}}
        jid = await jq.enqueue(session, run_id=run_id, stage=stage, dedupe_key=f"similar:{c.org_id}", payload=payload, priority=priority)
        await session.execute(pg_insert(RunOrganisation).values(run_id=run_id, organisation_id=c.org_id, stage="discovered").on_conflict_do_nothing())
        out.append({"org_id": c.org_id, "score": c.score, "stage": stage, "job_id": jid, "explanation": c.explain()})
    return out

