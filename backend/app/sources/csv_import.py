"""Operator-uploaded CSV / XLSX import of businesses and/or domain lists ("Imported URL lists", Scenario C).

Safety posture (the file is untrusted input):
  * hard limits on bytes, rows, columns, field length; streamed, never fully loaded
  * encoding detection without third-party deps (BOM / UTF-16 / UTF-8 / cp1252 fallback; NUL bytes in a
    non-UTF-16 file => binary => rejected)
  * XLSX is opened with ``read_only=True, data_only=True`` only after inspecting the zip container
    (entry count, total uncompressed size, per-entry compression ratio, macros, path traversal => zip-bomb guard)
  * cells starting with ``=`` or ``@`` (spreadsheet formulas) are dropped, never evaluated
  * domains are validated syntactically only -- nothing is fetched here
  * an ``email`` column is deliberately IGNORED: this importer provides businesses/domains; emails must be
    discovered from the business's own published pages by the crawler stage.
"""
from __future__ import annotations

import codecs
import csv
import re
import zipfile
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
from .persist import normalise_name
from .urls import normalise_website

ADAPTER_ID = "csv_import"
DEFAULT_LICENCE = "operator-supplied (uploader asserts the right to use this data)"

HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "domain": ("domain", "domainname", "website", "websiteurl", "url", "web", "site", "homepage", "webaddress", "weburl", "domains"),
    "name": ("companyname", "name", "businessname", "company", "organisation", "organization", "legalname", "business", "orgname"),
    "trading_name": ("tradingname", "tradename", "brand", "brandname", "tradingas"),
    "company_number": ("companynumber", "companyno", "companynum", "crn", "registrationnumber", "regno", "regnumber",
                       "companieshousenumber", "companyregistrationnumber", "number"),
    "country": ("country", "countrycode", "iso", "iso2", "cc"),
    "region": ("region", "county", "state", "province"),
    "city": ("city", "town", "posttown", "locality"),
    "postcode": ("postcode", "postalcode", "zip", "zipcode", "postcodezip"),
    "phone": ("phone", "telephone", "tel", "phonenumber", "mobile"),
    "address": ("address", "fulladdress", "streetaddress", "address1"),
    "category": ("category", "industry", "sector", "type", "sic", "keyword"),
}
_IGNORED_EMAIL = ("email", "emailaddress", "mail", "contactemail")
_ALIAS_LOOKUP = {a: k for k, al in HEADER_ALIASES.items() for a in al}

MAX_FIELD_LEN = 2000
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ImportRejected(AdapterError):
    """The file as a whole is unacceptable (too large, binary, zip bomb, macros ...)."""


@dataclass
class ImportReport:
    rows_read: int = 0
    accepted: int = 0
    rejected: int = 0
    duplicates: int = 0
    formula_cells_dropped: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
    sample_errors: list[dict[str, Any]] = field(default_factory=list)
    columns_mapped: dict[str, str] = field(default_factory=dict)
    columns_ignored: list[str] = field(default_factory=list)
    encoding: str | None = None

    def reject(self, row: int, reason: str) -> None:
        self.rejected += 1
        self.reasons[reason] = self.reasons.get(reason, 0) + 1
        if len(self.sample_errors) < 50:
            self.sample_errors.append({"row": row, "reason": reason})


# --------------------------------------------------------------------------- limits / detection
@dataclass(frozen=True)
class ImportLimits:
    max_bytes: int = 50 * 1024 * 1024
    max_rows: int = 200_000
    max_cols: int = 100
    xlsx_max_entries: int = 200
    xlsx_max_uncompressed: int = 256 * 1024 * 1024
    xlsx_max_ratio: int = 200

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> ImportLimits:
        base = cls(max_bytes=get_settings().max_upload_mb * 1024 * 1024)
        return cls(
            max_bytes=int(cfg.get("max_bytes", base.max_bytes)),
            max_rows=int(cfg.get("max_rows", base.max_rows)),
            max_cols=int(cfg.get("max_cols", base.max_cols)),
        )


def detect_encoding(sample: bytes) -> str:
    if sample.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if sample.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    if b"\x00" in sample:
        raise ImportRejected("binary data (NUL bytes) in a file claimed to be CSV")
    dec = codecs.getincrementaldecoder("utf-8")()
    try:
        dec.decode(sample, final=False)  # tolerate a multibyte char cut at the sample boundary
        return "utf-8"
    except UnicodeDecodeError:
        return "cp1252"


