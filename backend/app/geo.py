"""Geography: ISO country/subdivision data (pycountry), address hints, UK starter geography, coverage planning
and per-country source availability.

No boundaries, centroids or radii are fabricated.  Radius filtering (:func:`haversine_km`, ``RecordFilter``) only works on
records that carry their own lat/lon, with a centre supplied by the operator or a licensed gazetteer.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import pycountry

from .sources.geo_filters import (
    POSTCODE_PATTERNS,
    classify_uk_selector,
    haversine_km,
    normalise_postcode,
)

UK_GEO_PATH = Path(__file__).resolve().parent / "data" / "uk_geography.json"
__all__ = [
    "list_countries", "get_country", "list_subdivisions", "language_hints", "address_format", "source_availability",
    "load_uk_geography", "expand_selection", "plan_coverage", "parse_locality_list", "haversine_km", "CoveragePlan",
]


# --------------------------------------------------------------------------- countries / subdivisions
@lru_cache(maxsize=1)
def _countries() -> tuple[dict[str, Any], ...]:
    out = []
    for c in pycountry.countries:
        out.append({
            "alpha2": c.alpha_2, "alpha3": c.alpha_3, "numeric": c.numeric, "name": c.name,
            "official_name": getattr(c, "official_name", None), "common_name": getattr(c, "common_name", None),
        })
    return tuple(sorted(out, key=lambda d: d["name"]))


def list_countries() -> list[dict[str, Any]]:
    """All ISO 3166-1 countries: alpha2, alpha3, numeric, name, official_name, common_name."""
    return [dict(c) for c in _countries()]


def get_country(code: str) -> dict[str, Any] | None:
    code = (code or "").upper()
    for c in _countries():
        if code in (c["alpha2"], c["alpha3"]):
            return dict(c)
    return None


@lru_cache(maxsize=512)
def _subdivisions(cc: str) -> tuple[dict[str, Any], ...]:
    subs = pycountry.subdivisions.get(country_code=cc) or []
    return tuple(
        {"code": s.code, "name": s.name, "type": s.type, "parent": s.parent_code} for s in sorted(subs, key=lambda x: x.code)
    )


def list_subdivisions(country_code: str) -> list[dict[str, Any]]:
    """ISO 3166-2 subdivisions: code, name, type, parent (parent subdivision code or None)."""
    cc = (country_code or "").upper()
    if get_country(cc) is None:
        raise KeyError(f"unknown country {country_code!r}")
    return [dict(s) for s in _subdivisions(cc)]


# --------------------------------------------------------------------------- language / address hints
_LANGS: dict[str, list[str]] = {
    "GB": ["en", "cy", "gd"], "IE": ["en", "ga"], "US": ["en", "es"], "CA": ["en", "fr"], "AU": ["en"], "NZ": ["en", "mi"],
    "DE": ["de"], "FR": ["fr"], "NL": ["nl"], "BE": ["nl", "fr", "de"], "ES": ["es", "ca", "gl", "eu"], "PT": ["pt"],
    "IT": ["it"], "AT": ["de"], "CH": ["de", "fr", "it", "rm"], "SE": ["sv"], "NO": ["no", "nb", "nn"], "DK": ["da"],
    "FI": ["fi", "sv"], "PL": ["pl"], "CZ": ["cs"], "SK": ["sk"], "HU": ["hu"], "RO": ["ro"], "BG": ["bg"], "GR": ["el"],
    "HR": ["hr"], "SI": ["sl"], "EE": ["et"], "LV": ["lv"], "LT": ["lt"], "LU": ["lb", "fr", "de"], "MT": ["mt", "en"],
    "CY": ["el", "tr"], "IS": ["is"], "TR": ["tr"], "UA": ["uk"], "RU": ["ru"], "IN": ["en", "hi"], "PK": ["ur", "en"],
    "BD": ["bn"], "CN": ["zh"], "JP": ["ja"], "KR": ["ko"], "SG": ["en", "zh", "ms", "ta"], "MY": ["ms", "en"],
    "ID": ["id"], "PH": ["fil", "en"], "TH": ["th"], "VN": ["vi"], "AE": ["ar", "en"], "SA": ["ar"], "IL": ["he", "ar"],
    "EG": ["ar"], "ZA": ["en", "af", "zu", "xh"], "NG": ["en"], "KE": ["en", "sw"], "GH": ["en"], "BR": ["pt"],
    "MX": ["es"], "AR": ["es"], "CL": ["es"], "CO": ["es"], "PE": ["es"],
}


def language_hints(country_code: str) -> list[str]:
    """ISO 639-1-ish language codes commonly used for business content (hint for crawler/Accept-Language)."""
    return list(_LANGS.get(country_code.upper(), []))


_ADDR: dict[str, dict[str, Any]] = {
    "GB": {"template": "{line1}, {line2}, {city}, {county}, {postcode}", "postcode_example": "SW1A 1AA", "postcode_position": "after_city"},
    "IE": {"template": "{line1}, {line2}, {city}, {county}, {postcode}", "postcode_example": "D02 X285", "postcode_position": "after_county"},
    "US": {"template": "{line1}, {city}, {state} {postcode}", "postcode_example": "90210", "postcode_position": "after_state"},
    "CA": {"template": "{line1}, {city} {province} {postcode}", "postcode_example": "K1A 0B1", "postcode_position": "after_province"},
    "AU": {"template": "{line1}, {city} {state} {postcode}", "postcode_example": "2000", "postcode_position": "after_state"},
    "NZ": {"template": "{line1}, {suburb}, {city} {postcode}", "postcode_example": "6011", "postcode_position": "after_city"},
    "DE": {"template": "{line1}, {postcode} {city}", "postcode_example": "10115", "postcode_position": "before_city"},
    "FR": {"template": "{line1}, {postcode} {city}", "postcode_example": "75001", "postcode_position": "before_city"},
    "NL": {"template": "{line1}, {postcode} {city}", "postcode_example": "1012 AB", "postcode_position": "before_city"},
    "ES": {"template": "{line1}, {postcode} {city} ({province})", "postcode_example": "28001", "postcode_position": "before_city"},
    "IT": {"template": "{line1}, {postcode} {city} {province}", "postcode_example": "00184", "postcode_position": "before_city"},
}
_GENERIC_ADDR = {"template": "{line1}, {city}, {postcode}", "postcode_example": None, "postcode_position": "unknown"}


def address_format(country_code: str) -> dict[str, Any]:
    cc = country_code.upper()
    d = dict(_ADDR.get(cc, _GENERIC_ADDR))
    d["country_code"] = cc
    d["postcode_regex"] = POSTCODE_PATTERNS.get(cc)
    d["generic"] = cc not in _ADDR
    return d


# --------------------------------------------------------------------------- source availability
def source_availability(country_code: str) -> list[dict[str, Any]]:
    """Honest per-country view of sources from the adapter registry + the directory catalogue.

    status: ``ready`` (works without config) | ``needs_config`` | ``info_only`` (catalogue/pseudo entry) |
    ``not_permitted`` (catalogue says automated collection is not allowed) | ``not_implemented`` (open dataset
    listed, no adapter yet).  ``official_dataset`` marks official/government bulk or register datasets.
    """
    from .sources.base import list_adapters
    from .sources.geofabrik_regions import extract_paths

    cc = country_code.upper()
    out: list[dict[str, Any]] = []
    for a in list_adapters():
        i = a.info
        if not i.covers(cc):
            continue
        entry: dict[str, Any] = {
            "adapter_id": i.adapter_id, "name": i.name, "source_type": i.source_type, "priority": i.default_priority,
            "licence": i.licence, "official_dataset": i.source_type in ("bulk_dataset", "registry") and not i.info_only,
            "live_verified": i.live_verified,
        }
        if i.adapter_id.startswith("dir_"):
            # catalogue entry
            from .sources.directory_catalogue import get_entry

            e = get_entry(i.adapter_id[4:])
            st = e["automated_extraction"]
            entry["status"] = {
                "allowed_open_data": "not_implemented", "api_available": "info_only", "unknown_review_terms": "info_only",
                "permission_required": "permission_required",
            }.get(st, "not_permitted")
            entry["official_dataset"] = e["kind"] in ("bulk_dataset", "register") or e["kind"] == "open_data"
            entry["notes"] = f"[{st}] recommended: {e['recommended_access']}. {e['licence_notes']}"
        elif i.info_only:
            entry["status"], entry["notes"] = "info_only", i.notes
        elif i.requires_config:
            entry["status"], entry["notes"] = "needs_config", i.access_requirements
        else:
            entry["status"], entry["notes"] = "ready", i.notes or i.access_requirements
        if i.adapter_id == "osm_geofabrik":
            paths = extract_paths(cc)
            entry["notes"] = (f"Geofabrik extract(s) {paths} (path unverified). " if paths else
                              "No Geofabrik extract known for this country in the built-in table; supply a file. ") + entry["notes"]
            if not paths:
                entry["status"] = "needs_config"
        out.append(entry)
    out.sort(key=lambda d: (d["priority"], d["adapter_id"]))
    return out


# --------------------------------------------------------------------------- UK geography
@lru_cache(maxsize=1)
def load_uk_geography() -> dict[str, Any]:
    with open(UK_GEO_PATH, encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=1)
def _uk_index() -> dict[str, Any]:
    g = load_uk_geography()
    return {
        "areas": {a["code"]: a for a in g["postcode_areas"]},
        "cities": {c["name"].casefold(): c for c in g["cities"]},
        "las": {x["name"].casefold(): x for x in g["local_authorities"]},
        "regions": {r["id"]: r for r in g["regions"]},
        "region_names": {r["name"].casefold(): r["id"] for r in g["regions"]},
        "nations": {n["id"]: n for n in g["nations"]},
        "nation_names": {n["name"].casefold(): n["id"] for n in g["nations"]},
    }


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.casefold()).strip("-")


@dataclass
class Expansion:
    postcode_areas: list[str] = field(default_factory=list)
    postcode_selectors: list[str] = field(default_factory=list)  # districts / sectors / full, passed through
    localities: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def expand_selection(selection: dict[str, Any]) -> Expansion:
    """Expand a selection into postcode areas.

    Keys (all optional lists): ``nations``, ``regions``, ``cities``, ``local_authorities``, ``postcode_areas``,
    ``postcodes`` (area/district/sector/full), ``localities`` (free text).  Areas overlapping a nation/region
    boundary are included when they belong to it primarily *or* secondarily, and a note flags this.
    """
    ix = _uk_index()
    exp = Expansion()
    areas: dict[str, None] = {}
    spans: set[str] = set()

    def add(code: str) -> None:
        areas.setdefault(code, None)

    for n in selection.get("nations", []):
        nid = ix["nation_names"].get(str(n).casefold()) or _slug(str(n))
        if nid not in ix["nations"]:
            exp.unknown.append(str(n))
            continue
        for a in ix["areas"].values():
            if a["nation"] == nid:
                add(a["code"])
            elif nid in a["also_nations"]:
                add(a["code"])
                spans.add(a["code"])
    for r in selection.get("regions", []):
        rid = ix["region_names"].get(str(r).casefold()) or _slug(str(r))
        if rid not in ix["regions"]:
            exp.unknown.append(str(r))
            continue
        for a in ix["areas"].values():
            if a["region"] == rid:
                add(a["code"])
            elif rid in a["also_regions"]:
                add(a["code"])
                spans.add(a["code"])
    for c in selection.get("cities", []):
        city = ix["cities"].get(str(c).casefold())
        if not city:
            exp.unknown.append(str(c))
            continue
        for code in city["postcode_areas"]:
            add(code)
        exp.localities.append(city["name"])
    for la in selection.get("local_authorities", []):
        item = ix["las"].get(str(la).casefold())
        if not item:
            exp.unknown.append(str(la))
            continue
        exp.localities.append(item["name"])
        exp.notes.append(f"{item['name']}: no postcode-area mapping is shipped; matched by locality name only (imprecise)")
    for code in selection.get("postcode_areas", []):
        c = str(code).strip().upper()
        if c in ix["areas"]:
            add(c)
        else:
            exp.unknown.append(str(code))
    for pc in selection.get("postcodes", []):
        kind = classify_uk_selector(str(pc))
        p = re.sub(r"\s+", " ", str(pc).strip().upper())
        if kind == "area" and p in ix["areas"]:
            add(p)
        elif kind in ("district", "sector", "full"):
            exp.postcode_selectors.append(p)
        else:
            exp.unknown.append(str(pc))
    exp.localities += [str(x).strip() for x in selection.get("localities", []) if str(x).strip()]
    exp.postcode_areas = sorted(areas)
    if spans:
        exp.notes.append(f"areas overlapping the selection boundary were included: {', '.join(sorted(spans))}")
    exp.localities = list(dict.fromkeys(exp.localities))
    return exp


@dataclass
class CoveragePlan:
    strategy: str  # national_bulk_filter | regional_bulk_filter | bulk_filter_with_few_localities | locality_queries
    postcode_areas: list[str]
    postcode_selectors: list[str]
    localities: list[str]
    share_of_uk_areas: float
    bulk_passes: int
    per_locality_queries_allowed: bool
    max_search_queries: int
    avoided_queries_estimate: int
    recommended_adapters: list[str]
    notes: list[str]
    unknown: list[str]

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)


BROAD_AREA_THRESHOLD = 10  # selecting more than this many postcode areas counts as broad
BROAD_SHARE = 0.08
MAX_LOCALITIES_FOR_QUERIES = 8


def plan_coverage(selection: dict[str, Any], *, keywords: int = 1, search_query_cap: int = 20) -> CoveragePlan:
    """Recommend how to cover a UK selection.

    Bulk sources (Companies House snapshot, OSM extract) are always ONE streaming pass with all postcode / locality
    filters applied -- cost does not grow with the number of localities.  Per-locality queries only make sense for
    metered or politely rate-limited sources (search APIs, Wikidata), and are allowed only for narrow selections;
    broad selections are steered to ``national_bulk_filter`` so thousands of redundant locality queries are never generated.
    """
    exp = expand_selection(selection)
    total = len(load_uk_geography()["postcode_areas"])
    n_areas = len(exp.postcode_areas)
    share = n_areas / total if total else 0.0
    n_places = len(exp.localities) + len(exp.postcode_selectors)
    naive = max(1, max(n_areas, n_places)) * max(1, keywords)
    notes = list(exp.notes)
    if not (n_areas or n_places):
        strategy, allowed = "national_bulk_filter", False
        notes.append("empty selection: treated as the whole country; use one national bulk pass")
        share = 1.0
    elif n_areas > BROAD_AREA_THRESHOLD or share > BROAD_SHARE or n_places > MAX_LOCALITIES_FOR_QUERIES:
        strategy, allowed = ("national_bulk_filter" if share > 0.5 else "regional_bulk_filter"), False
        notes.append(f"broad coverage ({n_areas} postcode areas): use ONE bulk pass per dataset with postcode filters, "
                     "not per-locality queries")
    elif n_places > 3:
        strategy, allowed = "bulk_filter_with_few_localities", True
    else:
        strategy, allowed = "locality_queries", True
        notes.append("narrow selection: bulk filter is still preferred; locality queries are acceptable for search/Wikidata sources")
    cap = min(search_query_cap, naive) if allowed else 0
    if not allowed:
        notes.append("search-API discovery is disabled for this plan; use bulk datasets and enrich domains via the website stage")
    return CoveragePlan(
        strategy=strategy, postcode_areas=exp.postcode_areas, postcode_selectors=exp.postcode_selectors,
        localities=exp.localities, share_of_uk_areas=round(share, 3), bulk_passes=2,  # companies house + osm
        per_locality_queries_allowed=allowed, max_search_queries=cap, avoided_queries_estimate=0 if allowed else naive,
        recommended_adapters=["companies_house_bulk", "osm_geofabrik"] + (["wikidata"] if allowed or share > 0.5 else []),
        notes=notes, unknown=exp.unknown,
    )


# --------------------------------------------------------------------------- custom locality lists
MAX_LOCALITY_ENTRIES = 5000
MAX_LOCALITY_LEN = 100
_UK_PC_FULL = re.compile(r"^[A-Z]{1,2}[0-9][A-Z0-9]?\s*[0-9][A-Z]{2}$")


def parse_locality_list(text: str, country: str = "GB") -> list[dict[str, Any]]:
    """Parse operator text (commas / semicolons / newlines) into typed entries.

    kinds: postcode_area | postcode_district | postcode_sector | postcode_full | city | local_authority | region |
    nation | locality (free text, unmatched).  Control characters are stripped, lengths and counts are capped, duplicates removed.
    """
    ix = _uk_index() if country.upper() == "GB" else None
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in re.split(r"[\n\r;,]+", text or ""):
        s = re.sub(r"[\x00-\x1f\x7f]", "", raw).strip()
        if not s:
            continue
        if len(s) > MAX_LOCALITY_LEN:
            raise ValueError(f"locality entry too long: {s[:30]!r}...")
        key = s.casefold()
        if key in seen:
            continue
        seen.add(key)
        if len(out) >= MAX_LOCALITY_ENTRIES:
            raise ValueError("too many locality entries")
        entry: dict[str, Any] = {"raw": s, "kind": "locality", "value": s}
        if ix is not None:
            up = re.sub(r"\s+", " ", s.upper())
            kind = classify_uk_selector(up)
            if kind == "area" and up in ix["areas"]:
                entry.update(kind="postcode_area", value=up)
            elif kind == "district":
                entry.update(kind="postcode_district", value=up)
            elif kind == "sector":
                entry.update(kind="postcode_sector", value=up)
            elif kind == "full" and _UK_PC_FULL.match(up):
                entry.update(kind="postcode_full", value=normalise_postcode(up, "GB"))
            elif key in ix["cities"]:
                entry.update(kind="city", value=ix["cities"][key]["name"])
            elif key in ix["las"]:
                entry.update(kind="local_authority", value=ix["las"][key]["name"])
            elif key in ix["region_names"]:
                entry.update(kind="region", value=ix["regions"][ix["region_names"][key]]["name"])
            elif key in ix["nation_names"]:
                entry.update(kind="nation", value=ix["nations"][ix["nation_names"][key]]["name"])
        out.append(entry)
    return out


def selection_from_locality_entries(entries: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Turn :func:`parse_locality_list` output into an :func:`expand_selection` / :func:`plan_coverage` selection."""
    sel: dict[str, list[str]] = {k: [] for k in ("nations", "regions", "cities", "local_authorities", "postcode_areas", "postcodes", "localities")}
    m = {"nation": "nations", "region": "regions", "city": "cities", "local_authority": "local_authorities",
         "postcode_area": "postcode_areas", "postcode_district": "postcodes", "postcode_sector": "postcodes",
         "postcode_full": "postcodes", "locality": "localities"}
    for e in entries:
        sel[m[e["kind"]]].append(e["value"])
    return sel
