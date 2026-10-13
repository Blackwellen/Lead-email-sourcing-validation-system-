"""Persist :class:`BusinessRecord` into ``organisations`` with canonical-identity upsert semantics."""
from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import case, func, literal, literal_column
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Organisation, OrganisationDomain, OrganisationSource, RunOrganisation
from .base import SOURCE_TYPE_PRIORITY, BusinessRecord, adapter_infos
from .geo_filters import normalise_postcode
from .urls import normalise_website

_LEGAL_SUFFIXES = {
    "ltd", "limited", "llp", "plc", "lp", "llc", "inc", "incorporated", "corp", "corporation", "co", "company",
    "gmbh", "ag", "bv", "nv", "sarl", "sas", "sa", "srl", "spa", "ab", "as", "oy", "pty", "pvt", "cic", "cio",
}
_STOP_LEADING = {"the"}
_ALNUM = re.compile(r"[^a-z0-9]+")


def normalise_name(name: str | None) -> str:
    """Lower-case, accent-fold, '&'->'and', strip punctuation, drop legal-form suffixes and a leading 'the'."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold().replace("&", " and ")
    s = s.replace("'", "").replace("’", "")
    toks = [t for t in _ALNUM.split(s) if t]
    while toks and toks[0] in _STOP_LEADING:
        toks.pop(0)
    while len(toks) > 1 and toks[-1] in _LEGAL_SUFFIXES:
        toks.pop()
    return " ".join(toks)


def _norm_company_number(n: str | None) -> str | None:
    if not n:
        return None
    s = re.sub(r"\s+", "", n).upper()
    return s or None


def canonical_identity(rec: BusinessRecord) -> str:
    """country + company_number when present, else normalised name + postcode (or city).

    Formats: ``GB:cn:00000006`` / ``GB:nm:smith and sons:sw1a1aa``.
    """
    cc = (rec.country_code or "").upper()
    cn = _norm_company_number(rec.company_number)
    if cn:
        return f"{cc}:cn:{cn}"[:500]
    name = normalise_name(rec.legal_name) or normalise_name(rec.trading_name)
    if not name:
        raise ValueError("record has neither company_number nor a name; cannot build an identity")
    pc = normalise_postcode(rec.postal_code, cc)
    place = re.sub(r"\s+", "", pc).lower() if pc else normalise_name(rec.city)
    return f"{cc}:nm:{name}:{place or '-'}"[:500]


def _rank_expr():
    """SQL expression: trust rank of the existing row's discovery_source (lower = better)."""
    mapping = {i.adapter_id: i.default_priority for i in adapter_infos()}
    if not mapping:
        return literal(1000)
    return case(mapping, value=Organisation.discovery_source, else_=1000)


def _clean(s: str | None, maxlen: int | None = None) -> str | None:
    if s is None:
        return None
    s = re.sub(r"[\x00-\x08\x0b-\x1f]", "", str(s)).strip()
    if not s:
        return None
    return s[:maxlen] if maxlen else s


def _rec_rank(rec: BusinessRecord) -> int:
    for i in adapter_infos():
        if i.adapter_id == rec.adapter_id:
            return i.default_priority
    return SOURCE_TYPE_PRIORITY["manual_import"]


