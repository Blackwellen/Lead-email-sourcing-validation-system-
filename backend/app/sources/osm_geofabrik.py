"""OpenStreetMap extracts (Geofabrik) adapter.

Formats streamed without loading the dataset into memory:
  * ``.osm.pbf``       via the optional ``osmium`` (pyosmium) package -- ``pip install .[osm]``
  * ``.osm`` / ``.osm.bz2`` (OSM XML) via ``xml.etree.ElementTree.iterparse``
  * ``.json`` Overpass-style (``{"elements":[...]}``) and GeoJSON (``FeatureCollection`` / ``.geojsonl``)

Licence: Open Database Licence 1.0; attribution "© OpenStreetMap contributors" is recorded on every record.
Note ODbL share-alike applies to publicly *distributed* derived databases; internal use only is the expected mode.
Emails are emitted only when the OSM object itself carries ``email`` / ``contact:email``.
"""
from __future__ import annotations

import bz2
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import get_settings
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
from .download import download_file
from .geo_filters import RecordFilter, normalise_postcode, uk_postcode_area
from .geofabrik_regions import NI_POSTCODE_AREA, extract_paths, extract_url
from .urls import normalise_website

ADAPTER_ID = "osm_geofabrik"
LICENCE = "ODbL-1.0"
ATTRIBUTION = "© OpenStreetMap contributors (Open Database Licence 1.0), extracts by Geofabrik GmbH"
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}$")
DEFAULT_WILDCARD_KEYS = ("shop", "office", "craft")


# --------------------------------------------------------------------------- tag matching
class TagMatcher:
    """Matches OSM tags against ``key=value`` pairs and ``key=*`` / bare ``key`` wildcards."""

    def __init__(self, osm_tags: list[str] | None):
        self.pairs: set[tuple[str, str]] = set()
        self.wild: set[str] = set()
        for t in osm_tags or []:
            t = t.strip()
            if not t:
                continue
            if "=" in t:
                k, v = t.split("=", 1)
                k, v = k.strip(), v.strip()
                if v in ("*", ""):
                    self.wild.add(k)
                else:
                    self.pairs.add((k, v))
            else:
                self.wild.add(t)
        self.is_default = not self.pairs and not self.wild
        if self.is_default:
            self.wild = set(DEFAULT_WILDCARD_KEYS)

    def match(self, tags: dict[str, str]) -> list[str]:
        out = []
        for k in sorted({k for k, _ in self.pairs} | self.wild):
            v = tags.get(k)
            if v is None:
                continue
            for val in v.split(";"):
                val = val.strip()
                if (k, val) in self.pairs or k in self.wild:
                    out.append(f"{k}={val}")
        return list(dict.fromkeys(out))

    def signature(self) -> str:
        return hashlib.sha1(json.dumps([sorted(self.pairs), sorted(self.wild)]).encode()).hexdigest()[:12]


Element = dict[str, Any]  # {'type','id','lat','lon','tags'}


# --------------------------------------------------------------------------- parsers
def _open_text_binary(path: Path):
    return bz2.open(path, "rb") if path.suffix == ".bz2" else open(path, "rb")


