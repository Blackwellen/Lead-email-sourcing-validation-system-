"""Industry directory intelligence (DISC-03): permission-gated per-site adapters, an open-data member-register adapter,
an FSA FHRS adapter and a discovery helper for the Source Explorer.

Policy (default DENY, enforced in code):

* An automated adapter for a named directory / association runs ONLY when the catalogue entry's
  ``automated_extraction`` is ``allowed_open_data`` **or** the operator records the basis for access in the adapter config
  as ``permission_basis`` -- ``"written: <who/when/reference>"`` (written permission from the site owner; valid for any
  status) or ``"terms_reviewed: <what was checked/when>"`` (valid only for ``unknown_review_terms`` / ``api_available``
  entries).  Anything else raises :class:`NotPermitted` with an explanation.  ``prohibited`` entries need ``written:``.
* Before ANY HTTP fetch of a directory page or dataset, ``robots.txt`` is checked with the crawler's
  :class:`SafeFetcher` + :class:`RobotsCache` (:class:`DirectoryFetcher`); disallowed or un-checkable (deferred) means NOT fetched.
* No CAPTCHA / login bypass, no HTML scraping here: the generic adapter reads DOWNLOADABLE datasets
  (CSV / XLSX / JSON / XML) published as open data; HTML-scraping per-site adapters are an extension point
  (:class:`PermissionGatedSiteAdapter`) with no concrete implementation shipped.
"""
from __future__ import annotations

import csv
import io
import re
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from lxml import etree

from ._stream import iter_json_array, open_member
from .base import (
    AdapterError,
    AdapterHealth,
    AdapterInfo,
    AdapterUnavailable,
    BusinessRecord,
    Checkpoint,
    DiscoveryQuery,
    NotConfigured,
    NotPermitted,
    RatePolicy,
    SourceAdapter,
    register,
)
from .directory_catalogue import (
    DISCLAIMER_FALLBACK,
    get_entry,
    is_automation_permitted,
    load_catalogue,
)
from .download import download_file
from .geo_filters import RecordFilter, normalise_postcode

OGL = "Open Government Licence v3.0"


# =========================================================================== permission model
@dataclass(frozen=True)
class PermissionStatus:
    entry_id: str
    allowed: bool
    status: str  # allowed_open_data | allowed_by_permission | blocked | permission_required | review_terms | unknown
    basis: str | None
    reason: str
    catalogue_status: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"entry_id": self.entry_id, "allowed": self.allowed, "status": self.status, "basis": self.basis,
                "reason": self.reason, "catalogue_status": self.catalogue_status}


def _basis_of(config: dict[str, Any] | None) -> tuple[str | None, str]:
    """(kind, text) from ``permission_basis`` -- a string ``'written: ...'`` or a dict ``{type, reference, ...}``."""
    pb = (config or {}).get("permission_basis")
    if not pb:
        return None, ""
    if isinstance(pb, dict):
        kind = str(pb.get("type") or "").lower()
        text = " ".join(str(pb.get(k) or "") for k in ("reference", "date", "recorded_by", "note")).strip()
        return (kind or None), text
    m = re.match(r"^\s*(written|terms_reviewed)\s*:\s*(.*)$", str(pb), re.I | re.S)
    if not m:
        return None, str(pb)
    return m.group(1).lower(), m.group(2).strip()


