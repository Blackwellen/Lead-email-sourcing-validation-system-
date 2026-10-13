"""Contacts (e-mail directory), detail with explanations, actions, suppression and privacy workflows."""
from __future__ import annotations

import hashlib
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, literal, literal_column, select, text, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.orm import join as orm_join

from .. import jobqueue as jq
from ..db import get_session
from ..models import (
    Category,
    Contact,
    ContactEvidence,
    DeliveryEvent,
    EmailAddress,
    EvidenceSource,
    Organisation,
    OrganisationDomain,
    SuppressionEntry,
    User,
    ValidationResult,
)
from ..security import ApiError, audit, require_role
from ..settings_schema import load_section
from ..suppression import normalise_value, suppress, unsuppress
from ..system_runs import ensure_system_run
from .common import Ok, Page, SortSpec, apply_keyset, capped_count, finish_page, table_count
from .filters import ContactFilterSpec, build_conditions, contact_filters

log = logging.getLogger("api.contacts")
router = APIRouter(prefix="/api", dependencies=[Depends(require_role("viewer"))])
contacts_router = APIRouter(prefix="/contacts", tags=["contacts"])
privacy_router = APIRouter(tags=["privacy"])

E, Org, C = EmailAddress, Organisation, Contact
Cat = aliased(Category)

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
SORTS = {
    "id": SortSpec(E.id, "int"),
    "confidence": SortSpec(E.confidence, "float"),
    "last_validated_at": SortSpec(func.coalesce(E.last_validated_at, literal(EPOCH)), "dt"),
    "last_seen_at": SortSpec(E.last_seen_at, "dt"),
    "first_seen_at": SortSpec(E.first_seen_at, "dt"),
    "email": SortSpec(E.email_normalised, "str"),
}


# --------------------------------------------------------------------------- schemas
class ContactRow(BaseModel):
    id: int
    email: str
    origin: str
    published_or_inferred: str
    confidence: float
    role_type: str | None = None
    overall_reachability: str
    syntax_status: str
    domain_mail_status: str
    recipient_evidence_status: str
    catch_all_status: str
    marketing_status: str
    needs_review: bool
    suppressed: bool
    last_validated_at: datetime | None = None
    last_seen_at: datetime
    domain: str
    contact_id: int | None = None
    full_name: str | None = None
    job_title: str | None = None
    organisation_id: int
    company_name: str | None = None
    country_code: str
    region: str | None = None
    city: str | None = None
    category_id: int | None = None
    category_name: str | None = None
    website: str | None = Field(None, description="website URL of the organisation's best-matching domain")
    discovery_source: str | None = None


class EvidenceOut(BaseModel):
    evidence_id: int
    source_adapter: str
    source_url: str | None = None
    source_document: str | None = None
    evidence_type: str
    licence: str | None = None
    extraction_method: str | None = None
    observed_at: datetime
    context: dict[str, Any] = Field(default_factory=dict)
    run_id: int | None = None
    source_reliability: float


class ValidationOut(BaseModel):
    id: int
    email_id: int | None = None
    check_type: str
    result: str
    reason_code: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    response_code: int | None = None
    enhanced_status_code: str | None = None
    provider: str | None = None
    observed_at: datetime
    expires_at: datetime | None = None
    expired: bool = False
    verifier_version: str
    permission_context: str | None = None


class DeliveryOut(BaseModel):
    id: int
    event_type: str
    campaign: str | None = None
    occurred_at: datetime
    detail: str | None = None


class SuppressionOut(BaseModel):
    id: int
    kind: str
    value: str
    reason: str
    note: str | None = None
    created_by: int | None = None
    created_at: datetime


class StatusesOut(BaseModel):
    syntax: str
    domain_mail: str
    recipient_evidence: str
    overall_reachability: str
    marketing: str


class CatchAllOut(BaseModel):
    status: str
    domain: str


class FlagsOut(BaseModel):
    needs_review: bool
    previously_bounced: bool
    free_provider: bool
    disposable: bool
    role_type: str | None = None


class ContactProfileOut(BaseModel):
    id: int
    full_name: str | None = None
    given_name: str | None = None
    family_name: str | None = None
    job_title: str | None = None
    department: str | None = None
    identity_confidence: float


