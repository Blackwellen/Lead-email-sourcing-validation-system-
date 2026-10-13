"""UK public procurement via OCDS release packages: Contracts Finder and Find a Tender.

Both services publish Open Contracting Data Standard 1.1 *release packages* under the Open Government Licence v3.0
(package ``license`` field confirmed live for Find a Tender).  We read AWARDED suppliers:

* ``contracts_finder_ocds`` -- ``https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search``
  (``publishedFrom/publishedTo/stages=award/limit``, cursor paging via ``links.next``; structure verified live).
  The service answers 429 with ``Rate limit of 12 exceeded. Please retry after 120 seconds.`` -- we wait exactly as told
  (bounded) or stop and resume from the checkpoint.
* ``find_a_tender_ocds`` -- ``https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages``
  (``updatedFrom/updatedTo/stages=award/limit``, ``links.next``; structure verified live).

Each (award, supplier) becomes a :class:`BusinessRecord` (category_hint ``procurement_winner``, CPV->industry tags) plus
a ``contract_award`` signal whose ``source_url`` is the award notice (a signal without a URL is never created).
Supplier contact data is copied ONLY when the notice publishes it (``contactPoint.email`` -> ``email_published``;
``details.url`` / ``contactPoint.url`` -> candidate website); buyer data is never copied onto the supplier.

Input can be an API window (``published_from``/``published_to`` or ``days_back``) or local OCDS files
(``path``: ``.json`` package, ``.jsonl`` releases, ``.zip`` of these, or a directory).
"""
from __future__ import annotations

import asyncio
import json
import re
import zipfile
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlencode

from ._http import PoliteClient
from ._stream import gb_company_number, iter_json_array, parse_dt
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
from .cpv_map import cpv_to_industries
from .geo_filters import RecordFilter, normalise_postcode
from .signals_util import make_signal

LICENCE = "Open Government Licence v3.0"
_UK_NAMES = {"united kingdom", "uk", "great britain", "gb", "england", "scotland", "wales", "northern ireland",
             "england and wales", "uk (england)", "uk (scotland)"}
_PC_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b", re.I)
_BAD_AWARD_STATUS = {"cancelled", "unsuccessful", "withdrawn", "pending"}


def country_code_of(name_or_code: str | None) -> str | None:
    """ISO alpha-2 from a country name / alpha-2 / alpha-3 (None when unknown)."""
    if not name_or_code:
        return None
    s = str(name_or_code).strip()
    low = s.lower()
    if low in _UK_NAMES:
        return "GB"
    try:
        import pycountry

        if len(s) == 2:
            c = pycountry.countries.get(alpha_2=s.upper())
        elif len(s) == 3:
            c = pycountry.countries.get(alpha_3=s.upper())
        else:
            c = pycountry.countries.lookup(s)
        return c.alpha_2 if c else None
    except (LookupError, AttributeError, KeyError):
        return None


@dataclass
class AwardUnit:
    release: dict[str, Any]
    award: dict[str, Any]
    party: dict[str, Any]
    supplier_ref: dict[str, Any]
    index: int  # running (award, supplier) index inside the release


def iter_award_units(release: dict[str, Any]) -> Iterator[AwardUnit]:
    """(award, supplier) pairs of one OCDS release; cancelled/unsuccessful awards and unnamed suppliers are skipped."""
    parties = release.get("parties") or []
    by_id = {p.get("id"): p for p in parties if isinstance(p, dict) and p.get("id")}
    k = 0
    for award in release.get("awards") or []:
        if not isinstance(award, dict):
            continue
        if str(award.get("status") or "active").lower() in _BAD_AWARD_STATUS:
            continue
        for sref in award.get("suppliers") or []:
            if not isinstance(sref, dict):
                continue
            party = by_id.get(sref.get("id"))
            if party is None:
                party = {"id": sref.get("id"), "name": sref.get("name")}
            if not (party.get("name") or (party.get("identifier") or {}).get("legalName")):
                continue
            yield AwardUnit(release, award, party, sref, k)
            k += 1


