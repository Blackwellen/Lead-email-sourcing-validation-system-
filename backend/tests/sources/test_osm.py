import io
import json

import pytest

from app.sources.base import DiscoveryQuery
from app.sources.geofabrik_regions import COUNTRY_EXTRACTS, extract_paths, extract_url
from app.sources.osm_geofabrik import (
    OsmGeofabrikAdapter,
    TagMatcher,
    iter_elements,
    iter_json_array,
)
from tests.sources import fixtures as fx

try:
    import osmium  # noqa: F401
    HAVE_OSMIUM = True
except ImportError:  # pragma: no cover
    HAVE_OSMIUM = False


async def collect(adapter, q, cp=None, stop_after=None):
    out = []
    async for rec, c in adapter.iter_businesses(q, cp):
        out.append((rec, c))
        if stop_after and len(out) >= stop_after:
            break
    return out


def make_files(tmp_path):
    files = {
        "xml": fx.write_osm_xml(tmp_path / "t.osm"),
        "bz2": fx.write_osm_xml(tmp_path / "t.osm.bz2", bz=True),
        "overpass": fx.write_overpass_json(tmp_path / "o.json"),
        "geojson": fx.write_geojson(tmp_path / "g.geojson"),
        "geojsonl": fx.write_geojson(tmp_path / "g.geojsonl", lines=True),
    }
    if HAVE_OSMIUM:
        files["pbf"] = fx.write_pbf(tmp_path / "t.osm.pbf")
    return files


FORMATS = ["xml", "bz2", "overpass", "geojson", "geojsonl", *(["pbf"] if HAVE_OSMIUM else [])]


@pytest.mark.parametrize("fmt", FORMATS)
async def test_tag_filtering_all_formats(tmp_path, fmt):
    f = make_files(tmp_path)[fmt]
    a = OsmGeofabrikAdapter({"path": str(f), "country_code": "GB"})
    got = await collect(a, DiscoveryQuery(osm_tags=["office=estate_agent", "shop=estate_agent"]))
    names = sorted(r.trading_name for r, _ in got)
    assert names == ["Fixture Belfast Agents", "Fixture Estate Agents"]  # unnamed shop=estate_agent ignored
    got = await collect(a, DiscoveryQuery(osm_tags=["amenity=pub", "tourism=hotel", "craft=plumber"]))
    assert sorted(r.trading_name for r, _ in got) == ["Fixture Hotel", "Fixture Plumbers", "The Fixture Arms"]
    got = await collect(a, DiscoveryQuery(osm_tags=["amenity=community_centre"]))
    assert [r.trading_name for r, _ in got] == ["Fixture Hall"]
    assert got[0][0].source_ref.startswith("way/")
    assert got[0][0].lat is not None and got[0][0].lon is not None  # way centroid / centre resolved


@pytest.mark.parametrize("fmt", FORMATS)
async def test_record_mapping_and_provenance(tmp_path, fmt):
    f = make_files(tmp_path)[fmt]
    a = OsmGeofabrikAdapter({"path": str(f), "country_code": "GB"})
    got = await collect(a, DiscoveryQuery(osm_tags=["office=estate_agent"], postcodes=["M"]))
    assert len(got) == 1
    r = got[0][0]
    assert r.trading_name == "Fixture Estate Agents" and r.legal_name is None
    assert r.postal_code == "M1 1AA" and r.city == "Manchester" and r.country_code == "GB"
    assert r.website == "https://fixture-agents.example.com" or r.website == "http://fixture-agents.example.com"
    assert r.email_published == "info@fixture-agents.example.com"  # tagged on the object
    assert r.phone == "+44 161 000 0000"
    assert r.address == "1 Test Street, Manchester, M1 1AA"
    assert r.licence == "ODbL-1.0" and "OpenStreetMap contributors" in r.attribution
    assert r.category_hint == "office=estate_agent" and r.osm_tags == ["office=estate_agent"]
    assert r.source_url.startswith("https://www.openstreetmap.org/node/")
    assert abs(r.lat - 53.4808) < 1e-4


async def test_email_only_when_tagged(tmp_path):
    a = OsmGeofabrikAdapter({"path": str(make_files(tmp_path)["xml"]), "country_code": "GB"})
    got = await collect(a, DiscoveryQuery(osm_tags=["tourism=hotel", "craft=plumber", "amenity=pub"]))
    emails = {r.trading_name: r.email_published for r, _ in got}
    assert emails == {"Fixture Hotel": None, "Fixture Plumbers": None, "The Fixture Arms": "bar@fixture-arms.example.com"}


async def test_default_tags_and_wildcards(tmp_path):
    a = OsmGeofabrikAdapter({"path": str(make_files(tmp_path)["xml"]), "country_code": "GB"})
    got = await collect(a, DiscoveryQuery())  # defaults to shop/office/craft keys
    assert {r.trading_name for r, _ in got} == {"Fixture Estate Agents", "Fixture Plumbers", "Fixture Belfast Agents"}
    got = await collect(a, DiscoveryQuery(osm_tags=["amenity=*"]))
    assert {r.trading_name for r, _ in got} == {"Fixture Bench", "The Fixture Arms", "Fixture Hall"}  # key wildcard