class OrganisationBrief(BaseModel):
    id: int
    legal_name: str | None = None
    trading_name: str | None = None
    company_number: str | None = None
    legal_type: str | None = None
    status: str | None = None
    country_code: str
    region: str | None = None
    city: str | None = None
    postal_code: str | None = None
    phone: str | None = None
    industry: str | None = None
    category_id: int | None = None
    category_name: str | None = None
    discovery_source: str | None = None


class DomainBrief(BaseModel):
    id: int
    domain: str
    website_url: str | None = None
    status: str
    confidence: float
    parked: bool
    manual_override: bool


class FreshnessOut(BaseModel):
    last_validated_at: datetime | None = None
    age_days: int | None = None
    validity_days: int
    stale: bool
    next_expiry: datetime | None = None


class MarketingOut(BaseModel):
    status: str
    eligible: bool
    suppressed: bool
    suppressed_at: datetime | None = None
    note: str


class ContactDetail(BaseModel):
    id: int
    email: str
    email_original: str
    origin: str
    published_or_inferred: str
    confidence: float
    confidence_breakdown: dict[str, Any]
    statuses: StatusesOut
    catch_all: CatchAllOut
    flags: FlagsOut
    contact_profile: ContactProfileOut | None = None
    organisation: OrganisationBrief
    domains: list[DomainBrief]
    sources: list[EvidenceOut]
    validation_latest: list[ValidationOut]
    validation_history: list[ValidationOut]
    delivery_history: list[DeliveryOut]
    delivery_counts: dict[str, int]
    freshness: FreshnessOut
    marketing: MarketingOut
    suppressions: list[SuppressionOut]
    first_seen_at: datetime
    last_seen_at: datetime
    last_validated_at: datetime | None = None
    classification_explanation: list[str]


class SuppressIn(BaseModel):
    scope: Literal["email", "domain", "company"] = "email"
    reason: Literal["opt_out", "objection", "bounce", "complaint", "manual", "legal"] = "manual"
    note: str | None = Field(None, max_length=1000)


class UnsuppressIn(BaseModel):
    scope: Literal["email", "domain", "company"] = "email"


class SuppressionResult(BaseModel):
    entry_id: int | None
    emails_affected: int
    scope: str


class NeedsReviewIn(BaseModel):
    value: bool = True


class RevalidateIn(BaseModel):
    mode: Literal["fast", "deep"] = "fast"


class ContactBulkIn(BaseModel):
    action: Literal["revalidate", "mark_needs_review", "clear_needs_review", "suppress", "unsuppress", "export"]
    ids: list[int] = Field(min_length=1, max_length=10000)
    reason: Literal["opt_out", "objection", "bounce", "complaint", "manual", "legal"] = "manual"
    note: str | None = Field(None, max_length=1000)
    mode: Literal["fast", "deep"] = "fast"
    export_format: Literal["csv", "xlsx", "jsonl"] = "csv"
    export_preset: str = "full_intelligence"


class ContactBulkOut(BaseModel):
    action: str
    requested: int
    affected: int
    export_id: int | None = None


class ObjectionIn(BaseModel):
    email: str = Field(max_length=320)
    note: str | None = Field(None, max_length=1000)
    delete_personal_data: bool = False
    suppress_domain: bool = False


class ObjectionOut(BaseModel):
    suppression_entry_id: int
    matched_emails: int
    personal_fields_cleared: bool
    domain_suppressed: bool = False


class DeleteOut(BaseModel):
    email_deleted: bool
    contact_deleted: bool
    suppression_kept: bool


class SuppressionCreateIn(BaseModel):
    kind: Literal["email", "domain", "company"]
    value: str = Field(min_length=1, max_length=320)
    reason: Literal["opt_out", "objection", "bounce", "complaint", "manual", "legal"] = "manual"
    note: str | None = Field(None, max_length=1000)


def mask_pii_list(items: list[str]) -> list[str]:
    from ..logging_setup import mask_pii

    return [mask_pii(x) for x in items]