def inspect_xlsx(path: Path, limits: ImportLimits) -> None:
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise ImportRejected("not a valid XLSX (zip) file") from e
    with zf:
        infos = zf.infolist()
        if len(infos) > limits.xlsx_max_entries:
            raise ImportRejected(f"XLSX has too many archive entries ({len(infos)})")
        total = 0
        for i in infos:
            n = i.filename
            if n.startswith("/") or ".." in n.split("/") or "\\" in n:
                raise ImportRejected("XLSX contains unsafe entry paths")
            if n.lower().endswith("vbaproject.bin"):
                raise ImportRejected("macro-enabled workbooks are not accepted")
            total += i.file_size
            if total > limits.xlsx_max_uncompressed:
                raise ImportRejected("XLSX uncompressed size exceeds limit (possible zip bomb)")
            if i.file_size > 1_000_000 and i.compress_size > 0 and i.file_size / i.compress_size > limits.xlsx_max_ratio:
                raise ImportRejected("XLSX entry compression ratio is implausible (possible zip bomb)")
            if i.file_size > 1_000_000 and i.compress_size == 0:
                raise ImportRejected("XLSX entry with zero compressed size")
        if "[Content_Types].xml" not in zf.namelist() or not any(n.startswith("xl/") for n in zf.namelist()):
            raise ImportRejected("not an XLSX workbook")


def _clean_cell(v: Any, report: ImportReport) -> str | None:
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = _CTRL.sub("", str(v)).strip()
    if not s:
        return None
    if s[0] in "=@" or s.startswith(("\t=", "\r=")):
        report.formula_cells_dropped += 1
        return None
    return s[:MAX_FIELD_LEN]


def _canon_header(h: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(h or "").lower())


def map_headers(headers: list[Any], report: ImportReport) -> dict[int, str]:
    mapping: dict[int, str] = {}
    used: set[str] = set()
    for i, h in enumerate(headers):
        c = _canon_header(h)
        key = _ALIAS_LOOKUP.get(c)
        if key and key not in used:
            mapping[i] = key
            used.add(key)
            report.columns_mapped[str(h)] = key
        elif c:
            report.columns_ignored.append(str(h)[:60])
    return mapping


