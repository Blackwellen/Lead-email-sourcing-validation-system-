"""Shared geographic / record filtering helpers used by every adapter.

Postcode handling is structural for the UK (area / district / sector / full) and prefix-based elsewhere.
Radius filtering needs coordinates on the *record* plus a centre supplied by the operator or a licensed
gazetteer -- this module never invents coordinates.
"""
from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .base import BusinessRecord, DiscoveryQuery

# Full UK outward+inward code (BS 7666 style). Group 1 = outward code, group 2 = inward code.
UK_POSTCODE_RE = re.compile(
    r"^(GIR|[A-PR-UWYZ][A-HK-Y]?[0-9][0-9A-HJKPSTUW]?|[A-PR-UWYZ][0-9][A-HJKSTUW]|[A-PR-UWYZ][A-HK-Y][0-9][ABEHMNPRVWXY])"
    r"\s*([0-9][ABD-HJLNP-UW-Z]{2})$"
)
UK_OUTWARD_RE = re.compile(r"^[A-Z]{1,2}[0-9][A-Z0-9]?$")
UK_AREA_RE = re.compile(r"^[A-Z]{1,2}$")
UK_SECTOR_RE = re.compile(r"^[A-Z]{1,2}[0-9][A-Z0-9]?\s?[0-9]$")

# alpha2 -> regex (anchored, upper-case input). Used for validation hints and normalisation.
POSTCODE_PATTERNS: dict[str, str] = {
    "GB": UK_POSTCODE_RE.pattern,
    "IE": r"^(?:[AC-FHKNPRTV-Y][0-9]{2}|D6W)\s?[0-9AC-FHKNPRTV-Y]{4}$",  # Eircode
    "US": r"^[0-9]{5}(?:-[0-9]{4})?$",
    "CA": r"^[ABCEGHJ-NPRSTVXY][0-9][ABCEGHJ-NPRSTV-Z]\s?[0-9][ABCEGHJ-NPRSTV-Z][0-9]$",
    "AU": r"^[0-9]{4}$",
    "NZ": r"^[0-9]{4}$",
    "DE": r"^[0-9]{5}$",
    "FR": r"^(?:0[1-9]|[1-8][0-9]|9[0-8])[0-9]{3}$",
    "NL": r"^[1-9][0-9]{3}\s?[A-Z]{2}$",
    "ES": r"^(?:0[1-9]|[1-4][0-9]|5[0-2])[0-9]{3}$",
    "IT": r"^[0-9]{5}$",
}
_GENERIC_POSTCODE = r"^[A-Z0-9][A-Z0-9 -]{1,9}[A-Z0-9]$"


def postcode_regex(country: str | None) -> re.Pattern[str]:
    return re.compile(POSTCODE_PATTERNS.get((country or "").upper(), _GENERIC_POSTCODE))


def is_valid_postcode(postcode: str | None, country: str | None = "GB") -> bool:
    if not postcode:
        return False
    return bool(postcode_regex(country).match(postcode.strip().upper()))


def normalise_postcode(postcode: str | None, country: str | None = "GB") -> str | None:
    """Upper-case, collapse whitespace; UK codes get the canonical single space before the inward code."""
    if not postcode:
        return None
    pc = re.sub(r"\s+", " ", postcode.strip().upper())
    if not pc:
        return None
    if (country or "GB").upper() in ("GB", "UK"):
        m = UK_POSTCODE_RE.match(pc.replace(" ", ""))
        if m:
            return f"{m.group(1)} {m.group(2)}"
    return pc


def _nospace(s: str) -> str:
    return re.sub(r"[\s-]+", "", s.upper())


def uk_outward_code(postcode: str | None) -> str | None:
    """Outward code (district, e.g. 'SW1A') from a full or outward-only UK postcode."""
    if not postcode:
        return None
    pc = _nospace(postcode)
    m = UK_POSTCODE_RE.match(pc)
    if m:
        return m.group(1)
    if UK_OUTWARD_RE.match(pc):
        return pc
    return None


