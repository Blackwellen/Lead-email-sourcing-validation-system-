"""GLEIF Golden Copy (LEI-CDR level 1 "lei2" concatenated file) + optional relationship (RR) file.

Source: https://www.gleif.org/en/lei-data/gleif-golden-copy -- the publish index
``https://goldencopy.gleif.org/api/v2/golden-copies/publishes`` was fetched live and lists full / delta files in
csv, json and xml (full CSV zip ~480 MB, ~3.46 M records at build time).  Licence: CC0 1.0 (GLEIF publishes the LEI
data as open data with no restriction on use).

Formats (both inspected live from the intra-day delta files):
  * CSV  -- one header row, dotted column names (``Entity.LegalName``, ``Entity.LegalAddress.City`` ...).
  * JSON -- ``{"records": [ {"LEI": {"$": "..."}, "Entity": {"LegalName": {"$": "..."}, ...}, ...} ]}``.

Relationships: the separate RR file (``Relationship.StartNode.NodeID`` = child LEI, ``EndNode.NodeID`` = parent LEI,
``Relationship.RelationshipType`` e.g. ``IS_DIRECTLY_CONSOLIDATED_BY``) is indexed into a temporary SQLite table
(``rr_path``) so each LEI record can carry parent hints in ``rec.raw['relationships']`` for the graph engine -- only
hints; no organisation is created for the parent here.

``company_number`` is set only for registration authorities known to be a *national company register whose number
is unique within the country* (default: ``RA000585`` Companies House, GB; extend via config ``company_number_ras``
``{"RA000xxx": "XX"}``).  Everything else keeps the LEI as the identifier (name+postcode identity).
"""
from __future__ import annotations

import csv
import io
import re
import sqlite3
import tempfile
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from ._stream import ensure_local_file, gb_company_number, iter_json_array, open_member
from .base import (
    AdapterHealth,
    AdapterInfo,
    AdapterUnavailable,
    BusinessRecord,
    Checkpoint,
    DiscoveryQuery,
    NotConfigured,
    RatePolicy,
    SourceAdapter,
    register,
)
from .download import make_client
from .geo_filters import RecordFilter, normalise_postcode

ADAPTER_ID = "gleif_lei"
PUBLISHES_URL = "https://goldencopy.gleif.org/api/v2/golden-copies/publishes"
LICENCE = "CC0 1.0 Universal (GLEIF open data)"
ATTRIBUTION = "LEI data: Global Legal Entity Identifier Foundation (GLEIF), CC0 1.0."
DEFAULT_RAS = {"RA000585": "GB"}  # Companies House (England & Wales) -- registry numbers unique within GB
_REL_MAP = {
    "IS_DIRECTLY_CONSOLIDATED_BY": ("subsidiary_of", False),
    "IS_ULTIMATELY_CONSOLIDATED_BY": ("subsidiary_of", True),
    "IS_INTERNATIONAL_BRANCH_OF": ("branch_of", False),
    "IS_FUND-MANAGED_BY": ("member_of_group", False),
    "IS_SUBFUND_OF": ("member_of_group", False),
    "IS_FEEDER_TO": ("member_of_group", False),
}
csv.field_size_limit(1 << 24)


# --------------------------------------------------------------------------- field access
def _jv(node: Any) -> str | None:
    """Unwrap a GLEIF JSON leaf ``{"$": value}`` (or a plain scalar)."""
    if isinstance(node, dict):
        node = node.get("$")
    if node is None:
        return None
    t = str(node).strip()
    return t or None