def iter_xml_elements(path: Path, matcher: TagMatcher) -> Iterator[Element]:
    """Stream matching named nodes/ways from OSM XML. Ways get a centroid from their member nodes."""
    # pass 1: member node refs for matching ways (bounded by the number of matching ways)
    need: set[str] = set()
    with _open_text_binary(path) as fh:
        for _, el in ET.iterparse(fh, events=("end",)):
            if el.tag == "way":
                tags = {t.get("k"): t.get("v") for t in el.findall("tag")}
                if tags.get("name") and matcher.match(tags) and el.find("center") is None:
                    need.update(nd.get("ref") for nd in el.findall("nd"))
            if el.tag in ("node", "way", "relation"):
                el.clear()
    locs: dict[str, tuple[float, float]] = {}
    with _open_text_binary(path) as fh:
        for _, el in ET.iterparse(fh, events=("end",)):
            if el.tag == "node":
                nid = el.get("id")
                lat, lon = el.get("lat"), el.get("lon")
                if nid in need and lat and lon:
                    locs[nid] = (float(lat), float(lon))
                tags = {t.get("k"): t.get("v") for t in el.findall("tag")}
                if tags.get("name") and matcher.match(tags):
                    yield {"type": "node", "id": int(nid), "lat": float(lat) if lat else None,
                           "lon": float(lon) if lon else None, "tags": tags}
                el.clear()
            elif el.tag == "way":
                tags = {t.get("k"): t.get("v") for t in el.findall("tag")}
                if tags.get("name") and matcher.match(tags):
                    c = el.find("center")
                    if c is not None and c.get("lat"):
                        lat, lon = float(c.get("lat")), float(c.get("lon"))
                    else:
                        pts = [locs[nd.get("ref")] for nd in el.findall("nd") if nd.get("ref") in locs]
                        lat = sum(p[0] for p in pts) / len(pts) if pts else None
                        lon = sum(p[1] for p in pts) / len(pts) if pts else None
                    yield {"type": "way", "id": int(el.get("id")), "lat": lat, "lon": lon, "tags": tags}
                el.clear()
            elif el.tag == "relation":
                el.clear()


def iter_json_array(fh, key_re: re.Pattern[str], chunk: int = 1 << 16, max_item: int = 32 << 20) -> Iterator[Any]:
    """Incrementally yield items of the first array whose key matches ``key_re`` (constant memory)."""
    buf = ""
    while True:
        data = fh.read(chunk)
        if not data:
            return
        buf += data
        m = key_re.search(buf)
        if m:
            buf = buf[m.end():]
            break
        buf = buf[-128:]
    dec = json.JSONDecoder()
    i = 0
    while True:
        while i < len(buf) and buf[i] in " \t\r\n,":
            i += 1
        if i >= len(buf):
            data = fh.read(chunk)
            if not data:
                return
            buf = buf[i:] + data
            i = 0
            continue
        if buf[i] == "]":
            return
        try:
            obj, end = dec.raw_decode(buf, i)
        except json.JSONDecodeError:
            data = fh.read(chunk)
            if not data:
                raise
            buf = buf[i:] + data
            i = 0
            if len(buf) > max_item:
                raise ValueError("JSON item too large") from None
            continue
        yield obj
        i = end


def _flatten_coords(c: Any) -> Iterator[tuple[float, float]]:
    if isinstance(c, (list, tuple)) and c:
        if isinstance(c[0], (int, float)) and len(c) >= 2:
            yield float(c[0]), float(c[1])
        else:
            for x in c:
                yield from _flatten_coords(x)