def evaluate_permission(entry: dict[str, Any] | None, config: dict[str, Any] | None = None, *, entry_id: str | None = None) -> PermissionStatus:
    eid = entry["id"] if entry else (entry_id or "?")
    cat_status = entry["automated_extraction"] if entry else None
    if entry is not None and is_automation_permitted(entry):
        return PermissionStatus(eid, True, "allowed_open_data", "catalogue:allowed_open_data",
                                f"{entry['name']}: published as open data; still re-verify current terms. {DISCLAIMER_FALLBACK}", cat_status)
    kind, text = _basis_of(config)
    if kind == "written" and len(text) >= 10:
        return PermissionStatus(eid, True, "allowed_by_permission", f"written: {text}",
                                "operator recorded written permission from the site owner", cat_status)
    if kind == "terms_reviewed" and len(text) >= 10 and cat_status in ("unknown_review_terms", "api_available"):
        return PermissionStatus(eid, True, "allowed_by_permission", f"terms_reviewed: {text}",
                                "operator recorded a terms review that permits automated access", cat_status)
    name = entry["name"] if entry else eid
    if entry is None:
        return PermissionStatus(eid, False, "unknown", None,
                                f"{name}: not in the catalogue; automated access is denied by default. Record "
                                "permission_basis='written: ...' to enable.", None)
    mapping = {
        "prohibited": ("blocked", "terms prohibit automated extraction; only written permission from the owner can enable this"),
        "permission_required": ("permission_required", "access requires the owner's permission; record permission_basis='written: ...'"),
        "unknown_review_terms": ("review_terms", "terms not established; review them and record permission_basis='terms_reviewed: ...' or 'written: ...'"),
        "api_available": ("review_terms", "an official API with credentials exists; use that rather than automated page access"),
    }
    st, why = mapping.get(cat_status or "", ("blocked", "automated access not permitted"))
    extra = ""
    if kind and len(text) < 10:
        extra = " (permission_basis was given but is too short to be a credible record)"
    elif kind == "terms_reviewed":
        extra = " ('terms_reviewed' is not accepted for this catalogue status)"
    return PermissionStatus(eid, False, st, None, f"{name}: {why}.{extra} Recommended access: {entry['recommended_access']}. {DISCLAIMER_FALLBACK}", cat_status)


def require_permission(entry_id: str, config: dict[str, Any] | None = None) -> PermissionStatus:
    try:
        entry = get_entry(entry_id)
    except KeyError:
        entry = None
    st = evaluate_permission(entry, config, entry_id=entry_id)
    if not st.allowed:
        raise NotPermitted(st.reason)
    return st


# =========================================================================== robots-aware fetching
class DirectoryFetcher:
    """robots.txt-checked access for directory pages and dataset URLs (SafeFetcher + RobotsCache)."""

    def __init__(self, fetcher: Any | None = None, robots: Any | None = None):
        from ..crawler.robots import RobotsCache
        from ..crawler.safe_http import SafeFetcher

        self.fetcher = fetcher or SafeFetcher()
        self.robots = robots or RobotsCache(self.fetcher)

    async def check(self, url: str) -> None:
        """Raise :class:`NotPermitted` unless robots.txt allows fetching ``url`` (fail closed when robots cannot be checked)."""
        res = await self.robots.check(url)
        if res.allowed:
            return
        if getattr(res.decision, "value", str(res.decision)) == "deferred":
            raise AdapterUnavailable(f"robots.txt for {url} could not be checked right now ({res.reason}); not fetching")
        raise NotPermitted(f"robots.txt disallows fetching {url}: {res.reason}")

    async def fetch(self, url: str, **kw: Any):
        await self.check(url)
        return await self.fetcher.fetch(url, **kw)

    async def aclose(self) -> None:
        await self.fetcher.aclose()


# =========================================================================== extension point for per-site adapters
class PermissionGatedSiteAdapter(SourceAdapter):
    """Base class for per-site adapters.  Subclasses set ``catalogue_id`` and implement :meth:`parse`; this class
    performs the permission check and robots-aware page fetching before anything else happens."""

    catalogue_id: ClassVar[str]

    def permission(self) -> PermissionStatus:
        return require_permission(self.catalogue_id, self.config)

    def directory_fetcher(self) -> DirectoryFetcher:
        return self.config.get("directory_fetcher") or DirectoryFetcher(self.config.get("fetcher"), self.config.get("robots"))

    def parse(self, url: str, html: str) -> Iterator[BusinessRecord]:  # pragma: no cover - abstract-ish
        raise NotImplementedError

    async def iter_businesses(
        self, query: DiscoveryQuery, checkpoint: Checkpoint | None = None
    ) -> AsyncIterator[tuple[BusinessRecord, Checkpoint]]:
        self.permission()  # NotPermitted unless catalogue allows or written permission is recorded
        urls = list(self.config.get("urls") or [])
        if not urls:
            raise NotConfigured("config 'urls' (directory pages the operator may fetch) is required")
        fetcher = self.directory_fetcher()
        flt = RecordFilter(query)
        start = int((checkpoint or {}).get("page", -1)) + 1
        emitted = 0
        for i, url in enumerate(urls):
            if i < start:
                continue
            res = await fetcher.fetch(url)
            if not res.ok:
                continue
            for rec in self.parse(url, res.text()):
                rec.adapter_id = self.info.adapter_id
                if flt.accept(self.stamp(rec)):
                    yield rec, {"page": i}
                    emitted += 1
                    if query.limit and emitted >= query.limit:
                        return


