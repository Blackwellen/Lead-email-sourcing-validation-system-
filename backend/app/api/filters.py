"""Contact filter model shared by contact lists, verification tabs, batches and exports."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import Query
from pydantic import BaseModel, Field
from sqlalchemy import exists, func, or_, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Contact, ContactEvidence, EmailAddress, Organisation
from ..settings_schema import load_section
from ..suppression import suppressed_expr
from .common import prefix_pattern

Reach = Literal["safe", "risky", "invalid", "unknown"]  # the stored EmailAddress.overall_reachability values
OriginGroup = Literal["published", "inferred", "corroborated", "pattern_inferred", "role_speculative"]
Tri = Literal["yes", "no"]


class ContactFilterSpec(BaseModel):
    """Every field is validated by type/Literal so hostile input fails with 422 before reaching SQL."""

    q: str | None = Field(None, max_length=100)
    country: list[str] = Field(default_factory=list, max_length=60)
    region: list[str] = Field(default_factory=list, max_length=60)
    city: str | None = Field(None, max_length=120)
    category_id: list[int] = Field(default_factory=list, max_length=100)
    include_subcategories: bool = True
    industry: str | None = Field(None, max_length=200)
    origin: list[OriginGroup] = Field(default_factory=list)
    reachability: list[Reach] = Field(default_factory=list, max_length=10)
    syntax_status: list[str] = Field(default_factory=list, max_length=10)
    domain_mail_status: list[str] = Field(default_factory=list, max_length=10)
    recipient_evidence_status: list[str] = Field(default_factory=list, max_length=10)
    catch_all_status: list[str] = Field(default_factory=list, max_length=10)
    marketing_status: list[str] = Field(default_factory=list, max_length=10)
    contact_type: Literal["named", "role", "both"] | None = None
    role_type: str | None = Field(None, max_length=40)
    min_confidence: float | None = Field(None, ge=0, le=100)
    max_confidence: float | None = Field(None, ge=0, le=100)
    validated_after: datetime | None = None
    validated_before: datetime | None = None
    freshness: Literal["fresh", "stale", "never"] | None = None
    run_id: int | None = None
    source: list[str] = Field(default_factory=list, max_length=60)
    has_name: bool | None = None
    suppressed: bool | None = None
    needs_review: bool | None = None
    free_provider: bool | None = None
    previously_bounced: bool | None = None
    exclude_catch_all: bool = False
    exclude_unknown: bool = False
    organisation_id: int | None = None
    domain: str | None = Field(None, max_length=255)
    email_ids: list[int] = Field(default_factory=list, max_length=50000)
    organisation_ids: list[int] = Field(default_factory=list, max_length=50000)

    def is_empty(self) -> bool:
        return not self.model_dump(exclude_defaults=True, exclude={"include_subcategories"})


def _csv(v: str | None, cap: int = 60) -> list[str]:
    return [p.strip() for p in (v or "").split(",") if p.strip()][:cap]


def contact_filters(
    q: str | None = Query(None, max_length=100, description="Prefix search on e-mail, domain or company name"),
    country: str | None = Query(None, description="comma separated ISO codes"),
    region: str | None = None,
    city: str | None = Query(None, max_length=120),
    category_id: str | None = Query(None, description="comma separated category ids"),
    include_subcategories: bool = True,
    industry: str | None = Query(None, max_length=200),
    origin: str | None = Query(None, description="published,inferred,corroborated,pattern_inferred,role_speculative"),
    reachability: str | None = None,
    syntax_status: str | None = None,
    domain_mail_status: str | None = None,
    recipient_evidence_status: str | None = None,
    catch_all_status: str | None = None,
    marketing_status: str | None = None,
    contact_type: Literal["named", "role", "both"] | None = None,
    role_type: str | None = Query(None, max_length=40),
    min_confidence: float | None = Query(None, ge=0, le=100),
    max_confidence: float | None = Query(None, ge=0, le=100),
    validated_after: datetime | None = None,
    validated_before: datetime | None = None,
    freshness: Literal["fresh", "stale", "never"] | None = None,
    run_id: int | None = None,
    source: str | None = None,
    has_name: bool | None = None,
    suppressed: bool | None = None,
    needs_review: bool | None = None,
    free_provider: bool | None = None,
    previously_bounced: bool | None = None,
    exclude_catch_all: bool = False,
    exclude_unknown: bool = False,
    organisation_id: int | None = None,
    domain: str | None = Query(None, max_length=255),
) -> ContactFilterSpec:
    from ..security import ApiError

    try:
        return ContactFilterSpec(
            q=q, country=[c.upper() for c in _csv(country)], region=_csv(region), city=city,
            category_id=[int(x) for x in _csv(category_id, 100)], include_subcategories=include_subcategories, industry=industry,
            origin=_csv(origin, 10), reachability=_csv(reachability, 10), syntax_status=_csv(syntax_status, 10),
            domain_mail_status=_csv(domain_mail_status, 10), recipient_evidence_status=_csv(recipient_evidence_status, 10),
            catch_all_status=_csv(catch_all_status, 10), marketing_status=_csv(marketing_status, 10), contact_type=contact_type,
            role_type=role_type, min_confidence=min_confidence, max_confidence=max_confidence, validated_after=validated_after,
            validated_before=validated_before, freshness=freshness, run_id=run_id, source=_csv(source), has_name=has_name,
            suppressed=suppressed, needs_review=needs_review, free_provider=free_provider, previously_bounced=previously_bounced,
            exclude_catch_all=exclude_catch_all, exclude_unknown=exclude_unknown, organisation_id=organisation_id, domain=domain,
        )
    except ValueError as e:  # int() failures and pydantic validation errors
        raise ApiError(422, "invalid_filter", f"Invalid filter value: {str(e)[:200]}") from None


async def category_subtree(session: AsyncSession, ids: list[int]) -> list[int]:
    rows = (await session.execute(text(
        """WITH RECURSIVE t AS (SELECT id FROM categories WHERE id = ANY(:ids)
                                UNION SELECT c.id FROM categories c JOIN t ON c.parent_id = t.id)
           SELECT id FROM t"""), {"ids": ids})).scalars().all()
    return list(rows)


async def build_conditions(session: AsyncSession, f: ContactFilterSpec, *, org=Organisation, e=EmailAddress, con=Contact) -> list:
    """SQL conditions for a statement that joins ``e`` (email_addresses), ``org`` and (outer) ``con`` (contacts)."""
    c: list = []
    if f.q and f.q.strip():
        pat = prefix_pattern(f.q)
        c.append(or_(e.email_normalised.like(pat, escape="\\"), e.registrable_domain.like(pat, escape="\\"),
                     func.lower(org.legal_name).like(pat, escape="\\"), func.lower(org.trading_name).like(pat, escape="\\")))
    if f.country:
        c.append(org.country_code.in_(f.country))
    if f.region:
        c.append(org.region.in_(f.region))
    if f.city:
        c.append(func.lower(org.city) == f.city.strip().lower())
    if f.category_id:
        ids = await category_subtree(session, f.category_id) if f.include_subcategories else f.category_id
        c.append(org.category_id.in_(ids or [-1]))
    if f.industry:
        c.append(func.lower(org.industry).like(prefix_pattern(f.industry), escape="\\"))
    if f.origin:
        o: set[str] = set()
        for v in f.origin:
            o |= {"published": {"published", "corroborated"}, "inferred": {"pattern_inferred", "role_speculative"}}.get(v, {v})
        c.append(e.origin.in_(sorted(o)))
    for col, vals in ((e.overall_reachability, f.reachability), (e.syntax_status, f.syntax_status), (e.domain_mail_status, f.domain_mail_status),
                      (e.recipient_evidence_status, f.recipient_evidence_status), (e.catch_all_status, f.catch_all_status), (e.marketing_status, f.marketing_status)):
        if vals:
            c.append(col.in_(vals))
    if f.contact_type == "named":
        c.append(e.role_type.is_(None))
    elif f.contact_type == "role":
        c.append(e.role_type.is_not(None))
    if f.role_type:
        c.append(e.role_type == f.role_type)
    if f.min_confidence is not None:
        c.append(e.confidence >= f.min_confidence)
    if f.max_confidence is not None:
        c.append(e.confidence <= f.max_confidence)
    if f.validated_after:
        c.append(e.last_validated_at >= f.validated_after)
    if f.validated_before:
        c.append(e.last_validated_at <= f.validated_before)
    if f.freshness:
        days = (await load_section(session, "validation")).validity_days
        cutoff = datetime.now(UTC) - timedelta(days=days)
        c.append({"fresh": e.last_validated_at >= cutoff, "stale": e.last_validated_at < cutoff, "never": e.last_validated_at.is_(None)}[f.freshness])
    if f.run_id is not None:
        c.append(exists().where(ContactEvidence.email_id == e.id, ContactEvidence.run_id == f.run_id))
    if f.source:
        c.append(org.discovery_source.in_(f.source))
    if f.has_name is True:
        c.append(con.full_name.is_not(None))
    elif f.has_name is False:
        c.append(con.full_name.is_(None))
    if f.suppressed is True:
        c.append(or_(e.suppressed_at.is_not(None), suppressed_expr(e)))
    elif f.suppressed is False:
        c.append(e.suppressed_at.is_(None))
        c.append(~suppressed_expr(e))
    if f.needs_review is not None:
        c.append(e.needs_review.is_(f.needs_review))
    if f.free_provider is not None:
        c.append(e.is_free_provider.is_(f.free_provider))
    if f.previously_bounced is not None:
        c.append(e.previously_bounced.is_(f.previously_bounced))
    if f.exclude_catch_all:
        c.append(e.catch_all_status.notin_(("confirmed", "suspected")))
    if f.exclude_unknown:
        c.append(e.overall_reachability != "unknown")
        c.append(e.recipient_evidence_status != "inconclusive")
    if f.organisation_id is not None:
        c.append(e.organisation_id == f.organisation_id)
    if f.domain:
        c.append(e.registrable_domain == f.domain.strip().lower())
    if f.email_ids:
        c.append(e.id.in_(f.email_ids))
    if f.organisation_ids:
        c.append(e.organisation_id.in_(f.organisation_ids))
    return c


