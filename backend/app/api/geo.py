"""Geography: countries, subdivisions, UK localities, per-country source availability and coverage planning."""
from __future__ import annotations

import inspect
from typing import Any

import pycountry
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import SourceAdapterState
from ..security import ApiError, require_role
from .common import clean_q, country_code, escape_like
from .registry import adapter_catalog, optional

router = APIRouter(prefix="/api/geo", tags=["geo"], dependencies=[Depends(require_role("viewer"))])


class CountryRow(BaseModel):
    code: str
    name: str
    businesses: int
    published_emails: int
    sources_available: int


class SubdivisionRow(BaseModel):
    code: str
    name: str
    type: str
    parent_code: str | None
    businesses: int


class LocalityRow(BaseModel):
    kind: str  # city | region | postcode_area
    name: str
    count: int


class SourceAvail(BaseModel):
    adapter_id: str
    name: str
    status: str  # available | unconfigured | disabled | info_only | not_permitted
    source_type: str
    priority: int
    licence: str | None = None
    note: str | None = None


class CountryAvailability(BaseModel):
    country: str
    name: str | None
    sources: list[SourceAvail]
    usable: int


class AvailabilityOut(BaseModel):
    registry_loaded: bool
    countries: list[CountryAvailability]
    detail: Any = None


class CoverageCountry(BaseModel):
    country: str
    name: str | None
    plan: list[SourceAvail]
    existing_businesses: int
    existing_published_emails: int
    gaps: list[str] = Field(default_factory=list)


class CoveragePlan(BaseModel):
    countries: list[CoverageCountry]
    warnings: list[str]
    detail: Any = None


def _name(cc: str) -> str | None:
    c = pycountry.countries.get(alpha_2=cc)
    return c.name if c else None


async def _avail(session: AsyncSession, cc: str) -> list[SourceAvail]:
    cat = adapter_catalog()
    if cat is None:
        return []
    states = {r.adapter_id: r for r in (await session.execute(select(SourceAdapterState))).scalars()}
    out: list[SourceAvail] = []
    for aid, info in cat.items():
        cov = info.get("country_coverage") or []
        if "*" not in cov and cc not in cov:
            continue
        st = states.get(aid)
        status, note = "available", None
        if info.get("info_only"):
            status, note = "info_only", "Catalogue entry only; it yields no records."
        elif not info.get("automated_collection_permitted", True):
            status, note = "not_permitted", "Automated collection is not permitted by this source's terms."
        elif st is not None and not st.enabled:
            status = "disabled"
        elif info.get("requires_config") and not (st and st.config):
            status, note = "unconfigured", info.get("access_requirements") or "Needs configuration."
        out.append(SourceAvail(adapter_id=aid, name=info.get("name", aid), status=status, source_type=info.get("source_type", ""),
                               priority=st.priority if st else int(info.get("default_priority", 100)), licence=info.get("licence"), note=note))
    out.sort(key=lambda s: (s.priority, s.adapter_id))
    return out


@router.get("/countries", response_model=list[CountryRow])
async def list_countries(q: str | None = Query(None, max_length=60), with_data: bool = False, session: AsyncSession = Depends(get_session)) -> list[CountryRow]:
    counts = {r[0]: (r[1], r[2]) for r in (await session.execute(text(
        """SELECT o.country_code, count(DISTINCT o.id), count(e.id) FILTER (WHERE e.origin IN ('published','corroborated'))
           FROM organisations o LEFT JOIN email_addresses e ON e.organisation_id=o.id WHERE o.merged_into_id IS NULL GROUP BY 1"""))).all()}
    cat = adapter_catalog() or {}
    needle = (clean_q(q) or "").lower()
    rows = []
    for c in pycountry.countries:
        if needle and needle not in c.name.lower() and needle != c.alpha_2.lower():
            continue
        b, p = counts.get(c.alpha_2, (0, 0))
        if with_data and not b:
            continue
        n_src = sum(1 for i in cat.values() if "*" in (i.get("country_coverage") or []) or c.alpha_2 in (i.get("country_coverage") or []))
        rows.append(CountryRow(code=c.alpha_2, name=c.name, businesses=b, published_emails=p, sources_available=n_src))
    rows.sort(key=lambda r: (-r.businesses, r.name))
    return rows


@router.get("/countries/{code}/subdivisions", response_model=list[SubdivisionRow])
async def list_subdivisions(code: str, type: str | None = Query(None, max_length=40), q: str | None = Query(None, max_length=60), session: AsyncSession = Depends(get_session)) -> list[SubdivisionRow]:
    cc = country_code(code)
    if cc is None or pycountry.countries.get(alpha_2=cc) is None:
        raise ApiError(404, "not_found", "Unknown country code.")
    counts = {r[0]: r[1] for r in (await session.execute(text("SELECT region, count(*) FROM organisations WHERE country_code=:c AND merged_into_id IS NULL AND region IS NOT NULL GROUP BY 1"), {"c": cc})).all()}
    needle = (clean_q(q) or "").lower()
    out = []
    for s in pycountry.subdivisions.get(country_code=cc) or []:
        if type and s.type.lower() != type.lower():
            continue
        if needle and needle not in s.name.lower():
            continue
        out.append(SubdivisionRow(code=s.code, name=s.name, type=s.type, parent_code=s.parent_code, businesses=counts.get(s.name, 0)))
    out.sort(key=lambda r: (-r.businesses, r.name))
    return out


