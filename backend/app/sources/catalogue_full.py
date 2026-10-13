"""Rich source catalogue for the Source Explorer: every adapter + every directory-catalogue entry in one list.

``source_catalogue()`` returns plain dicts with: id, name, countries, source_type, access_method (bulk_file | api | manual_import |
restricted), licence, cost, credentials ('none' | 'optional' | 'required') + credentials_required, refresh_frequency,
automated_allowed, industries (relevance tags), configured, health, plus provenance fields (adapter_id, catalogue_id,
info_only, live_verified, url, notes, usage).  ``search_catalogue()`` filters and ranks them with a transparent, deterministic
score (:data:`SCORE_WEIGHTS`; every result carries ``score_breakdown``).
"""
from __future__ import annotations

import re
from typing import Any

from .base import AdapterInfo, SourceAdapter, list_adapters
from .directory_catalogue import is_automation_permitted, load_catalogue
from .industry_directories import _cat_tokens

ACCESS_METHODS = ("bulk_file", "api", "manual_import", "restricted")
SCORE_WEIGHTS: dict[str, float] = {
    "country_exact": 30.0, "country_worldwide": 10.0, "industry_exact": 25.0, "industry_partial": 12.0, "industry_generic": 5.0,
    "q_name_token": 8.0, "q_id_token": 5.0, "q_industry_token": 6.0, "q_text_token": 2.0,
    "automated_allowed": 10.0, "configured": 5.0, "live_verified": 3.0, "restricted": -15.0,
}
_SOURCE_TYPE_ACCESS = {"bulk_dataset": "bulk_file", "open_data": "api", "registry": "api", "search_api": "api", "web_archive": "api",
                       "manual_import": "manual_import", "directory": "restricted", "website": "api"}
_CATALOGUE_ACCESS = {"bulk dataset": "bulk_file", "official API": "api", "licensed feed": "restricted", "manual domain discovery": "manual_import",
                     "skip": "restricted"}


