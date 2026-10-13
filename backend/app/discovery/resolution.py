"""DISC-12: cross-source company resolution.

Evidence is ranked, strongest first:

1. exact identifiers (company number per country, LEI, ABN, charity number)  -> ``matched``
2. official-domain equality (confirmed/high domain) + a compatible name       -> ``matched``
3. normalised legal-name equality + same postcode / locality                  -> ``matched`` (high confidence)
4. similar name only (Jaro-Winkler / trigram, pure Python)                    -> NEVER merged: ``review``
   (an ``OrganisationRecordLink(status='review')`` row the Data Quality workbench lists)

Different company numbers (or any other conflicting identifier) are a hard ``rejected``: the two records are
proven to be different legal entities and are never merged, whatever the names say.

Legal entity, trading brand, branch and domain stay separate linked records: records flagged
``raw["entity_role"] in {"brand", "trading", "branch"}`` always become their own organisation, joined to the
parent by a ``BusinessRelationship`` (``brand_of`` / ``branch_of``); domains live in ``organisation_domains``.

Nothing here confirms a domain. All writes are idempotent upserts guarded by advisory locks; the caller owns
the transaction.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    AuditEvent,
    BusinessRelationship,
    BusinessSignal,
    Contact,
    ContactEvidence,
    EmailAddress,
    Organisation,
    OrganisationCompleteness,
    OrganisationDomain,
    OrganisationRecordLink,
    RunOrganisation,
    WebsiteSnapshot,
)
from ..pipeline.identity import is_intermediary_domain
from ..sources.base import BusinessRecord
from ..sources.geo_filters import normalise_postcode
from ..sources.persist import (
    _norm_company_number,
    _upsert_candidate_domain,
    _upsert_source,
    normalise_name,
    upsert_business,
)
from ..sources.urls import normalise_website
from .textutil import jaro_winkler, name_similarity

DECISIONS = ("matched", "created", "review", "rejected")
CONFIDENT_DOMAIN = ("confirmed", "high")
SEPARATE_ROLES = {"branch": "branch_of", "brand": "brand_of", "trading": "brand_of"}
# thresholds (documented, deterministic)
SIMILAR_JW = 0.90
SIMILAR_BLEND = 0.62
MAX_REVIEW_LINKS = 3


@dataclass
class Resolution:
    org_id: int | None
    decision: str  # matched | created | review | rejected
    basis: str
    confidence: float
    candidates: list[dict[str, Any]] = field(default_factory=list)
    relationships: list[tuple[str, int]] = field(default_factory=list)  # (relation, other_org_id) new org is FROM
    detail: dict[str, Any] = field(default_factory=dict)


class MergeConflict(ValueError):
    """Two organisations carry different registration numbers; refuse to merge without an explicit override."""


# --------------------------------------------------------------------------- identifiers
_LEI_RE = re.compile(r"^[A-Z0-9]{20}$")


def extract_identifiers(rec: BusinessRecord) -> list[tuple[str, str]]:
    """Verified-identifier candidates carried by a record, as ``[(type, value)]`` (country-scoped where needed)."""
    cc = (rec.country_code or "").upper()
    out: list[tuple[str, str]] = []
    cn = _norm_company_number(rec.company_number)
    if cn and cc:
        out.append(("company_number", f"{cc}:{cn}"))
    raw = rec.raw or {}

    def pick(*keys: str) -> str | None:
        for k in keys:
            v = raw.get(k)
            if v not in (None, ""):
                return str(v)
        return None

    lei = pick("lei", "LEI")
    if lei:
        lei = re.sub(r"\s+", "", lei).upper()
        if _LEI_RE.match(lei):
            out.append(("lei", lei))
    abn = pick("abn", "ABN")
    if abn:
        d = re.sub(r"\D", "", abn)
        if len(d) == 11:
            out.append(("abn", d))
    ch = pick("charity_number", "charity_no", "charity_id")
    if ch and cc:
        chv = re.sub(r"[\s-]+", "", ch).upper()
        out.append(("charity_no", f"{cc}:{chv}"))
    return out


def _surrogate_ref(rec: BusinessRecord) -> str:
    """Stable stand-in for an empty ``source_ref`` so unrelated anonymous records never share one link key."""
    import hashlib

    from ..sources.persist import canonical_identity

    try:
        return ("anon:" + canonical_identity(rec))[:300]
    except ValueError:
        return "anon:" + hashlib.sha256(repr((rec.legal_name, rec.trading_name, rec.website, rec.address)).encode()).hexdigest()[:40]


def _role_of(rec: BusinessRecord) -> str:
    r = str((rec.raw or {}).get("entity_role") or "legal").lower()
    return r if r in ("legal", "branch", "brand", "trading") else "legal"


def _rec_names(rec: BusinessRecord) -> list[str]:
    return [n for n in dict.fromkeys(normalise_name(x) for x in (rec.legal_name, rec.trading_name)) if n]


def _org_names(o: Organisation) -> list[str]:
    return [n for n in dict.fromkeys(normalise_name(x) for x in (o.legal_name, o.trading_name)) if n]


def _name_compatible(a: list[str], b: list[str]) -> bool:
    for x in a:
        for y in b:
            if x == y or name_similarity(x, y) >= 0.75:
                return True
            tx, ty = set(x.split()), set(y.split())
            small, big = (tx, ty) if len(tx) <= len(ty) else (ty, tx)
            if small and small <= big and any(len(t) >= 3 for t in small):
                return True
    return False


def _best_similarity(a: list[str], b: list[str]) -> tuple[float, bool, bool]:
    """(best blended score, any exact equality, is_similar_candidate)."""
    best, exact, similar = 0.0, False, False
    for x in a:
        for y in b:
            if x == y:
                exact = True
                best = 1.0
                continue
            sc = name_similarity(x, y)
            best = max(best, sc)
            if jaro_winkler(x, y) >= SIMILAR_JW or sc >= SIMILAR_BLEND:
                similar = True
    return best, exact, similar or exact


# --------------------------------------------------------------------------- merge chains
async def follow_merge(session: AsyncSession, org_id: int) -> int:
    """Final surviving organisation id after following ``merged_into_id`` (cycle/ depth safe)."""
    seen = {org_id}
    cur = org_id
    for _ in range(25):
        nxt = (await session.execute(select(Organisation.merged_into_id).where(Organisation.id == cur))).scalar_one_or_none()
        if nxt is None or nxt in seen:
            return cur
        seen.add(nxt)
        cur = nxt
    return cur


async def _canonical_ids(session: AsyncSession, ids: set[int]) -> set[int]:
    out: set[int] = set()
    for i in sorted(ids):
        out.add(await follow_merge(session, i))
    return out


async def _lock(session: AsyncSession, key: str) -> None:
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": key})


# --------------------------------------------------------------------------- lookups
async def _orgs_by_identifier(session: AsyncSession, itype: str, ivalue: str) -> set[int]:
    ids: set[int] = set((await session.execute(
        select(OrganisationRecordLink.organisation_id).where(
            OrganisationRecordLink.identifier_type == itype, OrganisationRecordLink.identifier_value == ivalue,
            OrganisationRecordLink.status == "linked"))).scalars())
    if itype == "company_number":
        cc, _, num = ivalue.partition(":")
        ids |= set((await session.execute(select(Organisation.id).where(
            Organisation.country_code == cc, Organisation.company_number == num))).scalars())
    return await _canonical_ids(session, ids)


async def _is_rejected(session: AsyncSession, org_id: int, adapter_id: str, source_ref: str) -> bool:
    return (await session.execute(select(OrganisationRecordLink.id).where(
        OrganisationRecordLink.organisation_id == org_id, OrganisationRecordLink.source_adapter == adapter_id,
        OrganisationRecordLink.source_ref == source_ref, OrganisationRecordLink.status == "rejected"))).first() is not None


async def _identifier_conflict(session: AsyncSession, org: Organisation, idents: list[tuple[str, str]]) -> dict | None:
    """First hard conflict between a record's identifiers and what ``org`` already carries."""
    for itype, ivalue in idents:
        if itype == "company_number":
            cc, _, num = ivalue.partition(":")
            if org.company_number and org.country_code == cc and org.company_number != num:
                return {"type": itype, "record": ivalue, "organisation": f"{org.country_code}:{org.company_number}"}
        other = (await session.execute(select(OrganisationRecordLink.identifier_value).where(
            OrganisationRecordLink.organisation_id == org.id, OrganisationRecordLink.identifier_type == itype,
            OrganisationRecordLink.status == "linked", OrganisationRecordLink.identifier_value != ivalue).limit(1))).scalar_one_or_none()
        if other:
            return {"type": itype, "record": ivalue, "organisation": other}
    return None