async def upsert_business(
    session: AsyncSession, rec: BusinessRecord, *, run_id: int | None = None, category_id: int | None = None
) -> tuple[int, bool]:
    """Insert or merge ``rec``; returns ``(organisation_id, created)``.

    Merge rule per column: keep the existing non-empty value unless the incoming record comes from a source
    of equal or better trust rank (lower priority number) -- then a non-null incoming value wins.  SIC codes
    are unioned.  A website produces a CANDIDATE domain with status 'unknown' (never confirmed here).
    """
    cc = (rec.country_code or "").upper()
    if len(cc) != 2:
        raise ValueError("country_code (ISO alpha-2) is required")
    identity = canonical_identity(rec)
    rank = _rec_rank(rec)
    vals: dict[str, Any] = dict(
        legal_name=_clean(rec.legal_name, 400),
        trading_name=_clean(rec.trading_name, 400),
        company_number=_norm_company_number(rec.company_number),
        legal_type=_clean(rec.legal_type, 100),
        status=_clean(rec.status, 60),
        country_code=cc,
        region=_clean(rec.region, 120),
        city=_clean(rec.city, 120),
        postal_code=normalise_postcode(rec.postal_code, cc) if rec.postal_code else None,
        address=_clean(rec.address),
        phone=_clean(rec.phone, 60),
        category_id=category_id,
        sic_codes=sorted({s for s in (re.sub(r"\D", "", x) for x in rec.sic_codes) if s}),
        canonical_identity=identity,
        discovery_source=rec.adapter_id or None,
        source_ref=_clean(rec.source_ref, 300),
    )
    ins = pg_insert(Organisation).values(**vals)
    ex = ins.excluded
    existing_rank = _rank_expr()
    better = literal(rank) <= existing_rank

    def merge(col: str):
        cur = getattr(Organisation, col)
        new = getattr(ex, col)
        return case(
            (new.is_(None), cur),
            (cur.is_(None), new),
            (better, new),
            else_=cur,
        )

    set_: dict[str, Any] = {c: merge(c) for c in (
        "legal_name", "trading_name", "company_number", "legal_type", "status", "region", "city", "postal_code",
        "address", "phone", "category_id", "source_ref",
    )}
    set_["discovery_source"] = case((Organisation.discovery_source.is_(None), ex.discovery_source), (better, ex.discovery_source),
                                    else_=Organisation.discovery_source)
    set_["sic_codes"] = literal_column(
        "(SELECT COALESCE(jsonb_agg(DISTINCT to_jsonb(x) ORDER BY to_jsonb(x)), '[]'::jsonb) "
        "FROM jsonb_array_elements_text(organisations.sic_codes || excluded.sic_codes) AS x)"
    )
    set_["updated_at"] = func.now()
    stmt = ins.on_conflict_do_update(index_elements=["canonical_identity"], set_=set_).returning(
        Organisation.id, literal_column("(xmax = 0)").label("inserted")
    )
    row = (await session.execute(stmt)).one()
    org_id, created = int(row.id), bool(row.inserted)

    now = datetime.now(UTC)
    site = normalise_website(rec.website) if rec.website else None
    await _upsert_source(session, org_id, rec, run_id, site.url if site else None, now)
    if run_id is not None:
        await session.execute(
            pg_insert(RunOrganisation).values(run_id=run_id, organisation_id=org_id, stage="discovered")
            .on_conflict_do_nothing()
        )
    if site is not None:
        await _upsert_candidate_domain(session, org_id, rec, site, now)
    return org_id, created


async def _upsert_source(
    session: AsyncSession, org_id: int, rec: BusinessRecord, run_id: int | None, website: str | None, now: datetime
) -> None:
    raw = _small_raw(rec.raw)
    ins = pg_insert(OrganisationSource).values(
        organisation_id=org_id, adapter_id=rec.adapter_id or "unknown", source_ref=(rec.source_ref or "")[:300],
        source_url=rec.source_url, licence=_clean(rec.licence, 200), attribution=rec.attribution,
        observed_at=rec.observed_at or now, website=website, email_published=_clean(rec.email_published, 320),
        lat=rec.lat, lon=rec.lon, run_id=run_id, raw=raw,
    )
    ex = ins.excluded
    await session.execute(
        ins.on_conflict_do_update(
            constraint="uq_orgsource",
            set_={
                "source_url": func.coalesce(ex.source_url, OrganisationSource.source_url),
                "licence": func.coalesce(ex.licence, OrganisationSource.licence),
                "attribution": func.coalesce(ex.attribution, OrganisationSource.attribution),
                "observed_at": func.coalesce(ex.observed_at, OrganisationSource.observed_at),
                "website": func.coalesce(ex.website, OrganisationSource.website),
                "email_published": func.coalesce(ex.email_published, OrganisationSource.email_published),
                "lat": func.coalesce(ex.lat, OrganisationSource.lat),
                "lon": func.coalesce(ex.lon, OrganisationSource.lon),
                "run_id": func.coalesce(ex.run_id, OrganisationSource.run_id),
                "raw": ex.raw,
                "last_seen_at": func.now(),
            },
        )
    )


def _small_raw(raw: dict[str, Any], limit: int = 4000) -> dict[str, Any]:
    import json

    try:
        s = json.dumps(raw, default=str)
    except (TypeError, ValueError):
        return {}
    if len(s) <= limit:
        return json.loads(s)
    return {"truncated": True}


async def _upsert_candidate_domain(session: AsyncSession, org_id: int, rec: BusinessRecord, site, now: datetime) -> None:
    evidence = [{
        "type": "source_candidate", "adapter": rec.adapter_id, "source_ref": rec.source_ref,
        "url": site.url, "observed_at": now.isoformat(),
        "note": "website published by the source dataset; NOT verified",
    }]
    ins = pg_insert(OrganisationDomain).values(
        organisation_id=org_id, domain=site.host, registrable_domain=site.registrable, website_url=site.url,
        verified_match_status="unknown", confidence=0.0, evidence=evidence,
    )
    # Existing rows (possibly already verified by the identity engine) are never downgraded or upgraded here.
    await session.execute(
        ins.on_conflict_do_update(
            constraint="uq_orgdomain",
            set_={"website_url": func.coalesce(OrganisationDomain.website_url, ins.excluded.website_url),
                  "updated_at": func.now()},
        )
    )


__all__ = ["canonical_identity", "normalise_name", "upsert_business"]
