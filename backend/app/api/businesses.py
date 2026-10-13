"""Businesses (organisations): list, detail, manual domain correction, bulk actions, duplicate merge."""
from __future__ import annotations

import logging
import re
import time
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import exists, func, literal, literal_column, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.orm import join as orm_join

from .. import jobqueue as jq
from ..db import get_session
from ..models import (
    Category,
    Contact,
    ContactEvidence,
    EmailAddress,
    EvidenceSource,
    Organisation,
    OrganisationCompleteness,
    OrganisationDomain,
    User,
    ValidationResult,
)
from ..security import ApiError, audit, require_role
from ..system_runs import ensure_system_run
from .common import (
    Page,
    SortSpec,
    apply_keyset,
    capped_count,
    check_enum,
    check_sort,
    clean_q,
    country_code,
    finish_page,
    prefix_pattern,
    table_count,
)
from .contacts import ContactRow, EvidenceOut, ValidationOut, _validation_out
from .filters import category_subtree

log = logging.getLogger("api.businesses")
router = APIRouter(prefix="/api/businesses", tags=["businesses"], dependencies=[Depends(require_role("viewer"))])
Org = Organisation
Cat = aliased(Category)
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
SORTS = {
    "id": SortSpec(Org.id, "int"),
    "updated_at": SortSpec(Org.updated_at, "dt"),
    "first_discovered_at": SortSpec(Org.first_discovered_at, "dt"),
    "last_checked_at": SortSpec(func.coalesce(Org.last_checked_at, literal(EPOCH)), "dt"),
    "name": SortSpec(func.lower(func.coalesce(Org.legal_name, Org.trading_name, "")), "str"),
}
DOMAIN_STATUSES = {"confirmed", "high", "review", "mismatch", "unknown", "none"}


class BusinessRow(BaseModel):
    id: int
    legal_name: str | None
    trading_name: str | None
    company_number: str | None
    legal_type: str | None
    country_code: str
    region: str | None
    city: str | None
    category_id: int | None
    category_name: str | None
    discovery_source: str | None
    domain: str | None
    domain_status: str | None
    email_count: int
    published_email_count: int
    best_confidence: float | None
    last_checked_at: datetime | None
    updated_at: datetime
    merged_into_id: int | None = None
    # additive: stored research-completeness summary (null until the worker has assessed the business)
    completeness_score: int | None = None
    official_website_state: str | None = None
    published_email_state: str | None = None


class CompletenessBrief(BaseModel):
    score: int
    flags: dict[str, str]
    needs_review: bool
    updated_at: datetime | None = None


class DomainOut(BaseModel):
    id: int
    domain: str
    registrable_domain: str
    website_url: str | None
    status: str
    confidence: float
    evidence: list[dict[str, Any]]
    parked: bool
    manual_override: bool
    last_checked_at: datetime | None
    last_crawled_at: datetime | None


class ContactBrief(BaseModel):
    id: int
    full_name: str | None
    job_title: str | None
    department: str | None
    identity_confidence: float


class EmailBrief(BaseModel):
    id: int
    email: str
    origin: str
    confidence: float
    role_type: str | None
    overall_reachability: str
    marketing_status: str
    suppressed: bool
    last_validated_at: datetime | None
    contact_id: int | None


class RunLink(BaseModel):
    run_id: int
    name: str
    status: str
    stage: str


class BusinessDetail(BaseModel):
    organisation: dict[str, Any]
    category_name: str | None
    domains: list[DomainOut]
    contacts: list[ContactBrief]
    emails: list[EmailBrief]
    evidence_sources: list[EvidenceOut]
    validation_history: list[ValidationOut]
    runs: list[RunLink]
    duplicates_of: int | None = None
    completeness: CompletenessBrief | None = None


class DomainDecisionIn(BaseModel):
    decision: Literal["confirm", "reject", "set"]
    domain: str | None = Field(None, max_length=255, description="required for 'set'")
    website_url: str | None = Field(None, max_length=1000)
    note: str | None = Field(None, max_length=500)


class BusinessBulkIn(BaseModel):
    action: Literal["add_to_category", "queue_refresh", "export"]
    ids: list[int] = Field(min_length=1, max_length=5000)
    category_id: int | None = None
    export_format: Literal["csv", "xlsx", "jsonl"] = "csv"
    export_preset: str = "full_intelligence"