def _region(v: Any) -> str | None:
    """Region text, dropping NUTS codes such as 'UKH12' that Find a Tender publishes in this field."""
    if not isinstance(v, str) or not v.strip():
        return None
    t = v.strip()
    return None if re.match(r"^[A-Z]{2}[A-Z0-9]*\d[A-Z0-9]*$", t) and len(t) <= 5 else t


def _coh_number(party: dict[str, Any]) -> str | None:
    ident = party.get("identifier") or {}
    cands: list[tuple[str | None, str | None]] = [(ident.get("scheme"), ident.get("id"))]
    cands += [(a.get("scheme"), a.get("id")) for a in party.get("additionalIdentifiers") or [] if isinstance(a, dict)]
    for scheme, val in cands:
        if scheme == "GB-COH" and val:
            return gb_company_number(str(val))
    pid = str(party.get("id") or "")
    if pid.startswith("GB-COH-"):
        return gb_company_number(pid[len("GB-COH-"):])
    return None


def _cpv_codes(release: dict[str, Any], award: dict[str, Any]) -> list[str]:
    codes: list[str] = []

    def add(c: Any) -> None:
        if isinstance(c, dict) and str(c.get("scheme", "CPV")).upper() == "CPV" and c.get("id"):
            v = str(c["id"])
            if v not in codes:
                codes.append(v)

    for it in award.get("items") or []:
        add((it or {}).get("classification"))
    tender = release.get("tender") or {}
    add(tender.get("classification"))
    for c in tender.get("additionalClassifications") or []:
        add(c)
    for it in tender.get("items") or []:
        add((it or {}).get("classification"))
        for c in (it or {}).get("additionalClassifications") or []:
            add(c)
    return codes


def _clean_url(u: Any) -> str | None:
    if not u or not isinstance(u, str):
        return None
    u = u.strip()
    return u if re.match(r"^(https?://|www\.)", u, re.I) else None


def build_unit_record(
    unit: AwardUnit, *, notice_url: str | None, default_country: str | None, adapter_id: str, source_name: str,
    package_uri: str | None = None,
) -> BusinessRecord | None:
    rel, award, party = unit.release, unit.award, unit.party
    ident = party.get("identifier") or {}
    name = (party.get("name") or ident.get("legalName") or "").strip()
    if not name:
        return None
    addr = party.get("address") or {}
    street = str(addr.get("streetAddress") or "").replace("\r", "").replace("\n", ", ").strip(", ")
    cn = _coh_number(party)
    pc_raw = addr.get("postalCode")
    if not pc_raw:
        m = _PC_RE.search(street.upper())
        pc_raw = f"{m.group(1)} {m.group(2)}" if m else None
    cc = country_code_of(addr.get("countryName")) or ("GB" if cn else None) or default_country
    if not cc:
        return None
    pc = normalise_postcode(pc_raw, cc) if pc_raw else None
    address = ", ".join(x for x in (street, addr.get("locality"), addr.get("region"), pc or pc_raw, addr.get("countryName")) if x)
    cp = party.get("contactPoint") or {}
    det = party.get("details") or {}
    site = _clean_url(det.get("url")) or _clean_url(cp.get("url"))
    email = (cp.get("email") or "").strip() or None
    cpv = _cpv_codes(rel, award)
    industries = cpv_to_industries(cpv)
    buyer = rel.get("buyer") or {}
    value = award.get("value") or {}
    adate = award.get("date") or award.get("datePublished") or rel.get("date")
    occurred = parse_dt(adate)
    tender = rel.get("tender") or {}
    title = (award.get("title") or tender.get("title") or "contract").strip()
    url = notice_url or package_uri
    rec = BusinessRecord(
        source_ref=cn or f"{party.get('id') or name}"[:300],
        legal_name=name,
        company_number=cn if cc == "GB" else None,
        status="active",
        country_code=cc,
        region=_region(addr.get("region")),
        city=addr.get("locality") or None,
        postal_code=pc,
        address=address or None,
        phone=(cp.get("telephone") or None),
        website=site,
        email_published=email,
        category_hint="procurement_winner",
        source_url=url,
        raw={
            "ocid": rel.get("ocid"), "release_id": rel.get("id"), "award_date": adate, "award_key": f"{award.get('id')}|{unit.supplier_ref.get('id') or name}",
            "cpv": cpv[:20], "industries": industries, "buyer": buyer.get("name"), "buyer_id": buyer.get("id"),
            "identifiers": ([{"type": "company_number", "value": cn}] if cn and cc == "GB" else []),
            "relationships": ([{
                "relation": "supplier_of", "direction": "from_this_to_counterparty", "counterparty": {
                    "name": buyer.get("name"), "id": buyer.get("id"), "role": "buyer"},
                "source_url": url, "confidence": 0.9}] if buyer.get("name") else []),
        },
    )
    if url:
        detail = {
            "buyer": buyer.get("name"), "buyer_id": buyer.get("id"), "ocid": rel.get("ocid"), "award_id": award.get("id"),
            "value": value.get("amount"), "currency": value.get("currency"), "award_date": adate,
            "cpv": cpv[:10], "industries": industries, "source": source_name,
            "supplier_ref": unit.supplier_ref.get("id"),
        }
        rec.raw["signals"] = [make_signal(
            "contract_award", f"Awarded: {title}"[:400], url, detail=detail, occurred_at=occurred or adate, confidence=0.9,
        )]
    return rec