def _cn_conflict(rec_cn: str | None, org: Organisation, cc: str) -> bool:
    return bool(rec_cn and org.company_number and org.country_code == cc and org.company_number != rec_cn)


async def _candidates_by_name(session: AsyncSession, rec: BusinessRecord, names: list[str], limit: int = 300) -> list[Organisation]:
    cc = (rec.country_code or "").upper()
    conds = []
    for n in names:
        first = n.split()[0]
        for pre in (first, f"the {first}"):
            conds.append(func.lower(Organisation.legal_name).like(pre + "%"))
            conds.append(func.lower(Organisation.trading_name).like(pre + "%"))
    pc = normalise_postcode(rec.postal_code, cc) if rec.postal_code else None
    if pc:
        conds.append(Organisation.postal_code == pc)
    if not conds:
        return []
    q = select(Organisation).where(Organisation.merged_into_id.is_(None), Organisation.country_code == cc, or_(*conds))
    cn = _norm_company_number(rec.company_number)
    if cn:
        # organisations that carry a DIFFERENT registration number are proven different legal entities and can never be merged
        # with this record, so only those with the very same name (kept for the audit trail) are worth loading: without this a
        # bulk register load would score every similarly named company for every row
        same = [func.lower(col) == v.lower() for col in (Organisation.legal_name, Organisation.trading_name)
                for v in (rec.legal_name, rec.trading_name) if v]
        q = q.where(or_(Organisation.company_number.is_(None), Organisation.company_number == cn, *same))
    q = q.order_by(Organisation.id).limit(limit)
    return list((await session.execute(q)).scalars())


async def _find_parent(session: AsyncSession, rec: BusinessRecord) -> int | None:
    raw = rec.raw or {}
    if raw.get("parent_org_id"):
        try:
            return await follow_merge(session, int(raw["parent_org_id"]))
        except (TypeError, ValueError):
            return None
    cc = (rec.country_code or "").upper()
    ident = raw.get("parent_identifier")
    if isinstance(ident, dict) and ident.get("type") and ident.get("value"):
        ids = await _orgs_by_identifier(session, str(ident["type"]), str(ident["value"]))
    elif raw.get("parent_company_number"):
        cn = _norm_company_number(str(raw["parent_company_number"]))
        ids = await _orgs_by_identifier(session, "company_number", f"{cc}:{cn}") if cn else set()
    else:
        return None
    return min(ids) if len(ids) == 1 else None


# --------------------------------------------------------------------------- links
async def link_record(
    session: AsyncSession, org_id: int, *, adapter_id: str, source_ref: str,
    identifiers: list[tuple[str, str]] | None = None, basis: str = "source_record", confidence: float = 1.0,
    status: str = "linked", candidate_org_id: int | None = None, detail: dict[str, Any] | None = None,
) -> int:
    """Upsert the provenance link of one source record to ``org_id`` (idempotent). Returns the main link id.

    The first identifier is stored on the main row; any further identifiers get companion rows
    (``source_ref#<type>``) because (organisation, adapter, source_ref) is unique.
    """
    idents = list(identifiers or [])
    first = idents[0] if idents else (None, None)
    main = await _upsert_link(session, org_id, adapter_id, source_ref, first[0], first[1], basis, confidence, status,
                              candidate_org_id, detail or {})
    if status == "linked":
        for itype, ivalue in idents[1:]:
            await _upsert_link(session, org_id, adapter_id, f"{source_ref[:280]}#{itype}", itype, ivalue, basis, confidence,
                               status, None, {"companion_of": source_ref})
    return main


