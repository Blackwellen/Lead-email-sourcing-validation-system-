"""EU Tenders Electronic Daily (TED) -- contract award notices via the public search API v3.

Endpoint: ``POST https://api.ted.europa.eu/v3/notices/search`` (no API key for public search; verified live: queries,
field list, ``ITERATION`` paging with ``iterationNextToken``, ``notice-type IN (can-standard can-social)`` and
``winner-country`` filters all answered HTTP 200).  Licence: TED data is reusable under the European Commission
reuse policy (Decision 2011/833/EU, CC BY 4.0 compatible) -- attribution "(c) European Union, https://ted.europa.eu".

Winner extraction is deliberately conservative.  The API returns *parallel lists* (``organisation-name-tenderer``,
``organisation-identifier-tenderer``, ``organisation-country-tenderer`` ...) for every tenderer of a notice, plus
``winner-identifier`` / ``winner-country`` for winners.  An organisation is treated as a winner when
  1. identifiers are parallel to names and its identifier is in ``winner-identifier``; else
  2. the notice has exactly one distinct tenderer; else
  3. ``len(winner-country) == len(names)`` (every listed tenderer won a lot) -- flagged ``winner_inference``.
Anything else is counted as ``ambiguous`` and skipped rather than guessed.  Attributes (city, postcode, e-mail, phone)
are only used when their list is parallel to the names.  An e-mail is copied only because the notice publishes it.

Each winner yields a BusinessRecord and a ``contract_award`` signal sourced at the TED notice page.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from ._http import PoliteClient
from ._stream import parse_dt
from .award_common import Established, drive_awards
from .base import (
    AdapterHealth,
    AdapterInfo,
    BusinessRecord,
    Checkpoint,
    DiscoveryQuery,
    RatePolicy,
    RetryPolicy,
    SourceAdapter,
    register,
)
from .cpv_map import cpv_to_industries
from .geo_filters import RecordFilter, normalise_postcode
from .signals_util import make_signal

ADAPTER_ID = "eu_ted"
SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"
LICENCE = "European Commission reuse policy (Decision 2011/833/EU; CC BY 4.0 compatible)"
ATTRIBUTION = "(c) European Union, https://ted.europa.eu -- TED open data."
EU_EEA = ("AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT",
          "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "IS", "LI", "NO")
FIELDS = [
    "publication-number", "publication-date", "notice-title", "buyer-name", "classification-cpv",
    "organisation-name-tenderer", "organisation-identifier-tenderer", "organisation-country-tenderer",
    "organisation-city-tenderer", "organisation-post-code-tenderer", "organisation-email-tenderer", "organisation-tel-tenderer",
    "winner-identifier", "winner-country", "winner-internet-address", "winner-decision-date", "tender-value", "tender-value-cur",
    "total-value",
]
_LANG_PREF = ("eng", "fra", "deu", "spa", "ita", "nld")


def alpha3_to_2(code: str | None) -> str | None:
    if not code:
        return None
    c = str(code).strip().upper()
    if len(c) == 2:
        return c
    try:
        import pycountry

        x = pycountry.countries.get(alpha_3=c)
        return x.alpha_2 if x else None
    except (LookupError, KeyError):
        return None


def alpha2_to_3(code: str) -> str | None:
    try:
        import pycountry

        x = pycountry.countries.get(alpha_2=code.upper())
        return x.alpha_3 if x else None
    except (LookupError, KeyError):
        return None


def _multilang_list(v: Any) -> list[str]:
    """``{"deu": [...], "eng": [...]}`` -> the list of the preferred language; plain lists pass through."""
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    if isinstance(v, dict):
        for lang in _LANG_PREF:
            if lang in v:
                return _multilang_list(v[lang])
        for lang in sorted(v):
            return _multilang_list(v[lang])
    return [str(v)]


def _multilang_text(v: Any) -> str | None:
    lst = _multilang_list(v)
    return lst[0] if lst else None


def _lst(v: Any) -> list[str]:
    if v is None:
        return []
    return [str(x) for x in v] if isinstance(v, list) else [str(v)]


def _first(v: Any) -> str | None:
    l_ = _lst(v)
    return l_[0] if l_ else None


def notice_url(n: dict[str, Any]) -> str | None:
    html = (n.get("links") or {}).get("html") or {}
    for lang in ("ENG", *html.keys()):
        if html.get(lang):
            return html[lang]
    pn = n.get("publication-number")
    return f"https://ted.europa.eu/en/notice/-/detail/{pn}" if pn else None


def winners_of_notice(n: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    """Return ``(winner dicts, inference)``; ``([], 'ambiguous')`` when winners cannot be determined safely."""
    names = _multilang_list(n.get("organisation-name-tenderer"))
    if not names:
        return [], "no_tenderers"
    N = len(names)
    ids = _lst(n.get("organisation-identifier-tenderer"))
    wids = set(_lst(n.get("winner-identifier")))
    wc = _lst(n.get("winner-country"))

    def par(key: str) -> list[str | None]:
        v = _lst(n.get(key))
        return v if len(v) == N else [None] * N

    countries, cities, pcs = par("organisation-country-tenderer"), par("organisation-city-tenderer"), par("organisation-post-code-tenderer")
    emails, tels = par("organisation-email-tenderer"), par("organisation-tel-tenderer")
    idp = ids if len(ids) == N else [None] * N
    if wids and len(ids) == N:
        flags = [i in wids for i in idp]
        inference = "winner-identifier"
    elif len({x.casefold() for x in names}) == 1:
        flags = [True] + [False] * (N - 1)
        inference = "single-tenderer"
    elif wc and len(wc) == N:
        flags = [True] * N
        inference = "winner-country-count"
    else:
        return [], "ambiguous"
    single_site = _lst(n.get("winner-internet-address"))
    out, seen = [], set()
    for i in range(N):
        if not flags[i]:
            continue
        key = (names[i].casefold(), idp[i] or "")
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "name": names[i], "identifier": idp[i], "country": countries[i], "city": cities[i], "postcode": pcs[i],
            "email": emails[i], "tel": tels[i],
            "website": single_site[0] if (len(single_site) == 1 and len(out) == 0 and sum(flags) == 1) else None,
        })
    return out, inference


def notice_to_records(n: dict[str, Any]) -> tuple[list[BusinessRecord], str | None]:
    wins, inference = winners_of_notice(n)
    if not wins:
        return [], inference
    url = notice_url(n)
    cpvs = list(dict.fromkeys(_lst(n.get("classification-cpv"))))
    industries = cpv_to_industries(cpvs)
    buyer = _multilang_text(n.get("buyer-name"))
    title = _multilang_text(n.get("notice-title")) or "contract award notice"
    pub = n.get("publication-date")
    decision = _first(n.get("winner-decision-date"))
    occurred = parse_dt(decision) or parse_dt(pub)
    value = _first(n.get("tender-value")) or n.get("total-value")
    cur = _first(n.get("tender-value-cur"))
    recs: list[BusinessRecord] = []
    for w in wins:
        cc = alpha3_to_2(w["country"])
        if not cc:
            continue
        pc = normalise_postcode(w["postcode"], cc) if w["postcode"] else None
        rec = BusinessRecord(
            source_ref=f"{n.get('publication-number')}|{w['identifier'] or w['name']}"[:300],
            legal_name=w["name"],
            status="active",
            country_code=cc,
            city=w["city"],
            postal_code=pc,
            address=", ".join(x for x in (w["city"], pc or w["postcode"], cc) if x) or None,
            phone=w["tel"],
            website=w["website"],
            email_published=w["email"],
            category_hint="procurement_winner",
            source_url=url,
            licence=LICENCE,
            attribution=ATTRIBUTION,
            observed_at=datetime.now(UTC),
            raw={
                "publication_number": n.get("publication-number"), "award_date": decision or pub, "cpv": cpvs[:20], "industries": industries, "buyer": buyer,
                "award_key": f"{n.get('publication-number')}|{w['identifier'] or w['name']}", "winner_inference": inference,
                "identifiers": ([{"type": "vat_or_national_id", "value": w["identifier"]}] if w["identifier"] else []),
                "relationships": ([{"relation": "supplier_of", "counterparty": {"name": buyer, "role": "buyer"},
                                    "source_url": url, "confidence": 0.85}] if buyer else []),
            },
        )
        if url:
            rec.raw["signals"] = [make_signal(
                "contract_award", f"Awarded (TED): {title}"[:400], url,
                detail={"buyer": buyer, "publication_number": n.get("publication-number"), "value": value, "currency": cur,
                        "cpv": cpvs[:10], "industries": industries, "winner_inference": inference, "country": cc},
                occurred_at=occurred, confidence=0.85 if inference != "winner-country-count" else 0.7,
            )]
        recs.append(rec)
    return recs, inference


@register
class TedAdapter(SourceAdapter):
    info = AdapterInfo(
        adapter_id=ADAPTER_ID,
        name="EU TED -- awarded contractors (API v3)",
        country_coverage=EU_EEA,
        source_type="open_data",
        licence=LICENCE,
        source_url="https://ted.europa.eu/en/simap/developers-corner-for-reusers",
        access_requirements="None for public search (no key). Config: days_back or published_from/published_to (YYYYMMDD), notice_types, page_size (<=250).",
        automated_collection_permitted=True,
        rate_policy=RatePolicy(requests_per_minute=30, concurrency=1),
        retry_policy=RetryPolicy(max_attempts=4, backoff_s=2.0, retry_on_status=(500, 502, 503, 504), stop_on_429=False),
        fields_available=("legal_name", "country", "city", "postal_code", "email_published", "phone", "identifiers", "cpv", "award_value", "buyer"),
        incremental_support=True,
        attribution=ATTRIBUTION,
        live_verified=True,
        cost="free", credentials="none", refresh_frequency="daily (notices published Mon-Fri)",
        access_method="api", industries=("*", "construction", "it_services", "engineering_architecture", "business_services", "healthcare"),
        allowed_usage="Reuse permitted with attribution. Contact details only where the notice publishes them (personal data rules apply).",
        notes=("Live-verified: search endpoint, field names, IN/AND query syntax, ITERATION paging. Winners are inferred from parallel "
               "lists conservatively; ambiguous multi-tenderer notices are skipped. Suppliers outside the EEA (and UK/CH) can appear "
               "because country is taken from the tenderer's own address."),
    )

    def _query(self, q: DiscoveryQuery) -> str:
        now = datetime.now(UTC)
        frm = str(self.config.get("published_from") or (now - timedelta(days=int(self.config.get("days_back", 7)))).strftime("%Y%m%d")).replace("-", "")
        parts = [f"publication-date>={frm}"]
        if self.config.get("published_to"):
            parts.append(f"publication-date<={str(self.config['published_to']).replace('-', '')}")
        types = self.config.get("notice_types") or ["can-standard", "can-social", "can-desg"]
        parts.insert(0, f"notice-type IN ({' '.join(types)})")
        a3 = [alpha2_to_3(c) for c in q.countries]
        a3 = [c for c in a3 if c]
        if a3:
            parts.append(f"winner-country IN ({' '.join(a3)})")
        return " AND ".join(parts)

    async def health(self) -> AdapterHealth:
        return AdapterHealth(True, "ok", "public API (no key); not probed")

    async def iter_businesses(
        self, query: DiscoveryQuery, checkpoint: Checkpoint | None = None
    ) -> AsyncIterator[tuple[BusinessRecord, Checkpoint]]:
        est = Established.from_config(self.config, query.extra)
        flt = RecordFilter(query)
        text_query = self._query(query)
        size = max(1, min(250, int(self.config.get("page_size", 100))))
        stats: dict[str, int] = {"ambiguous": 0}
        self.last_stats = stats

        async def units(resume: Checkpoint | None) -> AsyncIterator[tuple[BusinessRecord, Checkpoint]]:
            resumed = bool(resume and resume.get("mode") == "ted")
            token = ((resume or {}).get("token") or None) if resumed else None
            skip = int((resume or {}).get("done", 0)) if resumed else 0
            async with PoliteClient(
                client=self.config.get("http_client"), min_interval_s=float(self.config.get("min_interval_s", 2.0)),
                max_attempts=int(self.config.get("max_attempts", 4)), max_wait_s=float(self.config.get("max_wait_s", 120)),
                sleep=self.config.get("sleep") or asyncio.sleep,
            ) as http:
                while True:
                    body: dict[str, Any] = {"query": text_query, "fields": FIELDS, "limit": size, "paginationMode": "ITERATION"}
                    if token:
                        body["iterationNextToken"] = token
                    data = await http.post_json(SEARCH_URL, body)
                    notices = data.get("notices") or []
                    n_units = 0
                    for n in notices:
                        recs, inf = notice_to_records(n)
                        if inf == "ambiguous":
                            stats["ambiguous"] += 1
                        for rec in recs:
                            n_units += 1
                            if n_units <= skip:
                                continue
                            yield rec, {"mode": "ted", "token": token or "", "done": n_units}
                    skip = 0
                    nxt = data.get("iterationNextToken")
                    if not nxt or not notices:
                        return
                    token = nxt

        async for rec, cp in drive_awards(units, established=est, flt=flt, checkpoint=checkpoint, limit=query.limit,
                                           adapter_id=ADAPTER_ID, spill_dir=self.config.get("spill_dir"), stats=stats):
            yield rec, cp


__all__ = ["TedAdapter", "notice_to_records", "winners_of_notice", "alpha3_to_2", "FIELDS"]
