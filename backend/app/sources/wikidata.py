"""Wikidata SPARQL adapter: organisations with an official website (P856), by country and class.

Licence: CC0 1.0 (Wikidata structured data).  Endpoint: https://query.wikidata.org/sparql -- subject to the
Wikimedia User-Agent policy (descriptive UA with contact details; we require ``LEAD_CONTACT_EMAIL``) and the
query-service limits (60 s per query, polite request rates).  Pagination is LIMIT/OFFSET over a stable ORDER BY
with GROUP BY ?item, so each organisation appears once; a 429/503 is honoured via Retry-After with a bounded
number of waits, after which the run stops (checkpointed).

Direct instances (P31) are matched by default; set config ``subclasses: true`` for the P279* closure. Class selection comes from ``query.extra['wikidata_classes']`` (list of QIDs, subclass-closure applied) or the
adapter config; default is Q4830453 (business).  There is deliberately no category->QID mapping in the taxonomy:
operators choose classes explicitly.
"""
from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx

from ..config import get_settings
from .base import (
    AdapterError,
    AdapterHealth,
    AdapterInfo,
    BusinessRecord,
    Checkpoint,
    DiscoveryQuery,
    NotConfigured,
    RatePolicy,
    SourceAdapter,
    register,
)
from .geo_filters import RecordFilter
from .urls import normalise_website

ADAPTER_ID = "wikidata"
ENDPOINT = "https://query.wikidata.org/sparql"
LICENCE = "CC0 1.0"
ATTRIBUTION = "Wikidata (CC0 1.0), https://www.wikidata.org/"
DEFAULT_CLASSES = ("Q4830453",)  # business
_QID = re.compile(r"^Q[1-9]\d{0,12}$")
_CC = re.compile(r"^[A-Z]{2}$")


def build_query(country: str, classes: list[str], *, limit: int, offset: int, active_only: bool = True, subclasses: bool = False) -> str:
    """SPARQL for one page.  The paginated sub-select only identifies items (cheap); labels/optionals are joined after.

    ``subclasses=True`` follows ``P279*`` (expensive on big classes such as 'business'; may hit the 60 s limit).
    """
    if not _CC.match(country):
        raise ValueError("country must be an ISO alpha-2 code")
    if not classes or not all(_QID.match(c) for c in classes):
        raise ValueError("classes must be Wikidata QIDs")
    values = " ".join(f"wd:{c}" for c in classes)
    active = "FILTER NOT EXISTS { ?item wdt:P576 [] . }" if active_only else ""
    cls_path = "wdt:P31/wdt:P279*" if subclasses else "wdt:P31"
    return f"""SELECT ?item (SAMPLE(?lbl) AS ?label) (SAMPLE(?site) AS ?website) (SAMPLE(?hqLbl) AS ?hq)
 (SAMPLE(?coord) AS ?coords) (SAMPLE(?em) AS ?email) (SAMPLE(?ph) AS ?phone)
WHERE {{
  {{ SELECT ?item WHERE {{
      VALUES ?cls {{ {values} }}
      ?item {cls_path} ?cls .
      ?item wdt:P17 ?ctry . ?ctry wdt:P297 "{country}" .
      ?item wdt:P856 [] .
      {active}
    }} ORDER BY ?item LIMIT {int(limit)} OFFSET {int(offset)} }}
  ?item wdt:P856 ?site .
  ?item rdfs:label ?lbl . FILTER(LANG(?lbl) = "en")
  OPTIONAL {{ ?item wdt:P159 ?hqItem . ?hqItem rdfs:label ?hqLbl . FILTER(LANG(?hqLbl) = "en") }}
  OPTIONAL {{ ?item wdt:P625 ?coord . }}
  OPTIONAL {{ ?item wdt:P968 ?em . }}
  OPTIONAL {{ ?item wdt:P1329 ?ph . }}
}}
GROUP BY ?item
ORDER BY ?item"""


_POINT = re.compile(r"Point\(\s*(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*\)")


def binding_to_record(b: dict[str, Any], country: str, observed_at: datetime) -> BusinessRecord | None:
    def val(k: str) -> str | None:
        v = b.get(k, {}).get("value")
        return v.strip() if isinstance(v, str) and v.strip() else None

    item = val("item")
    label = val("label")
    if not item or not label:
        return None
    qid = item.rsplit("/", 1)[-1]
    if not _QID.match(qid):
        return None
    site = normalise_website(val("website"))
    if site is None:
        return None
    lat = lon = None
    m = _POINT.match(val("coords") or "")
    if m:
        lon, lat = float(m.group(1)), float(m.group(2))
    email = val("email")
    if email and email.lower().startswith("mailto:"):
        email = email[7:]
    if email and not re.match(r"^[^@\s]{1,64}@[^@\s]+\.[A-Za-z]{2,}$", email):
        email = None
    phone = val("phone")
    if phone and phone.lower().startswith("tel:"):
        phone = phone[4:]
    return BusinessRecord(
        source_ref=qid, trading_name=label, status="active", country_code=country, city=val("hq"),
        website=site.url, email_published=email.lower() if email else None, phone=phone, lat=lat, lon=lon,
        source_url=f"https://www.wikidata.org/wiki/{qid}", licence=LICENCE, attribution=ATTRIBUTION,
        observed_at=observed_at, raw={"qid": qid},
    )


