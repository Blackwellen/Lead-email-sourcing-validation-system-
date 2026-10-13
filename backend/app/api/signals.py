"""Business signals (news / hiring / awards / events ...). Every row carries its source URL, observation time and confidence."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import BusinessSignal, Organisation
from ..security import ApiError, require_role
from ..sources.signals_util import SIGNAL_TYPES
from .common import Page, SortSpec, apply_keyset, capped_count, clean_q, country_code, csv_strs, escape_like, finish_page
from .filters import category_subtree

router = APIRouter(prefix="/api/signals", tags=["signals"], dependencies=[Depends(require_role("viewer"))])
S = BusinessSignal
SORTS = {"observed_at": SortSpec(S.observed_at, "dt"), "confidence": SortSpec(S.confidence, "float"), "id": SortSpec(S.id, "int")}


class SignalOut(BaseModel):
    id: int
    organisation_id: int
    organisation: str | None
    country_code: str
    industry: str | None
    signal_type: str
    title: str
    detail: dict[str, Any]
    source_adapter: str
    source_url: str = Field(description="always present: no source URL, no signal")
    occurred_at: datetime | None
    observed_at: datetime
    confidence: float


class FacetCount(BaseModel):
    key: str
    count: int


class SignalFacets(BaseModel):
    total: int
    by_type: list[FacetCount]
    by_country: list[FacetCount]
    first_observed_at: datetime | None
    last_observed_at: datetime | None


@router.get("", response_model=Page[SignalOut])
async def list_signals(
    country: str | None = None, industry: str | None = Query(None, max_length=200), category_id: int | None = None,
    signal_type: str | None = Query(None, description="comma separated signal types"), observed_from: datetime | None = None, observed_to: datetime | None = None,
    min_confidence: float | None = Query(None, ge=0, le=1), q: str | None = Query(None, max_length=100),
    sort: str = "observed_at", direction: str = "desc", limit: int = Query(50, ge=1, le=200), cursor: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> Page[SignalOut]:
    types = csv_strs(signal_type, "signal_type", max_items=len(SIGNAL_TYPES))
    bad = [t for t in types if t not in SIGNAL_TYPES]
    if bad:
        raise ApiError(422, "invalid_filter", f"Unknown signal type(s) {bad}; allowed: {', '.join(SIGNAL_TYPES)}.")
    conds: list = [Organisation.merged_into_id.is_(None)]
    if cc := country_code(country):
        conds.append(Organisation.country_code == cc)
    if industry and industry.strip():
        conds.append(func.lower(Organisation.industry) == industry.strip().lower())
    if category_id is not None:
        conds.append(Organisation.category_id.in_(await category_subtree(session, [category_id]) or [-1]))
    if types:
        conds.append(S.signal_type.in_(types))
    if observed_from:
        conds.append(S.observed_at >= observed_from)
    if observed_to:
        conds.append(S.observed_at <= observed_to)
    if min_confidence is not None:
        conds.append(S.confidence >= min_confidence)
    if clean_q(q):
        conds.append(S.title.ilike("%" + escape_like(clean_q(q) or "") + "%", escape="\\"))
    spec = SORTS.get(sort)
    if spec is None:
        raise ApiError(422, "invalid_sort", f"Cannot sort by '{sort}'. Allowed: {', '.join(sorted(SORTS))}.")
    stmt = (select(S, Organisation.legal_name, Organisation.trading_name, Organisation.country_code, Organisation.industry,
                   spec.column.label("_sv"), S.id.label("_id")).join(Organisation, Organisation.id == S.organisation_id).where(*conds))
    stmt = apply_keyset(stmt, SORTS, sort, direction, S.id, cursor, limit)
    rows, nxt = finish_page((await session.execute(stmt)).all(), sort, direction, limit)
    total = None
    if not cursor:
        total, _capped = await capped_count(session, select(S.id).join(Organisation, Organisation.id == S.organisation_id).where(*conds))
    items = [SignalOut(id=s.id, organisation_id=s.organisation_id, organisation=legal or trading, country_code=cc, industry=ind, signal_type=s.signal_type, title=s.title,
                       detail=s.detail or {}, source_adapter=s.source_adapter, source_url=s.source_url, occurred_at=s.occurred_at, observed_at=s.observed_at,
                       confidence=s.confidence) for s, legal, trading, cc, ind, *_ in rows]
    return Page[SignalOut](items=items, next_cursor=nxt, total_estimate=total)


@router.get("/facets", response_model=SignalFacets)
async def signal_facets(session: AsyncSession = Depends(get_session)) -> SignalFacets:
    base = select(S).join(Organisation, Organisation.id == S.organisation_id).where(Organisation.merged_into_id.is_(None)).subquery()
    by_type = [FacetCount(key=k, count=n) for k, n in (await session.execute(
        select(base.c.signal_type, func.count()).group_by(base.c.signal_type).order_by(func.count().desc(), base.c.signal_type))).all()]
    by_cc = (await session.execute(
        select(Organisation.country_code, func.count()).join(S, S.organisation_id == Organisation.id).where(Organisation.merged_into_id.is_(None))
        .group_by(Organisation.country_code).order_by(func.count().desc(), Organisation.country_code))).all()
    mm = (await session.execute(select(func.min(base.c.observed_at), func.max(base.c.observed_at), func.count()))).one()
    return SignalFacets(total=int(mm[2]), by_type=by_type, by_country=[FacetCount(key=k, count=n) for k, n in by_cc], first_observed_at=mm[0], last_observed_at=mm[1])