async def _upsert_link(session, org_id, adapter_id, source_ref, itype, ivalue, basis, confidence, status, cand, detail) -> int:
    stmt = pg_insert(OrganisationRecordLink).values(
        organisation_id=org_id, source_adapter=adapter_id[:80], source_ref=(source_ref or "")[:300], identifier_type=itype,
        identifier_value=ivalue[:120] if ivalue else None, match_basis=basis[:40], match_confidence=round(float(confidence), 4),
        status=status, candidate_org_id=cand, detail=detail)
    ex = stmt.excluded
    # a manual 'rejected' decision is never silently overwritten by an automatic one
    stmt = stmt.on_conflict_do_update(
        constraint="uq_orglink",
        set_={
            "identifier_type": func.coalesce(ex.identifier_type, OrganisationRecordLink.identifier_type),
            "identifier_value": func.coalesce(ex.identifier_value, OrganisationRecordLink.identifier_value),
            "match_basis": ex.match_basis, "match_confidence": ex.match_confidence,
            "candidate_org_id": func.coalesce(ex.candidate_org_id, OrganisationRecordLink.candidate_org_id),
            "detail": ex.detail, "observed_at": func.now(),
            "status": text("CASE WHEN organisation_record_links.status = 'rejected' AND organisation_record_links.match_basis = 'manual' "
                           "THEN 'rejected' ELSE excluded.status END"),
        },
    ).returning(OrganisationRecordLink.id)
    return (await session.execute(stmt)).scalar_one()


# --------------------------------------------------------------------------- resolution
async def resolve_record(
    session: AsyncSession, rec: BusinessRecord, *, adapter_id: str, source_ref: str, persist: bool = True
) -> Resolution:
    """Decide whether ``rec`` is an already-known company. ``persist`` writes the review/rejected/linked link rows."""
    cc = (rec.country_code or "").upper()
    source_ref = source_ref or _surrogate_ref(rec)
    idents = extract_identifiers(rec)
    names = _rec_names(rec)
    role = _role_of(rec)
    rec_cn = _norm_company_number(rec.company_number)
    await _lock(session, "resolve:" + (idents[0][1] if idents else f"{cc}:{names[0] if names else source_ref}"))

    # 0. this exact source record was already resolved -> stable answer (idempotency)
    prev = (await session.execute(select(OrganisationRecordLink.organisation_id, OrganisationRecordLink.match_confidence).where(
        OrganisationRecordLink.source_adapter == adapter_id, OrganisationRecordLink.source_ref == source_ref,
        OrganisationRecordLink.status == "linked").order_by(OrganisationRecordLink.id).limit(1))).first()
    if prev is not None:
        return Resolution(await follow_merge(session, prev[0]), "matched", "previous_link", float(prev[1] or 1.0))

    # branch / brand / trading records are always their own organisation (never collapsed into the parent)
    if role != "legal":
        parent = await _find_parent(session, rec)
        rels = [(SEPARATE_ROLES[role], parent)] if parent else []
        return Resolution(None, "created", "role_separate", 1.0, relationships=rels,
                          detail={"role": role, "parent_resolved": parent is not None})

    # 1. exact identifiers ------------------------------------------------------------------
    found: dict[int, list[tuple[str, str]]] = defaultdict(list)
    for itype, ivalue in idents:
        for oid in await _orgs_by_identifier(session, itype, ivalue):
            found[oid].append((itype, ivalue))
    if found:
        if len(found) > 1:
            cands = [{"org_id": o, "basis": "identifier", "identifiers": v} for o, v in sorted(found.items())]
            res = Resolution(None, "review", "identifier_ambiguous", 0.5, candidates=cands,
                             detail={"note": "identifiers point at different organisations"})
            if persist:
                for c in cands[:MAX_REVIEW_LINKS]:
                    if not await _is_rejected(session, c["org_id"], adapter_id, source_ref):
                        await link_record(session, c["org_id"], adapter_id=adapter_id, source_ref=source_ref, identifiers=idents[:1],
                                          basis="identifier_ambiguous", confidence=0.5, status="review", detail=res.detail)
            return res
        oid, matched_on = next(iter(found.items()))
        org = await session.get(Organisation, oid)
        assert org is not None
        conflict = await _identifier_conflict(session, org, idents)
        if conflict:
            return await _reject(session, rec, adapter_id, source_ref, [oid], conflict, persist)
        if not await _is_rejected(session, oid, adapter_id, source_ref):
            res = Resolution(oid, "matched", "identifier:" + matched_on[0][0], 0.99,
                             candidates=[{"org_id": oid, "basis": "identifier", "identifiers": matched_on}])
            if persist:
                await link_record(session, oid, adapter_id=adapter_id, source_ref=source_ref, identifiers=idents,
                                  basis="registration_number" if matched_on[0][0] == "company_number" else matched_on[0][0],
                                  confidence=0.99)
            return res

    conflicts: list[dict[str, Any]] = []
    conflict_orgs: list[int] = []

    # 2. official domain equality + compatible name --------------------------------------------
    site = normalise_website(rec.website) if rec.website else None
    if site is not None and not is_intermediary_domain(site.host):
        dom_orgs = set((await session.execute(select(OrganisationDomain.organisation_id).where(
            OrganisationDomain.registrable_domain == site.registrable,
            OrganisationDomain.verified_match_status.in_(CONFIDENT_DOMAIN), OrganisationDomain.parked.is_(False)))).scalars())
        scored: list[tuple[float, Organisation]] = []
        for oid in sorted(await _canonical_ids(session, dom_orgs)):
            org = await session.get(Organisation, oid)
            if org is None or not _name_compatible(names, _org_names(org)):
                continue
            if _cn_conflict(rec_cn, org, cc):
                conflicts.append({"type": "company_number", "record": rec_cn, "organisation": org.company_number, "org_id": oid})
                conflict_orgs.append(oid)
                continue
            if await _is_rejected(session, oid, adapter_id, source_ref):
                continue
            scored.append((max((name_similarity(a, b) for a in names for b in _org_names(org)), default=0.0), org))
        if scored:
            scored.sort(key=lambda t: (-t[0], t[1].id))
            if len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.1:
                org = scored[0][1]
                res = Resolution(org.id, "matched", "official_domain+name", round(min(0.97, 0.85 + 0.1 * scored[0][0]), 3),
                                 candidates=[{"org_id": org.id, "basis": "domain", "domain": site.registrable}])
                if persist:
                    await link_record(session, org.id, adapter_id=adapter_id, source_ref=source_ref, identifiers=idents[:1],
                                      basis="domain", confidence=res.confidence)
                return res
            cands = [{"org_id": o.id, "basis": "domain", "score": s} for s, o in scored[:MAX_REVIEW_LINKS]]
            return await _review(session, adapter_id, source_ref, idents, cands, "domain_ambiguous", 0.6, persist)

    # 3 + 4. name based ----------------------------------------------------------------------
    matched: list[tuple[float, str, Organisation]] = []
    review: list[tuple[float, str, Organisation]] = []
    pc = normalise_postcode(rec.postal_code, cc) if rec.postal_code else None
    city = normalise_name(rec.city)
    for org in await _candidates_by_name(session, rec, names) if names else []:
        if org.id in conflict_orgs:
            continue
        if _cn_conflict(rec_cn, org, cc):
            # proven different legal entity: only an EXACT name clash is worth an audit trail; skipping the fuzzy scoring of
            # every similarly named company keeps bulk loads linear (a similar name never changed the outcome here)
            if set(names) & set(_org_names(org)):
                conflicts.append({"type": "company_number", "record": rec_cn, "organisation": org.company_number, "org_id": org.id,
                                  "name_score": 1.0})
                conflict_orgs.append(org.id)
            continue
        best, exact, similar = _best_similarity(names, _org_names(org))
        if not similar:
            continue
        if _cn_conflict(rec_cn, org, cc):
            conflicts.append({"type": "company_number", "record": rec_cn, "organisation": org.company_number, "org_id": org.id,
                              "name_score": best})
            conflict_orgs.append(org.id)
            continue
        if await _is_rejected(session, org.id, adapter_id, source_ref):
            continue
        opc = org.postal_code
        ocity = normalise_name(org.city)
        both_pc = bool(pc and opc)
        if exact and both_pc and pc == opc:
            matched.append((0.92, "name+postcode", org))
        elif exact and not both_pc and city and ocity and city == ocity:
            matched.append((0.8, "name+locality", org))
        elif exact:
            review.append((0.55, "name_only_location_differs", org))
        else:
            review.append((round(min(0.85, best), 3), "similar_name", org))
    if matched:
        matched.sort(key=lambda t: (-t[0], t[2].id))
        distinct = {m[2].id for m in matched if m[0] == matched[0][0]}
        if len(distinct) == 1:
            conf, basis, org = matched[0]
            res = Resolution(org.id, "matched", basis, conf, candidates=[{"org_id": org.id, "basis": basis, "score": conf}])
            if persist:
                await link_record(session, org.id, adapter_id=adapter_id, source_ref=source_ref, identifiers=idents[:1],
                                  basis="name_geo", confidence=conf)
            return res
        review = [(0.6, "name_ambiguous", m[2]) for m in matched] + review
    if review:
        review.sort(key=lambda t: (-t[0], t[2].id))
        cands = [{"org_id": o.id, "basis": b, "score": s} for s, b, o in review[:MAX_REVIEW_LINKS]]
        return await _review(session, adapter_id, source_ref, idents, cands, review[0][1], review[0][0], persist)
    if conflicts:
        return await _reject(session, rec, adapter_id, source_ref, conflict_orgs, conflicts[0], persist)
    return Resolution(None, "created", "no_candidate", 1.0)