# --------------------------------------------------------------------------- release sources
def _iter_releases_from_stream(fh, name: str) -> Iterator[dict[str, Any]]:
    low = name.lower()
    if low.endswith((".jsonl", ".ndjson")):
        for line in fh:
            line = line.strip()
            if line:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    yield obj
        return
    for obj in iter_json_array(fh, key="releases"):
        if isinstance(obj, dict):
            yield obj


def iter_releases_from_path(path: Path) -> Iterator[dict[str, Any]]:
    """Releases of a package file / jsonl / zip of those / directory (sorted, deterministic order)."""
    if path.is_dir():
        for p in sorted(path.iterdir()):
            if p.suffix.lower() in (".json", ".jsonl", ".ndjson", ".zip"):
                yield from iter_releases_from_path(p)
        return
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            for n in sorted(i.filename for i in zf.infolist() if not i.is_dir() and i.filename.lower().endswith((".json", ".jsonl", ".ndjson"))):
                with zf.open(n) as fh:
                    yield from _iter_releases_from_stream(fh, n)
        return
    with open(path, "rb") as fh:
        yield from _iter_releases_from_stream(fh, path.name)


class OCDSAdapterBase(SourceAdapter):
    """Common implementation; subclasses set the service constants."""

    API_URL: ClassVar[str]
    FROM_PARAM: ClassVar[str]
    TO_PARAM: ClassVar[str]
    DEFAULT_COUNTRY: ClassVar[str | None] = "GB"
    SOURCE_NAME: ClassVar[str]
    MIN_INTERVAL_S: ClassVar[float] = 6.0

    # -- per-service
    def notice_url(self, release: dict[str, Any], award: dict[str, Any]) -> str | None:
        for d in (award.get("documents") or []):
            if isinstance(d, dict) and d.get("url") and str(d.get("documentType", "")).lower().startswith("award"):
                return d["url"]
        for d in (award.get("documents") or []):
            if isinstance(d, dict) and d.get("url"):
                return d["url"]
        for d in ((release.get("tender") or {}).get("documents") or []):
            if isinstance(d, dict) and d.get("url") and "/Notice/" in d["url"]:
                return d["url"]
        return None

    # -- sources
    def _window(self) -> tuple[str, str]:
        now = datetime.now(UTC)
        to = self.config.get("published_to") or now.strftime("%Y-%m-%d")
        frm = self.config.get("published_from") or (now - timedelta(days=int(self.config.get("days_back", 7)))).strftime("%Y-%m-%d")
        return str(frm), str(to)

    def _first_url(self) -> str:
        frm, to = self._window()
        fmt = (lambda s: s if "T" in s else s + "T00:00:00") if self.FROM_PARAM == "updatedFrom" else (lambda s: s)
        q = {self.FROM_PARAM: fmt(frm), self.TO_PARAM: fmt(to), "stages": "award", "limit": int(self.config.get("page_size", 100))}
        return f"{self.API_URL}?{urlencode(q)}"

    def _make_client(self) -> PoliteClient:
        return PoliteClient(
            client=self.config.get("http_client"), min_interval_s=float(self.config.get("min_interval_s", self.MIN_INTERVAL_S)),
            max_attempts=int(self.config.get("max_attempts", 4)), max_wait_s=float(self.config.get("max_wait_s", 180)),
            sleep=self.config.get("sleep") or asyncio.sleep, headers={"Accept": "application/json"},
        )

    async def _units(self, resume: Checkpoint | None) -> AsyncIterator[tuple[BusinessRecord, Checkpoint]]:
        path = self.config.get("path")
        skip = 0
        if path:
            p = Path(path)
            if not p.exists():
                raise NotConfigured(f"configured path does not exist: {p}")
            skip = int((resume or {}).get("unit", -1)) + 1 if resume and resume.get("mode") == "file" else 0
            n = -1
            for rel in iter_releases_from_path(p):
                for u in iter_award_units(rel):
                    n += 1
                    if n < skip:
                        continue
                    rec = build_unit_record(u, notice_url=self.notice_url(rel, u.award), default_country=self.DEFAULT_COUNTRY,
                                            adapter_id=self.info.adapter_id, source_name=self.SOURCE_NAME,
                                            package_uri=self.config.get("source_url"))
                    if rec is None:
                        continue
                    rec.observed_at = datetime.now(UTC)
                    yield rec, {"mode": "file", "unit": n}
            return
        url = (resume or {}).get("page_url") if resume and resume.get("mode") == "api" else None
        skip_in_page = int((resume or {}).get("done", 0)) if url else 0
        url = url or self._first_url()
        async with self._make_client() as http:
            while url:
                pkg = await http.get_json(url)
                releases = pkg.get("releases") or []
                n = 0
                for rel in releases:
                    for u in iter_award_units(rel):
                        n += 1
                        if n <= skip_in_page:
                            continue
                        rec = build_unit_record(u, notice_url=self.notice_url(rel, u.award), default_country=self.DEFAULT_COUNTRY,
                                                adapter_id=self.info.adapter_id, source_name=self.SOURCE_NAME,
                                                package_uri=None)
                        if rec is None:
                            continue
                        rec.observed_at = datetime.now(UTC)
                        yield rec, {"mode": "api", "page_url": url, "done": n}
                skip_in_page = 0
                nxt = (pkg.get("links") or {}).get("next")
                url = nxt if (nxt and releases) else None

    async def iter_businesses(
        self, query: DiscoveryQuery, checkpoint: Checkpoint | None = None
    ) -> AsyncIterator[tuple[BusinessRecord, Checkpoint]]:
        est = Established.from_config(self.config, query.extra)
        flt = RecordFilter(query)
        self.last_stats: dict[str, int] = {}
        async for rec, cp in drive_awards(
            self._units, established=est, flt=flt, checkpoint=checkpoint, limit=query.limit, adapter_id=self.info.adapter_id,
            spill_dir=self.config.get("spill_dir"), stats=self.last_stats,
        ):
            yield rec, cp

    async def health(self) -> AdapterHealth:
        p = self.config.get("path")
        if p:
            ok = Path(p).exists()
            return AdapterHealth(ok, "ok" if ok else "unavailable", f"{'present' if ok else 'missing'}: {p}")
        return AdapterHealth(True, "ok", "live API (public, no key); not probed")