# =========================================================================== generic open-data member register
_MAP_FIELDS = (
    "legal_name", "trading_name", "company_number", "legal_type", "status", "region", "city", "postal_code", "address", "phone",
    "website", "email", "category_hint", "source_ref", "sic", "country_code", "lat", "lon",
)


def _xml_get(el: etree._Element, path: str) -> str | None:
    if path.startswith("@"):
        return (el.get(path[1:]) or "").strip() or None
    if "/@" in path:
        p, _, attr = path.partition("/@")
        sub = el.find(p)
        return ((sub.get(attr) or "").strip() or None) if sub is not None else None
    t = el.findtext(path)
    return t.strip() if t and t.strip() else None


def _json_get(obj: Any, path: str) -> str | None:
    cur = obj
    for k in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(k)
        else:
            return None
    if cur is None or isinstance(cur, dict | list):
        return None
    t = str(cur).strip()
    return t or None


def _rows_csv(fh, cfg: dict[str, Any], skip: int) -> Iterator[tuple[int, Callable[[str], str | None]]]:
    text = io.TextIOWrapper(fh, encoding=cfg.get("encoding", "utf-8-sig"), errors="replace", newline="")
    rd = csv.reader(text, delimiter=cfg.get("delimiter", ","))
    header = next(rd, None)
    if header is None:
        return
    header = [h.strip() for h in header]
    ix = {h: i for i, h in enumerate(header)}
    for n, row in enumerate(rd):
        if n < skip or not row:
            continue

        def get(col: str, row=row) -> str | None:
            i = ix.get(col)
            if i is None or i >= len(row):
                return None
            t = row[i].strip()
            return t or None

        yield n, get


def _rows_xlsx(path: Path, cfg: dict[str, Any], skip: int) -> Iterator[tuple[int, Callable[[str], str | None]]]:
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[cfg["sheet"]] if cfg.get("sheet") else wb.worksheets[0]
        it = ws.iter_rows(values_only=True)
        for _ in range(int(cfg.get("header_row", 0))):
            next(it, None)
        header = next(it, None)
        if header is None:
            return
        ix = {str(h).strip(): i for i, h in enumerate(header) if h is not None}
        for n, row in enumerate(it):
            if n < skip or row is None:
                continue

            def get(col: str, row=row) -> str | None:
                i = ix.get(col)
                if i is None or i >= len(row) or row[i] is None:
                    return None
                t = str(row[i]).strip()
                return t or None

            yield n, get
    finally:
        wb.close()


def _rows_json(fh, cfg: dict[str, Any], skip: int) -> Iterator[tuple[int, Callable[[str], str | None]]]:
    for n, obj in enumerate(iter_json_array(fh, key=cfg.get("record_path"))):
        if n < skip:
            continue
        yield n, (lambda col, obj=obj: _json_get(obj, col))


def _rows_xml(fh, cfg: dict[str, Any], skip: int) -> Iterator[tuple[int, Callable[[str], str | None]]]:
    tag = cfg.get("record_tag")
    if not tag:
        raise NotConfigured("xml format needs config 'record_tag'")
    n = -1
    for _ev, el in etree.iterparse(fh, events=("end",), tag=tag, resolve_entities=False, no_network=True, huge_tree=True):
        n += 1
        if n >= skip:
            yield n, (lambda col, el=el: _xml_get(el, col))
        el.clear()
        parent = el.getparent()
        if parent is not None:
            while el.getprevious() is not None:
                del parent[0]