class BusinessBulkOut(BaseModel):
    action: str
    requested: int
    affected: int
    export_id: int | None = None
    jobs_queued: int | None = None


class BusinessMergeIn(BaseModel):
    primary_id: int
    duplicate_ids: list[int] = Field(min_length=1, max_length=50)


class BusinessMergeOut(BaseModel):
    primary_id: int
    merged: list[int]
    domains_moved: int
    contacts_moved: int
    emails_moved: int


def normalise_domain(raw: str) -> tuple[str, str]:
    """(host, registrable_domain) from user input; rejects anything that is not a plain hostname/URL."""
    v = raw.strip().lower()
    v = re.sub(r"^[a-z][a-z0-9+.-]*://", "", v)
    v = v.split("/")[0].split("?")[0].split("#")[0].split("@")[-1].split(":")[0].removeprefix("www.")
    if not re.fullmatch(r"(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", v):
        raise ApiError(422, "invalid_domain", f"'{raw[:80]}' is not a valid domain name.")
    try:
        import tldextract

        ext = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)(v)
        reg = ext.top_domain_under_public_suffix if hasattr(ext, "top_domain_under_public_suffix") else ext.registered_domain
        reg = reg or v
    except Exception:
        reg = ".".join(v.split(".")[-2:])
    return v, reg