async def _review(session, adapter_id, source_ref, idents, cands, basis, conf, persist) -> Resolution:
    res = Resolution(None, "review", basis, conf, candidates=cands, detail={"never_auto_merged": True})
    if persist:
        for c in cands:
            if await _is_rejected(session, c["org_id"], adapter_id, source_ref):
                continue
            await link_record(session, c["org_id"], adapter_id=adapter_id, source_ref=source_ref, identifiers=idents[:1],
                              basis="name_similar" if "name" in c["basis"] or "similar" in c["basis"] else c["basis"],
                              confidence=c.get("score", conf), status="review", detail={"candidate_basis": c["basis"]})
    return res


async def _reject(session, rec, adapter_id, source_ref, org_ids, conflict, persist) -> Resolution:
    # audit links are bounded like review links: a common name must not write one 'rejected' row per similarly named company
    # (that made bulk loads quadratic); the decision itself and the first conflicting organisations stay recorded.
    org_ids = list(dict.fromkeys(org_ids))[:MAX_REVIEW_LINKS]
    res = Resolution(None, "rejected", "conflicting_identifier", 1.0,
                     candidates=[{"org_id": o, "basis": "conflict"} for o in dict.fromkeys(org_ids)], detail={"conflict": conflict})
    if persist:
        for o in dict.fromkeys(org_ids):
            await link_record(session, o, adapter_id=adapter_id, source_ref=source_ref, identifiers=extract_identifiers(rec)[:1],
                              basis="conflicting_identifier", confidence=1.0, status="rejected", detail=res.detail)
    return res


# --------------------------------------------------------------------------- upsert hook
async def _upsert_relationship(session: AsyncSession, from_id: int, to_id: int, relation: str, adapter: str,
                               source_url: str | None, *, depth: int = 0, confidence: float = 0.8) -> None:
    if from_id == to_id:
        return
    stmt = pg_insert(BusinessRelationship).values(
        from_org_id=from_id, to_org_id=to_id, relation=relation, source_adapter=adapter[:80], source_url=source_url,
        depth=depth, confidence=confidence)
    await session.execute(stmt.on_conflict_do_update(
        constraint="uq_bizrel", set_={"source_url": func.coalesce(stmt.excluded.source_url, BusinessRelationship.source_url),
                                      "confidence": func.greatest(BusinessRelationship.confidence, stmt.excluded.confidence),
                                      "observed_at": func.now()}))


_FILL = ("legal_name", "trading_name", "legal_type", "status", "region", "city", "postal_code", "address", "phone")


