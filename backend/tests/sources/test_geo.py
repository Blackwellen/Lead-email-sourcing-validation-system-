import re

import pytest

from app import geo
from app.geo import (
    expand_selection,
    list_countries,
    list_subdivisions,
    load_uk_geography,
    parse_locality_list,
    plan_coverage,
    selection_from_locality_entries,
    source_availability,
)
from app.sources.geo_filters import UK_POSTCODE_RE


def test_countries_and_subdivisions():
    cs = list_countries()
    assert len(cs) >= 240
    gb = geo.get_country("gb")
    assert gb["alpha3"] == "GBR" and gb["name"] == "United Kingdom"
    assert geo.get_country("USA")["alpha2"] == "US"
    subs = list_subdivisions("US")
    assert len(subs) >= 50 and {"code", "name", "type", "parent"} <= set(subs[0])
    assert any(s["code"] == "US-CA" and s["type"] == "State" for s in subs)
    # hierarchy: parent codes reference real subdivisions when present
    fr = list_subdivisions("FR")
    codes = {s["code"] for s in fr}
    assert all(s["parent"] in codes or s["parent"] is None for s in fr)
    with pytest.raises(KeyError):
        list_subdivisions("ZZ")


def test_language_and_address_hints():
    assert "cy" in geo.language_hints("GB") and geo.language_hints("ZZ") == []
    for cc in ("GB", "IE", "US", "CA", "AU", "NZ", "DE", "FR", "NL", "ES", "IT"):
        f = geo.address_format(cc)
        assert not f["generic"] and re.match(f["postcode_regex"], f["postcode_example"].upper()), cc
    g = geo.address_format("JP")
    assert g["generic"] and g["postcode_regex"] is None


# ---------------------------------------------------------------- UK geography integrity
def test_uk_postcode_areas_unique_and_complete():
    g = load_uk_geography()
    codes = [a["code"] for a in g["postcode_areas"]]
    assert len(codes) == len(set(codes)) and len(codes) >= 120
    for must in ("AB", "AL", "B", "BA", "BT", "EC", "EH", "G", "IV", "M", "ZE", "WC", "YO"):
        assert must in codes
    assert not {"GY", "JE", "IM"} & set(codes)  # crown dependencies are not UK
    rid = {r["id"] for r in g["regions"]}
    nid = {n["id"] for n in g["nations"]}
    for a in g["postcode_areas"]:
        assert re.fullmatch(r"[A-Z]{1,2}", a["code"]) and a["town"]
        assert a["region"] in rid and a["nation"] in nid and set(a["also_regions"]) <= rid


def test_uk_regions_nations_las():
    g = load_uk_geography()
    assert {n["name"] for n in g["nations"]} == {"England", "Scotland", "Wales", "Northern Ireland"}
    english = [r for r in g["regions"] if r["nation"] == "england"]
    assert len(english) == 9 and len(g["regions"]) == 12
    by_type: dict[str, int] = {}
    for la in g["local_authorities"]:
        by_type[la["type"]] = by_type.get(la["type"], 0) + 1
        assert la["region"] in {r["id"] for r in g["regions"]}
    assert by_type["metropolitan_borough"] == 36 and by_type["london_borough"] == 32
    assert by_type["principal_area"] == 22 and by_type["council_area"] == 32 and by_type["district"] == 11
    assert by_type["county"] == 21
    names = [la["name"] for la in g["local_authorities"]]
    assert len(names) == len(set(names))
    assert g["uncertainty"]


def test_every_city_maps_to_region_and_existing_areas():
    g = load_uk_geography()
    rid = {r["id"] for r in g["regions"]}
    codes = {a["code"] for a in g["postcode_areas"]}
    assert len(g["cities"]) >= 150
    names = [c["name"] for c in g["cities"]]
    assert len(names) == len(set(names))
    for c in g["cities"]:
        assert c["region"] in rid and c["postcode_areas"] and set(c["postcode_areas"]) <= codes, c
    assert {"Manchester", "Edinburgh", "Cardiff", "Belfast", "Bristol", "Leeds"} <= set(names)
    # every region has at least one city and every postcode area is reachable from some city or is London
    assert {c["region"] for c in g["cities"]} == rid


def test_expand_selection():
    e = expand_selection({"cities": ["Manchester", "Leeds"]})
    assert e.postcode_areas == ["LS", "M"] and e.localities == ["Manchester", "Leeds"]
    e = expand_selection({"nations": ["Northern Ireland"]})
    assert e.postcode_areas == ["BT"]
    e = expand_selection({"nations": ["Wales"]})
    assert {"CF", "SA", "LL"} <= set(e.postcode_areas) and "CH" in e.postcode_areas  # CH overlaps Wales
    assert any("overlapping" in n for n in e.notes)
    e = expand_selection({"regions": ["London"], "postcodes": ["SW1A", "M1 1", "m1 1aa", "XX"], "postcode_areas": ["bt", "ZZ"]})
    assert "E" in e.postcode_areas and "BT" in e.postcode_areas
    assert e.postcode_selectors == ["SW1A", "M1 1", "M1 1AA"] and set(e.unknown) == {"XX", "ZZ"}
    e = expand_selection({"local_authorities": ["Kent", "Atlantis"]})
    assert e.localities == ["Kent"] and e.unknown == ["Atlantis"] and e.notes
    e = expand_selection({"nations": ["Atlantis"]})
    assert e.unknown == ["Atlantis"]