@router.get("/localities", response_model=list[LocalityRow])
async def list_localities(
    country: str = Query("GB", min_length=2, max_length=2), kind: str = Query("city", pattern="^(city|region|postcode_area)$"),
    q: str | None = Query(None, max_length=60), region: str | None = Query(None, max_length=120), limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
) -> list[LocalityRow]:
    cc = country_code(country)
    params: dict[str, Any] = {"c": cc, "n": limit}
    where = "country_code=:c AND merged_into_id IS NULL"
    if region:
        where += " AND region = :rg"
        params["rg"] = region
    if q and q.strip():
        where += " AND lower(" + {"city": "city", "region": "region", "postcode_area": "postal_code"}[kind] + ") LIKE :p ESCAPE '\\'"
        params["p"] = escape_like(q.strip().lower()) + "%"
    if kind == "postcode_area":
        expr, extra = "regexp_replace(upper(postal_code), '^([A-Z]{1,2}).*$', '\\1')", " AND postal_code ~* '^[A-Z]{1,2}[0-9]'"
    else:
        expr, extra = kind, f" AND {kind} IS NOT NULL"
    rows = (await session.execute(text(f"SELECT {expr} AS n, count(*) FROM organisations WHERE {where}{extra} GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT :n"), params)).all()
    return [LocalityRow(kind=kind, name=r[0], count=r[1]) for r in rows]


async def _delegate(fn_name: str, *args: Any) -> Any:
    mod = optional("app.geo")
    fn = getattr(mod, fn_name, None) if mod else None
    if fn is None:
        return None
    try:
        res = fn(*args)
        if inspect.isawaitable(res):
            res = await res
        import dataclasses

        if dataclasses.is_dataclass(res) and not isinstance(res, type):
            res = dataclasses.asdict(res)
        from fastapi.encoders import jsonable_encoder

        return jsonable_encoder(res)
    except Exception:
        return None


@router.get("/source-availability", response_model=AvailabilityOut)
async def source_availability(countries: str | None = Query(None, description="comma separated ISO codes; default = countries that have data"), session: AsyncSession = Depends(get_session)) -> AvailabilityOut:
    if countries:
        ccs = [country_code(c) for c in countries.split(",")[:30] if c.strip()]
    else:
        ccs = [r[0] for r in (await session.execute(text("SELECT country_code FROM organisations GROUP BY 1 ORDER BY count(*) DESC LIMIT 30"))).all()] or ["GB"]
    out = []
    for cc in ccs:
        src = await _avail(session, cc)
        out.append(CountryAvailability(country=cc, name=_name(cc), sources=src, usable=sum(1 for s in src if s.status == "available")))
    return AvailabilityOut(registry_loaded=adapter_catalog() is not None, countries=out, detail=await _delegate("source_availability", ccs))


@router.get("/coverage-plan", response_model=CoveragePlan)
async def coverage_plan(countries: str = Query(..., description="comma separated ISO codes"), session: AsyncSession = Depends(get_session)) -> CoveragePlan:
    ccs = [country_code(c) for c in countries.split(",")[:30] if c.strip()]
    if not ccs:
        raise ApiError(422, "invalid_filter", "Provide at least one country code.")
    out: list[CoverageCountry] = []
    warnings: list[str] = []
    for cc in ccs:
        src = await _avail(session, cc)
        plan = [s for s in src if s.status == "available"]
        row = (await session.execute(text(
            """SELECT count(DISTINCT o.id), count(e.id) FILTER (WHERE e.origin IN ('published','corroborated')) FROM organisations o
               LEFT JOIN email_addresses e ON e.organisation_id=o.id WHERE o.country_code=:c AND o.merged_into_id IS NULL"""), {"c": cc})).one()
        gaps = []
        if not plan:
            gaps.append("No usable source for this country; configure or enable a source.")
        gaps += [f"{s.name}: {s.note or s.status}" for s in src if s.status in ("unconfigured", "disabled")]
        if not plan:
            warnings.append(f"{cc}: no usable sources.")
        out.append(CoverageCountry(country=cc, name=_name(cc), plan=plan, existing_businesses=row[0], existing_published_emails=row[1], gaps=gaps))
    return CoveragePlan(countries=out, warnings=warnings, detail=await _delegate("plan_coverage", ccs))