def _geometry_centre(geom: dict | None) -> tuple[float | None, float | None]:
    if not geom or "coordinates" not in geom:
        return None, None
    pts = list(_flatten_coords(geom["coordinates"]))
    if not pts:
        return None, None
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    lat, lon = (min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None, None
    return lat, lon


def _from_overpass(e: dict) -> Element | None:
    if e.get("type") not in ("node", "way") or not isinstance(e.get("tags"), dict):
        return None
    lat, lon = e.get("lat"), e.get("lon")
    c = e.get("center")
    if (lat is None or lon is None) and isinstance(c, dict):
        lat, lon = c.get("lat"), c.get("lon")
    return {"type": e["type"], "id": e.get("id"), "lat": lat, "lon": lon, "tags": {str(k): str(v) for k, v in e["tags"].items()}}


def _from_feature(f: dict) -> Element | None:
    if not isinstance(f, dict) or f.get("type") != "Feature":
        return None
    props = f.get("properties") or {}
    tags = props.get("tags") if isinstance(props.get("tags"), dict) else props
    tags = {str(k): str(v) for k, v in tags.items() if v is not None and not isinstance(v, (dict, list))}
    ident = str(f.get("id") or props.get("@id") or props.get("id") or "")
    typ, _, num = ident.partition("/")
    if not num:
        typ, num = "node", ident
    lat, lon = _geometry_centre(f.get("geometry"))
    try:
        n = int(num)
    except ValueError:
        n = hash(num) & 0x7FFFFFFF
    return {"type": typ if typ in ("node", "way", "relation") else "node", "id": n, "lat": lat, "lon": lon, "tags": tags}


def iter_json_elements(path: Path, matcher: TagMatcher) -> Iterator[Element]:
    suffix = "".join(path.suffixes).lower()
    if suffix.endswith((".geojsonl", ".ndjson", ".jsonl", ".geojsons")):
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip().lstrip("\x1e")
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                el = _from_feature(obj) or _from_overpass(obj)
                if el and el["tags"].get("name") and matcher.match(el["tags"]):
                    yield el
        return
    key_re = re.compile(r'"(?:elements|features)"\s*:\s*\[')
    with open(path, encoding="utf-8", errors="replace") as fh:
        for obj in iter_json_array(fh, key_re):
            el = _from_overpass(obj) if isinstance(obj, dict) and "type" in obj and obj.get("type") != "Feature" else _from_feature(obj)
            if el and el["tags"].get("name") and matcher.match(el["tags"]):
                yield el


def _osmium_available() -> bool:
    try:
        import osmium  # noqa: F401
    except ImportError:
        return False
    return True


def iter_pbf_elements(path: Path, matcher: TagMatcher, *, ways: bool = True) -> Iterator[Element]:
    try:
        import osmium
    except ImportError as e:  # pragma: no cover - exercised only without the optional dependency
        raise AdapterUnavailable("reading .osm.pbf needs the optional 'osmium' package (pip install osmium)") from e
    kinds = osmium.osm.NODE | (osmium.osm.WAY if ways else 0)
    fp = osmium.FileProcessor(str(path), kinds)
    if ways:
        fp = fp.with_locations()
    if matcher.wild:
        fp = fp.with_filter(osmium.filter.KeyFilter(*sorted({k for k, _ in matcher.pairs} | matcher.wild)))
    else:
        fp = fp.with_filter(osmium.filter.TagFilter(*sorted(matcher.pairs)))
    for o in fp:
        tags = {t.k: t.v for t in o.tags}
        if not tags.get("name") or not matcher.match(tags):
            continue
        if o.is_node():
            loc = o.location
            lat, lon = (loc.lat, loc.lon) if loc.valid() else (None, None)
            yield {"type": "node", "id": o.id, "lat": lat, "lon": lon, "tags": tags}
        else:
            pts = []
            for n in o.nodes:
                try:
                    if n.location.valid():
                        pts.append((n.lat, n.lon))
                except Exception:  # noqa: BLE001  (invalid location on unresolved node refs)
                    continue
            lat = sum(p[0] for p in pts) / len(pts) if pts else None
            lon = sum(p[1] for p in pts) / len(pts) if pts else None
            yield {"type": "way", "id": o.id, "lat": lat, "lon": lon, "tags": tags}


def iter_elements(path: Path, matcher: TagMatcher, *, ways: bool = True) -> Iterator[Element]:
    name = path.name.lower()
    if name.endswith(".pbf"):
        return iter_pbf_elements(path, matcher, ways=ways)
    if name.endswith((".osm", ".osm.bz2", ".xml", ".xml.bz2")):
        return iter_xml_elements(path, matcher)
    if name.endswith((".json", ".geojson", ".geojsonl", ".ndjson", ".jsonl")):
        return iter_json_elements(path, matcher)
    raise NotConfigured(f"unsupported OSM file type: {path.name}")


# --------------------------------------------------------------------------- element -> record
def _first(tags: dict[str, str], *keys: str) -> str | None:
    for k in keys:
        v = tags.get(k)
        if v and v.strip():
            return v.split(";")[0].strip()
    return None


def element_to_record(
    el: Element, matcher: TagMatcher, *, default_country: str | None, observed_at: datetime | None = None
) -> BusinessRecord | None:
    tags = el["tags"]
    name = (tags.get("name") or "").strip()
    if not name:
        return None
    postcode = _first(tags, "addr:postcode", "contact:postcode")
    cc = (tags.get("addr:country") or "").strip().upper()
    if len(cc) != 2:
        cc = (default_country or "").upper()
        if cc == "IE" and postcode and uk_postcode_area(postcode) == NI_POSTCODE_AREA:
            cc = "GB"  # Northern Ireland objects sit inside the Ireland extract
    if len(cc) != 2:
        return None
    street = _first(tags, "addr:street")
    hn = _first(tags, "addr:housenumber")
    city = _first(tags, "addr:city", "addr:town", "addr:village", "addr:hamlet", "addr:suburb")
    region = _first(tags, "addr:county", "addr:state", "addr:province")
    addr = ", ".join(x for x in (_first(tags, "addr:housename"), " ".join(p for p in (hn, street) if p) or None,
                                 _first(tags, "addr:suburb") if city != _first(tags, "addr:suburb") else None, city,
                                 region, postcode) if x)
    site = normalise_website(_first(tags, "website", "contact:website", "url", "brand:website"), default_scheme="http")
    email = _first(tags, "email", "contact:email")
    email = email.lower() if email and _EMAIL_RE.match(email) else None
    matched = matcher.match(tags)
    ref = f"{el['type']}/{el['id']}"
    raw = {"osm_id": ref}
    for k in ("operator", "brand", "opening_hours"):
        if tags.get(k):
            raw[k] = tags[k][:120]
    return BusinessRecord(
        source_ref=ref,
        trading_name=name,
        legal_name=None,
        status="active",
        country_code=cc,
        region=region,
        city=city,
        postal_code=normalise_postcode(postcode, cc) if postcode else None,
        address=addr or None,
        phone=_first(tags, "phone", "contact:phone", "contact:mobile"),
        website=site.url if site else None,
        email_published=email,
        osm_tags=matched,
        category_hint=matched[0] if matched else None,
        lat=el.get("lat"),
        lon=el.get("lon"),
        raw=raw,
        source_url=f"https://www.openstreetmap.org/{ref}",
        licence=LICENCE,
        attribution=ATTRIBUTION,
        observed_at=observed_at,
    )


# --------------------------------------------------------------------------- adapter
@register
class OsmGeofabrikAdapter(SourceAdapter):
    info = AdapterInfo(
        adapter_id=ADAPTER_ID,
        name="OpenStreetMap extracts (Geofabrik)",
        country_coverage=("*",),
        source_type="open_data",
        licence=LICENCE,
        source_url="https://download.geofabrik.de/",
        access_requirements=(
            "Public downloads; keep downloads polite (use -latest extracts, avoid repeated re-fetching). Reading .pbf "
            "needs the optional 'osmium' package. Config: 'paths' (local files) or auto-download per country."
        ),
        automated_collection_permitted=True,
        rate_policy=RatePolicy(requests_per_minute=2, concurrency=1),
        fields_available=("trading_name", "address", "phone", "website", "email_published", "osm_tags", "lat", "lon"),
        incremental_support=True,
        parser_version="1",
        attribution=ATTRIBUTION,
        supports_radius=True,
        live_verified=False,
        notes=(
            "download.geofabrik.de was NOT reachable from the build sandbox, so the country->path table is "
            "unverified and downloads are untested. Parsers are tested on locally generated PBF/XML/JSON fixtures."
        ),
    )

    def _paths(self) -> list[Path]:
        cfg = self.config.get("paths") or ([self.config["path"]] if self.config.get("path") else [])
        return [Path(p) for p in cfg]

    async def health(self) -> AdapterHealth:
        paths = self._paths()
        pbf = any(p.name.lower().endswith(".pbf") for p in paths) or not paths
        if pbf and not _osmium_available():
            return AdapterHealth(
                bool(paths) and not any(p.name.lower().endswith(".pbf") for p in paths), "degraded",
                "optional package 'osmium' not installed: .osm.pbf unsupported (OSM XML / JSON still work)",
            )
        if paths:
            missing = [str(p) for p in paths if not p.exists()]
            if missing:
                return AdapterHealth(False, "unavailable", f"missing files: {missing}")
            return AdapterHealth(True, "ok", f"{len(paths)} local file(s)")
        return AdapterHealth(True, "ok", "will auto-download the Geofabrik extract for the query country (unverified)")

    async def _resolve_files(self, query: DiscoveryQuery) -> list[tuple[Path, str | None]]:
        paths = self._paths()
        if paths:
            missing = [p for p in paths if not p.exists()]
            if missing:
                raise NotConfigured(f"configured OSM files missing: {missing}")
            dcc = self.config.get("country_code") or (query.countries[0] if len(query.countries) == 1 else None)
            return [(p, dcc) for p in paths]
        if len(query.countries) != 1:
            raise NotConfigured("auto-download needs exactly one country in the query (or configure 'paths')")
        cc = query.countries[0].upper()
        cands = extract_paths(cc)
        if not cands:
            raise NotConfigured(f"no Geofabrik extract known for {cc}; configure 'paths'")
        chosen = cands[:1]
        wants_ni = cc == "GB" and (
            self.config.get("include_northern_ireland")
            or any(uk_postcode_area(p) == NI_POSTCODE_AREA or p.upper() == NI_POSTCODE_AREA for p in query.postcodes)
        )
        if wants_ni:
            chosen = cands
        out: list[tuple[Path, str | None]] = []
        base = Path(self.config.get("data_dir") or get_settings().data_dir) / "osm"
        for c in chosen:
            url = self.config.get("url") if len(chosen) == 1 and self.config.get("url") else extract_url(c)
            res = await download_file(url, base / (c.replace("/", "_") + "-latest.osm.pbf"),
                                      client=self.config.get("http_client"), max_bytes=int(self.config.get("max_bytes", 30 * 1024**3)))
            # the Ireland extract also holds Northern Ireland: default to IE, NI (BT) is re-labelled GB per record
            out.append((res.path, "IE" if c.endswith("ireland-and-northern-ireland") else cc))
        return out

    async def iter_businesses(
        self, query: DiscoveryQuery, checkpoint: Checkpoint | None = None
    ) -> AsyncIterator[tuple[BusinessRecord, Checkpoint]]:
        files = await self._resolve_files(query)
        matcher = TagMatcher(query.osm_tags)
        sig = matcher.signature()
        cp = checkpoint or {}
        fi0, n0 = (int(cp.get("fi", 0)), int(cp.get("n", -1)) + 1) if cp.get("sig") in (None, sig) else (0, 0)
        flt = RecordFilter(query)
        need_kw = matcher.is_default and flt.kw_re is not None
        observed = datetime.now(UTC)
        emitted = 0
        for fi in range(fi0, len(files)):
            path, file_cc = files[fi]
            skip = n0 if fi == fi0 else 0
            n = -1
            for el in iter_elements(path, matcher, ways=bool(self.config.get("ways", True))):
                n += 1
                if n < skip:
                    continue
                rec = element_to_record(el, matcher, default_country=file_cc, observed_at=observed)
                if rec is None:
                    continue
                rec.adapter_id = ADAPTER_ID
                if not (flt.country_ok(rec) and flt.geo_ok(rec) and flt.radius_ok(rec) and not flt.excluded(rec)):
                    continue
                if need_kw and not flt.category_ok(rec):
                    continue
                yield rec, {"fi": fi, "n": n, "sig": sig, "file": path.name}
                emitted += 1
                if query.limit and emitted >= query.limit:
                    return


__all__ = ["OsmGeofabrikAdapter", "TagMatcher", "element_to_record", "iter_elements"]