# --------------------------------------------------------------------------- list
def _row(r) -> ContactRow:
    m = r._mapping
    return ContactRow(
        id=m["id"], email=m["email"], origin=m["origin"], published_or_inferred="Published" if m["origin"] in ("published", "corroborated") else "Inferred",
        confidence=m["confidence"], role_type=m["role_type"], overall_reachability=m["overall_reachability"], syntax_status=m["syntax_status"],
        domain_mail_status=m["domain_mail_status"], recipient_evidence_status=m["recipient_evidence_status"], catch_all_status=m["catch_all_status"],
        marketing_status=m["marketing_status"], needs_review=m["needs_review"], suppressed=m["suppressed_at"] is not None,
        last_validated_at=m["last_validated_at"], last_seen_at=m["last_seen_at"], domain=m["domain"], contact_id=m["contact_id"],
        full_name=m["full_name"], job_title=m["job_title"], organisation_id=m["organisation_id"], company_name=m["legal_name"] or m["trading_name"],
        country_code=m["country_code"], region=m["region"], city=m["city"], category_id=m["category_id"], category_name=m["category_name"],
        website=m["website"], discovery_source=m["discovery_source"],
    )


def _website_expr():
    d = OrganisationDomain
    return (select(func.coalesce(d.website_url, d.domain)).where(d.organisation_id == Org.id, d.parked.is_(False), d.verified_match_status != "mismatch")
            .order_by(d.confidence.desc(), d.id).limit(1).correlate(Org).scalar_subquery())


def _base_from():
    return orm_join(E, Org, Org.id == E.organisation_id).outerjoin(C, C.id == E.contact_id).outerjoin(Cat, Cat.id == Org.category_id)


async def contact_rows(
    session: AsyncSession, conds: list, sort: str, direction: str, limit: int, cursor: str | None, *, with_total: bool = True, filtered: bool = True
) -> Page[ContactRow]:
    spec = SORTS.get(sort)
    if spec is None:
        raise ApiError(422, "invalid_sort", f"Cannot sort by '{sort}'. Allowed: {', '.join(sorted(SORTS))}.")
    stmt = (
        select(
            E.id, E.email_normalised.label("email"), E.origin, E.confidence, E.role_type, E.overall_reachability, E.syntax_status, E.domain_mail_status,
            E.recipient_evidence_status, E.catch_all_status, E.marketing_status, E.needs_review, E.suppressed_at, E.last_validated_at, E.last_seen_at,
            E.domain, E.contact_id, C.full_name, C.job_title, E.organisation_id, Org.legal_name, Org.trading_name, Org.country_code, Org.region, Org.city,
            Org.category_id, Cat.name.label("category_name"), _website_expr().label("website"), Org.discovery_source, spec.column.label("_sv"), E.id.label("_id"),
        )
        .select_from(_base_from())
        .where(*conds)
    )
    stmt = apply_keyset(stmt, SORTS, sort, direction, E.id, cursor, limit)
    rows, nxt = finish_page((await session.execute(stmt)).all(), sort, direction, limit)
    total, approx = None, False
    if with_total and not cursor:
        if not conds:
            total, approx = await table_count(session, "email_addresses")
        else:
            cnt, capped = await capped_count(session, select(literal_column("1")).select_from(_base_from()).where(*conds))
            total, approx = cnt, capped
    return Page[ContactRow](items=[_row(r) for r in rows], next_cursor=nxt, total_estimate=total, approximate=approx)


@contacts_router.get("", response_model=Page[ContactRow])
async def list_contacts(
    f: ContactFilterSpec = Depends(contact_filters), sort: str = "id", direction: str = "desc",
    limit: int = Query(50, ge=1, le=500), cursor: str | None = None, session: AsyncSession = Depends(get_session),
) -> Page[ContactRow]:
    conds = await build_conditions(session, f)
    return await contact_rows(session, conds, sort, direction, limit, cursor)


# --------------------------------------------------------------------------- detail
def _validation_out(v: ValidationResult, now: datetime) -> ValidationOut:
    return ValidationOut(
        id=v.id, email_id=v.email_id, check_type=v.check_type, result=v.result, reason_code=v.reason_code, detail=v.detail or {}, response_code=v.response_code,
        enhanced_status_code=v.enhanced_status_code, provider=v.provider, observed_at=v.observed_at, expires_at=v.expires_at,
        expired=bool(v.expires_at and v.expires_at < now), verifier_version=v.verifier_version, permission_context=v.permission_context,
    )