def _dig(d: Any, *path: str) -> Any:
    for k in path:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def flatten_json_record(r: dict[str, Any]) -> dict[str, str | None]:
    """JSON lei2 record -> the CSV column namespace used by :func:`record_from_flat`."""
    out: dict[str, str | None] = {
        "LEI": _jv(r.get("LEI")),
        "Entity.LegalName": _jv(_dig(r, "Entity", "LegalName")),
        "Entity.LegalJurisdiction": _jv(_dig(r, "Entity", "LegalJurisdiction")),
        "Entity.EntityCategory": _jv(_dig(r, "Entity", "EntityCategory")),
        "Entity.EntityStatus": _jv(_dig(r, "Entity", "EntityStatus")),
        "Entity.EntityCreationDate": _jv(_dig(r, "Entity", "EntityCreationDate")),
        "Entity.LegalForm.EntityLegalFormCode": _jv(_dig(r, "Entity", "LegalForm", "EntityLegalFormCode")),
        "Entity.RegistrationAuthority.RegistrationAuthorityID": _jv(_dig(r, "Entity", "RegistrationAuthority", "RegistrationAuthorityID")),
        "Entity.RegistrationAuthority.RegistrationAuthorityEntityID": _jv(_dig(r, "Entity", "RegistrationAuthority", "RegistrationAuthorityEntityID")),
        "Registration.RegistrationStatus": _jv(_dig(r, "Registration", "RegistrationStatus")),
        "Registration.LastUpdateDate": _jv(_dig(r, "Registration", "LastUpdateDate")),
    }
    for addr in ("LegalAddress", "HeadquartersAddress"):
        a = _dig(r, "Entity", addr) or {}
        for f in ("FirstAddressLine", "City", "Region", "Country", "PostalCode"):
            out[f"Entity.{addr}.{f}"] = _jv(a.get(f))
        extra = a.get("AdditionalAddressLine") or []
        if isinstance(extra, dict):
            extra = [extra]
        for i, x in enumerate(extra[:3], 1):
            out[f"Entity.{addr}.AdditionalAddressLine.{i}"] = _jv(x)
    others = _dig(r, "Entity", "OtherEntityNames", "OtherEntityName") or []
    if isinstance(others, dict):
        others = [others]
    for i, o in enumerate(others[:5], 1):
        out[f"Entity.OtherEntityNames.OtherEntityName.{i}"] = _jv(o)
        out[f"Entity.OtherEntityNames.OtherEntityName.{i}.type"] = o.get("@type") if isinstance(o, dict) else None
    return out


def _g(d: dict[str, Any], k: str) -> str | None:
    v = d.get(k)
    if v is None:
        return None
    t = str(v).strip()
    return t or None


def record_from_flat(d: dict[str, Any], *, parents: list[dict[str, Any]] | None = None,
                     ras: dict[str, str] | None = None, observed_at: datetime | None = None) -> BusinessRecord | None:
    lei = _g(d, "LEI")
    name = _g(d, "Entity.LegalName")
    if not lei or not name:
        return None
    use = "Entity.LegalAddress" if _g(d, "Entity.LegalAddress.Country") else "Entity.HeadquartersAddress"
    country = (_g(d, f"{use}.Country") or _g(d, "Entity.LegalJurisdiction") or "").split("-")[0].upper()
    if len(country) != 2:
        return None
    lines = [_g(d, f"{use}.FirstAddressLine")] + [_g(d, f"{use}.AdditionalAddressLine.{i}") for i in (1, 2, 3)]
    city, region, pc = _g(d, f"{use}.City"), _g(d, f"{use}.Region"), _g(d, f"{use}.PostalCode")
    pcn = normalise_postcode(pc, country) if pc else None
    address = ", ".join(x for x in [*lines, city, region, pcn or pc, country] if x)
    ra, ra_id = _g(d, "Entity.RegistrationAuthority.RegistrationAuthorityID"), _g(d, "Entity.RegistrationAuthority.RegistrationAuthorityEntityID")
    ras = ras or DEFAULT_RAS
    cn = None
    if ra and ra_id and ras.get(ra) == country:
        cn = gb_company_number(ra_id) if country == "GB" else re.sub(r"\s+", "", ra_id).upper()
    trading = None
    for i in range(1, 6):
        if (_g(d, f"Entity.OtherEntityNames.OtherEntityName.{i}.type") or "").upper() == "TRADING_OR_OPERATING_NAME":
            trading = _g(d, f"Entity.OtherEntityNames.OtherEntityName.{i}")
            break
    est = (_g(d, "Entity.EntityStatus") or "").title() or None
    idents = [{"type": "lei", "value": lei}]
    if ra and ra_id:
        idents.append({"type": "registration_authority", "value": f"{ra}:{ra_id}"})
    rels = []
    for p in parents or []:
        rels.append(p)
    return BusinessRecord(
        source_ref=lei,
        legal_name=name,
        trading_name=trading,
        company_number=cn,
        legal_type=_g(d, "Entity.LegalForm.EntityLegalFormCode"),
        status=est,
        country_code=country,
        region=region,
        city=city,
        postal_code=pcn,
        address=address or None,
        category_hint="lei_entity",
        source_url=f"https://search.gleif.org/#/record/{lei}",
        licence=LICENCE,
        attribution=ATTRIBUTION,
        observed_at=observed_at,
        raw={
            "lei": lei, "registration_authority": ra, "registration_entity_id": ra_id,
            "jurisdiction": _g(d, "Entity.LegalJurisdiction"), "entity_category": _g(d, "Entity.EntityCategory"),
            "lei_status": _g(d, "Registration.RegistrationStatus"), "last_update": _g(d, "Registration.LastUpdateDate"),
            "identifiers": idents, "relationships": rels,
        },
    )