async def _enrich_matched(session: AsyncSession, org_id: int, rec: BusinessRecord, run_id: int | None, category_id: int | None) -> None:
    """Attach a matched record's provenance/domain to an existing organisation (fill-null only; never overwrite)."""
    org = await session.get(Organisation, org_id)
    assert org is not None
    cc = (rec.country_code or "").upper()
    inc = {
        "legal_name": rec.legal_name, "trading_name": rec.trading_name, "legal_type": rec.legal_type, "status": rec.status,
        "region": rec.region, "city": rec.city, "address": rec.address, "phone": rec.phone,
        "postal_code": normalise_postcode(rec.postal_code, cc) if rec.postal_code else None,
    }
    for k in _FILL:
        v = inc.get(k)
        if v and not getattr(org, k):
            setattr(org, k, str(v).strip()[:400] if k != "address" else str(v).strip())
    cn = _norm_company_number(rec.company_number)
    if cn and not org.company_number and org.country_code == cc:
        org.company_number = cn
    if category_id and not org.category_id:
        org.category_id = category_id
    sic = sorted({s for s in (re.sub(r"\D", "", x) for x in rec.sic_codes) if s} | set(org.sic_codes or []))
    if sic != list(org.sic_codes or []):
        org.sic_codes = sic
    org.updated_at = datetime.now(UTC)
    now = datetime.now(UTC)
    site = normalise_website(rec.website) if rec.website else None
    await _upsert_source(session, org_id, rec, run_id, site.url if site else None, now)
    if run_id is not None:
        await session.execute(pg_insert(RunOrganisation).values(run_id=run_id, organisation_id=org_id, stage="discovered")
                              .on_conflict_do_nothing())
    if site is not None:
        await _upsert_candidate_domain(session, org_id, rec, site, now)
    await session.flush()


async def resolving_upsert_ex(
    session: AsyncSession, rec: BusinessRecord, adapter_id: str, run_id: int | None = None, category_id: int | None = None
) -> tuple[int, bool, Resolution]:
    """Resolution first, then :func:`app.sources.persist.upsert_business` (which is not modified).

    * ``matched``  -> the record is attached to the existing organisation (no duplicate row);
    * ``created`` / ``review`` / ``rejected`` -> a separate organisation is upserted; for ``review`` the
      workbench link points from the candidate to the new organisation; ``rejected`` keeps an audit link.
    """
    adapter_id = adapter_id or rec.adapter_id or "unknown"
    rec = replace(rec, adapter_id=rec.adapter_id or adapter_id)
    source_ref = rec.source_ref or _surrogate_ref(rec)
    res = await resolve_record(session, rec, adapter_id=adapter_id, source_ref=source_ref)
    idents = extract_identifiers(rec)
    if res.decision == "matched" and res.org_id is not None:
        await _enrich_matched(session, res.org_id, rec, run_id, category_id)
        return res.org_id, False, res
    role = _role_of(rec)
    rec2 = rec
    if role != "legal":
        raw = dict(rec.raw or {})
        if rec.company_number:
            raw.setdefault("shared_company_number", rec.company_number)
        rec2 = replace(rec, company_number=None, raw=raw)
        idents = []
    org_id, created = await upsert_business(session, rec2, run_id=run_id, category_id=category_id)
    org_id = await follow_merge(session, org_id)
    await link_record(session, org_id, adapter_id=adapter_id, source_ref=source_ref, identifiers=idents,
                      basis="registration_number" if idents else ("role_separate" if role != "legal" else "name_geo"),
                      confidence=1.0)
    if res.decision in ("review", "rejected"):
        await session.execute(
            update(OrganisationRecordLink).where(
                OrganisationRecordLink.source_adapter == adapter_id, OrganisationRecordLink.source_ref == source_ref,
                OrganisationRecordLink.status.in_(("review", "rejected")), OrganisationRecordLink.organisation_id != org_id)
            .values(candidate_org_id=org_id))
        # the new record collapsed onto the candidate itself: nothing left to review
        await session.execute(delete(OrganisationRecordLink).where(
            OrganisationRecordLink.source_adapter == adapter_id, OrganisationRecordLink.source_ref == source_ref,
            OrganisationRecordLink.status == "review", OrganisationRecordLink.organisation_id == org_id,
            OrganisationRecordLink.candidate_org_id.is_(None)))
    for relation, other in res.relationships:
        await _upsert_relationship(session, org_id, other, relation, adapter_id, rec.source_url, confidence=0.8)
    return org_id, created, res


async def resolving_upsert(
    session: AsyncSession, rec: BusinessRecord, adapter_id: str, run_id: int | None = None, category_id: int | None = None
) -> tuple[int, bool]:
    """Drop-in for ``upsert_business`` that resolves against existing companies first: ``(org_id, created)``."""
    org_id, created, _ = await resolving_upsert_ex(session, rec, adapter_id, run_id, category_id)
    return org_id, created


# --------------------------------------------------------------------------- workbench listing
async def find_duplicate_candidates(session: AsyncSession, limit: int = 50, cursor: int | None = None) -> dict[str, Any]:
    """Keyset-paginated review queue (``status='review'`` links) for the Data Quality workbench."""
    limit = max(1, min(int(limit), 500))
    q = (select(OrganisationRecordLink).where(OrganisationRecordLink.status == "review").order_by(OrganisationRecordLink.id)
         .limit(limit + 1))
    if cursor is not None:
        q = q.where(OrganisationRecordLink.id > cursor)
    links = list((await session.execute(q)).scalars())
    more = len(links) > limit
    links = links[:limit]
    ids = {x for ln in links for x in (ln.organisation_id, ln.candidate_org_id) if x}
    orgs = {o.id: o for o in (await session.execute(select(Organisation).where(Organisation.id.in_(ids)))).scalars()} if ids else {}

    def brief(oid: int | None):
        o = orgs.get(oid) if oid else None
        if o is None:
            return None
        return {"id": o.id, "legal_name": o.legal_name, "trading_name": o.trading_name, "company_number": o.company_number,
                "postal_code": o.postal_code, "city": o.city, "country_code": o.country_code, "merged_into_id": o.merged_into_id}

    items = [{
        "link_id": ln.id, "basis": ln.match_basis, "confidence": ln.match_confidence, "adapter": ln.source_adapter,
        "source_ref": ln.source_ref, "observed_at": ln.observed_at, "detail": ln.detail,
        "organisation": brief(ln.organisation_id), "candidate": brief(ln.candidate_org_id),
    } for ln in links]
    return {"items": items, "next_cursor": links[-1].id if more and links else None}


