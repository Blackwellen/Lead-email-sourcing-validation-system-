"""Australian Business Register -- ABN Bulk Extract (data.gov.au) with optional ABN Lookup web-service enrichment.

Primary source (no credentials): the *ABN Bulk Extract* dataset, ``https://data.gov.au/data/dataset/abn-bulk-extract``
(CKAN ``package_show`` verified live: licence "Creative Commons Attribution 3.0 Australia", two ~500 MB zip parts
``public_split_1_10.zip`` / ``public_split_11_20.zip`` of XML files, schema ``bulkextract.xsd`` which was fetched and used to
write this parser).  XML shape: ``Transfer/ABR`` records with ``ABN[@status,@ABNStatusFromDate]``, ``EntityType``,
``MainEntity`` (``NonIndividualName`` + ``BusinessAddress/AddressDetails`` State/Postcode) or ``LegalEntity``
(``IndividualName``), ``ASICNumber`` (ACN), ``GST``, ``DGR``, ``OtherEntity`` trading/business names.

Privacy: sole traders (``LegalEntity``/``IND``) are natural persons.  They are SKIPPED unless config
``include_individuals`` is true (and then only the entity's ``OtherEntity`` business names are used as the name).
The bulk extract has no street address, phone, website or e-mail.

Optional: ABN Lookup *web services* need a free GUID (register at https://abr.business.gov.au/Tools/WebServices).
With config ``guid`` and ``abns`` the adapter looks each ABN up (``AbnDetails.aspx``); this path could NOT be verified
(no GUID available) and is clearly marked as such.
"""
from __future__ import annotations

import re
import zipfile
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lxml import etree

from ._http import PoliteClient
from ._stream import ensure_local_file
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
from .geo_filters import RecordFilter

ADAPTER_ID = "abn_lookup_au"
DATASET_URL = "https://data.gov.au/data/dataset/abn-bulk-extract"
CKAN_URL = "https://data.gov.au/data/api/3/action/package_show?id=abn-bulk-extract"
LOOKUP_URL = "https://abr.business.gov.au/json/AbnDetails.aspx"
LICENCE = "Creative Commons Attribution 3.0 Australia"
ATTRIBUTION = "ABN data: Australian Business Register (ABR), Commonwealth of Australia, licensed under CC BY 3.0 AU."
_DATE8 = re.compile(r"^(\d{4})(\d{2})(\d{2})$")


def _txt(e: etree._Element | None) -> str | None:
    if e is None or e.text is None:
        return None
    t = e.text.strip()
    return t or None


def _date8(s: str | None) -> str | None:
    if not s:
        return None
    m = _DATE8.match(s.strip())
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def abr_to_record(abr: etree._Element, *, include_individuals: bool = False, observed_at: datetime | None = None) -> BusinessRecord | None:
    abn_el = abr.find("ABN")
    abn = re.sub(r"\D", "", _txt(abn_el) or "")
    if len(abn) != 11:
        return None
    et = abr.find("EntityType")
    et_ind = _txt(et.find("EntityTypeInd")) if et is not None else None
    et_text = _txt(et.find("EntityTypeText")) if et is not None else None
    main = abr.find("MainEntity")
    legal = abr.find("LegalEntity")
    other_names = [(n.get("type"), _txt(n.find("NonIndividualNameText"))) for n in abr.findall("OtherEntity/NonIndividualName")]
    trading = next((t for ty, t in other_names if ty in ("TRD", "BN") and t), None) or next((t for _ty, t in other_names if t), None)
    if main is not None:
        name = _txt(main.find("NonIndividualName/NonIndividualNameText"))
        addr = main.find("BusinessAddress/AddressDetails")
    elif legal is not None:
        if not include_individuals:
            return None  # natural person (sole trader): not collected by default
        name = trading  # never the person's own name
        addr = legal.find("BusinessAddress/AddressDetails")
        if not name:
            return None
    else:
        return None
    if not name:
        return None
    state = _txt(addr.find("State")) if addr is not None else None
    pc = _txt(addr.find("Postcode")) if addr is not None else None
    acn_el = abr.find("ASICNumber")
    acn = re.sub(r"\D", "", _txt(acn_el) or "") if acn_el is not None else ""
    status = "Active" if (abn_el.get("status") if abn_el is not None else "") == "ACT" else "Cancelled"
    gst = abr.find("GST")
    idents = [{"type": "abn", "value": abn}]
    if len(acn) == 9:
        idents.append({"type": "acn", "value": acn})
    return BusinessRecord(
        source_ref=abn,
        legal_name=name,
        trading_name=trading if trading and trading != name else None,
        company_number=abn,
        legal_type=et_text or et_ind,
        status=status,
        country_code="AU",
        region=state,
        postal_code=pc,
        address=" ".join(x for x in (state, pc) if x) or None,
        category_hint="abn_entity",
        source_url=f"https://abr.business.gov.au/ABN/View?abn={abn}",
        licence=LICENCE,
        attribution=ATTRIBUTION,
        observed_at=observed_at,
        raw={
            "abn": abn, "acn": acn or None, "entity_type": et_ind,
            "abn_status_from": _date8(abn_el.get("ABNStatusFromDate") if abn_el is not None else None),
            "gst_status": gst.get("status") if gst is not None else None, "record_last_updated": _date8(abr.get("recordLastUpdatedDate")),
            "other_names": [t for _ty, t in other_names if t][:5], "identifiers": idents,
        },
    )