def detect_format(path: Path, fmt: str | None) -> str:
    if fmt and fmt != "auto":
        return fmt.lower()
    s = path.name.lower()
    if s.endswith(".gz"):
        s = s[:-3]
    for ext, f in ((".xlsx", "xlsx"), (".xlsm", "xlsx"), (".json", "json"), (".xml", "xml"), (".csv", "csv"), (".txt", "csv"), (".tsv", "csv")):
        if s.endswith(ext):
            return f
    if zipfile_member_hint(path):
        return zipfile_member_hint(path) or "csv"
    return "csv"


def zipfile_member_hint(path: Path) -> str | None:
    import zipfile

    if path.suffix.lower() == ".zip" and zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            for n in zf.namelist():
                low = n.lower()
                for ext, f in ((".csv", "csv"), (".json", "json"), (".xml", "xml"), (".tsv", "csv"), (".txt", "csv")):
                    if low.endswith(ext):
                        return f
    return None


def _pick(get: Callable[[str], str | None], spec: Any) -> str | None:
    """Mapping value -> text: column name, list of columns (first non-empty), ``=constant``."""
    if spec is None:
        return None
    if isinstance(spec, str):
        if spec.startswith("="):
            return spec[1:].strip() or None
        return get(spec)
    for s in spec:
        v = _pick(get, s)
        if v:
            return v
    return None


def _join(get: Callable[[str], str | None], spec: Any) -> str | None:
    if spec is None:
        return None
    if isinstance(spec, str):
        return _pick(get, spec)
    parts = [p for p in (_pick(get, s) for s in spec) if p]
    return ", ".join(parts) or None


def map_row(get: Callable[[str], str | None], cfg: dict[str, Any], observed_at: datetime | None, source_ref_default: str) -> BusinessRecord | None:
    m: dict[str, Any] = cfg.get("mapping") or {}
    name = _pick(get, m.get("legal_name")) or _pick(get, m.get("trading_name"))
    if not name:
        return None
    cc = (_pick(get, m.get("country_code")) or cfg.get("country_code") or "").upper()
    if len(cc) != 2:
        return None
    pc = _pick(get, m.get("postal_code"))
    sic_raw = _pick(get, m.get("sic")) or ""
    lat, lon = _pick(get, m.get("lat")), _pick(get, m.get("lon"))
    try:
        latf, lonf = (float(lat) if lat else None), (float(lon) if lon else None)
    except ValueError:
        latf = lonf = None
    rec = BusinessRecord(
        source_ref=(_pick(get, m.get("source_ref")) or source_ref_default)[:300],
        legal_name=_pick(get, m.get("legal_name")) or name,
        trading_name=_pick(get, m.get("trading_name")),
        company_number=_pick(get, m.get("company_number")),
        legal_type=_pick(get, m.get("legal_type")),
        status=_pick(get, m.get("status")),
        country_code=cc,
        region=_pick(get, m.get("region")),
        city=_pick(get, m.get("city")),
        postal_code=normalise_postcode(pc, cc) if pc else None,
        address=_join(get, m.get("address")),
        phone=_pick(get, m.get("phone")),
        website=_pick(get, m.get("website")),
        email_published=_pick(get, m.get("email")) if cfg.get("dataset_publishes_email", False) else None,
        sic_codes=[s for s in re.split(r"[;,|\s]+", sic_raw) if s.isdigit()],
        category_hint=_pick(get, m.get("category_hint")) or cfg.get("category_hint"),
        lat=latf, lon=lonf,
        source_url=cfg.get("source_url"),
        licence=cfg.get("licence"),
        attribution=cfg.get("attribution"),
        observed_at=observed_at,
        raw={"register": cfg.get("register_name") or cfg.get("entry_id")},
    )
    return rec