def explain(e: EmailAddress, org: Organisation, contact: Contact | None, sources: list[EvidenceOut], latest: list[ValidationOut],
            delivery_counts: dict[str, int], stale: bool, supp: list[SuppressionEntry]) -> list[str]:
    """Human sentences derived only from stored data -- nothing is inferred beyond what is recorded."""
    out: list[str] = []
    n = len(sources)
    urls = [s.source_url for s in sources if s.source_url][:3]
    if e.origin == "published":
        out.append(f"This address is published: it was found on {n} recorded source{'s' if n != 1 else ''}" + (f" (e.g. {', '.join(urls)})." if urls else "."))
    elif e.origin == "corroborated":
        out.append(f"This address was published and corroborated by {n} independent source{'s' if n != 1 else ''}.")
    elif e.origin == "pattern_inferred":
        out.append("This address was inferred from a naming pattern at the company's domain; it has not been seen published anywhere.")
    else:
        out.append("This is a speculative role address (such as info@ or sales@) generated for the domain; it has not been seen published.")
    if e.role_type:
        out.append(f"It is a role mailbox ({e.role_type}), not a named individual.")
    elif contact and contact.full_name:
        out.append(f"It belongs to a named individual: {contact.full_name}" + (f", {contact.job_title}." if contact.job_title else "."))
    else:
        out.append("No person is linked to this address.")
    if e.is_free_provider:
        out.append("The mailbox is on a free consumer e-mail provider rather than the company's own domain.")
    if e.is_disposable:
        out.append("The domain is a known disposable-mail provider.")
    labels = {
        "syntax_status": {"valid": "The address syntax is valid.", "invalid": "The address syntax is invalid."},
        "domain_mail_status": {"accepts_mail": "The domain publishes mail servers (MX) and can receive e-mail.", "no_mail_service": "The domain has no mail service, so it cannot receive e-mail.",
                               "null_mx": "The domain publishes a null MX: it explicitly refuses e-mail.", "temporary_dns_failure": "DNS lookups for the domain failed temporarily, so mail service is unconfirmed."},
    }
    for attr, m in labels.items():
        s = m.get(getattr(e, attr))
        if s:
            out.append(s)
    rs = {"untested": "The mailbox itself has not been tested.", "inconclusive": "Tests of the mailbox were inconclusive (the server gave no reliable answer).",
          "smtp_recipient_accepted": "The mail server accepted the recipient during diagnostics (this is evidence, not a guarantee of delivery).",
          "smtp_recipient_rejected": "The mail server rejected the recipient during diagnostics."}
    if e.recipient_evidence_status in rs:
        out.append(rs[e.recipient_evidence_status])
    ca = {"confirmed": "The domain accepts all recipients (catch-all), so mailbox existence cannot be proven.", "suspected": "The domain looks like a catch-all, so mailbox existence cannot be proven.",
          "not_catch_all": "The domain does not behave as a catch-all."}
    if e.catch_all_status in ca:
        out.append(ca[e.catch_all_status])
    out.append(f"Overall reachability is '{e.overall_reachability}' with a confidence score of {round(e.confidence, 1)}.")
    if latest:
        failing = [v for v in latest if v.result in ("fail", "invalid", "rejected", "error")]
        if failing:
            out.append("Checks that did not pass: " + ", ".join(f"{v.check_type} ({v.reason_code or v.result})" for v in failing[:5]) + ".")
    if latest and any(v.reason_code == "resolver_unreliable" for v in latest):
        out.append("Mail routing could not be determined: the DNS resolver this server uses did not return MX records for "
                   "known mail domains, so the A-record fallback cannot be trusted. Fix DNS (System page) and re-validate.")
    if e.last_validated_at is None:
        out.append("This address has never been validated (for example, it was found after the run reached its target).")
    elif stale:
        out.append("The last validation is older than the freshness window, so the status may have changed; re-validate before use.")
    if delivery_counts.get("hard_bounce"):
        out.append(f"It hard-bounced {delivery_counts['hard_bounce']} time(s) in imported delivery feedback.")
    if delivery_counts.get("complaint"):
        out.append("A spam complaint was imported for this address.")
    if e.suppressed_at or supp:
        out.append("It is on the suppression list (" + ", ".join(sorted({f"{s.kind}:{s.reason}" for s in supp})) + ") and must not be used for marketing." if supp else "It is flagged as suppressed and must not be used for marketing.")
    out.append(f"Marketing status is '{e.marketing_status}'; this is policy triage, independent of technical validity, and is not legal advice.")
    return out