@register
class ContractsFinderOCDS(OCDSAdapterBase):
    API_URL = "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search"
    FROM_PARAM, TO_PARAM = "publishedFrom", "publishedTo"
    SOURCE_NAME = "contracts_finder"
    MIN_INTERVAL_S = 6.0
    info = AdapterInfo(
        adapter_id="contracts_finder_ocds",
        name="UK Contracts Finder -- awarded contracts (OCDS)",
        country_coverage=("GB",),
        source_type="open_data",
        licence=LICENCE,
        source_url="https://www.contractsfinder.service.gov.uk/apidocumentation/Notices/1/GET-Published-OCDS-Search",
        access_requirements="None (public OCDS API, no key). Config: published_from/published_to or days_back; or 'path' to saved packages. min_awards / min_award_years for the supplier-established filter.",
        automated_collection_permitted=True,
        rate_policy=RatePolicy(requests_per_minute=10, concurrency=1),
        retry_policy=RetryPolicy(max_attempts=4, backoff_s=2.0, retry_on_status=(500, 502, 503, 504), stop_on_429=False),
        fields_available=("legal_name", "company_number", "address", "postal_code", "website", "email_published", "phone", "cpv", "award_value", "buyer"),
        incremental_support=True,
        attribution="Contains public sector information licensed under the Open Government Licence v3.0. Source: Contracts Finder (Crown Commercial Service).",
        live_verified=True,
        cost="free", credentials="none", refresh_frequency="continuous (notices published daily)",
        access_method="api", industries=("*", "construction", "facilities", "it_services", "business_services", "property"),
        allowed_usage="OGL v3.0 with attribution; supplier contact details only where the notice publishes them (personal data: UK GDPR/PECR apply).",
        notes=("Live-verified: award release structure (parties[].identifier GB-COH, awards[].suppliers/value/documents) and the "
               "429 'Rate limit of 12 exceeded. Please retry after 120 seconds.' behaviour were observed. Only ~25% of supplier "
               "parties carry a GB-COH identifier; others merge by name+postcode. Supplier email/website are rarely published."),
    )