@register
class WikidataAdapter(SourceAdapter):
    info = AdapterInfo(
        adapter_id=ADAPTER_ID,
        name="Wikidata organisations with official website (SPARQL)",
        country_coverage=("*",),
        source_type="open_data",
        licence=LICENCE,
        source_url="https://query.wikidata.org/",
        access_requirements="Public endpoint; descriptive User-Agent with contact (set LEAD_CONTACT_EMAIL); polite rate.",
        automated_collection_permitted=True,
        rate_policy=RatePolicy(requests_per_minute=12, concurrency=1),
        fields_available=("trading_name", "website", "city", "lat", "lon", "email_published", "phone"),
        incremental_support=True,
        parser_version="1",
        requires_config=True,
        supports_radius=True,
        attribution=ATTRIBUTION,
        live_verified=False,
        notes="Optional. Coverage is skewed to larger/notable organisations. At build time the endpoint answered a trivial ASK query but the real page query timed out (65 s) through the sandbox proxy, so the query shape is UNVERIFIED; narrow classes if WDQS times out.",
    )

    def _ua(self) -> str:
        ua = self.config.get("user_agent")
        if ua:
            return str(ua)
        s = get_settings()
        if not s.contact_email:
            raise NotConfigured("Wikimedia policy needs a contact in the User-Agent: set LEAD_CONTACT_EMAIL")
        return s.user_agent.replace("set LEAD_CONTACT_EMAIL", s.contact_email)

    async def health(self) -> AdapterHealth:
        try:
            self._ua()
        except NotConfigured as e:
            return AdapterHealth(False, "not_configured", str(e))
        return AdapterHealth(True, "ok", "configured (endpoint not probed)")

    async def _fetch(self, client: httpx.AsyncClient, sparql: str) -> dict[str, Any]:
        waits = 0
        delay = float(self.config.get("min_interval_s", 5.0))
        while True:
            r = await client.get(ENDPOINT, params={"query": sparql, "format": "json"},
                                 headers={"Accept": "application/sparql-results+json", "User-Agent": self._ua()})
            if r.status_code in (429, 503):
                waits += 1
                if waits > int(self.config.get("max_waits", 3)):
                    raise AdapterError(f"Wikidata asked us to back off (HTTP {r.status_code}); stopping, resume from checkpoint")
                ra = r.headers.get("retry-after", "")
                await asyncio.sleep(min(float(ra) if ra.isdigit() else delay * 2 * waits, float(self.config.get("max_wait_s", 120))))
                continue
            if r.status_code >= 400:
                raise AdapterError(f"Wikidata HTTP {r.status_code}: {r.text[:200]}")
            return r.json()

    async def iter_businesses(
        self, query: DiscoveryQuery, checkpoint: Checkpoint | None = None
    ) -> AsyncIterator[tuple[BusinessRecord, Checkpoint]]:
        self._ua()
        if not query.countries:
            raise NotConfigured("wikidata needs at least one country in the query")
        classes = list(query.extra.get("wikidata_classes") or self.config.get("classes") or DEFAULT_CLASSES)
        page = int(self.config.get("page_size", 200))
        cp = checkpoint or {}
        ci0, off0 = int(cp.get("ci", 0)), int(cp.get("offset", 0))
        flt = RecordFilter(query)
        own = self.config.get("http_client") is None
        if own:
            from ..crawler.safe_http import safe_client
        client: httpx.AsyncClient = self.config.get("http_client") or safe_client(timeout=70.0, follow_redirects=False)
        observed = datetime.now(UTC)
        emitted = 0
        pause = float(self.config.get("min_interval_s", 5.0))
        try:
            for ci in range(ci0, len(query.countries)):
                cc = query.countries[ci].upper()
                offset = off0 if ci == ci0 else 0
                while True:
                    data = await self._fetch(client, build_query(cc, classes, limit=page, offset=offset, active_only=query.active_only,
                                                       subclasses=bool(self.config.get("subclasses", False))))
                    rows = data.get("results", {}).get("bindings", [])
                    for j, b in enumerate(rows):
                        rec = binding_to_record(b, cc, observed)
                        if rec is None:
                            continue
                        rec.adapter_id = ADAPTER_ID
                        if not (flt.geo_ok(rec) and flt.radius_ok(rec) and not flt.excluded(rec)):
                            continue
                        if (flt.kw_re is not None) and not flt.category_ok(rec):
                            continue
                        # checkpoint = position AFTER this row inside the page
                        yield rec, {"ci": ci, "offset": offset + j + 1, "page_start": offset}
                        emitted += 1
                        if query.limit and emitted >= query.limit:
                            return
                    if len(rows) < page:
                        break
                    offset += page
                    await asyncio.sleep(pause)
        finally:
            if own:
                await client.aclose()


__all__ = ["WikidataAdapter", "build_query", "binding_to_record"]