async def resolve_review(session: AsyncSession, link_id: int, *, accept: bool, user_id: int | None) -> dict[str, Any]:
    """Operator decision on a review link: accept -> merge the two organisations, reject -> remember 'not the same'."""
    ln = await session.get(OrganisationRecordLink, link_id)
    if ln is None or ln.status != "review":
        raise ValueError("review link not found")
    if not accept:
        ln.status = "rejected"
        ln.match_basis = "manual"
        session.add(AuditEvent(user_id=user_id, action="organisation.link_rejected", object_type="organisation",
                               object_id=str(ln.organisation_id), detail={"link_id": link_id, "candidate": ln.candidate_org_id}))
        await session.flush()
        return {"status": "rejected"}
    if ln.candidate_org_id is None:
        ln.status = "linked"
        ln.match_basis = "manual"
        await session.flush()
        return {"status": "linked"}
    keep, drop = sorted((ln.organisation_id, ln.candidate_org_id))
    res = await merge_organisations(session, keep, drop, user_id, "accepted duplicate review")
    return {"status": "merged", **res}


# --------------------------------------------------------------------------- merge / split
_RANK = {"confirmed": 4, "high": 3, "review": 2, "unknown": 1, "mismatch": 0}
_FLAG_RANK = {"yes": 3, "in_progress": 2, "no": 1, "unknown": 0}
_FLAGS = ("official_website", "contact_page_inspected", "published_email", "contact_person_corroborated",
          "email_infrastructure_checked", "recipient_evidence", "campaign_eligibility", "recently_checked")
_SIMPLE_MOVES = (
    ("email_addresses", "organisation_id"), ("contact_evidence", "organisation_id"), ("document_records", "organisation_id"),
)
# run_yield unique keys embed the organisation id ('org:<id>', 'pair:<id>:<email>'): re-key them to the kept organisation, dropping
# the dropped organisation's rows that would then collide (the run already counts that company / pair once).
_YIELD_REKEY = ("CASE WHEN {t}.unique_key = 'org:' || :from_s THEN 'org:' || :to_s "
                "WHEN {t}.unique_key LIKE 'pair:' || :from_s || ':%' "
                "THEN 'pair:' || :to_s || substr({t}.unique_key, length('pair:' || :from_s) + 1) "
                "ELSE {t}.unique_key END")


async def _ids(session: AsyncSession, sql: str, **p) -> list[int]:
    return [r[0] for r in (await session.execute(text(sql), p)).all()]


