"""Global search (top bar): businesses, contacts, domains, company numbers and runs.

Bounded and index-backed: prefix matches on lower(legal_name) / lower(trading_name) / email_normalised (text_pattern_ops
indexes), exact matches on company number and registrable domain, and run names (a small table). Never a full scan."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import EmailAddress, Organisation, OrganisationDomain, SourcingRun
from ..security import require_role
from ..system_runs import not_system_filter
from .common import prefix_pattern

router = APIRouter(prefix="/api/search", tags=["search"], dependencies=[Depends(require_role("viewer"))])


class BusinessHit(BaseModel):
    id: int
    name: str
    detail: str | None = None


class ContactHit(BaseModel):
    id: int
    email: str
    organisation_id: int
    organisation: str | None = None


class RunHit(BaseModel):
    id: int
    name: str
    status: str


class SearchOut(BaseModel):
    query: str
    businesses: list[BusinessHit]
    contacts: list[ContactHit]
    runs: list[RunHit]


_DOMAINISH = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$")


@router.get("", response_model=SearchOut, summary="Search businesses, contacts and runs")
async def global_search(q: str = Query(..., min_length=2, max_length=120), limit: int = Query(6, ge=1, le=20),
                        session: AsyncSession = Depends(get_session)) -> SearchOut:
    term = q.strip()
    low = term.lower()
    pat = prefix_pattern(term)
    O = Organisation  # noqa: N806,E741
    conds = [func.lower(O.legal_name).like(pat, escape="\\"), func.lower(O.trading_name).like(pat, escape="\\")]
    if re.fullmatch(r"[A-Za-z]{0,2}\d{4,10}", term):
        conds.append(O.company_number == term.upper())
    if _DOMAINISH.match(low):
        conds.append(O.id.in_(select(OrganisationDomain.organisation_id).where(OrganisationDomain.registrable_domain == low)))
    orgs = (await session.execute(
        select(O.id, func.coalesce(O.trading_name, O.legal_name), O.city, O.country_code, O.company_number)
        .where(or_(*conds), O.merged_into_id.is_(None)).order_by(O.legal_name).limit(limit))).all()
    emails = (await session.execute(
        select(EmailAddress.id, EmailAddress.email_normalised, EmailAddress.organisation_id, func.coalesce(O.trading_name, O.legal_name))
        .join(O, O.id == EmailAddress.organisation_id)
        .where(or_(EmailAddress.email_normalised.like(pat, escape="\\"),
                   EmailAddress.registrable_domain == low if _DOMAINISH.match(low) else False))
        .order_by(EmailAddress.email_normalised).limit(limit))).all()
    runs = (await session.execute(
        select(SourcingRun.id, SourcingRun.name, SourcingRun.status)
        .where(func.lower(SourcingRun.name).like("%" + pat, escape="\\"), not_system_filter(), SourcingRun.archived.is_(False))
        .order_by(SourcingRun.id.desc()).limit(limit))).all()
    return SearchOut(
        query=term,
        businesses=[BusinessHit(id=r[0], name=r[1] or f"Business {r[0]}", detail=" · ".join(x for x in (r[2], r[3], r[4]) if x) or None) for r in orgs],
        contacts=[ContactHit(id=r[0], email=r[1], organisation_id=r[2], organisation=r[3]) for r in emails],
        runs=[RunHit(id=r[0], name=r[1], status=r[2]) for r in runs],
    )