@register
class FindATenderOCDS(OCDSAdapterBase):
    API_URL = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"
    FROM_PARAM, TO_PARAM = "updatedFrom", "updatedTo"
    SOURCE_NAME = "find_a_tender"
    MIN_INTERVAL_S = 6.0
    info = AdapterInfo(
        adapter_id="find_a_tender_ocds",
        name="UK Find a Tender -- awarded contracts (OCDS)",
        country_coverage=("GB",),
        source_type="open_data",
        licence=LICENCE,
        source_url="https://www.find-tender.service.gov.uk/Developer/Documentation",
        access_requirements="None (public OCDS API, no key). Config: published_from/published_to or days_back; or 'path'.",
        automated_collection_permitted=True,
        rate_policy=RatePolicy(requests_per_minute=10, concurrency=1),
        retry_policy=RetryPolicy(max_attempts=4, backoff_s=2.0, retry_on_status=(500, 502, 503, 504), stop_on_429=False),
        fields_available=("legal_name", "company_number", "address", "postal_code", "website", "email_published", "cpv", "buyer"),
        incremental_support=True,
        attribution="Contains public sector information licensed under the Open Government Licence v3.0. Source: Find a Tender Service (Cabinet Office).",
        live_verified=True,
        cost="free", credentials="none", refresh_frequency="continuous (notices published daily)",
        access_method="api", industries=("*", "construction", "it_services", "business_services", "healthcare", "engineering_architecture"),
        allowed_usage="OGL v3.0 (package licence field) with attribution.",
        notes=("Live-verified: release package with awards/parties (GB-COH supplier ids such as 'GB-COH-<number>') and "
               "links.next cursor paging. Award value is frequently absent. Award notice URL is built as /Notice/<release id> "
               "(documented FTS notice URL pattern; not individually fetched)."),
    )

    def notice_url(self, release: dict[str, Any], award: dict[str, Any]) -> str | None:
        got = super().notice_url(release, award)
        if got:
            return got
        rid = str(release.get("id") or "")
        m = re.match(r"^(\d{6}-\d{4})", rid)
        return f"https://www.find-tender.service.gov.uk/Notice/{m.group(1)}" if m else None


__all__ = [
    "ContractsFinderOCDS", "FindATenderOCDS", "OCDSAdapterBase", "iter_award_units", "iter_releases_from_path",
    "build_unit_record", "country_code_of",
]