# --------------------------------------------------------------------------- row sources
def _iter_csv_rows(path: Path, limits: ImportLimits, report: ImportReport) -> Iterator[list[Any]]:
    with open(path, "rb") as fh:
        sample = fh.read(1 << 20)
    enc = detect_encoding(sample)
    report.encoding = enc
    text_sample = sample.decode(enc if enc != "utf-8" else "utf-8", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text_sample[:65536], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    csv.field_size_limit(MAX_FIELD_LEN * 8)
    with open(path, encoding=enc, errors="replace", newline="") as fh:
        for row in csv.reader(fh, dialect):
            if len(row) > limits.max_cols:
                raise ImportRejected(f"too many columns ({len(row)})")
            yield row


def _iter_xlsx_rows(path: Path, limits: ImportLimits, sheet: str | None) -> Iterator[list[Any]]:
    inspect_xlsx(path, limits)
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[sheet] if sheet else wb.worksheets[0]
        for row in ws.iter_rows(values_only=True, max_col=limits.max_cols + 1):
            if len(row) > limits.max_cols:
                raise ImportRejected(f"too many columns (>{limits.max_cols})")
            yield list(row)
    finally:
        wb.close()


def iter_import_rows(
    path: Path, *, limits: ImportLimits, report: ImportReport, default_country: str | None,
    sheet: str | None = None, licence: str | None = None, label: str | None = None,
) -> Iterator[tuple[int, BusinessRecord]]:
    """Yield ``(row_index, record)`` for valid, de-duplicated rows. Updates ``report`` in place."""
    path = Path(path)
    if not path.is_file():
        raise NotConfigured(f"import file not found: {path}")
    size = path.stat().st_size
    if size == 0:
        raise ImportRejected("empty file")
    if size > limits.max_bytes:
        raise ImportRejected(f"file too large ({size} bytes > {limits.max_bytes})")
    is_xlsx = path.suffix.lower() in (".xlsx", ".xlsm") or zipfile.is_zipfile(path)
    if path.suffix.lower() == ".xls":
        raise ImportRejected("legacy .xls is not supported; save as .xlsx or .csv")
    rows = _iter_xlsx_rows(path, limits, sheet) if is_xlsx else _iter_csv_rows(path, limits, report)
    mapping: dict[int, str] | None = None
    seen: set[str] = set()
    n = -1
    now = datetime.now(UTC)
    for raw in rows:
        if mapping is None:
            if not any(c not in (None, "") for c in raw):
                continue
            mapping = map_headers(raw, report)
            if "domain" not in mapping.values() and "name" not in mapping.values():
                raise ImportRejected("no recognisable header: need a domain/website column and/or a company name column")
            ignored_mail = [h for h in raw if _canon_header(h) in _IGNORED_EMAIL]
            if ignored_mail:
                report.columns_ignored.append("email (ignored by design)")
            continue
        n += 1
        report.rows_read += 1
        if n >= limits.max_rows:
            raise ImportRejected(f"row limit exceeded ({limits.max_rows})")
        cells: dict[str, str | None] = {k: None for k in HEADER_ALIASES}
        for i, key in mapping.items():
            if i < len(raw):
                cells[key] = _clean_cell(raw[i], report)
        if not any(cells.values()):
            report.rows_read -= 1
            continue
        site = None
        if cells["domain"]:
            for part in re.split(r"[;,\s|]+", cells["domain"]):
                site = normalise_website(part)
                if site:
                    break
            if site is None:
                report.reject(n, "invalid_domain")
                continue
        name = cells["name"]
        if not site and not name:
            report.reject(n, "no_domain_or_name")
            continue
        cc = (cells["country"] or default_country or "").strip().upper()
        if len(cc) == 3 or cc in ("UK", "UNITED KINGDOM", "ENGLAND", "SCOTLAND", "WALES", "NORTHERN IRELAND"):
            cc = "GB"
        if len(cc) != 2 or not cc.isalpha():
            report.reject(n, "missing_country")
            continue
        cn = re.sub(r"\s+", "", cells["company_number"] or "").upper() or None
        if cn:
            key = f"cn:{cc}:{cn}"
        elif site:
            key = f"d:{site.host}"
        else:
            key = f"n:{cc}:{normalise_name(name)}:{cells['postcode'] or cells['city'] or ''}"
        if key in seen:
            report.duplicates += 1
            continue
        seen.add(key)
        derived = False
        if not name and site:
            name, derived = site.host, True
        legal: str | None
        trading: str | None
        if cn:
            legal, trading = name, cells["trading_name"]
        elif cells["trading_name"]:
            legal, trading = (None if derived else name), cells["trading_name"]
        else:
            legal, trading = None, name
        report.accepted += 1
        yield n, BusinessRecord(
            source_ref=(cn or (site.registrable if site else normalise_name(name)))[:300],
            legal_name=legal,
            trading_name=trading,
            company_number=cn,
            status="active",
            country_code=cc,
            region=cells["region"],
            city=cells["city"],
            postal_code=cells["postcode"],
            address=cells["address"],
            phone=cells["phone"],
            website=site.url if site else None,
            category_hint=cells["category"],
            raw={"import_row": n, "import_label": label, **({"name_derived_from_domain": True} if derived else {})},
            licence=licence or DEFAULT_LICENCE,
            observed_at=now,
            source_url=None,
        )


def validate_import_file(path: Path, *, default_country: str | None = None, config: dict[str, Any] | None = None) -> ImportReport:
    """Dry run: parse everything, return the report (counts, reasons, mapped columns). No DB, no network."""
    cfg = config or {}
    rep = ImportReport()
    for _ in iter_import_rows(Path(path), limits=ImportLimits.from_config(cfg), report=rep, default_country=default_country,
                              sheet=cfg.get("sheet")):
        pass
    return rep


@register
class CsvImportAdapter(SourceAdapter):
    info = AdapterInfo(
        adapter_id=ADAPTER_ID,
        name="Imported business / URL list (CSV, XLSX)",
        country_coverage=("*",),
        source_type="manual_import",
        licence=DEFAULT_LICENCE,
        source_url="",
        access_requirements="Operator uploads a CSV/XLSX; config 'path' (+ optional 'country_code', 'sheet', limits).",
        automated_collection_permitted=True,
        rate_policy=RatePolicy(requests_per_minute=None),
        fields_available=("legal_name", "trading_name", "company_number", "country", "region", "city", "postal_code",
                          "address", "phone", "website"),
        incremental_support=True,
        parser_version="1",
        requires_config=True,
        live_verified=True,
        notes="Local file parsing only; no network access. Emails in the upload are ignored by design.",
    )

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self.last_report = ImportReport()

    async def health(self) -> AdapterHealth:
        p = self.config.get("path")
        if not p:
            return AdapterHealth(False, "not_configured", "no import file 'path' configured")
        return AdapterHealth(Path(p).is_file(), "ok" if Path(p).is_file() else "unavailable", str(p))

    async def iter_businesses(
        self, query: DiscoveryQuery, checkpoint: Checkpoint | None = None
    ) -> AsyncIterator[tuple[BusinessRecord, Checkpoint]]:
        p = self.config.get("path")
        if not p:
            raise NotConfigured("csv_import needs config 'path'")
        self.last_report = rep = ImportReport()
        default_cc = self.config.get("country_code") or (query.countries[0] if len(query.countries) == 1 else None)
        start = int((checkpoint or {}).get("row", -1))
        emitted = 0
        wanted = {c.upper() for c in query.countries}
        for n, rec in iter_import_rows(
            Path(p), limits=ImportLimits.from_config(self.config), report=rep, default_country=default_cc,
            sheet=self.config.get("sheet"), licence=self.config.get("licence"), label=self.config.get("label"),
        ):
            if n <= start:
                continue  # already delivered in an earlier session (dedupe state was rebuilt by the parse above)
            if wanted and rec.country_code not in wanted:
                continue
            rec.adapter_id = ADAPTER_ID
            yield rec, {"row": n}
            emitted += 1
            if query.limit and emitted >= query.limit:
                return


__all__ = ["CsvImportAdapter", "ImportLimits", "ImportReport", "ImportRejected", "validate_import_file", "iter_import_rows"]