async def test_geo_and_radius_and_exclusions(tmp_path):
    a = OsmGeofabrikAdapter({"path": str(make_files(tmp_path)["overpass"]), "country_code": "GB"})
    q = DiscoveryQuery(osm_tags=["office=estate_agent", "amenity=pub"], localities=["london"])
    assert [r.trading_name for r, _ in await collect(a, q)] == ["The Fixture Arms"]
    q = DiscoveryQuery(osm_tags=["office=estate_agent", "craft=plumber"], centre_lat=53.48, centre_lon=-2.24, radius_km=2)
    assert {r.trading_name for r, _ in await collect(a, q)} == {"Fixture Estate Agents", "Fixture Plumbers"}
    q = DiscoveryQuery(osm_tags=["office=estate_agent"], exclusions=["belfast"])
    assert [r.trading_name for r, _ in await collect(a, q)] == ["Fixture Estate Agents"]


async def test_northern_ireland_inside_ireland_extract(tmp_path):
    a = OsmGeofabrikAdapter({"path": str(make_files(tmp_path)["xml"]), "country_code": "IE"})
    got = await collect(a, DiscoveryQuery(osm_tags=["office=estate_agent"]))
    by = {r.trading_name: r.country_code for r, _ in got}
    assert by["Fixture Belfast Agents"] == "GB"  # BT postcode relabelled
    got = await collect(a, DiscoveryQuery(osm_tags=["office=estate_agent"], countries=["GB"]))
    assert {r.trading_name for r, _ in got} == {"Fixture Belfast Agents", "Fixture Estate Agents"}  # addr:country=GB on the other


async def test_no_country_means_skipped(tmp_path):
    a = OsmGeofabrikAdapter({"path": str(make_files(tmp_path)["xml"])})
    got = await collect(a, DiscoveryQuery(osm_tags=["craft=plumber"]))
    assert got == []  # cannot assign a country -> not guessed


@pytest.mark.parametrize("fmt", FORMATS)
async def test_checkpoint_resume(tmp_path, fmt):
    a = OsmGeofabrikAdapter({"path": str(make_files(tmp_path)[fmt]), "country_code": "GB"})
    q = DiscoveryQuery(osm_tags=["office=estate_agent", "amenity=pub", "tourism=hotel", "craft=plumber", "amenity=community_centre"])
    full = [r.source_ref for r, _ in await collect(a, q)]
    assert len(full) == 6
    for k in range(1, len(full)):
        first = await collect(a, q, stop_after=k)
        rest = await collect(a, q, cp=first[-1][1])
        assert [r.source_ref for r, _ in first] + [r.source_ref for r, _ in rest] == full


async def test_checkpoint_with_different_query_restarts(tmp_path):
    a = OsmGeofabrikAdapter({"path": str(make_files(tmp_path)["xml"]), "country_code": "GB"})
    first = await collect(a, DiscoveryQuery(osm_tags=["amenity=pub"]), stop_after=1)
    got = await collect(a, DiscoveryQuery(osm_tags=["office=estate_agent"]), cp=first[-1][1])
    assert len(got) == 2


def test_tag_matcher():
    m = TagMatcher(["shop=estate_agent", "amenity", "craft=*"])
    assert m.match({"shop": "estate_agent"}) == ["shop=estate_agent"]
    assert m.match({"shop": "bakery"}) == []
    assert m.match({"amenity": "pub"}) == ["amenity=pub"]
    assert m.match({"craft": "plumber;electrician"}) == ["craft=plumber", "craft=electrician"]


def test_incremental_json_reader_small_chunks():
    items = [{"i": i, "s": "x" * 50, "n": {"a": [1, 2, {"b": "]}"}]}} for i in range(200)]
    doc = json.dumps({"generator": "t", "elements": items, "tail": 1})
    got = list(iter_json_array(io.StringIO(doc), __import__("re").compile(r'"elements"\s*:\s*\['), chunk=7))
    assert got == items


def test_unsupported_extension(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("x")
    from app.sources.base import NotConfigured

    with pytest.raises(NotConfigured):
        iter_elements(p, TagMatcher(None))


def test_geofabrik_table():
    assert extract_paths("GB")[0] == "europe/great-britain"
    assert "europe/ireland-and-northern-ireland" in extract_paths("GB")
    assert extract_paths("de") == ["europe/germany"]
    assert extract_paths("ZZ") == []
    assert extract_url("europe/germany") == "https://download.geofabrik.de/europe/germany-latest.osm.pbf"
    import pycountry

    valid = {c.alpha_2 for c in pycountry.countries} | {"XK"}
    assert set(COUNTRY_EXTRACTS) <= valid
    assert len(COUNTRY_EXTRACTS) > 150


async def test_health_messages(tmp_path):
    h = await OsmGeofabrikAdapter({"path": str(tmp_path / "missing.osm")}).health()
    assert not h.ok
    h = await OsmGeofabrikAdapter({"path": str(make_files(tmp_path)["xml"])}).health()
    assert h.ok


async def test_pbf_without_osmium_gives_clear_error(tmp_path, monkeypatch):
    import builtins

    real = builtins.__import__

    def fake(name, *a, **k):
        if name == "osmium":
            raise ImportError("no osmium")
        return real(name, *a, **k)

    p = tmp_path / "x.osm.pbf"
    p.write_bytes(b"not really")
    monkeypatch.setattr(builtins, "__import__", fake)
    from app.sources.base import AdapterUnavailable

    with pytest.raises(AdapterUnavailable, match="osmium"):
        list(iter_elements(p, TagMatcher(None)))
    h = await OsmGeofabrikAdapter({"path": str(p)}).health()
    assert not h.ok and "osmium" in h.message
