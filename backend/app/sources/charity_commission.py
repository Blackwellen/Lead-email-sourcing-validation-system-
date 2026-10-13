"""Charity Commission for England and Wales -- register of charities public data extract.

Source page: https://register-of-charities.charitycommission.gov.uk/register/full-register-download (the page
links ``.../data/json/publicextract.charity.zip`` and ``.../data/txt/publicextract.charity.zip``; both were
fetched at build time -- the JSON member is one ~500 MB JSON array, the TXT member is a tab-separated file with
the same column names).  Licence: Open Government Licence v3.0 (stated on the download page).

Mapping: ``registered_charity_number`` + ``linked_charity_number`` -> ``source_ref`` ``"<no>-<linked>"``;
``charity_company_registration_number`` -> ``company_number`` (so incorporated charities merge with the
Companies House identity); ``charity_contact_web`` -> candidate ``website`` (never confirmed here);
``charity_contact_email`` -> ``email_published`` ONLY because the register itself publishes it.
Streaming: JSON via :func:`iter_json_array`, TXT via ``csv``; resume uses the source-item offset.
"""
from __future__ import annotations

import csv
import io
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._stream import ensure_local_file, gb_company_number, iter_json_array, open_member
from .base import (
    AdapterHealth,
    AdapterInfo,
    BusinessRecord,
    Checkpoint,
    DiscoveryQuery,
    RatePolicy,
    SourceAdapter,
    register,
)
from .geo_filters import RecordFilter, normalise_postcode

ADAPTER_ID = "charity_commission_ew"
PAGE = "https://register-of-charities.charitycommission.gov.uk/register/full-register-download"
JSON_URL = "https://ccewuksprdoneregsadata1.blob.core.windows.net/data/json/publicextract.charity.zip"
LICENCE = "Open Government Licence v3.0"
ATTRIBUTION = "Contains public sector information licensed under the Open Government Licence v3.0. Source: Charity Commission for England and Wales."
csv.field_size_limit(1 << 24)


def _s(v: Any) -> str | None:
    if v is None:
        return None
    t = str(v).strip()
    return t or None


def iter_extract(path: Path, *, member: str | None = None, skip: int = 0) -> Iterator[tuple[int, dict[str, Any], str]]:
    """Yield ``(index, row_dict, member_name)`` for every charity row (JSON array or TSV) after ``skip`` rows."""
    with open_member(path, member, (".json", ".txt", ".tsv", ".csv")) as (fh, name):
        lname = name.lower()
        if lname.endswith(".json"):
            for i, obj in enumerate(iter_json_array(fh)):
                if i < skip:
                    continue
                if isinstance(obj, dict):
                    yield i, obj, name
            return
        text = io.TextIOWrapper(fh, encoding="utf-8-sig", errors="replace", newline="")
        reader = csv.reader(text, delimiter="," if lname.endswith(".csv") else "\t", quoting=csv.QUOTE_NONE if not lname.endswith(".csv") else csv.QUOTE_MINIMAL)
        header = next(reader, None)
        if not header:
            return
        header = [h.strip() for h in header]
        for i, row in enumerate(reader):
            if i < skip:
                continue
            if not row:
                continue
            yield i, {h: (row[j].strip() if j < len(row) else None) or None for j, h in enumerate(header)}, name