@contacts_router.get("/{contact_id}", response_model=ContactDetail)
async def get_contact(contact_id: int, session: AsyncSession = Depends(get_session)) -> ContactDetail:
    e = await session.get(EmailAddress, contact_id)
    if e is None:
        raise ApiError(404, "not_found", "Contact not found.")
    org = await session.get(Organisation, e.organisation_id)
    contact = await session.get(Contact, e.contact_id) if e.contact_id else None
    now = datetime.now(UTC)
    ev_rows = (await session.execute(
        select(ContactEvidence, EvidenceSource).join(EvidenceSource, EvidenceSource.id == ContactEvidence.evidence_id)
        .where(ContactEvidence.email_id == e.id).order_by(ContactEvidence.observed_at.desc()).limit(100))).all()
    sources = [EvidenceOut(evidence_id=s.id, source_adapter=s.source_adapter, source_url=s.source_url, source_document=s.source_document, evidence_type=s.evidence_type,
                           licence=s.licence, extraction_method=ce.extraction_method, observed_at=ce.observed_at, context=ce.context or {}, run_id=ce.run_id,
                           source_reliability=s.source_reliability) for ce, s in ev_rows]
    latest_rows = (await session.execute(
        select(ValidationResult).ext(distinct_on(ValidationResult.check_type)).where(ValidationResult.email_id == e.id).order_by(ValidationResult.check_type, ValidationResult.observed_at.desc(), ValidationResult.id.desc()))).scalars().all()
    hist_rows = (await session.execute(select(ValidationResult).where(ValidationResult.email_id == e.id).order_by(ValidationResult.observed_at.desc(), ValidationResult.id.desc()).limit(100))).scalars().all()
    latest = [_validation_out(v, now) for v in latest_rows]
    history = [_validation_out(v, now) for v in hist_rows]
    dl = (await session.execute(select(DeliveryEvent).where(DeliveryEvent.email_id == e.id).order_by(DeliveryEvent.occurred_at.desc()).limit(100))).scalars().all()
    counts: dict[str, int] = {}
    for d in dl:
        counts[d.event_type] = counts.get(d.event_type, 0) + 1
    supp = (await session.execute(select(SuppressionEntry).where(
        ((SuppressionEntry.kind == "email") & (SuppressionEntry.value == e.email_normalised))
        | ((SuppressionEntry.kind == "domain") & SuppressionEntry.value.in_([e.registrable_domain, e.domain]))
        | ((SuppressionEntry.kind == "company") & (SuppressionEntry.value == str(e.organisation_id)))))).scalars().all()
    domains = (await session.execute(select(OrganisationDomain).where(OrganisationDomain.organisation_id == org.id).order_by(OrganisationDomain.confidence.desc()).limit(20))).scalars().all()
    validity = (await load_section(session, "validation")).validity_days
    stale = e.last_validated_at is None or e.last_validated_at < now - timedelta(days=validity)
    exp = [v.expires_at for v in latest if v.expires_at]
    cat = await session.get(Category, org.category_id) if org.category_id else None
    return ContactDetail(
        id=e.id, email=e.email_normalised, email_original=e.email_original, origin=e.origin,
        published_or_inferred="Published" if e.origin in ("published", "corroborated") else "Inferred", confidence=e.confidence,
        confidence_breakdown=e.confidence_breakdown or {},
        statuses={"syntax": e.syntax_status, "domain_mail": e.domain_mail_status, "recipient_evidence": e.recipient_evidence_status,
                  "overall_reachability": e.overall_reachability, "marketing": e.marketing_status},
        catch_all={"status": e.catch_all_status, "domain": e.domain},
        flags={"needs_review": e.needs_review, "previously_bounced": e.previously_bounced, "free_provider": e.is_free_provider, "disposable": e.is_disposable, "role_type": e.role_type},
        contact_profile=None if contact is None else {"id": contact.id, "full_name": contact.full_name, "given_name": contact.given_name, "family_name": contact.family_name,
                                                      "job_title": contact.job_title, "department": contact.department, "identity_confidence": contact.identity_confidence},
        organisation={"id": org.id, "legal_name": org.legal_name, "trading_name": org.trading_name, "company_number": org.company_number, "legal_type": org.legal_type,
                      "status": org.status, "country_code": org.country_code, "region": org.region, "city": org.city, "postal_code": org.postal_code, "phone": org.phone,
                      "industry": org.industry, "category_id": org.category_id, "category_name": cat.name if cat else None, "discovery_source": org.discovery_source},
        domains=[{"id": d.id, "domain": d.domain, "website_url": d.website_url, "status": d.verified_match_status, "confidence": d.confidence, "parked": d.parked,
                  "manual_override": d.manual_override} for d in domains],
        sources=sources, validation_latest=latest, validation_history=history,
        delivery_history=[DeliveryOut(id=d.id, event_type=d.event_type, campaign=d.campaign, occurred_at=d.occurred_at, detail=d.detail) for d in dl], delivery_counts=counts,
        freshness={"last_validated_at": e.last_validated_at, "age_days": None if e.last_validated_at is None else (now - e.last_validated_at).days,
                   "validity_days": validity, "stale": stale, "next_expiry": min(exp) if exp else None},
        marketing={"status": e.marketing_status, "eligible": e.marketing_status == "eligible" and e.suppressed_at is None and not supp,
                   "suppressed": e.suppressed_at is not None or bool(supp), "suppressed_at": e.suppressed_at,
                   "note": "Policy-driven triage, independent of technical validity; not legal advice."},
        suppressions=[SuppressionOut(id=s.id, kind=s.kind, value=s.value, reason=s.reason, note=s.note, created_by=s.created_by, created_at=s.created_at) for s in supp],
        first_seen_at=e.first_seen_at, last_seen_at=e.last_seen_at, last_validated_at=e.last_validated_at,
        classification_explanation=explain(e, org, contact, sources, latest, counts, stale, list(supp)),
    )