# --------------------------------------------------------------------------- RR (relationship) index
class RelationshipIndex:
    """SQLite index child LEI -> parents built once from an RR csv/json file (disk-backed, bounded RAM)."""

    def __init__(self, path: Path, directory: Path | str | None = None):
        fd, name = tempfile.mkstemp(prefix="gleif-rr-", suffix=".sqlite", dir=str(directory) if directory else None)
        import os

        os.close(fd)
        self.path = Path(name)
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA journal_mode=OFF")
        self.db.execute("PRAGMA synchronous=OFF")
        self.db.execute("CREATE TABLE rel (child TEXT, parent TEXT, rtype TEXT, status TEXT, start TEXT)")
        self._load(path)
        self.db.execute("CREATE INDEX ix_child ON rel(child)")
        self.db.commit()

    def _load(self, path: Path) -> None:
        batch: list[tuple[str, str, str, str, str]] = []
        with open_member(path, None, (".csv", ".json")) as (fh, name):
            if name.lower().endswith(".json"):
                for r in iter_json_array(fh, key="relations"):
                    rec = r.get("RelationshipRecord", r) if isinstance(r, dict) else {}
                    rel = rec.get("Relationship", rec)
                    periods = _dig(rel, "RelationshipPeriods", "RelationshipPeriod") or []
                    if isinstance(periods, dict):
                        periods = [periods]
                    start = next((_jv(p.get("StartDate")) for p in periods if _jv(p.get("PeriodType")) == "RELATIONSHIP_PERIOD"), None)
                    batch.append((
                        _jv(_dig(rel, "StartNode", "NodeID")) or "", _jv(_dig(rel, "EndNode", "NodeID")) or "",
                        _jv(rel.get("RelationshipType")) or "", _jv(rel.get("RelationshipStatus")) or "", start or "",
                    ))
                    if len(batch) >= 20000:
                        self._flush(batch)
            else:
                rd = csv.reader(io.TextIOWrapper(fh, encoding="utf-8-sig", errors="replace", newline=""))
                header = next(rd, [])
                ix = {h: i for i, h in enumerate(header)}
                need = ("Relationship.StartNode.NodeID", "Relationship.EndNode.NodeID", "Relationship.RelationshipType")
                if any(k not in ix for k in need):
                    raise NotConfigured("not a GLEIF RR csv (Relationship.StartNode.NodeID ... missing)")
                st = ix.get("Relationship.RelationshipStatus")
                p1 = ix.get("Relationship.Period.1.startDate")
                for row in rd:
                    if len(row) <= max(ix[k] for k in need):
                        continue
                    batch.append((row[ix[need[0]]], row[ix[need[1]]], row[ix[need[2]]],
                                  row[st] if st is not None and st < len(row) else "",
                                  row[p1] if p1 is not None and p1 < len(row) else ""))
                    if len(batch) >= 20000:
                        self._flush(batch)
        self._flush(batch)

    def _flush(self, batch: list) -> None:
        self.db.executemany("INSERT INTO rel VALUES (?,?,?,?,?)", batch)
        batch.clear()

    def parents(self, lei: str) -> list[dict[str, Any]]:
        out = []
        for parent, rtype, status, start in self.db.execute(
            "SELECT parent, rtype, status, start FROM rel WHERE child = ? ORDER BY rtype, parent", (lei,)
        ):
            if status and status.upper() != "ACTIVE":
                continue
            mapped = _REL_MAP.get(rtype)
            if not mapped or not parent:
                continue
            rel, ultimate = mapped
            out.append({
                "relation": rel, "ultimate": ultimate, "gleif_type": rtype, "since": start or None,
                "counterparty": {"identifier_type": "lei", "identifier_value": parent},
                "source_url": f"https://search.gleif.org/#/record/{lei}", "confidence": 0.95, "basis": "gleif_rr",
            })
        return out

    def close(self) -> None:
        try:
            self.db.close()
        finally:
            self.path.unlink(missing_ok=True)


def iter_lei_rows(path: Path, *, member: str | None = None, skip: int = 0) -> Iterator[tuple[int, dict[str, Any], str]]:
    with open_member(path, member, (".csv", ".json")) as (fh, name):
        if name.lower().endswith(".json"):
            for i, r in enumerate(iter_json_array(fh, key="records")):
                if i < skip:
                    continue
                if isinstance(r, dict):
                    yield i, flatten_json_record(r), name
            return
        rd = csv.reader(io.TextIOWrapper(fh, encoding="utf-8-sig", errors="replace", newline=""))
        header = next(rd, None)
        if not header:
            return
        if "LEI" not in header:
            raise NotConfigured("not a GLEIF lei2 csv (LEI column missing)")
        for i, row in enumerate(rd):
            if i < skip or not row:
                continue
            yield i, {h: (row[j] if j < len(row) else None) for j, h in enumerate(header)}, name