def row_to_record(d: dict[str, Any], observed_at: datetime | None = None) -> BusinessRecord | None:
    name = _s(d.get("charity_name"))
    num = _s(d.get("registered_charity_number"))
    if not name or not num:
        return None
    linked = _s(d.get("linked_charity_number")) or "0"
    lines = [x for x in (_s(d.get(f"charity_contact_address{i}")) for i in range(1, 6)) if x]
    pc = _s(d.get("charity_contact_postcode"))
    pcn = normalise_postcode(pc, "GB") if pc else None
    address = ", ".join([*lines, *([pcn or pc] if pc else [])]) or None
    status = _s(d.get("charity_registration_status")) or None
    web = _s(d.get("charity_contact_web"))
    activities = _s(d.get("charity_activities"))
    cn = gb_company_number(_s(d.get("charity_company_registration_number")))
    rec = BusinessRecord(
        source_ref=f"{num}-{linked}",
        legal_name=name,
        company_number=cn,
        legal_type="Charity" + (f" ({_s(d.get('charity_type'))})" if _s(d.get("charity_type")) else ""),
        status=status,
        country_code="GB",
        postal_code=pcn,
        address=address,
        phone=_s(d.get("charity_contact_phone")),
        website=web,
        email_published=_s(d.get("charity_contact_email")),
        category_hint="charity",
        source_url=f"https://register-of-charities.charitycommission.gov.uk/charity-search/-/charity-details/{num}",
        licence=LICENCE,
        attribution=ATTRIBUTION,
        observed_at=observed_at,
        raw={
            "charity_number": num, "linked_number": linked,
            "date_of_registration": _s(d.get("date_of_registration")), "date_of_removal": _s(d.get("date_of_removal")),
            "latest_income": d.get("latest_income"), "latest_expenditure": d.get("latest_expenditure"),
            "insolvent": d.get("charity_insolvent"), "is_cio": d.get("charity_is_cio"),
            "activities": (activities or "")[:600] or None,
            "identifiers": [{"type": "charity_no", "value": num}],
        },
    )
    return rec


@register
class CharityCommissionAdapter(SourceAdapter):
    info = AdapterInfo(
        adapter_id=ADAPTER_ID,
        name="Charity Commission for England and Wales -- register extract",
        country_coverage=("GB",),  # England and Wales only (Scotland: OSCR, N. Ireland: CCNI are separate registers)
        source_type="bulk_dataset",
        licence=LICENCE,
        source_url=PAGE,
        access_requirements="None. Public JSON/TXT zip download (~55 MB zipped, ~500 MB JSON). Config: 'path' or 'url'.",
        automated_collection_permitted=True,
        rate_policy=RatePolicy(requests_per_minute=None, concurrency=1),
        fields_available=("legal_name", "company_number", "status", "address", "postal_code", "phone", "website",
                          "email_published", "activities"),
        incremental_support=True,
        requires_config=True,
        attribution=ATTRIBUTION,
        live_verified=True,
        cost="free", credentials="none", refresh_frequency="weekly (extract dated the day of download; cadence per Commission, not independently measured)",
        access_method="bulk_file", industries=("charity", "third_sector", "property", "events", "education", "hospitality"),
        allowed_usage="OGL v3.0: copy, publish, adapt and exploit commercially with attribution; personal data (e.g. contact emails) remains subject to UK GDPR/PECR.",
        notes=("Live-verified at build time: download page and both zip URLs return HTTP 200; the JSON member header and the TXT "
               "column header were inspected (field names above). Contact email/phone/web are published by the register and "
               "may be empty for most charities. `city` is not populated (address lines are free text) -- filter by postcode."),
    )

    async def health(self) -> AdapterHealth:
        p = self.config.get("path")
        if p:
            ok = Path(p).exists()
            return AdapterHealth(ok, "ok" if ok else "unavailable", f"file {'present' if ok else 'missing'}: {p}")
        if self.config.get("url"):
            return AdapterHealth(True, "ok", "will download on first run (not verified)")
        return AdapterHealth(False, "not_configured", f"set 'path' or 'url' (e.g. {JSON_URL})")

    async def iter_businesses(
        self, query: DiscoveryQuery, checkpoint: Checkpoint | None = None
    ) -> AsyncIterator[tuple[BusinessRecord, Checkpoint]]:
        if not query.covers_country("GB"):
            return
        path = await ensure_local_file(self.config, ADAPTER_ID)
        flt = RecordFilter(query)
        cp = checkpoint or {}
        skip = int(cp.get("row", -1)) + 1 if cp.get("file") in (None, path.name) else 0
        observed = datetime.now(UTC)
        emitted = 0
        for i, row, _member in iter_extract(path, member=self.config.get("member"), skip=skip):
            rec = row_to_record(row, observed)
            if rec is None:
                continue
            rec.adapter_id = ADAPTER_ID
            if not flt.accept(rec):
                continue
            yield rec, {"row": i, "file": path.name}
            emitted += 1
            if query.limit and emitted >= query.limit:
                return


__all__ = ["CharityCommissionAdapter", "iter_extract", "row_to_record"]