def uk_postcode_area(postcode: str | None) -> str | None:
    """Postcode area letters, e.g. 'SW1A 1AA' -> 'SW'."""
    out = uk_outward_code(postcode)
    if not out:
        return None
    m = re.match(r"^[A-Z]{1,2}", out)
    return m.group(0) if m else None


def uk_postcode_sector(postcode: str | None) -> str | None:
    if not postcode:
        return None
    pc = _nospace(postcode)
    m = UK_POSTCODE_RE.match(pc)
    return f"{m.group(1)} {m.group(2)[0]}" if m else None


def postcode_parts(postcode: str | None, country: str | None = "GB") -> dict[str, str | None]:
    """{'area','district','sector','full'} for UK; for others 'full' only."""
    cc = (country or "GB").upper()
    if cc in ("GB", "UK"):
        return {
            "area": uk_postcode_area(postcode),
            "district": uk_outward_code(postcode),
            "sector": uk_postcode_sector(postcode),
            "full": normalise_postcode(postcode, "GB"),
        }
    return {"area": None, "district": None, "sector": None, "full": normalise_postcode(postcode, cc)}


def classify_uk_selector(selector: str) -> str | None:
    """'area' | 'district' | 'sector' | 'full' | None for a UK postcode selector string."""
    s = re.sub(r"\s+", " ", selector.strip().upper())
    if not s:
        return None
    if UK_AREA_RE.match(s):
        return "area"
    if UK_OUTWARD_RE.match(s):
        return "district"
    if UK_POSTCODE_RE.match(_nospace(s)):
        return "full"
    if UK_SECTOR_RE.match(s) and " " in s:
        return "sector"
    return None


def postcode_matches(postcode: str | None, selectors: Iterable[str], country: str | None = "GB") -> bool:
    """True if ``postcode`` falls in any selector (UK: area/district/sector/full; else prefix)."""
    if not postcode:
        return False
    cc = (country or "GB").upper()
    sels = [s for s in selectors if s and s.strip()]
    if not sels:
        return True
    if cc in ("GB", "UK"):
        parts = postcode_parts(postcode, "GB")
        for sel in sels:
            kind = classify_uk_selector(sel)
            norm = re.sub(r"\s+", " ", sel.strip().upper())
            if kind == "area" and parts["area"] == norm:
                return True
            if kind == "district" and parts["district"] == norm:
                return True
            if kind == "sector" and parts["sector"] == norm:
                return True
            if kind == "full" and parts["full"] == normalise_postcode(norm, "GB"):
                return True
        return False
    pc = _nospace(postcode)
    return any(pc.startswith(_nospace(s)) for s in sels)


# --------------------------------------------------------------------------- distance
def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def within_radius(lat: float | None, lon: float | None, centre_lat: float, centre_lon: float, radius_km: float) -> bool:
    if lat is None or lon is None:
        return False
    return haversine_km(lat, lon, centre_lat, centre_lon) <= radius_km


# --------------------------------------------------------------------------- text helpers
def _fold(s: str) -> str:
    return re.sub(r"\s+", " ", s.casefold()).strip()


def compile_term_regex(terms: Sequence[str]) -> re.Pattern[str] | None:
    """Word-boundary, case-insensitive alternation of ``terms`` (None if no usable terms)."""
    cleaned = sorted({_fold(t) for t in terms if t and t.strip()}, key=len, reverse=True)
    if not cleaned:
        return None
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(t) for t in cleaned) + r")(?![a-z0-9])", re.I)


def sic_matches(record_sics: Iterable[str], wanted: Iterable[str]) -> bool:
    """Exact match, or prefix match when the wanted code is shorter than 5 digits (e.g. '681' / '68')."""
    rec = [re.sub(r"\D", "", s) for s in record_sics if s]
    for w in wanted:
        wd = re.sub(r"\D", "", w or "")
        if not wd:
            continue
        if any(r == wd or (len(wd) < 5 and r.startswith(wd)) for r in rec):
            return True
    return False