# --------------------------------------------------------------------------- actions
async def enqueue_verify(session: AsyncSession, email_ids: list[int], mode: str, run_id: int | None = None, tag: str = "reval") -> int:
    rid = run_id or await ensure_system_run(session, "system-verification")
    bucket = int(time.time() // 60)
    n = 0
    for i in email_ids:
        if await jq.enqueue(session, run_id=rid, stage="verify", dedupe_key=f"{tag}:{i}:{bucket}", payload={"email_id": i, "mode": mode}, priority=50):
            n += 1
    return n


async def _get_email(session: AsyncSession, contact_id: int) -> EmailAddress:
    e = await session.get(EmailAddress, contact_id)
    if e is None:
        raise ApiError(404, "not_found", "Contact not found.")
    return e


@contacts_router.post("/{contact_id}/revalidate", response_model=Ok)
async def revalidate_contact(contact_id: int, body: RevalidateIn | None = None, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> Ok:
    e = await _get_email(session, contact_id)
    mode = body.mode if body else "fast"
    n = await enqueue_verify(session, [e.id], mode)
    await audit(session, user, "contact.revalidate", "email", e.id, {"mode": mode, "queued": n})
    await session.commit()
    return Ok(message="Re-validation queued." if n else "A re-validation for this address is already queued.")


@contacts_router.post("/{contact_id}/needs-review", response_model=Ok)
async def set_needs_review(contact_id: int, body: NeedsReviewIn, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> Ok:
    e = await _get_email(session, contact_id)
    e.needs_review = body.value
    await audit(session, user, "contact.needs_review", "email", e.id, {"value": body.value})
    await session.commit()
    return Ok(message="Updated.")


async def _scope_value(session: AsyncSession, e: EmailAddress, scope: str) -> str:
    return {"email": e.email_normalised, "domain": e.registrable_domain, "company": str(e.organisation_id)}[scope]


@contacts_router.post("/{contact_id}/suppress", response_model=SuppressionResult)
async def suppress_contact(contact_id: int, body: SuppressIn, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> SuppressionResult:
    e = await _get_email(session, contact_id)
    value = await _scope_value(session, e, body.scope)
    entry_id, n = await suppress(session, kind=body.scope, value=value, reason=body.reason, note=body.note, user_id=user.id)
    await audit(session, user, "contact.suppress", "email", e.id, {"scope": body.scope, "reason": body.reason, "affected": n,
                                                                     "value": value if body.scope != "email" else hashlib.sha256(value.encode()).hexdigest()[:16]})
    await session.commit()
    return SuppressionResult(entry_id=entry_id, emails_affected=n, scope=body.scope)


@contacts_router.post("/{contact_id}/unsuppress", response_model=SuppressionResult)
async def unsuppress_contact(contact_id: int, body: UnsuppressIn, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> SuppressionResult:
    e = await _get_email(session, contact_id)
    value = await _scope_value(session, e, body.scope)
    removed, n = await unsuppress(session, kind=body.scope, value=value)
    if not removed:
        raise ApiError(404, "not_found", f"There is no {body.scope}-level suppression for this contact.")
    await audit(session, user, "contact.unsuppress", "email", e.id, {"scope": body.scope, "released": n})
    await session.commit()
    return SuppressionResult(entry_id=None, emails_affected=n, scope=body.scope)


@contacts_router.post("/bulk", response_model=ContactBulkOut)
async def bulk_contacts(body: ContactBulkIn, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> ContactBulkOut:
    ids = sorted(set(body.ids))
    emails = (await session.execute(select(E.id, E.email_normalised).where(E.id.in_(ids)))).all()
    found = [r[0] for r in emails]
    affected, export_id = 0, None
    if body.action == "revalidate":
        affected = await enqueue_verify(session, found, body.mode, tag="bulk")
    elif body.action in ("mark_needs_review", "clear_needs_review"):
        res = await session.execute(update(E).where(E.id.in_(found)).values(needs_review=body.action == "mark_needs_review"))
        affected = res.rowcount or 0
    elif body.action == "suppress":
        for _, em in emails:
            await suppress(session, kind="email", value=em, reason=body.reason, note=body.note, user_id=user.id)
            affected += 1
    elif body.action == "unsuppress":
        for _, em in emails:
            removed, _n = await unsuppress(session, kind="email", value=em)
            affected += 1 if removed else 0
    else:
        from ..exports import ExportFilters, create_export

        ex = await create_export(session, user=user, preset=body.export_preset, fmt=body.export_format, filters=ExportFilters(email_ids=found), run_inline=False)
        export_id, affected = ex.id, len(found)
    await audit(session, user, f"contact.bulk_{body.action}", "email", None, {"requested": len(ids), "affected": affected})
    await session.commit()
    return ContactBulkOut(action=body.action, requested=len(ids), affected=affected, export_id=export_id)


# --------------------------------------------------------------------------- deletion (admin)
@contacts_router.delete("/{contact_id}", response_model=DeleteOut)
async def delete_contact_data(contact_id: int, keep_suppression: bool = True, user: User = Depends(require_role("admin")), session: AsyncSession = Depends(get_session)) -> DeleteOut:
    """Hard-delete an e-mail address and its dependent rows (right to erasure). The suppression entry is kept (or created)
    so the address is not re-collected and marketed to later."""
    e = await _get_email(session, contact_id)
    norm, contact_pk, h = e.email_normalised, e.contact_id, hashlib.sha256(e.email_normalised.encode()).hexdigest()[:16]
    if keep_suppression:
        await suppress(session, kind="email", value=norm, reason="legal", note="erasure request (data deleted)", user_id=user.id)
    await session.delete(e)
    await session.flush()
    contact_deleted = False
    if contact_pk:
        remaining = (await session.execute(select(func.count()).select_from(E).where(E.contact_id == contact_pk))).scalar_one()
        if remaining == 0:
            await session.execute(text("DELETE FROM contacts WHERE id=:i"), {"i": contact_pk})
            contact_deleted = True
    await audit(session, user, "contact.delete_data", "email", contact_id, {"email_sha256": h, "suppression_kept": keep_suppression, "contact_deleted": contact_deleted})
    await session.commit()
    return DeleteOut(email_deleted=True, contact_deleted=contact_deleted, suppression_kept=keep_suppression)


# --------------------------------------------------------------------------- privacy / suppression list
@privacy_router.post("/privacy/objection", response_model=ObjectionOut)
async def record_objection(body: ObjectionIn, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> ObjectionOut:
    from .auth import normalise_email

    try:
        norm = normalise_email(body.email)
    except ValueError as ex:
        raise ApiError(422, "invalid_email", str(ex)) from None
    entry_id, matched = await suppress(session, kind="email", value=norm, reason="objection", note=(body.note or "right to object")[:1000], user_id=user.id)
    cleared = False
    if body.delete_personal_data:
        rows = (await session.execute(select(E.id, E.contact_id).where(E.email_normalised == norm))).all()
        cids = [r[1] for r in rows if r[1]]
        eids = [r[0] for r in rows]
        if cids:
            await session.execute(update(Contact).where(Contact.id.in_(cids)).values(full_name=None, given_name=None, family_name=None, job_title=None, department=None, identity_key=None, identity_confidence=0.0))
            cleared = True
        if eids:
            await session.execute(text("UPDATE contact_evidence SET context = '{}'::jsonb WHERE email_id = ANY(:ids)"), {"ids": eids})
            await session.execute(update(E).where(E.id.in_(eids)).values(email_original=norm))
            cleared = True
    domain_done = False
    if body.suppress_domain:
        await suppress(session, kind="domain", value=norm.split("@", 1)[1], reason="objection", note="organisation objected", user_id=user.id)
        domain_done = True
    await audit(session, user, "privacy.objection", "email", None, {"email_sha256": hashlib.sha256(norm.encode()).hexdigest()[:16], "matched": matched,
                                                                       "personal_data_cleared": cleared, "domain_suppressed": domain_done})
    await session.commit()
    return ObjectionOut(suppression_entry_id=entry_id, matched_emails=matched, personal_fields_cleared=cleared, domain_suppressed=domain_done)


@privacy_router.get("/suppressions", response_model=Page[SuppressionOut])
async def list_suppressions(kind: Literal["email", "domain", "company"] | None = None, q: str | None = Query(None, max_length=100), limit: int = Query(50, ge=1, le=500),
                            cursor: str | None = None, session: AsyncSession = Depends(get_session)) -> Page[SuppressionOut]:
    from .common import prefix_pattern

    sorts = {"id": SortSpec(SuppressionEntry.id, "int")}
    stmt = select(SuppressionEntry, SuppressionEntry.id.label("_sv"), SuppressionEntry.id.label("_id"))
    if kind:
        stmt = stmt.where(SuppressionEntry.kind == kind)
    if q and q.strip():
        stmt = stmt.where(SuppressionEntry.value.like(prefix_pattern(q), escape="\\"))
    stmt = apply_keyset(stmt, sorts, "id", "desc", SuppressionEntry.id, cursor, limit)
    rows, nxt = finish_page((await session.execute(stmt)).all(), "id", "desc", limit)
    return Page[SuppressionOut](items=[SuppressionOut(id=s.id, kind=s.kind, value=s.value, reason=s.reason, note=s.note, created_by=s.created_by, created_at=s.created_at) for s, *_ in rows], next_cursor=nxt)


@privacy_router.post("/suppressions", response_model=SuppressionResult, status_code=201)
async def create_suppression(body: SuppressionCreateIn, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> SuppressionResult:
    try:
        value = normalise_value(body.kind, body.value)
    except ValueError as ex:
        raise ApiError(422, "invalid_value", str(ex)) from None
    entry_id, n = await suppress(session, kind=body.kind, value=value, reason=body.reason, note=body.note, user_id=user.id)
    await audit(session, user, "suppression.create", "suppression", entry_id, {"kind": body.kind, "reason": body.reason, "affected": n})
    await session.commit()
    return SuppressionResult(entry_id=entry_id, emails_affected=n, scope=body.kind)


@privacy_router.delete("/suppressions/{entry_id}", response_model=SuppressionResult)
async def delete_suppression(entry_id: int, user: User = Depends(require_role("admin")), session: AsyncSession = Depends(get_session)) -> SuppressionResult:
    s = await session.get(SuppressionEntry, entry_id)
    if s is None:
        raise ApiError(404, "not_found", "Suppression entry not found.")
    _, n = await unsuppress(session, kind=s.kind, value=s.value)
    await audit(session, user, "suppression.delete", "suppression", entry_id, {"kind": s.kind, "reason": s.reason, "released": n})
    await session.commit()
    return SuppressionResult(entry_id=None, emails_affected=n, scope=s.kind)


router.include_router(contacts_router)
router.include_router(privacy_router)