async def merge_organisations(
    session: AsyncSession, keep_id: int, drop_id: int, user_id: int | None, reason: str,
    *, allow_identifier_conflict: bool = False,
) -> dict[str, Any]:
    """Merge ``drop_id`` into ``keep_id``: re-point every dependent row, handle unique-key collisions, set
    ``merged_into_id`` and write an audit event holding the moved row ids (so :func:`split_organisation` can undo).

    Runs inside a SAVEPOINT: any failure leaves the surrounding transaction untouched.
    """
    if keep_id == drop_id:
        raise ValueError("cannot merge an organisation into itself")
    async with session.begin_nested():
        rows = {o.id: o for o in (await session.execute(select(Organisation).where(Organisation.id.in_([keep_id, drop_id]))
                                                         .order_by(Organisation.id).with_for_update())).scalars()}
        if keep_id not in rows or drop_id not in rows:
            raise ValueError("organisation not found")
        keep, drop = rows[keep_id], rows[drop_id]
        if drop.merged_into_id is not None:
            raise ValueError(f"organisation {drop_id} is already merged into {drop.merged_into_id}")
        if keep.merged_into_id is not None:
            raise ValueError(f"organisation {keep_id} is itself merged into {keep.merged_into_id}")
        if (not allow_identifier_conflict and keep.company_number and drop.company_number
                and keep.country_code == drop.country_code and keep.company_number != drop.company_number):
            raise MergeConflict(f"different company numbers ({keep.company_number} vs {drop.company_number})")

        moved: dict[str, Any] = {}
        deleted: dict[str, int] = {}
        K, D = keep_id, drop_id

        # -- review links between the pair are resolved by this merge
        await session.execute(text(
            "UPDATE organisation_record_links SET status='linked', match_basis='manual_merge', candidate_org_id=NULL "
            "WHERE status='review' AND ((organisation_id=:k AND candidate_org_id=:d) OR (organisation_id=:d AND candidate_org_id=:k))"),
            {"k": K, "d": D})

        # -- domains (+ their snapshots)
        kdoms = {d.domain: d for d in (await session.execute(select(OrganisationDomain).where(OrganisationDomain.organisation_id == K))).scalars()}
        moved["organisation_domains"] = []
        deleted["organisation_domains"] = 0
        for d in (await session.execute(select(OrganisationDomain).where(OrganisationDomain.organisation_id == D))).scalars().all():
            k = kdoms.get(d.domain)
            if k is None:
                d.organisation_id = K
                moved["organisation_domains"].append(d.id)
                continue
            if (d.manual_override, _RANK.get(d.verified_match_status, 0), d.confidence) > (
                    k.manual_override, _RANK.get(k.verified_match_status, 0), k.confidence):
                k.verified_match_status, k.confidence, k.manual_override = d.verified_match_status, d.confidence, d.manual_override
                k.evidence = [*(k.evidence or []), *(d.evidence or [])][-50:]
                k.parked = d.parked
            k.website_url = k.website_url or d.website_url
            await session.execute(update(WebsiteSnapshot).where(WebsiteSnapshot.domain_id == d.id).values(domain_id=k.id))
            await session.delete(d)
            deleted["organisation_domains"] += 1
        await session.flush()

        # -- contacts (identity_key unique per organisation)
        kc = {c.identity_key: c for c in (await session.execute(select(Contact).where(
            Contact.organisation_id == K, Contact.identity_key.is_not(None)))).scalars()}
        moved["contacts"] = []
        deleted["contacts"] = 0
        for c in (await session.execute(select(Contact).where(Contact.organisation_id == D))).scalars().all():
            k = kc.get(c.identity_key) if c.identity_key else None
            if k is None:
                c.organisation_id = K
                moved["contacts"].append(c.id)
                continue
            k.job_title = k.job_title or c.job_title
            k.department = k.department or c.department
            k.identity_confidence = max(k.identity_confidence, c.identity_confidence)
            await session.execute(update(EmailAddress).where(EmailAddress.contact_id == c.id).values(contact_id=k.id))
            await session.execute(update(ContactEvidence).where(ContactEvidence.contact_id == c.id).values(contact_id=k.id))
            await session.delete(c)
            deleted["contacts"] += 1
        await session.flush()

        # -- simple re-points
        for table, col in _SIMPLE_MOVES:
            ids = await _ids(session, f"SELECT id FROM {table} WHERE {col}=:d", d=D)
            if ids:
                await session.execute(text(f"UPDATE {table} SET {col}=:k WHERE id = ANY(:ids)"), {"k": K, "ids": ids})
            moved[table] = ids

        # -- run yield: re-key per-organisation keys so a merged pair of companies counts once per run
        r = await session.execute(text(
            f"DELETE FROM run_yield x USING run_yield y WHERE x.organisation_id=:d AND y.run_id=x.run_id AND y.id<>x.id "
            f"AND y.unique_key = {_YIELD_REKEY.format(t='x')}"), {"d": D, "from_s": str(D), "to_s": str(K)})
        deleted["run_yield"] = r.rowcount or 0
        moved["run_yield"] = [row[0] for row in (await session.execute(text(
            f"UPDATE run_yield SET organisation_id=:k, unique_key={_YIELD_REKEY.format(t='run_yield')} WHERE organisation_id=:d RETURNING id"),
            {"k": K, "d": D, "from_s": str(D), "to_s": str(K)})).all()]

        # -- (adapter, ref)-unique tables: drop D's duplicates of K's rows, move the rest
        for table, a, b in (("organisation_sources", "adapter_id", "source_ref"), ("organisation_record_links", "source_adapter", "source_ref")):
            r = await session.execute(text(
                f"DELETE FROM {table} x USING {table} y WHERE x.organisation_id=:d AND y.organisation_id=:k AND x.{a}=y.{a} AND x.{b}=y.{b}"),
                {"d": D, "k": K})
            deleted[table] = r.rowcount or 0
            ids = await _ids(session, f"SELECT id FROM {table} WHERE organisation_id=:d", d=D)
            if ids:
                await session.execute(text(f"UPDATE {table} SET organisation_id=:k WHERE id = ANY(:ids)"), {"k": K, "ids": ids})
            moved[table] = ids
        await session.execute(text("UPDATE organisation_record_links SET candidate_org_id=:k WHERE candidate_org_id=:d"), {"k": K, "d": D})
        await session.execute(text("DELETE FROM organisation_record_links WHERE organisation_id=candidate_org_id"))
        await _upsert_link(session, K, "merge", f"org:{D}", "merged_org", str(D), "manual", 1.0, "linked", None,
                           {"reason": reason, "merged_by": user_id})

        # -- relationships (no self loops, no duplicates)
        r = await session.execute(text(
            "DELETE FROM business_relationships WHERE (from_org_id=:d AND to_org_id=:k) OR (from_org_id=:k AND to_org_id=:d)"), {"k": K, "d": D})
        deleted["business_relationships"] = r.rowcount or 0
        r = await session.execute(text(
            "DELETE FROM business_relationships r USING business_relationships k WHERE r.from_org_id=:d AND k.from_org_id=:k "
            "AND k.to_org_id=r.to_org_id AND k.relation=r.relation AND k.source_adapter=r.source_adapter"), {"k": K, "d": D})
        deleted["business_relationships"] += r.rowcount or 0
        r = await session.execute(text(
            "DELETE FROM business_relationships r USING business_relationships k WHERE r.to_org_id=:d AND k.to_org_id=:k "
            "AND k.from_org_id=r.from_org_id AND k.relation=r.relation AND k.source_adapter=r.source_adapter"), {"k": K, "d": D})
        deleted["business_relationships"] += r.rowcount or 0
        for side in ("from_org_id", "to_org_id"):
            ids = await _ids(session, f"SELECT id FROM business_relationships WHERE {side}=:d", d=D)
            if ids:
                await session.execute(text(f"UPDATE business_relationships SET {side}=:k WHERE id = ANY(:ids)"), {"k": K, "ids": ids})
            moved[f"business_relationships.{side}"] = ids

        # -- signals
        r = await session.execute(text(
            "DELETE FROM business_signals x USING business_signals y WHERE x.organisation_id=:d AND y.organisation_id=:k "
            "AND x.dedupe_hash=y.dedupe_hash"), {"k": K, "d": D})
        deleted["business_signals"] = r.rowcount or 0
        ids = await _ids(session, "SELECT id FROM business_signals WHERE organisation_id=:d", d=D)
        if ids:
            await session.execute(update(BusinessSignal).where(BusinessSignal.id.in_(ids)).values(organisation_id=K))
        moved["business_signals"] = ids

        # -- run membership (composite PK)
        r = await session.execute(text(
            "DELETE FROM run_organisations x USING run_organisations y WHERE x.organisation_id=:d AND y.organisation_id=:k AND x.run_id=y.run_id"),
            {"k": K, "d": D})
        deleted["run_organisations"] = r.rowcount or 0
        runs = await _ids(session, "SELECT run_id FROM run_organisations WHERE organisation_id=:d", d=D)
        if runs:
            await session.execute(text("UPDATE run_organisations SET organisation_id=:k WHERE organisation_id=:d"), {"k": K, "d": D})
        moved["run_organisations"] = runs

        # -- completeness: keep the stronger flag per dimension, union the research memory
        dc = await session.get(OrganisationCompleteness, D)
        if dc is not None:
            kcmp = await session.get(OrganisationCompleteness, K)
            if kcmp is None:
                await session.execute(update(OrganisationCompleteness).where(OrganisationCompleteness.organisation_id == D)
                                      .values(organisation_id=K))
                moved["organisation_completeness"] = [D]
            else:
                for f in _FLAGS:
                    if _FLAG_RANK.get(getattr(dc, f), 0) > _FLAG_RANK.get(getattr(kcmp, f), 0):
                        setattr(kcmp, f, getattr(dc, f))
                kcmp.gap_steps_tried = sorted(set(kcmp.gap_steps_tried or []) | set(dc.gap_steps_tried or []))
                kcmp.gap_log = [*(kcmp.gap_log or []), *(dc.gap_log or [])][-100:]
                kcmp.needs_review = bool(kcmp.needs_review or dc.needs_review)
                await session.delete(dc)
                deleted["organisation_completeness"] = 1
        await session.flush()

        # -- queued work addressed to the dropped org
        await session.execute(text(
            "UPDATE sourcing_jobs SET payload = jsonb_set(payload, '{organisation_id}', to_jsonb(CAST(:k AS bigint))) "
            "WHERE state IN ('queued','paused') AND payload->>'organisation_id' = :d"), {"k": K, "d": str(D)})

        # -- organisation columns: fill gaps on keep from drop
        for col in ("legal_name", "trading_name", "company_number", "legal_type", "status", "region", "city", "postal_code",
                    "address", "phone", "industry", "subcategory", "category_id"):
            if getattr(keep, col) in (None, "") and getattr(drop, col) not in (None, ""):
                setattr(keep, col, getattr(drop, col))
        keep.sic_codes = sorted(set(keep.sic_codes or []) | set(drop.sic_codes or []))
        if drop.last_checked_at and (keep.last_checked_at is None or drop.last_checked_at > keep.last_checked_at):
            keep.last_checked_at = drop.last_checked_at
        drop.merged_into_id = K
        audit = AuditEvent(user_id=user_id, action="organisation.merge", object_type="organisation", object_id=str(K), detail={
            "keep_id": K, "drop_id": D, "reason": reason, "moved": moved, "deleted_conflicts": deleted,
            "drop_snapshot": {"legal_name": drop.legal_name, "trading_name": drop.trading_name, "company_number": drop.company_number,
                              "canonical_identity": drop.canonical_identity, "postal_code": drop.postal_code}})
        session.add(audit)
        await session.flush()
    return {"keep_id": K, "drop_id": D, "moved": {k: (len(v) if isinstance(v, list) else v) for k, v in moved.items()},
            "deleted_conflicts": deleted, "audit_id": audit.id}