def _fold_place(s: str | None) -> str:
    return _fold(s or "")


class RecordFilter:
    """Compiles a :class:`DiscoveryQuery` into predicates over :class:`BusinessRecord`.

    Semantics (documented contract for adapters):
      * country: record country must be in ``query.countries`` (when given).
      * geography: union of postcode selectors, localities (city/county/region equality) and regions.
        If none are given the geography is unconstrained.
      * category: ``extra['category_match']`` = 'any' (default; SIC *or* name keyword) | 'all' | 'sic' | 'keywords'.
        Unconstrained when the query has neither SIC codes nor keywords/synonyms.
      * exclusions: any exclusion term in the name rejects the record.
      * radius: needs centre + radius; records without coordinates are rejected unless
        ``extra['radius_keep_unlocated']`` is true.
    """

    def __init__(self, query: DiscoveryQuery):
        self.q = query
        self.countries = {c.upper() for c in query.countries}
        self.postcodes = [p for p in query.postcodes if p and p.strip()]
        self.localities = {_fold_place(x) for x in query.localities if x and x.strip()}
        self.regions = {_fold_place(x) for x in query.regions if x and x.strip()}
        self.kw_re = compile_term_regex(list(query.keywords) + list(query.synonyms))
        self.ex_re = compile_term_regex(query.exclusions)
        self.sics = [s for s in query.sic_codes if s]
        self.mode = str(query.extra.get("category_match", "any"))
        self.keep_unlocated = bool(query.extra.get("radius_keep_unlocated", False))
        self.has_radius = None not in (query.centre_lat, query.centre_lon, query.radius_km)

    # -- parts
    def country_ok(self, rec: BusinessRecord) -> bool:
        return not self.countries or (rec.country_code or "").upper() in self.countries

    def geo_ok(self, rec: BusinessRecord) -> bool:
        if not (self.postcodes or self.localities or self.regions):
            return True
        if self.postcodes and postcode_matches(rec.postal_code, self.postcodes, rec.country_code):
            return True
        city, region = _fold_place(rec.city), _fold_place(rec.region)
        if self.localities and (city in self.localities or region in self.localities):
            return True
        return bool(self.regions and (region in self.regions or city in self.regions))

    def category_ok(self, rec: BusinessRecord) -> bool:
        if not self.sics and not self.kw_re:
            return True
        sic_hit = bool(self.sics) and sic_matches(rec.sic_codes, self.sics)
        name = " ".join(x for x in (rec.legal_name, rec.trading_name) if x)
        kw_hit = bool(self.kw_re) and bool(self.kw_re.search(name))
        mode = self.mode
        if mode == "sic":
            return sic_hit or not self.sics
        if mode == "keywords":
            return kw_hit or not self.kw_re
        if mode == "all":
            return (sic_hit or not self.sics) and (kw_hit or not self.kw_re)
        return sic_hit or kw_hit

    def excluded(self, rec: BusinessRecord) -> bool:
        if not self.ex_re:
            return False
        name = " ".join(x for x in (rec.legal_name, rec.trading_name) if x)
        return bool(self.ex_re.search(name))

    def active_ok(self, rec: BusinessRecord) -> bool:
        if not self.q.active_only:
            return True
        st = (rec.status or "active").strip().lower()
        return st.startswith("active") or st in ("open", "live", "registered", "")

    def radius_ok(self, rec: BusinessRecord) -> bool:
        if not self.has_radius:
            return True
        if rec.lat is None or rec.lon is None:
            return self.keep_unlocated
        assert self.q.centre_lat is not None and self.q.centre_lon is not None and self.q.radius_km is not None
        return haversine_km(rec.lat, rec.lon, self.q.centre_lat, self.q.centre_lon) <= self.q.radius_km

    def accept(self, rec: BusinessRecord) -> bool:
        return (
            self.country_ok(rec)
            and self.active_ok(rec)
            and self.geo_ok(rec)
            and self.category_ok(rec)
            and not self.excluded(rec)
            and self.radius_ok(rec)
        )