def _sources(path: Path) -> list[tuple[Path, str]]:
    """Ordered (file, member) pairs: zip members (*.xml), a directory's files, or one xml."""
    out: list[tuple[Path, str]] = []
    if path.is_dir():
        for p in sorted(path.iterdir()):
            if p.suffix.lower() in (".zip", ".xml"):
                out.extend(_sources(p))
        return out
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            out.extend((path, n) for n in sorted(i.filename for i in zf.infolist() if i.filename.lower().endswith(".xml")))
        return out
    return [(path, path.name)]


def iter_abr(path: Path, member: str, *, skip: int = 0) -> Iterator[tuple[int, etree._Element]]:
    """Stream ``ABR`` elements of one XML (zip member or file); memory stays flat (elements are freed as consumed)."""
    if zipfile.is_zipfile(path):
        zf = zipfile.ZipFile(path)
        fh = zf.open(member)
        closer = (fh.close, zf.close)
    else:
        fh = open(path, "rb")
        closer = (fh.close,)
    try:
        n = -1
        for _ev, el in etree.iterparse(fh, events=("end",), tag="ABR", resolve_entities=False, no_network=True, huge_tree=True):
            n += 1
            if n >= skip:
                yield n, el
            el.clear()
            parent = el.getparent()
            if parent is not None:
                while el.getprevious() is not None:
                    del parent[0]
    finally:
        for c in closer:
            c()


def parse_lookup_response(text: str) -> dict[str, Any]:
    """Strip the JSONP wrapper of an AbnDetails response and parse the JSON body."""
    import json

    t = text.strip()
    m = re.match(r"^[\w$.]+\((.*)\)\s*;?$", t, re.S)
    return json.loads(m.group(1) if m else t)


def lookup_to_record(d: dict[str, Any]) -> BusinessRecord | None:
    abn = re.sub(r"\D", "", str(d.get("Abn") or ""))
    name = (d.get("EntityName") or "").strip()
    if len(abn) != 11 or not name or d.get("Message"):
        return None
    if str(d.get("EntityTypeCode") or "").upper() == "IND":
        return None
    bn = d.get("BusinessName") or []
    return BusinessRecord(
        source_ref=abn, legal_name=name, trading_name=(bn[0] if isinstance(bn, list) and bn else None), company_number=abn,
        legal_type=d.get("EntityTypeName"), status="Active" if str(d.get("AbnStatus", "")).lower().startswith("active") else "Cancelled",
        country_code="AU", region=d.get("AddressState"), postal_code=str(d.get("AddressPostcode") or "") or None,
        address=" ".join(x for x in (d.get("AddressState"), str(d.get("AddressPostcode") or "")) if x) or None,
        category_hint="abn_entity", source_url=f"https://abr.business.gov.au/ABN/View?abn={abn}", licence=LICENCE,
        attribution=ATTRIBUTION, observed_at=datetime.now(UTC),
        raw={"abn": abn, "acn": d.get("Acn") or None, "gst": d.get("Gst"), "identifiers": [{"type": "abn", "value": abn}], "via": "abn_lookup_web_services"},
    )