@register
class OpenDataMemberRegisterAdapter(SourceAdapter):
    """Generic adapter for member registers / public registers published as DOWNLOADABLE open datasets."""

    info = AdapterInfo(
        adapter_id="open_data_member_register",
        name="Open-data member / public register (CSV, XLSX, JSON, XML) -- configurable",
        country_coverage=("*",),
        source_type="open_data",
        licence="Per dataset -- the operator MUST set config 'licence' (and 'attribution')",
        source_url="https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers",
        access_requirements=("Config: 'path' or 'url' (+ robots.txt check), 'format', 'mapping' {field: column|[columns]|'=constant'}, "
                             "'country_code', 'licence'. Optional 'entry_id' to gate on a catalogue entry + 'permission_basis'."),
        automated_collection_permitted=True,
        rate_policy=RatePolicy(requests_per_minute=None, concurrency=1),
        fields_available=("legal_name", "trading_name", "company_number", "status", "address", "postal_code", "phone", "website", "category"),
        incremental_support=True,
        requires_config=True,
        cost="free", credentials="none", refresh_frequency="per dataset",
        access_method="bulk_file", industries=("*",),
        allowed_usage="Only datasets the publisher releases for reuse (open licence) or for which written permission is recorded. A dataset-published e-mail is only kept when config 'dataset_publishes_email' is true.",
        notes="Presets: UK_LICENSED_SPONSORS (Home Office sponsor register CSV; header verified live). Not a scraper.",
    )

    def _entry_gate(self) -> PermissionStatus | None:
        eid = self.config.get("entry_id")
        if not eid:
            if not self.config.get("licence"):
                raise NotConfigured("config 'licence' (the dataset's reuse licence) is required when no catalogue 'entry_id' is given")
            return None
        return require_permission(eid, self.config)

    async def health(self) -> AdapterHealth:
        p = self.config.get("path")
        if p:
            ok = Path(p).exists()
            return AdapterHealth(ok, "ok" if ok else "unavailable", f"{'present' if ok else 'missing'}: {p}")
        if self.config.get("url"):
            return AdapterHealth(True, "ok", "will download after a robots.txt check (not verified)")
        return AdapterHealth(False, "not_configured", "set 'path' or 'url' and 'mapping'")

    async def _acquire(self) -> Path:
        p = self.config.get("path")
        if p:
            if not Path(p).exists():
                raise NotConfigured(f"configured path does not exist: {p}")
            return Path(p)
        url = self.config.get("url")
        if not url:
            raise NotConfigured("set 'path' or 'url'")
        if self.config.get("check_robots", True):
            df = self.config.get("directory_fetcher") or DirectoryFetcher(self.config.get("fetcher"), self.config.get("robots"))
            await df.check(url)
        from ..config import get_settings

        dest = Path(self.config.get("data_dir") or get_settings().data_dir) / "directories" / (url.rsplit("/", 1)[-1].split("?")[0] or "download")
        res = await download_file(url, dest, client=self.config.get("http_client"), max_bytes=int(self.config.get("max_bytes", 500 * 1024**2)))
        return res.path

    def _iter_rows(self, path: Path, skip: int):
        fmt = detect_format(path, self.config.get("format"))
        if fmt == "xlsx":
            yield from _rows_xlsx(path, self.config, skip)
            return
        with open_member(path, self.config.get("member"), (f".{fmt}", ".csv", ".json", ".xml", ".txt", ".tsv")) as (fh, _n):
            if fmt == "csv":
                yield from _rows_csv(fh, self.config, skip)
            elif fmt == "json":
                yield from _rows_json(fh, self.config, skip)
            elif fmt == "xml":
                yield from _rows_xml(fh, self.config, skip)
            else:
                raise NotConfigured(f"unsupported format {fmt!r}")

    async def iter_businesses(
        self, query: DiscoveryQuery, checkpoint: Checkpoint | None = None
    ) -> AsyncIterator[tuple[BusinessRecord, Checkpoint]]:
        self._entry_gate()
        if not (self.config.get("mapping") or {}).get("legal_name") and not (self.config.get("mapping") or {}).get("trading_name"):
            raise NotConfigured("config 'mapping' must map 'legal_name' (or 'trading_name')")
        path = await self._acquire()
        flt = RecordFilter(query)
        cp = checkpoint or {}
        skip = int(cp.get("row", -1)) + 1 if cp.get("file") in (None, path.name) else 0
        observed = datetime.now(UTC)
        emitted = 0
        for n, get in self._iter_rows(path, skip):
            rec = map_row(get, self.config, observed, f"{path.name}#{n}")
            if rec is None:
                continue
            rec.adapter_id = self.info.adapter_id
            if not flt.accept(self.stamp(rec)):
                continue
            yield rec, {"row": n, "file": path.name}
            emitted += 1
            if query.limit and emitted >= query.limit:
                return