def test_plan_coverage_prefers_bulk_for_broad_selections():
    broad = plan_coverage({"nations": ["England"]}, keywords=5)
    assert broad.strategy == "national_bulk_filter" and not broad.per_locality_queries_allowed
    assert broad.max_search_queries == 0 and broad.avoided_queries_estimate > 100
    assert broad.recommended_adapters[:2] == ["companies_house_bulk", "osm_geofabrik"]
    region = plan_coverage({"regions": ["North West"]})
    assert region.strategy == "regional_bulk_filter" and not region.per_locality_queries_allowed
    many = plan_coverage({"cities": [c["name"] for c in load_uk_geography()["cities"][:40]]})
    assert not many.per_locality_queries_allowed and many.max_search_queries == 0
    narrow = plan_coverage({"cities": ["Manchester"]}, keywords=3)
    assert narrow.strategy == "locality_queries" and narrow.per_locality_queries_allowed and narrow.max_search_queries <= 20
    empty = plan_coverage({})
    assert empty.strategy == "national_bulk_filter" and empty.share_of_uk_areas == 1.0
    assert plan_coverage({"cities": ["Atlantis"]}).unknown == ["Atlantis"]
    assert broad.to_dict()["strategy"] == "national_bulk_filter"


def test_parse_locality_list():
    entries = parse_locality_list("M, SW1A\nLeeds; Kent, M1 1AA, north west, England, Springfield, leeds, bt1 5gs ")
    kinds = {e["raw"]: (e["kind"], e["value"]) for e in entries}
    assert kinds["M"] == ("postcode_area", "M") and kinds["SW1A"] == ("postcode_district", "SW1A")
    assert kinds["Leeds"] == ("city", "Leeds") and kinds["Kent"] == ("local_authority", "Kent")
    assert kinds["M1 1AA"] == ("postcode_full", "M1 1AA") and kinds["north west"][0] == "region"
    assert kinds["England"][0] == "nation" and kinds["Springfield"][0] == "locality"
    assert kinds["bt1 5gs"] == ("postcode_full", "BT1 5GS")
    assert len([e for e in entries if e["raw"].lower() == "leeds"]) == 1  # de-duplicated
    sel = selection_from_locality_entries(entries)
    assert "M" in sel["postcode_areas"] and "Leeds" in sel["cities"] and "Springfield" in sel["localities"]
    assert plan_coverage(sel).postcode_areas  # round-trips into a plan


def test_parse_locality_list_hostile_input():
    with pytest.raises(ValueError):
        parse_locality_list("x" * 500)
    with pytest.raises(ValueError):
        parse_locality_list(",".join(f"place{i}" for i in range(6000)))
    e = parse_locality_list("Leeds\x00\x1b[31m, <script>alert(1)</script>")
    assert all("\x00" not in x["value"] and "\x1b" not in x["value"] for x in e)
    assert parse_locality_list("") == [] and parse_locality_list(" , ;; \n") == []


def test_uk_postcode_regex_sanity():
    for ok in ("SW1A 1AA", "M1 1AA", "B33 8TH", "CR2 6XH", "DN55 1PT", "W1A 0AX", "EC1A 1BB", "GIR 0AA"):
        assert UK_POSTCODE_RE.match(ok), ok
    for bad in ("SW1A 1CA", "QA1 1AA", "M1 1A", "123 456"):
        assert not UK_POSTCODE_RE.match(bad), bad  # C, I, K, M, O, V are never used in the inward letters


# ---------------------------------------------------------------- availability
def test_source_availability_gb_is_honest():
    av = {a["adapter_id"]: a for a in source_availability("GB")}
    ch = av["companies_house_bulk"]
    assert ch["status"] == "ready" and ch["official_dataset"] is True and "Open Government Licence" in ch["licence"]
    assert av["osm_geofabrik"]["status"] == "ready" and "europe/great-britain" in av["osm_geofabrik"]["notes"]
    assert av["wikidata"]["status"] == "needs_config" and av["brave_search"]["status"] == "needs_config"
    assert av["dir_yell"]["status"] == "not_permitted" and av["dir_gas_safe"]["status"] == "permission_required"
    assert av["dir_fsa_fhrs"]["status"] == "not_implemented"
    assert av["company_websites"]["status"] == "info_only"
    assert not any(a["live_verified"] for a in av.values() if a["adapter_id"] in ("companies_house_bulk", "osm_geofabrik"))
    prios = [a["priority"] for a in source_availability("GB")]
    assert prios == sorted(prios)


def test_source_availability_other_countries():
    fr = {a["adapter_id"]: a for a in source_availability("FR")}
    assert "companies_house_bulk" not in fr and "dir_yell" not in fr
    assert fr["osm_geofabrik"]["status"] == "ready" and "europe/france" in fr["osm_geofabrik"]["notes"]
    assert fr["dir_sirene_fr"]["status"] == "not_implemented"
    assert fr["csv_import"]["status"] == "needs_config"
    tv = {a["adapter_id"]: a for a in source_availability("AQ")}
    assert tv["osm_geofabrik"]["status"] == "needs_config" and "No Geofabrik extract" in tv["osm_geofabrik"]["notes"]
