"""UK Companies House "BasicCompanyData" monthly snapshot adapter.

Source: https://download.companieshouse.gov.uk/en_output.html  (product ZIP of one CSV, ~5M rows).
Licence: Open Government Licence v3.0 (attribution: "Contains public sector information licensed under the
Open Government Licence v3.0 / Companies House").  The file holds no websites or emails; ``URI`` is a
linked-data identifier, NOT a company website, and is deliberately ignored.  The per-company REST API is
never called from here.

Streaming: the zip member is read through ``zipfile.ZipFile.open`` + ``io.TextIOWrapper`` + ``csv.reader``
(constant memory).  Resume uses a data-row offset (deflate streams are not seekable).
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx

from ..config import get_settings
from .base import (
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
from .download import download_file, make_client
from .geo_filters import RecordFilter, normalise_postcode

ADAPTER_ID = "companies_house_bulk"
PRODUCT_PAGE = "https://download.companieshouse.gov.uk/en_output.html"
DOWNLOAD_BASE = "https://download.companieshouse.gov.uk/"
LICENCE = "Open Government Licence v3.0"
ATTRIBUTION = "Contains public sector information licensed under the Open Government Licence v3.0. Source: Companies House."
_SIC_RE = re.compile(r"^\s*(\d{4,5})")
_FILE_RE = re.compile(r"BasicCompanyDataAsOneFile-\d{4}-\d{2}-\d{2}\.zip")
_MAX_FIELD = 1 << 20

# canonical (lower-case, whitespace stripped) header -> internal key
_HEADERS = {
    "companyname": "name", "companynumber": "number", "companycategory": "category", "companystatus": "status",
    "countryoforigin": "origin", "incorporationdate": "incorporated", "dissolutiondate": "dissolved",
    "regaddress.careof": "care_of", "regaddress.pobox": "po_box", "regaddress.addressline1": "line1",
    "regaddress.addressline2": "line2", "regaddress.posttown": "town", "regaddress.county": "county",
    "regaddress.country": "country", "regaddress.postcode": "postcode",
    "siccode.sictext_1": "sic1", "siccode.sictext_2": "sic2", "siccode.sictext_3": "sic3", "siccode.sictext_4": "sic4",
}


def _canon_header(h: str) -> str:
    return re.sub(r"\s+", "", h.replace("﻿", "")).lower()


def _open_csv(path: Path, member: str | None):
    """Return (binary stream, closer, member name) for a .zip (first/ named .csv member) or plain .csv."""
    if zipfile.is_zipfile(path):
        zf = zipfile.ZipFile(path)
        names = [i.filename for i in zf.infolist() if i.filename.lower().endswith(".csv") and not i.is_dir()]
        if member:
            names = [n for n in names if n == member]
        if not names:
            zf.close()
            raise NotConfigured(f"no CSV member found in {path}")
        fh = zf.open(names[0])
        return fh, zf.close, names[0]
    fh = open(path, "rb")
    return fh, fh.close, path.name


def iter_rows(path: Path, *, member: str | None = None, skip: int = 0):
    """Yield ``(row_index, mapped_row)`` for data rows (0-based) after skipping ``skip`` rows."""
    csv.field_size_limit(_MAX_FIELD)
    raw, closer, _ = _open_csv(path, member)
    try:
        text_io = io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline="")
        reader = csv.reader(text_io)
        header = next(reader, None)
        if header is None:
            return
        idx: dict[str, int] = {}
        for i, h in enumerate(header):
            key = _HEADERS.get(_canon_header(h))
            if key and key not in idx:
                idx[key] = i
        if "number" not in idx or "name" not in idx:
            raise NotConfigured("not a Companies House BasicCompanyData file (CompanyName/CompanyNumber missing)")
        n = -1
        for row in reader:
            n += 1
            if n < skip:
                continue
            if not row:
                continue
            yield n, {k: (row[i].strip() if i < len(row) else "") for k, i in idx.items()}
    finally:
        closer()


def row_to_record(r: dict[str, str], observed_at: datetime | None = None) -> BusinessRecord | None:
    number = r.get("number", "").strip().upper()
    name = r.get("name", "").strip()
    if not number or not name:
        return None
    sics = []
    for k in ("sic1", "sic2", "sic3", "sic4"):
        m = _SIC_RE.match(r.get(k, ""))
        if m and m.group(1) not in sics:
            sics.append(m.group(1))
    addr_parts = [r.get(k, "") for k in ("care_of", "po_box", "line1", "line2", "town", "county", "country", "postcode")]
    address = ", ".join(p for p in addr_parts if p)
    pc = normalise_postcode(r.get("postcode"), "GB") if r.get("postcode") else None
    return BusinessRecord(
        source_ref=number,
        legal_name=name,
        company_number=number,
        legal_type=r.get("category") or None,
        status=r.get("status") or None,
        country_code="GB",
        region=r.get("county") or None,
        city=r.get("town") or None,
        postal_code=pc,
        address=address or None,
        sic_codes=sics,
        source_url=f"https://find-and-update.company-information.service.gov.uk/company/{number}",
        licence=LICENCE,
        attribution=ATTRIBUTION,
        observed_at=observed_at,
        raw={
            "incorporation_date": r.get("incorporated") or None,
            "dissolution_date": r.get("dissolved") or None,
            "country_of_origin": r.get("origin") or None,
        },
    )


@register
class CompaniesHouseBulkAdapter(SourceAdapter):
    info = AdapterInfo(
        adapter_id=ADAPTER_ID,
        name="UK Companies House BasicCompanyData (monthly snapshot)",
        country_coverage=("GB",),
        source_type="bulk_dataset",
        licence=LICENCE,
        source_url=PRODUCT_PAGE,
        access_requirements="None (public download, ~470 MB zip). Config: 'path' (local zip/csv) or 'url'/'auto_download'.",
        automated_collection_permitted=True,
        rate_policy=RatePolicy(requests_per_minute=None, concurrency=1),
        fields_available=(
            "legal_name", "company_number", "legal_type", "status", "region", "city", "postal_code", "address",
            "sic_codes", "incorporation_date",
        ),
        incremental_support=True,
        parser_version="1",
        requires_config=False,
        attribution=ATTRIBUTION,
        live_verified=False,
        notes=(
            "Product page reachable at build time (HTTP 200, file names BasicCompanyDataAsOneFile-YYYY-MM-DD.zip "
            "confirmed). The full ~470 MB zip was not downloaded here; parsing is tested on locally generated "
            "synthetic fixtures in the documented column layout."
        ),
    )

    # -- file acquisition -------------------------------------------------
    @property
    def data_dir(self) -> Path:
        return Path(self.config.get("data_dir") or get_settings().data_dir) / "companies_house"

    async def resolve_latest_url(self, client: httpx.AsyncClient | None = None) -> str:
        own = client is None
        cli = client or make_client()
        try:
            r = await cli.get(PRODUCT_PAGE)
            r.raise_for_status()
            m = _FILE_RE.search(r.text)
            if not m:
                raise NotConfigured("could not find BasicCompanyDataAsOneFile zip link on the product page")
            return DOWNLOAD_BASE + m.group(0)
        finally:
            if own:
                await cli.aclose()

    async def ensure_file(self, client: httpx.AsyncClient | None = None) -> Path:
        p = self.config.get("path")
        if p:
            path = Path(p)
            if not path.exists():
                raise NotConfigured(f"configured path does not exist: {path}")
            return path
        url = self.config.get("url")
        if not url and self.config.get("auto_download"):
            url = await self.resolve_latest_url(client)
        if not url:
            raise NotConfigured("set config 'path' to a downloaded BasicCompanyData zip/csv, or 'url'/'auto_download'")
        dest = self.data_dir / url.rsplit("/", 1)[-1]
        res = await download_file(url, dest, client=client, max_bytes=int(self.config.get("max_bytes", 2 * 1024**3)))
        return res.path

    async def health(self) -> AdapterHealth:
        p = self.config.get("path")
        if p:
            ok = Path(p).exists()
            return AdapterHealth(ok, "ok" if ok else "unavailable", f"file {'present' if ok else 'missing'}: {p}")
        if self.config.get("url") or self.config.get("auto_download"):
            return AdapterHealth(True, "ok", "will download on first run (not verified)")
        return AdapterHealth(False, "not_configured", "no 'path', 'url' or 'auto_download' configured")

    # -- iteration ---------------------------------------------------------
    async def iter_businesses(
        self, query: DiscoveryQuery, checkpoint: Checkpoint | None = None
    ) -> AsyncIterator[tuple[BusinessRecord, Checkpoint]]:
        if not query.covers_country("GB"):
            return
        path = await self.ensure_file(self.config.get("http_client"))
        flt = RecordFilter(query)
        cp = checkpoint or {}
        # row offsets are only meaningful for the same snapshot file; a new monthly file restarts at 0
        skip = int(cp.get("row", -1)) + 1 if cp.get("file") in (None, path.name) else 0
        emitted = 0
        observed = datetime.now(UTC)
        for n, row in iter_rows(path, member=self.config.get("member"), skip=skip):
            rec = row_to_record(row, observed)
            if rec is None:
                continue
            rec.adapter_id = ADAPTER_ID
            if not flt.accept(rec):
                continue
            yield rec, {"row": n, "file": path.name}
            emitted += 1
            if query.limit and emitted >= query.limit:
                return


__all__ = ["CompaniesHouseBulkAdapter", "iter_rows", "row_to_record"]