UK_LICENSED_SPONSORS: dict[str, Any] = {
    "format": "csv", "country_code": "GB", "licence": OGL,
    "attribution": "Contains public sector information licensed under the Open Government Licence v3.0. Source: Home Office, Register of licensed sponsors.",
    "register_name": "uk_licensed_sponsors", "category_hint": "licensed_sponsor",
    "source_url": "https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers",
    "mapping": {"legal_name": "Organisation Name", "city": "Town/City", "region": "County", "source_ref": "Organisation Name"},
    "url_discovery": "https://www.gov.uk/api/content/government/publications/register-of-licensed-sponsors-workers (details.attachments[].url)",
}

# =========================================================================== FSA Food Hygiene Rating Scheme
FHRS_URL = "https://ratings.food.gov.uk/OpenDataFiles/FHRS{code}en-GB.xml"
FHRS_AUTHORITIES_URL = "https://api.ratings.food.gov.uk/Authorities/basic"  # header 'x-api-version: 2'
_FHRS_INDUSTRY = (
    (re.compile(r"restaurant|cafe|canteen|takeaway|sandwich|caterer|mobile", re.I), ("hospitality", "catering")),
    (re.compile(r"pub|bar|nightclub", re.I), ("hospitality", "pubs")),
    (re.compile(r"hotel|bed & breakfast|guest house|hostel", re.I), ("hospitality", "hotels")),
    (re.compile(r"hospital|childcare|caring|school|college|university", re.I), ("care_education",)),
    (re.compile(r"retail|supermarket|hypermarket", re.I), ("retail_goods",)),
    (re.compile(r"manufactur|packer|distribut|transport|importer|exporter", re.I), ("food_drink",)),
)


def fhrs_record(el: etree._Element, observed_at: datetime | None) -> BusinessRecord | None:
    name = _xml_get(el, "BusinessName")
    fid = _xml_get(el, "FHRSID")
    if not name or not fid:
        return None
    lines = [_xml_get(el, f"AddressLine{i}") for i in (1, 2, 3, 4)]
    lines = [x for x in lines if x]
    pc = _xml_get(el, "PostCode")
    pcn = normalise_postcode(pc, "GB") if pc else None
    btype = _xml_get(el, "BusinessType")
    inds: list[str] = []
    for pat, tags in _FHRS_INDUSTRY:
        if btype and pat.search(btype):
            inds.extend(t for t in tags if t not in inds)
    lat, lon = _xml_get(el, "Geocode/Latitude"), _xml_get(el, "Geocode/Longitude")
    try:
        latf, lonf = (float(lat) if lat else None), (float(lon) if lon else None)
    except ValueError:
        latf = lonf = None
    return BusinessRecord(
        source_ref=fid,
        legal_name=name,
        trading_name=name,
        status="Active",
        country_code="GB",
        city=lines[-1] if lines else None,
        postal_code=pcn,
        address=", ".join([*lines, *([pcn or pc] if pc else [])]) or None,
        category_hint=(btype or "food_business"),
        lat=latf, lon=lonf,
        source_url=f"https://ratings.food.gov.uk/business/{fid}",
        licence=OGL,
        attribution="Contains public sector information licensed under the Open Government Licence v3.0. Source: Food Standards Agency, Food Hygiene Rating Scheme.",
        observed_at=observed_at,
        raw={
            "fhrsid": fid, "business_type": btype, "rating": _xml_get(el, "RatingValue"), "rating_date": _xml_get(el, "RatingDate"),
            "local_authority": _xml_get(el, "LocalAuthorityName"), "industries": inds, "premises_name_only": True,
            "identifiers": [{"type": "fhrsid", "value": fid, "basis": "source_id", "confidence": 0.6}],
        },
    )