@router.get("", response_model=Page[BusinessRow])
async def list_businesses(
    q: str | None = Query(None, max_length=100, description="prefix search on company name or domain"),
    country: str | None = None, region: str | None = Query(None, max_length=120), city: str | None = Query(None, max_length=120),
    category_id: int | None = None, include_subcategories: bool = True,
    domain_status: str | None = Query(None, description="confirmed|high|review|mismatch|unknown|none"),
    company_type: str | None = Query(None, max_length=100), source: str | None = Query(None, max_length=80),
    email_availability: Literal["any", "published", "none"] | None = None, min_confidence: float | None = Query(None, ge=0, le=100),
    updated_after: datetime | None = None, updated_before: datetime | None = None, include_merged: bool = False,
    sort: str = "id", direction: str = "desc", limit: int = Query(50, ge=1, le=500), cursor: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> Page[BusinessRow]:
    check_sort(SORTS, sort)
    check_enum(domain_status, DOMAIN_STATUSES, "domain_status")
    conds: list = []
    if not include_merged:
        conds.append(Org.merged_into_id.is_(None))
    if clean_q(q):
        pat = prefix_pattern(q)
        conds.append(or_(func.lower(Org.legal_name).like(pat, escape="\\"), func.lower(Org.trading_name).like(pat, escape="\\"),
                         exists().where(OrganisationDomain.organisation_id == Org.id, OrganisationDomain.registrable_domain.like(pat, escape="\\"))))
    if cc := country_code(country):
        conds.append(Org.country_code == cc)
    if region:
        conds.append(Org.region == region)
    if city:
        conds.append(func.lower(Org.city) == city.strip().lower())
    if category_id is not None:
        ids = await category_subtree(session, [category_id]) if include_subcategories else [category_id]
        conds.append(Org.category_id.in_(ids or [-1]))
    if domain_status == "none":
        conds.append(~exists().where(OrganisationDomain.organisation_id == Org.id))
    elif domain_status:
        conds.append(exists().where(OrganisationDomain.organisation_id == Org.id, OrganisationDomain.verified_match_status == domain_status))
    if company_type:
        conds.append(Org.legal_type == company_type)
    if source:
        conds.append(Org.discovery_source == source)
    if email_availability == "published":
        conds.append(exists().where(EmailAddress.organisation_id == Org.id, EmailAddress.origin.in_(("published", "corroborated"))))
    elif email_availability == "none":
        conds.append(~exists().where(EmailAddress.organisation_id == Org.id))
    elif email_availability == "any":
        conds.append(exists().where(EmailAddress.organisation_id == Org.id))
    if min_confidence is not None:
        conds.append(exists().where(EmailAddress.organisation_id == Org.id, EmailAddress.confidence >= min_confidence))
    if updated_after:
        conds.append(Org.updated_at >= updated_after)
    if updated_before:
        conds.append(Org.updated_at <= updated_before)

    d = aliased(OrganisationDomain)
    best = (select(d.domain, d.verified_match_status).where(d.organisation_id == Org.id).order_by(d.confidence.desc(), d.id).limit(1).correlate(Org).subquery().lateral())
    e = aliased(EmailAddress)
    n_all = select(func.count()).select_from(e).where(e.organisation_id == Org.id).correlate(Org).scalar_subquery()
    n_pub = select(func.count()).select_from(e).where(e.organisation_id == Org.id, e.origin.in_(("published", "corroborated"))).correlate(Org).scalar_subquery()
    best_conf = select(func.max(e.confidence)).where(e.organisation_id == Org.id).correlate(Org).scalar_subquery()
    oc = aliased(OrganisationCompleteness)
    c_score = select(oc.score).where(oc.organisation_id == Org.id).correlate(Org).scalar_subquery()
    c_site = select(oc.official_website).where(oc.organisation_id == Org.id).correlate(Org).scalar_subquery()
    c_mail = select(oc.published_email).where(oc.organisation_id == Org.id).correlate(Org).scalar_subquery()
    stmt = (
        select(Org, Cat.name.label("cat_name"), best.c.domain, best.c.verified_match_status, n_all.label("n_all"), n_pub.label("n_pub"), best_conf.label("best_conf"),
               c_score.label("c_score"), c_site.label("c_site"), c_mail.label("c_mail"), SORTS[sort].column.label("_sv"), Org.id.label("_id"))
        .select_from(orm_join(Org, Cat, Cat.id == Org.category_id, isouter=True).outerjoin(best, literal(True)))
        .where(*conds)
    )
    stmt = apply_keyset(stmt, SORTS, sort, direction, Org.id, cursor, limit)
    rows, nxt = finish_page((await session.execute(stmt)).all(), sort, direction, limit)
    total, approx = None, False
    if not cursor:
        if len(conds) <= 1 and not include_merged:
            total, approx = await table_count(session, "organisations")
        else:
            total, approx = await capped_count(session, select(literal_column("1")).select_from(Org).where(*conds))
    items = [
        BusinessRow(id=r[0].id, legal_name=r[0].legal_name, trading_name=r[0].trading_name, company_number=r[0].company_number, legal_type=r[0].legal_type,
                    country_code=r[0].country_code, region=r[0].region, city=r[0].city, category_id=r[0].category_id, category_name=r.cat_name,
                    discovery_source=r[0].discovery_source, domain=r.domain, domain_status=r.verified_match_status, email_count=r.n_all, published_email_count=r.n_pub,
                    best_confidence=r.best_conf, last_checked_at=r[0].last_checked_at, updated_at=r[0].updated_at, merged_into_id=r[0].merged_into_id,
                    completeness_score=r.c_score, official_website_state=r.c_site, published_email_state=r.c_mail)
        for r in rows
    ]
    return Page[BusinessRow](items=items, next_cursor=nxt, total_estimate=total, approximate=approx)


async def _org(session: AsyncSession, org_id: int) -> Organisation:
    o = await session.get(Organisation, org_id)
    if o is None:
        raise ApiError(404, "not_found", "Business not found.")
    return o


def _domain_out(d: OrganisationDomain) -> DomainOut:
    return DomainOut(id=d.id, domain=d.domain, registrable_domain=d.registrable_domain, website_url=d.website_url, status=d.verified_match_status, confidence=d.confidence,
                     evidence=d.evidence or [], parked=d.parked, manual_override=d.manual_override, last_checked_at=d.last_checked_at, last_crawled_at=d.last_crawled_at)


@router.get("/{business_id}", response_model=BusinessDetail)
async def get_business(business_id: int, session: AsyncSession = Depends(get_session)) -> BusinessDetail:
    o = await _org(session, business_id)
    now = datetime.now(UTC)
    cat = await session.get(Category, o.category_id) if o.category_id else None
    domains = (await session.execute(select(OrganisationDomain).where(OrganisationDomain.organisation_id == o.id).order_by(OrganisationDomain.confidence.desc()).limit(50))).scalars().all()
    contacts = (await session.execute(select(Contact).where(Contact.organisation_id == o.id).order_by(Contact.identity_confidence.desc()).limit(200))).scalars().all()
    emails = (await session.execute(select(EmailAddress).where(EmailAddress.organisation_id == o.id).order_by(EmailAddress.confidence.desc()).limit(200))).scalars().all()
    ev = (await session.execute(
        select(ContactEvidence, EvidenceSource).join(EvidenceSource, EvidenceSource.id == ContactEvidence.evidence_id)
        .where(ContactEvidence.organisation_id == o.id).order_by(ContactEvidence.observed_at.desc()).limit(100))).all()
    vh = (await session.execute(
        select(ValidationResult).join(EmailAddress, EmailAddress.id == ValidationResult.email_id).where(EmailAddress.organisation_id == o.id)
        .order_by(ValidationResult.observed_at.desc()).limit(100))).scalars().all()
    runs = (await session.execute(
        text("SELECT r.id, r.name, r.status, ro.stage FROM run_organisations ro JOIN sourcing_runs r ON r.id = ro.run_id WHERE ro.organisation_id=:o ORDER BY r.id DESC LIMIT 50"), {"o": o.id})).all()
    comp = await session.get(OrganisationCompleteness, o.id)
    return BusinessDetail(
        completeness=CompletenessBrief(score=comp.score, needs_review=comp.needs_review, updated_at=comp.updated_at, flags={
            k: getattr(comp, k) for k in ("official_website", "contact_page_inspected", "published_email", "contact_person_corroborated",
                                          "email_infrastructure_checked", "recipient_evidence", "campaign_eligibility", "recently_checked")}) if comp else None,
        organisation={c.name: getattr(o, c.name) for c in Organisation.__table__.columns}, category_name=cat.name if cat else None,
        domains=[_domain_out(d) for d in domains],
        contacts=[ContactBrief(id=c.id, full_name=c.full_name, job_title=c.job_title, department=c.department, identity_confidence=c.identity_confidence) for c in contacts],
        emails=[EmailBrief(id=x.id, email=x.email_normalised, origin=x.origin, confidence=x.confidence, role_type=x.role_type, overall_reachability=x.overall_reachability,
                           marketing_status=x.marketing_status, suppressed=x.suppressed_at is not None, last_validated_at=x.last_validated_at, contact_id=x.contact_id) for x in emails],
        evidence_sources=[EvidenceOut(evidence_id=s.id, source_adapter=s.source_adapter, source_url=s.source_url, source_document=s.source_document, evidence_type=s.evidence_type,
                                      licence=s.licence, extraction_method=ce.extraction_method, observed_at=ce.observed_at, context=ce.context or {}, run_id=ce.run_id,
                                      source_reliability=s.source_reliability) for ce, s in ev],
        validation_history=[_validation_out(v, now) for v in vh], runs=[RunLink(run_id=r[0], name=r[1], status=r[2], stage=r[3]) for r in runs], duplicates_of=o.merged_into_id,
    )


@router.get("/{business_id}/contacts", response_model=Page[ContactRow])
async def business_contacts(business_id: int, sort: str = "id", direction: str = "desc", limit: int = Query(50, ge=1, le=500), cursor: str | None = None, session: AsyncSession = Depends(get_session)) -> Page[ContactRow]:
    from .contacts import contact_rows

    await _org(session, business_id)
    return await contact_rows(session, [EmailAddress.organisation_id == business_id], sort, direction, limit, cursor, with_total=False)


# --------------------------------------------------------------------------- manual domain correction
@router.post("/{business_id}/domains/{domain_id}/decision", response_model=DomainOut)
async def decide_domain(business_id: int, domain_id: int, body: DomainDecisionIn, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> DomainOut:
    o = await _org(session, business_id)
    d = (await session.execute(select(OrganisationDomain).where(OrganisationDomain.id == domain_id, OrganisationDomain.organisation_id == o.id).with_for_update())).scalar_one_or_none()
    if d is None and body.decision != "set":
        raise ApiError(404, "not_found", "That domain is not linked to this business.")
    ev = {"type": "manual", "decision": body.decision, "by_user": user.id, "at": datetime.now(UTC).isoformat(), "note": (body.note or "")[:500]}
    before = d.verified_match_status if d else None
    if body.decision == "set":
        if not body.domain:
            raise ApiError(422, "domain_required", "Provide the domain to set.")
        host, reg = normalise_domain(body.domain)
        d = (await session.execute(select(OrganisationDomain).where(OrganisationDomain.organisation_id == o.id, OrganisationDomain.domain == host))).scalar_one_or_none()
        if d is None:
            d = OrganisationDomain(organisation_id=o.id, domain=host, registrable_domain=reg, website_url=body.website_url or f"https://{host}", evidence=[])
            session.add(d)
        elif body.website_url:
            d.website_url = body.website_url
        d.verified_match_status, d.confidence, d.parked = "confirmed", 1.0, False
    elif body.decision == "confirm":
        d.verified_match_status, d.confidence, d.parked = "confirmed", max(d.confidence or 0.0, 1.0), False
    else:
        d.verified_match_status, d.confidence = "mismatch", 0.0
    d.manual_override = True
    d.evidence = [*(d.evidence or []), ev]
    await session.flush()
    await audit(session, user, "business.domain_override", "organisation", o.id, {"domain_id": d.id, "domain": d.domain, "decision": body.decision, "before": before, "after": d.verified_match_status, "note": body.note})
    await session.commit()
    await session.refresh(d)
    return _domain_out(d)


# --------------------------------------------------------------------------- bulk
async def enqueue_refresh(session: AsyncSession, org_ids: list[int]) -> int:
    """Queue resolve_domain / crawl jobs under the hidden persistent 'system-refresh' run."""
    rid = await ensure_system_run(session, "system-refresh")
    hour = time.strftime("%Y%m%d%H", time.gmtime())
    doms = {}
    for oid, dom in (await session.execute(select(OrganisationDomain.organisation_id, OrganisationDomain.domain).where(
            OrganisationDomain.organisation_id.in_(org_ids), OrganisationDomain.parked.is_(False), OrganisationDomain.verified_match_status.in_(("confirmed", "high"))))).all():
        doms.setdefault(oid, []).append(dom)
    n = 0
    for oid in org_ids:
        if oid in doms:
            for dom in doms[oid][:3]:
                if await jq.enqueue(session, run_id=rid, stage="crawl", dedupe_key=f"refresh:{oid}:{dom}:{hour}", payload={"organisation_id": oid, "domain": dom, "refresh": True}, priority=60, host_key=dom):
                    n += 1
        elif await jq.enqueue(session, run_id=rid, stage="resolve_domain", dedupe_key=f"refresh:{oid}:{hour}", payload={"organisation_id": oid, "refresh": True}, priority=60):
            n += 1
    return n


@router.post("/bulk", response_model=BusinessBulkOut)
async def bulk_businesses(body: BusinessBulkIn, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> BusinessBulkOut:
    ids = sorted(set(body.ids))
    found = list((await session.execute(select(Org.id).where(Org.id.in_(ids), Org.merged_into_id.is_(None)))).scalars())
    out = BusinessBulkOut(action=body.action, requested=len(ids), affected=0)
    if body.action == "add_to_category":
        if body.category_id is None or await session.get(Category, body.category_id) is None:
            raise ApiError(422, "invalid_category", "Choose an existing category.")
        res = await session.execute(text("UPDATE organisations SET category_id=:c, updated_at=now() WHERE id = ANY(:ids)"), {"c": body.category_id, "ids": found})
        out.affected = res.rowcount or 0
    elif body.action == "queue_refresh":
        out.jobs_queued = await enqueue_refresh(session, found)
        out.affected = len(found)
    else:
        from ..exports import ExportFilters, create_export

        ex = await create_export(session, user=user, preset=body.export_preset, fmt=body.export_format, filters=ExportFilters(organisation_ids=found), run_inline=False)
        out.export_id, out.affected = ex.id, len(found)
    await audit(session, user, f"business.bulk_{body.action}", "organisation", None, {"requested": len(ids), "affected": out.affected, "category_id": body.category_id})
    await session.commit()
    return out


@router.post("/merge", response_model=BusinessMergeOut)
async def merge_businesses(body: BusinessMergeIn, user: User = Depends(require_role("admin")), session: AsyncSession = Depends(get_session)) -> BusinessMergeOut:
    dups = sorted(set(body.duplicate_ids) - {body.primary_id})
    if not dups:
        raise ApiError(422, "invalid_merge", "Choose at least one duplicate different from the primary business.")
    primary = (await session.execute(select(Org).where(Org.id == body.primary_id).with_for_update())).scalar_one_or_none()
    if primary is None or primary.merged_into_id is not None:
        raise ApiError(404, "not_found", "The primary business does not exist or has itself been merged.")
    rows = list((await session.execute(select(Org).where(Org.id.in_(dups)).with_for_update())).scalars())
    if len(rows) != len(dups) or any(r.merged_into_id is not None for r in rows):
        raise ApiError(404, "not_found", "One or more duplicates do not exist or were already merged.")
    stats = {"domains": 0, "contacts": 0, "emails": 0}
    p = body.primary_id
    for dup in dups:
        # domains: move those the primary lacks, drop exact duplicates
        stats["domains"] += (await session.execute(text(
            "UPDATE organisation_domains d SET organisation_id=:p WHERE d.organisation_id=:d AND NOT EXISTS (SELECT 1 FROM organisation_domains x WHERE x.organisation_id=:p AND x.domain=d.domain)"), {"p": p, "d": dup})).rowcount or 0
        await session.execute(text("DELETE FROM organisation_domains WHERE organisation_id=:d"), {"d": dup})
        # contacts: identical identities collapse onto the primary's contact
        await session.execute(text(
            """UPDATE email_addresses e SET contact_id = pc.id FROM contacts dc JOIN contacts pc ON pc.organisation_id=:p AND pc.identity_key IS NOT NULL AND pc.identity_key = dc.identity_key
               WHERE e.contact_id = dc.id AND dc.organisation_id=:d"""), {"p": p, "d": dup})
        await session.execute(text("UPDATE contact_evidence ce SET contact_id = pc.id FROM contacts dc JOIN contacts pc ON pc.organisation_id=:p AND pc.identity_key IS NOT NULL AND pc.identity_key = dc.identity_key WHERE ce.contact_id = dc.id AND dc.organisation_id=:d"), {"p": p, "d": dup})
        await session.execute(text("DELETE FROM contacts dc USING contacts pc WHERE dc.organisation_id=:d AND pc.organisation_id=:p AND pc.identity_key IS NOT NULL AND pc.identity_key = dc.identity_key"), {"p": p, "d": dup})
        stats["contacts"] += (await session.execute(text("UPDATE contacts SET organisation_id=:p WHERE organisation_id=:d"), {"p": p, "d": dup})).rowcount or 0
        stats["emails"] += (await session.execute(text("UPDATE email_addresses SET organisation_id=:p WHERE organisation_id=:d"), {"p": p, "d": dup})).rowcount or 0
        await session.execute(text("UPDATE contact_evidence SET organisation_id=:p WHERE organisation_id=:d"), {"p": p, "d": dup})
        await session.execute(text("UPDATE run_yield SET organisation_id=:p WHERE organisation_id=:d"), {"p": p, "d": dup})
        await session.execute(text("INSERT INTO run_organisations (run_id, organisation_id, stage) SELECT run_id, :p, stage FROM run_organisations WHERE organisation_id=:d ON CONFLICT DO NOTHING"), {"p": p, "d": dup})
        await session.execute(text("DELETE FROM run_organisations WHERE organisation_id=:d"), {"d": dup})
        await session.execute(text("UPDATE suppression_entries SET value=:p WHERE kind='company' AND value=:d AND NOT EXISTS (SELECT 1 FROM suppression_entries s WHERE s.kind='company' AND s.value=:p)"), {"p": str(p), "d": str(dup)})
        await session.execute(text(
            """UPDATE organisations o SET
                 phone = COALESCE(o.phone, x.phone), postal_code = COALESCE(o.postal_code, x.postal_code), company_number = COALESCE(o.company_number, x.company_number),
                 category_id = COALESCE(o.category_id, x.category_id), industry = COALESCE(o.industry, x.industry), region = COALESCE(o.region, x.region), city = COALESCE(o.city, x.city),
                 address = COALESCE(o.address, x.address)
               FROM organisations x WHERE o.id=:p AND x.id=:d"""), {"p": p, "d": dup})
        await session.execute(text("UPDATE organisations SET merged_into_id=:p, category_id=NULL WHERE id=:d"), {"p": p, "d": dup})
    await audit(session, user, "business.merge", "organisation", p, {"duplicates": dups, **stats})
    await session.commit()
    return BusinessMergeOut(primary_id=p, merged=dups, domains_moved=stats["domains"], contacts_moved=stats["contacts"], emails_moved=stats["emails"])