_UNDO_TABLES = {
    "organisation_domains": "organisation_id", "contacts": "organisation_id", "email_addresses": "organisation_id",
    "contact_evidence": "organisation_id", "document_records": "organisation_id", "run_yield": "organisation_id",
    "organisation_sources": "organisation_id", "organisation_record_links": "organisation_id", "business_signals": "organisation_id",
}


async def split_organisation(session: AsyncSession, org_id: int, user_id: int | None, reason: str = "") -> dict[str, Any]:
    """Undo the latest merge of ``org_id`` into another organisation (rows deleted as duplicates are not restored)."""
    async with session.begin_nested():
        org = (await session.execute(select(Organisation).where(Organisation.id == org_id).with_for_update())).scalar_one_or_none()
        if org is None or org.merged_into_id is None:
            raise ValueError("organisation is not merged")
        keep_id = org.merged_into_id
        ev = (await session.execute(
            select(AuditEvent).where(AuditEvent.action == "organisation.merge", AuditEvent.detail["drop_id"].astext == str(org_id))
            .order_by(AuditEvent.id.desc()).limit(1))).scalar_one_or_none()
        if ev is None:
            org.merged_into_id = None
            await session.flush()
            return {"restored": {}, "note": "no merge audit event found; only the merge pointer was cleared"}
        moved = ev.detail.get("moved", {})
        restored: dict[str, int] = {}
        for table, col in _UNDO_TABLES.items():
            ids = moved.get(table) or []
            if ids:
                r = await session.execute(text(f"UPDATE {table} SET {col}=:d WHERE id = ANY(:ids) AND {col}=:k"),
                                          {"d": org_id, "k": keep_id, "ids": ids})
                restored[table] = r.rowcount or 0
        for side in ("from_org_id", "to_org_id"):
            ids = moved.get(f"business_relationships.{side}") or []
            if ids:
                r = await session.execute(text(f"UPDATE business_relationships SET {side}=:d WHERE id = ANY(:ids) AND {side}=:k"),
                                          {"d": org_id, "k": keep_id, "ids": ids})
                restored[f"business_relationships.{side}"] = r.rowcount or 0
        if moved.get("run_yield"):  # restore the organisation-scoped keys the merge re-keyed (K -> D)
            await session.execute(text(
                f"UPDATE run_yield SET unique_key={_YIELD_REKEY.format(t='run_yield')} WHERE id = ANY(:ids) AND organisation_id=:k"),
                {"k": org_id, "from_s": str(keep_id), "to_s": str(org_id), "ids": moved["run_yield"]})
        runs = moved.get("run_organisations") or []
        if runs:
            r = await session.execute(text(
                "UPDATE run_organisations SET organisation_id=:d WHERE organisation_id=:k AND run_id = ANY(:runs)"),
                {"d": org_id, "k": keep_id, "runs": runs})
            restored["run_organisations"] = r.rowcount or 0
        await session.execute(text("DELETE FROM organisation_record_links WHERE organisation_id=:k AND source_adapter='merge' "
                                   "AND source_ref=:ref"), {"k": keep_id, "ref": f"org:{org_id}"})
        org.merged_into_id = None
        session.add(AuditEvent(user_id=user_id, action="organisation.split", object_type="organisation", object_id=str(org_id),
                               detail={"keep_id": keep_id, "undoes": ev.id, "reason": reason, "restored": restored}))
        await session.flush()
    return {"organisation_id": org_id, "was_merged_into": keep_id, "restored": restored}


async def unlink_record(
    session: AsyncSession, *, org_id: int, source_adapter: str, source_ref: str, user_id: int | None, reason: str = ""
) -> bool:
    """Operator says this source record does NOT belong to ``org_id``: the link becomes a manual 'rejected' decision
    that no later automatic resolution will override or re-create."""
    r = await _upsert_link(session, org_id, source_adapter, source_ref, None, None, "manual", 1.0, "rejected", None,
                           {"reason": reason, "by": user_id})
    session.add(AuditEvent(user_id=user_id, action="organisation.unlink_record", object_type="organisation", object_id=str(org_id),
                           detail={"adapter": source_adapter, "source_ref": source_ref, "reason": reason}))
    await session.flush()
    return r is not None


__all__ = [
    "MergeConflict", "Resolution", "extract_identifiers", "find_duplicate_candidates", "follow_merge", "link_record",
    "merge_organisations", "resolve_record", "resolve_review", "resolving_upsert", "resolving_upsert_ex",
    "split_organisation", "unlink_record",
]