async def resolve_latest_url(client: httpx.AsyncClient | None = None, *, fmt: str = "csv", kind: str = "lei2",
                             delta: str | None = None) -> str:
    """Latest Golden Copy file URL from the publish index (``delta``: None=full, 'IntraDay'|'LastDay'|...)."""
    own = client is None
    cli = client or make_client()
    try:
        r = await cli.get(PUBLISHES_URL)
        r.raise_for_status()
        latest = r.json()["data"][0][kind]
        node = latest["full_file"] if delta is None else latest["delta_files"][delta]
        return node[fmt]["url"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise AdapterUnavailable(f"unexpected GLEIF publish index shape: {exc}") from exc
    finally:
        if own:
            await cli.aclose()


@register
class GleifLeiAdapter(SourceAdapter):
    info = AdapterInfo(
        adapter_id=ADAPTER_ID,
        name="GLEIF LEI Golden Copy (legal entities worldwide)",
        country_coverage=("*",),
        source_type="bulk_dataset",
        licence=LICENCE,
        source_url="https://www.gleif.org/en/lei-data/gleif-golden-copy/download-the-golden-copy",
        access_requirements="None. Config: 'path' (zip/csv/json), or 'url' / 'auto_download' (+ 'format' csv|json, 'delta' IntraDay|LastDay|LastWeek|LastMonth). Optional 'rr_path' for parent relationships.",
        automated_collection_permitted=True,
        rate_policy=RatePolicy(requests_per_minute=None, concurrency=1),
        fields_available=("legal_name", "lei", "registration_authority", "company_number", "address", "status", "legal_form", "parent_relationships"),
        incremental_support=True,
        attribution=ATTRIBUTION,
        live_verified=True,
        cost="free", credentials="none", refresh_frequency="daily (3 publishes per day; delta files intra-day / last-day / last-week / last-month)",
        access_method="bulk_file", industries=("finance", "legal_entities", "*"),
        allowed_usage="CC0: unrestricted. Only entities that hold an LEI (mostly financial/large/regulated) are covered -- not a full business register.",
        notes=("Live-verified: publish index JSON and the intra-day lei2 csv/json + rr csv zips were downloaded and their "
               "headers/structure inspected. The ~480 MB full file was not downloaded. No websites or emails exist in this dataset."),
    )

    async def health(self) -> AdapterHealth:
        p = self.config.get("path")
        if p:
            ok = Path(p).exists()
            return AdapterHealth(ok, "ok" if ok else "unavailable", f"{'present' if ok else 'missing'}: {p}")
        if self.config.get("url") or self.config.get("auto_download"):
            return AdapterHealth(True, "ok", "will download on first run (not verified)")
        return AdapterHealth(False, "not_configured", "set 'path', 'url' or 'auto_download'")

    async def _path(self) -> Path:
        if self.config.get("path") or self.config.get("url"):
            return await ensure_local_file(self.config, ADAPTER_ID)
        if self.config.get("auto_download"):
            cfg = dict(self.config)
            cfg["url"] = await resolve_latest_url(
                self.config.get("http_client"), fmt=str(self.config.get("format", "csv")), delta=self.config.get("delta"))
            return await ensure_local_file(cfg, ADAPTER_ID)
        raise NotConfigured("set 'path', 'url' or 'auto_download'")

    async def iter_businesses(
        self, query: DiscoveryQuery, checkpoint: Checkpoint | None = None
    ) -> AsyncIterator[tuple[BusinessRecord, Checkpoint]]:
        path = await self._path()
        flt = RecordFilter(query)
        cp = checkpoint or {}
        skip = int(cp.get("row", -1)) + 1 if cp.get("file") in (None, path.name) else 0
        ras = {**DEFAULT_RAS, **(self.config.get("company_number_ras") or {})}
        rr = RelationshipIndex(Path(self.config["rr_path"]), self.config.get("spill_dir")) if self.config.get("rr_path") else None
        observed = datetime.now(UTC)
        emitted = 0
        try:
            for i, flat, _m in iter_lei_rows(path, member=self.config.get("member"), skip=skip):
                lei = _g(flat, "LEI")
                rec = record_from_flat(flat, parents=rr.parents(lei) if (rr and lei) else None, ras=ras, observed_at=observed)
                if rec is None:
                    continue
                rec.adapter_id = ADAPTER_ID
                if not flt.accept(rec):
                    continue
                yield rec, {"row": i, "file": path.name}
                emitted += 1
                if query.limit and emitted >= query.limit:
                    return
        finally:
            if rr:
                rr.close()


__all__ = ["GleifLeiAdapter", "RelationshipIndex", "iter_lei_rows", "record_from_flat", "flatten_json_record", "resolve_latest_url"]