def _tokens(s: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", s.lower()) if len(t) > 1]


def _access_method(info: AdapterInfo, restricted: bool) -> str:
    if restricted:
        return "restricted"
    return info.access_method or _SOURCE_TYPE_ACCESS.get(info.source_type, "api")


def _is_pseudo(a: SourceAdapter) -> bool:
    return a.__class__.__module__.endswith(".pseudo")


def _health_static(configured: bool, info: AdapterInfo, allowed: bool) -> dict[str, str]:
    if info.info_only and not allowed:
        return {"status": "info_only", "message": "catalogue entry only; no automated adapter is enabled"}
    if not allowed:
        return {"status": "restricted", "message": "automated collection not permitted"}
    if not configured:
        return {"status": "not_configured", "message": info.access_requirements[:200]}
    return {"status": "ok", "message": "configured (not probed)"}


def source_catalogue(configs: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """All sources, sorted by id.  ``configs`` maps adapter_id (or catalogue id) -> stored adapter config (decides ``configured``)."""
    configs = configs or {}
    adapters = {a.info.adapter_id: a for a in list_adapters() if not _is_pseudo(a)}
    cat = {e["id"]: e for e in load_catalogue()}
    linked: set[str] = set()
    out: list[dict[str, Any]] = []

    def build(a: SourceAdapter | None, entry: dict[str, Any] | None) -> dict[str, Any]:
        info = a.info if a else None
        cfg = (configs.get(info.adapter_id) if info else None) or (configs.get(entry["id"]) if entry else None) or {}
        if entry is not None:
            catalogue_allowed = is_automation_permitted(entry)
        else:
            catalogue_allowed = True
        has_runner = bool(info and not info.info_only and info.automated_collection_permitted)
        allowed = has_runner and catalogue_allowed if info else catalogue_allowed
        if info and info.info_only and entry is None:
            allowed = False
        restricted = not allowed
        if info is not None:
            name, countries, stype = info.name, list(info.country_coverage), info.source_type
            licence = info.licence
            cost, creds, note = info.cost, info.credentials, info.credentials_note
            refresh = info.refresh_frequency
            industries = list(info.industries) or (list(entry["categories"]) if entry else ["*"])
            url, live, notes, usage = info.source_url, info.live_verified, info.notes, info.allowed_usage
            requires_cfg = info.requires_config
        else:
            assert entry is not None
            name, countries, stype = entry["name"], list(entry["countries"]), {"directory": "directory", "association": "directory", "register": "registry"}.get(entry["kind"], "open_data")
            licence = entry["licence_notes"]
            ras = entry["recommended_access"]
            cost = "paid" if ras == "licensed feed" else "free"
            creds = "required" if entry["automated_extraction"] == "api_available" else "none"
            note, refresh = "", ""
            industries = list(entry["categories"])
            url, live, notes, usage = entry["url"], False, entry["licence_notes"], ""
            requires_cfg = False
        acc = _access_method(info, restricted) if info else (
            "restricted" if restricted else _CATALOGUE_ACCESS.get(entry["recommended_access"], "bulk_file"))
        configured = bool(allowed and (not requires_cfg or bool(cfg)))
        health = _health_static(configured, info, allowed) if info else {
            "status": "restricted" if restricted else "not_implemented", "message": entry["automated_extraction"]}
        cid = entry["id"] if entry else None
        aid = info.adapter_id if info else None
        return {
            "id": aid or cid, "adapter_id": aid, "catalogue_id": cid, "name": name, "countries": countries,
            "source_type": stype, "access_method": acc, "licence": licence, "cost": cost,
            "credentials": creds, "credentials_required": creds == "required", "credentials_note": note,
            "refresh_frequency": refresh, "automated_allowed": bool(allowed), "industries": industries,
            "configured": configured, "health": health, "info_only": bool(info.info_only) if info else True, "live_verified": live,
            "url": url, "notes": notes, "usage": usage,
            "automated_extraction": entry["automated_extraction"] if entry else None,
        }

    for e in cat.values():
        aid = e.get("adapter_id") or f"dir_{e['id']}"
        a = adapters.get(aid)
        if a is not None:
            linked.add(aid)
        out.append(build(a if e.get("adapter_id") else None, e))
    for aid, a in adapters.items():
        if aid in linked or aid.startswith("dir_"):
            continue
        out.append(build(a, None))
    out.sort(key=lambda r: r["id"])
    return out


def _score(row: dict[str, Any], *, country: str | None, toks: set[str], q: str | None) -> tuple[float, dict[str, float], bool]:
    bd: dict[str, float] = {}
    W = SCORE_WEIGHTS
    if country:
        if country in row["countries"]:
            bd["country_exact"] = W["country_exact"]
        elif "*" in row["countries"]:
            bd["country_worldwide"] = W["country_worldwide"]
        else:
            return 0.0, {}, False
    if toks:
        inds = {i.lower() for i in row["industries"]}
        if toks & inds:
            bd["industry_exact"] = W["industry_exact"]
        elif any(t in i or i in t for t in toks for i in inds if i != "*"):
            bd["industry_partial"] = W["industry_partial"]
        elif "*" in inds:
            bd["industry_generic"] = W["industry_generic"]
        else:
            return 0.0, {}, False
    if q:
        qt = _tokens(q)
        name_t, id_t = set(_tokens(row["name"])), set(_tokens(row["id"]))
        ind_t = {t for i in row["industries"] for t in _tokens(i)}
        text_t = set(_tokens(f"{row['licence']} {row['notes']} {row['source_type']}"))
        hit = False
        for t in qt:
            for key, pool in (("q_name_token", name_t), ("q_id_token", id_t), ("q_industry_token", ind_t), ("q_text_token", text_t)):
                if t in pool:
                    bd[key] = bd.get(key, 0.0) + W[key]
                    hit = True
        if not hit:
            return 0.0, {}, False
    if row["automated_allowed"]:
        bd["automated_allowed"] = W["automated_allowed"]
    else:
        bd["restricted"] = W["restricted"]
    if row["configured"]:
        bd["configured"] = W["configured"]
    if row["live_verified"]:
        bd["live_verified"] = W["live_verified"]
    return round(sum(bd.values()), 4), bd, True


def search_catalogue(
    country: str | None = None, business_type: str | None = None, access_method: str | None = None, licence: str | None = None,
    cost: str | None = None, q: str | None = None, *, configs: dict[str, dict[str, Any]] | None = None, rows: list[dict[str, Any]] | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Filter + rank.  Hard filters: country coverage, business-type relevance, access_method, licence substring, cost, free text.
    Order: score desc, then id asc -- identical input always gives identical output."""
    cc = country.upper() if country else None
    toks = _cat_tokens(business_type) if business_type else set()
    toks = {t.lower() for t in toks}
    out: list[tuple[float, str, dict[str, Any]]] = []
    for r in rows if rows is not None else source_catalogue(configs):
        if access_method and r["access_method"] != access_method:
            continue
        if cost and r["cost"] != cost:
            continue
        if licence and licence.casefold() not in (r["licence"] or "").casefold():
            continue
        score, bd, ok = _score(r, country=cc, toks=toks, q=q)
        if not ok:
            continue
        out.append((score, r["id"], {**r, "score": score, "score_breakdown": bd}))
    out.sort(key=lambda t: (-t[0], t[1]))
    res = [t[2] for t in out]
    return res[:limit] if limit else res


__all__ = ["source_catalogue", "search_catalogue", "SCORE_WEIGHTS", "ACCESS_METHODS"]