@register
class AbnLookupAdapter(SourceAdapter):
    info = AdapterInfo(
        adapter_id=ADAPTER_ID,
        name="Australian Business Register -- ABN Bulk Extract (data.gov.au)",
        country_coverage=("AU",),
        source_type="bulk_dataset",
        licence=LICENCE,
        source_url=DATASET_URL,
        access_requirements=("Bulk extract: none (public zip download ~1 GB in two parts). Config: 'path' (zip / xml / directory) or 'url'. "
                             "OPTIONAL web-service enrichment: free GUID (config 'guid') + 'abns' list."),
        automated_collection_permitted=True,
        rate_policy=RatePolicy(requests_per_minute=None, concurrency=1),
        fields_available=("legal_name", "trading_name", "abn", "acn", "legal_type", "status", "region", "postal_code", "gst_status"),
        incremental_support=True,
        attribution=ATTRIBUTION,
        live_verified=True,
        cost="free", credentials="optional", credentials_note="Bulk extract needs none. ABN Lookup web services need a free GUID (not available in the build sandbox).",
        refresh_frequency="weekly (dataset metadata_modified observed 2026-10-06; ABR documents weekly refresh)",
        access_method="bulk_file", industries=("*",),
        allowed_usage="CC BY 3.0 AU with attribution. Sole-trader (individual) records are excluded by default for privacy. ABR terms of use were not independently reviewed here -- check them before any outreach.",
        notes=("Live-verified: CKAN package metadata (licence, resource URLs) and the XSD. The ~500 MB zip parts were not downloaded; parsing "
               "is tested on synthetic XML in the documented shape. The GUID web-service path (AbnDetails.aspx JSONP) is UNVERIFIED."),
    )

    async def health(self) -> AdapterHealth:
        p = self.config.get("path")
        if p:
            ok = Path(p).exists()
            return AdapterHealth(ok, "ok" if ok else "unavailable", f"{'present' if ok else 'missing'}: {p}")
        if self.config.get("guid") and self.config.get("abns"):
            return AdapterHealth(True, "ok", "web-service lookups configured (GUID present; unverified)")
        if self.config.get("url"):
            return AdapterHealth(True, "ok", "will download on first run (not verified)")
        return AdapterHealth(False, "not_configured", f"set 'path' or 'url' (resources listed at {DATASET_URL})")

    async def _iter_lookup(self, flt: RecordFilter, limit: int | None, checkpoint: Checkpoint | None):
        abns = [re.sub(r"\D", "", str(a)) for a in self.config.get("abns") or []]
        skip = int((checkpoint or {}).get("lookup", -1)) + 1 if checkpoint and "lookup" in checkpoint else 0
        import asyncio

        emitted = 0
        async with PoliteClient(client=self.config.get("http_client"), min_interval_s=float(self.config.get("min_interval_s", 1.0)),
                                sleep=self.config.get("sleep") or asyncio.sleep) as http:
            for i, abn in enumerate(abns):
                if i < skip:
                    continue
                resp = await http.request("GET", LOOKUP_URL, params={"abn": abn, "callback": "cb", "guid": self.config["guid"]})
                if resp.status_code >= 400:
                    continue
                rec = lookup_to_record(parse_lookup_response(resp.text))
                if rec is None:
                    continue
                rec.adapter_id = ADAPTER_ID
                if not flt.accept(rec):
                    continue
                yield rec, {"lookup": i}
                emitted += 1
                if limit and emitted >= limit:
                    return

    async def iter_businesses(
        self, query: DiscoveryQuery, checkpoint: Checkpoint | None = None
    ) -> AsyncIterator[tuple[BusinessRecord, Checkpoint]]:
        if not query.covers_country("AU"):
            return
        flt = RecordFilter(query)
        if not self.config.get("path") and not self.config.get("url") and self.config.get("guid") and self.config.get("abns"):
            async for item in self._iter_lookup(flt, query.limit, checkpoint):
                yield item
            return
        path = await ensure_local_file(self.config, ADAPTER_ID)
        if not path.exists():
            raise NotConfigured(f"missing: {path}")
        srcs = _sources(path)
        keys = [f"{p.name}::{m}" for p, m in srcs]
        cp = checkpoint or {}
        start_i, skip = 0, 0
        if cp.get("key") in keys:
            start_i, skip = keys.index(cp["key"]), int(cp.get("n", -1)) + 1
        incl = bool(self.config.get("include_individuals", False))
        types = {t.upper() for t in (self.config.get("entity_types") or [])}
        observed = datetime.now(UTC)
        emitted = 0
        for idx in range(start_i, len(srcs)):
            p, m = srcs[idx]
            for n, el in iter_abr(p, m, skip=skip if idx == start_i else 0):
                rec = abr_to_record(el, include_individuals=incl, observed_at=observed)
                if rec is None:
                    continue
                if types and (rec.raw.get("entity_type") or "").upper() not in types:
                    continue
                rec.adapter_id = ADAPTER_ID
                if not flt.accept(rec):
                    continue
                yield rec, {"key": keys[idx], "n": n}
                emitted += 1
                if query.limit and emitted >= query.limit:
                    return


__all__ = ["AbnLookupAdapter", "abr_to_record", "iter_abr", "lookup_to_record", "parse_lookup_response"]