@register
class FsaFhrsAdapter(SourceAdapter):
    """FSA FHRS open data: one XML file per local authority.  The council's own e-mail / website in the file are IGNORED."""

    info = AdapterInfo(
        adapter_id="fsa_fhrs",
        name="Food Standards Agency -- Food Hygiene Rating Scheme open data (per local authority)",
        country_coverage=("GB",),
        source_type="open_data",
        licence=OGL,
        source_url="https://ratings.food.gov.uk/open-data/",
        access_requirements="None. Config: 'authority_codes' (e.g. ['501']; ids from the Authorities/basic API) or 'paths'; files are fetched after a robots.txt check.",
        automated_collection_permitted=True,
        rate_policy=RatePolicy(requests_per_minute=30, concurrency=1),
        fields_available=("trading_name", "address", "postal_code", "lat", "lon", "business_type", "rating"),
        incremental_support=True,
        requires_config=True,
        attribution="Contains public sector information licensed under the Open Government Licence v3.0. Source: Food Standards Agency.",
        live_verified=True,
        cost="free", credentials="none", refresh_frequency="daily (per-authority files regenerated; ExtractDate in header)",
        access_method="bulk_file", industries=("hospitality", "pubs", "hotels", "catering", "venues", "retail_goods", "food_drink"),
        allowed_usage="OGL v3.0 with attribution (licence stated by the FSA; licence text was not re-fetched from the open-data page).",
        notes=("Live-verified: FHRS501en-GB.xml (1.4 MB, EstablishmentDetail structure) and the Authorities/basic API (363 authorities, "
               "header x-api-version: 2). robots.txt on ratings.food.gov.uk returned 404 (no restrictions). Names are premises names, not "
               "legal entities; no company numbers, phones or business e-mails. LocalAuthorityEmailAddress/WebSite belong to the council "
               "and are never used. `city` is the last address line (heuristic)."),
    )

    async def health(self) -> AdapterHealth:
        if self.config.get("paths") or self.config.get("authority_codes"):
            return AdapterHealth(True, "ok", "configured")
        return AdapterHealth(False, "not_configured", "set 'authority_codes' or 'paths'")

    async def _files(self) -> list[tuple[str, Path]]:
        out: list[tuple[str, Path]] = []
        for p in self.config.get("paths") or []:
            out.append((Path(p).name, Path(p)))
        codes = [str(c) for c in self.config.get("authority_codes") or []]
        if codes:
            from ..config import get_settings

            df = self.config.get("directory_fetcher") or DirectoryFetcher(self.config.get("fetcher"), self.config.get("robots"))
            for code in codes:
                url = FHRS_URL.format(code=code)
                await df.check(url)
                dest = Path(self.config.get("data_dir") or get_settings().data_dir) / "fsa_fhrs" / f"FHRS{code}en-GB.xml"
                res = await download_file(url, dest, client=self.config.get("http_client"), max_bytes=int(self.config.get("max_bytes", 200 * 1024**2)))
                out.append((f"FHRS{code}", res.path))
        if not out:
            raise NotConfigured("set 'authority_codes' or 'paths'")
        return out

    async def iter_businesses(
        self, query: DiscoveryQuery, checkpoint: Checkpoint | None = None
    ) -> AsyncIterator[tuple[BusinessRecord, Checkpoint]]:
        if not query.covers_country("GB"):
            return
        files = await self._files()
        names = [n for n, _ in files]
        cp = checkpoint or {}
        start_i, skip = 0, 0
        if cp.get("file") in names:
            start_i, skip = names.index(cp["file"]), int(cp.get("n", -1)) + 1
        flt = RecordFilter(query)
        observed = datetime.now(UTC)
        emitted = 0
        for idx in range(start_i, len(files)):
            name, path = files[idx]
            with open(path, "rb") as fh:
                for n, el in _enumerate_tag(fh, "EstablishmentDetail", skip if idx == start_i else 0):
                    rec = fhrs_record(el, observed)
                    if rec is None:
                        continue
                    rec.adapter_id = self.info.adapter_id
                    if not flt.accept(rec):
                        continue
                    yield rec, {"file": name, "n": n}
                    emitted += 1
                    if query.limit and emitted >= query.limit:
                        return


def _enumerate_tag(fh, tag: str, skip: int) -> Iterator[tuple[int, etree._Element]]:
    n = -1
    for _ev, el in etree.iterparse(fh, events=("end",), tag=tag, resolve_entities=False, no_network=True, huge_tree=True):
        n += 1
        if n >= skip:
            yield n, el
        el.clear()
        parent = el.getparent()
        if parent is not None:
            while el.getprevious() is not None:
                del parent[0]


