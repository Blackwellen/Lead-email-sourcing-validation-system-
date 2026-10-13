"""USAspending.gov -- federal contract awards by recipient (API v2, no key).

Endpoint ``POST https://api.usaspending.gov/api/v2/search/spending_by_award/`` (verified live: filters
``award_type_codes`` + ``time_period``, field list incl. ``Recipient UEI`` / ``Recipient Location`` / ``NAICS`` / ``PSC``,
``page_metadata.hasNext`` paging).  Licence: USAspending data is U.S. Government work in the public domain; the site
requires no registration or API key.  The API documents a request-size cap (``limit`` <= 100) and asks for reasonable
use; we pace requests (default 1/s) and back off on 429/5xx.

Each (award, recipient) yields a BusinessRecord (identity = name + ZIP; the UEI is kept as an identifier, it is not a
company-register number) and a ``contract_award`` signal sourced at the USAspending award page.

Offline mode: ``path`` may point to a saved API response (``{"results": [...]}``) or a JSON array of result rows.
The bulk-download CSV archives are NOT implemented (format not verified here).
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ._http import PoliteClient
from ._stream import iter_json_array, open_member, parse_dt
from .award_common import Established, drive_awards
from .base import (
    AdapterHealth,
    AdapterInfo,
    BusinessRecord,
    Checkpoint,
    DiscoveryQuery,
    NotConfigured,
    RatePolicy,
    RetryPolicy,
    SourceAdapter,
    register,
)
from .geo_filters import RecordFilter, normalise_postcode
from .signals_util import make_signal

ADAPTER_ID = "usaspending"
SEARCH_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
LICENCE = "Public domain (U.S. Government work); USAspending.gov open data"
ATTRIBUTION = "Source: USAspending.gov (U.S. Department of the Treasury), public domain."
FIELDS = [
    "Award ID", "Recipient Name", "Recipient UEI", "Recipient Location", "Award Amount", "Start Date", "End Date",
    "Awarding Agency", "NAICS", "PSC", "Contract Award Type", "Description", "recipient_id", "generated_internal_id",
]
NAICS_SECTOR = {
    "11": "agriculture", "21": "mining", "22": "utilities", "23": "construction", "31": "manufacturing", "32": "manufacturing",
    "33": "manufacturing", "42": "wholesale", "44": "retail_goods", "45": "retail_goods", "48": "transport", "49": "logistics",
    "51": "information", "52": "finance", "53": "property", "54": "professional_services", "55": "management",
    "56": "business_services", "61": "education", "62": "healthcare", "71": "leisure", "72": "hospitality",
    "81": "personal_services", "92": "public_admin",
}
_SKIP_NAMES = ("MULTIPLE RECIPIENTS", "REDACTED", "MISCELLANEOUS FOREIGN AWARDEES", "UNKNOWN")


def row_to_record(r: dict[str, Any]) -> BusinessRecord | None:
    name = (r.get("Recipient Name") or "").strip()
    if not name or any(name.upper().startswith(s) for s in _SKIP_NAMES):
        return None
    loc = r.get("Recipient Location") or {}
    cc = None
    code = loc.get("location_country_code")
    if code:
        from .eu_ted import alpha3_to_2

        cc = alpha3_to_2(code)
    cc = cc or "US"
    zip5 = loc.get("zip5") or loc.get("foreign_postal_code")
    pc = normalise_postcode(str(zip5), cc) if zip5 else None
    lines = [loc.get("address_line1"), loc.get("address_line2"), loc.get("address_line3")]
    city = loc.get("city_name")
    state = loc.get("state_code")
    address = ", ".join(x for x in [*lines, city, state, pc or zip5, loc.get("country_name")] if x)
    internal = r.get("generated_internal_id")
    award_url = f"https://www.usaspending.gov/award/{internal}" if internal else None
    naics = r.get("NAICS") or {}
    psc = r.get("PSC") or {}
    ncode = str(naics.get("code") or "")
    sector = NAICS_SECTOR.get(ncode[:2])
    uei = r.get("Recipient UEI")
    adate = r.get("Start Date")
    rec = BusinessRecord(
        source_ref=f"{uei or name}|{r.get('Award ID') or internal}"[:300],
        legal_name=name,
        status="active",
        country_code=cc,
        region=state,
        city=city,
        postal_code=pc,
        address=address or None,
        category_hint="procurement_winner",
        source_url=award_url,
        licence=LICENCE,
        attribution=ATTRIBUTION,
        observed_at=datetime.now(UTC),
        raw={
            "uei": uei, "naics": ncode or None, "naics_text": naics.get("description"), "psc": psc.get("code"),
            "industries": [sector] if sector else [], "cpv": [], "award_key": str(internal or r.get("Award ID")),
            "recipient_url": f"https://www.usaspending.gov/recipient/{r['recipient_id']}" if r.get("recipient_id") else None,
            "identifiers": ([{"type": "uei", "value": uei}] if uei else []),
            "relationships": ([{"relation": "supplier_of", "counterparty": {"name": r.get("Awarding Agency"), "role": "awarding_agency"},
                                "source_url": award_url, "confidence": 0.9}] if r.get("Awarding Agency") and award_url else []),
        },
    )
    if award_url:
        rec.raw["signals"] = [make_signal(
            "contract_award", f"Federal contract {r.get('Award ID') or ''}: {(r.get('Description') or 'award')[:200]}".strip(), award_url,
            detail={"awarding_agency": r.get("Awarding Agency"), "amount": r.get("Award Amount"), "currency": "USD",
                    "start_date": adate, "end_date": r.get("End Date"), "naics": ncode or None, "psc": psc.get("code"),
                    "contract_type": r.get("Contract Award Type"), "award_id": r.get("Award ID"), "uei": uei},
            occurred_at=parse_dt(adate) or adate, confidence=0.9,
        )]
    return rec


@register
class UsaSpendingAdapter(SourceAdapter):
    info = AdapterInfo(
        adapter_id=ADAPTER_ID,
        name="USAspending.gov -- federal contract recipients",
        country_coverage=("US",),
        source_type="open_data",
        licence=LICENCE,
        source_url="https://api.usaspending.gov/docs/endpoints",
        access_requirements="None (no key, no registration). Config: start_date/end_date or days_back, award_type_codes (default A,B,C,D), naics_codes, recipient_search_text, max_pages; or 'path' to a saved response.",
        automated_collection_permitted=True,
        rate_policy=RatePolicy(requests_per_minute=60, concurrency=1),
        retry_policy=RetryPolicy(max_attempts=4, backoff_s=2.0, retry_on_status=(500, 502, 503, 504), stop_on_429=False),
        fields_available=("legal_name", "address", "postal_code", "uei", "naics", "psc", "award_value", "awarding_agency"),
        incremental_support=True,
        attribution=ATTRIBUTION,
        live_verified=True,
        cost="free", credentials="none", refresh_frequency="daily (awards typically appear within days of reporting)",
        access_method="api", industries=("*", "construction", "it_services", "professional_services", "healthcare", "manufacturing"),
        allowed_usage="Public domain: unrestricted use. No websites/emails are in the data.",
        notes=("Live-verified: spending_by_award search with the field list above. Page-number paging over a moving dataset can shift "
               "between runs; use a closed date window. Bulk-download CSV archives not implemented. Search start dates are limited to 2007-10-01 by the API."),
    )

    async def health(self) -> AdapterHealth:
        p = self.config.get("path")
        if p:
            ok = Path(p).exists()
            return AdapterHealth(ok, "ok" if ok else "unavailable", f"{'present' if ok else 'missing'}: {p}")
        return AdapterHealth(True, "ok", "public API (no key); not probed")

    def _body(self, page: int, size: int) -> dict[str, Any]:
        now = datetime.now(UTC)
        end = str(self.config.get("end_date") or now.strftime("%Y-%m-%d"))
        start = str(self.config.get("start_date") or (now - timedelta(days=int(self.config.get("days_back", 30)))).strftime("%Y-%m-%d"))
        filters: dict[str, Any] = {
            "award_type_codes": list(self.config.get("award_type_codes") or ["A", "B", "C", "D"]),
            "time_period": [{"start_date": start, "end_date": end}],
        }
        if self.config.get("naics_codes"):
            filters["naics_codes"] = list(self.config["naics_codes"])
        if self.config.get("recipient_search_text"):
            filters["recipient_search_text"] = list(self.config["recipient_search_text"])
        return {"filters": filters, "fields": FIELDS, "page": page, "limit": size, "sort": "Award ID", "order": "asc"}

    async def iter_businesses(
        self, query: DiscoveryQuery, checkpoint: Checkpoint | None = None
    ) -> AsyncIterator[tuple[BusinessRecord, Checkpoint]]:
        est = Established.from_config(self.config, query.extra)
        flt = RecordFilter(query)
        size = max(1, min(100, int(self.config.get("page_size", 100))))
        max_pages = int(self.config.get("max_pages", 1000))
        stats: dict[str, int] = {}
        self.last_stats = stats

        async def units(resume: Checkpoint | None) -> AsyncIterator[tuple[BusinessRecord, Checkpoint]]:
            path = self.config.get("path")
            resumed = bool(resume and resume.get("mode") == "usa")
            if path:
                p = Path(path)
                if not p.exists():
                    raise NotConfigured(f"configured path does not exist: {p}")
                skip = int((resume or {}).get("unit", -1)) + 1 if resume and resume.get("mode") == "usa-file" else 0
                with open_member(p, None, (".json",)) as (fh, _name):
                    for i, row in enumerate(iter_json_array(fh, key="results")):
                        if i < skip:
                            continue
                        rec = row_to_record(row)
                        if rec is not None:
                            yield rec, {"mode": "usa-file", "unit": i}
                return
            page = int(resume["page"]) if resumed else 1
            skip_in_page = int(resume.get("done", 0)) if resumed else 0
            async with PoliteClient(
                client=self.config.get("http_client"), min_interval_s=float(self.config.get("min_interval_s", 1.0)),
                max_attempts=int(self.config.get("max_attempts", 4)), max_wait_s=float(self.config.get("max_wait_s", 120)),
                sleep=self.config.get("sleep") or asyncio.sleep,
            ) as http:
                for _ in range(max_pages):
                    data = await http.post_json(SEARCH_URL, self._body(page, size))
                    rows = data.get("results") or []
                    for i, row in enumerate(rows, 1):
                        if i <= skip_in_page:
                            continue
                        rec = row_to_record(row)
                        if rec is not None:
                            yield rec, {"mode": "usa", "page": page, "done": i}
                    skip_in_page = 0
                    if not (data.get("page_metadata") or {}).get("hasNext") or not rows:
                        return
                    page += 1

        async for rec, cp in drive_awards(units, established=est, flt=flt, checkpoint=checkpoint, limit=query.limit,
                                           adapter_id=ADAPTER_ID, spill_dir=self.config.get("spill_dir"), stats=stats):
            yield rec, cp


__all__ = ["UsaSpendingAdapter", "row_to_record", "FIELDS"]