# =========================================================================== discovery helper (Source Explorer)
_CATEGORY_ALIASES: dict[str, tuple[str, ...]] = {
    "estate_agents": ("estate agent", "estate agents", "estate_agent", "property sales"),
    "letting_agents": ("letting agent", "letting agents", "lettings", "letting_agent"),
    "property_managers": ("property manager", "property management", "block manager", "block_managers"),
    "electricians": ("electrician", "electrical"),
    "plumbers": ("plumber", "plumbing", "heating"),
    "pubs": ("pub", "bar", "pubs"),
    "venues": ("venue", "event venue", "venues", "function room"),
    "hotels": ("hotel", "b&b", "accommodation"),
    "catering": ("caterer", "catering", "restaurant", "cafe"),
    "accountants": ("accountant", "accounting"),
    "trades": ("trade", "tradesperson", "tradesmen", "builder"),
    "events": ("event", "exhibition", "conference"),
}


def _cat_tokens(category: str) -> set[str]:
    base = re.sub(r"[^a-z0-9]+", " ", category.lower()).strip()
    toks = {base, base.replace(" ", "_")}
    for k, al in _CATEGORY_ALIASES.items():
        if base == k.replace("_", " ") or base in al:
            toks.add(k)
    return {t for t in toks if t}


@dataclass
class DirectoryMatch:
    entry: dict[str, Any]
    permission: PermissionStatus
    score: float
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        e = self.entry
        return {
            "id": e["id"], "name": e["name"], "url": e["url"], "kind": e["kind"], "countries": e["countries"],
            "categories": e["categories"], "automated_extraction": e["automated_extraction"],
            "recommended_access": e["recommended_access"], "licence_notes": e["licence_notes"], "adapter_id": e.get("adapter_id"),
            "permission": self.permission.to_dict(), "relevance": self.score, "relevance_reasons": self.reasons,
        }


class DirectoryDiscovery:
    """Find associations / directories / registers relevant to a category + country, with their permission status."""

    def __init__(self, entries: list[dict[str, Any]] | None = None, configs: dict[str, dict[str, Any]] | None = None):
        self.entries = entries if entries is not None else load_catalogue()
        self.configs = configs or {}  # per-entry adapter config (carries permission_basis)

    def permission_for(self, entry: dict[str, Any]) -> PermissionStatus:
        return evaluate_permission(entry, self.configs.get(entry["id"]))

    def find(self, category: str | None = None, country: str | None = None, *, include_blocked: bool = True,
             limit: int | None = None) -> list[DirectoryMatch]:
        cc = country.upper() if country else None
        toks = _cat_tokens(category) if category else set()
        out: list[DirectoryMatch] = []
        for e in self.entries:
            reasons: list[str] = []
            score = 0.0
            if cc:
                if cc in e["countries"]:
                    score += 3
                    reasons.append(f"country {cc} listed")
                elif "*" in e["countries"]:
                    score += 1
                    reasons.append("worldwide source")
                else:
                    continue
            cats = set(e["categories"])
            if category:
                if toks & cats:
                    score += 10
                    reasons.append("category match")
                elif any(t in c or c in t for t in toks for c in cats if c != "*"):
                    score += 6
                    reasons.append("category partial match")
                elif "*" in cats:
                    score += 2
                    reasons.append("all categories")
                else:
                    continue
            perm = self.permission_for(e)
            if perm.allowed:
                score += 2
                reasons.append("automation permitted")
            elif not include_blocked:
                continue
            if e.get("adapter_id"):
                score += 1
                reasons.append("adapter implemented")
            out.append(DirectoryMatch(e, perm, score, reasons))
        out.sort(key=lambda m: (-m.score, m.entry["id"]))
        return out[:limit] if limit else out


__all__ = [
    "PermissionStatus", "evaluate_permission", "require_permission", "DirectoryFetcher", "PermissionGatedSiteAdapter",
    "OpenDataMemberRegisterAdapter", "FsaFhrsAdapter", "DirectoryDiscovery", "DirectoryMatch", "UK_LICENSED_SPONSORS",
    "fhrs_record", "NotPermitted", "AdapterError",
]
