import pytest

from app.sources.base import BusinessRecord, DiscoveryQuery
from app.sources.geo_filters import (
    RecordFilter,
    classify_uk_selector,
    haversine_km,
    is_valid_postcode,
    normalise_postcode,
    postcode_matches,
    sic_matches,
    uk_outward_code,
    uk_postcode_area,
    uk_postcode_sector,
)


@pytest.mark.parametrize("pc,area,district,sector", [
    ("SW1A 1AA", "SW", "SW1A", "SW1A 1"), ("sw1a1aa", "SW", "SW1A", "SW1A 1"), ("M1 1AA", "M", "M1", "M1 1"),
    ("EC1A 1BB", "EC", "EC1A", "EC1A 1"), ("B33 8TH", "B", "B33", "B33 8"), ("CR2 6XH", "CR", "CR2", "CR2 6"),
    ("DN55 1PT", "DN", "DN55", "DN55 1"), ("W1A 0AX", "W", "W1A", "W1A 0"),
])
def test_uk_postcode_parts(pc, area, district, sector):
    assert uk_postcode_area(pc) == area
    assert uk_outward_code(pc) == district
    assert uk_postcode_sector(pc) == sector


@pytest.mark.parametrize("bad", ["", None, "12345", "ABCDE", "SW1A", "Q1 1AA", "SW1A 1A"])
def test_invalid_uk_postcodes(bad):
    assert not is_valid_postcode(bad, "GB")


def test_normalise_postcode():
    assert normalise_postcode(" sw1a   1aa ") == "SW1A 1AA"
    assert normalise_postcode("M11AA") == "M1 1AA"
    assert normalise_postcode("75001", "FR") == "75001"
    assert normalise_postcode("", "GB") is None


def test_selector_classification_and_matching():
    assert classify_uk_selector("S") == "area"
    assert classify_uk_selector("SW1A") == "district"
    assert classify_uk_selector("SW1A 1") == "sector"
    assert classify_uk_selector("SW1A 1AA") == "full"
    # area 'S' (Sheffield) must NOT match SW / SE ... (the classic startswith bug)
    assert postcode_matches("S1 2AB", ["S"])
    assert not postcode_matches("SW1A 1AA", ["S"])
    assert postcode_matches("SW1A 1AA", ["SW"])
    assert postcode_matches("SW1A 1AA", ["SW1A"])
    assert not postcode_matches("SW1A 1AA", ["SW1"])  # SW1 is a different district from SW1A
    assert postcode_matches("SW1A 1AA", ["SW1A 1"])
    assert not postcode_matches("SW1A 2AA", ["SW1A 1"])
    assert postcode_matches("SW1A 1AA", ["sw1a1aa"])
    assert postcode_matches("anything", [])
    assert not postcode_matches(None, ["M"])
    assert postcode_matches("75008", ["75"], "FR")


def test_haversine_known_distance():
    # London (51.5074,-0.1278) to Paris (48.8566, 2.3522) ~ 343.5 km
    d = haversine_km(51.5074, -0.1278, 48.8566, 2.3522)
    assert 340 < d < 347
    assert haversine_km(1, 1, 1, 1) == 0


def test_sic_matching_exact_and_prefix():
    assert sic_matches(["68310"], ["68310"])
    assert not sic_matches(["68310"], ["68320"])
    assert sic_matches(["68310"], ["683"])
    assert not sic_matches(["68310"], ["99999"])


def rec(**kw):
    base = dict(source_ref="r", legal_name="Fixture Co Ltd", country_code="GB", status="Active")
    base.update(kw)
    return BusinessRecord(**base)


def test_record_filter_semantics():
    q = DiscoveryQuery(countries=["GB"], postcodes=["M"], localities=["Leeds"], sic_codes=["68310"], keywords=["lettings"],
                       exclusions=["dormant"])
    f = RecordFilter(q)
    assert f.accept(rec(postal_code="M1 1AA", sic_codes=["68310"]))
    assert f.accept(rec(city="LEEDS", sic_codes=["68310"]))  # geography is a union
    assert not f.accept(rec(postal_code="B1 1AA", city="Bristol", sic_codes=["68310"]))
    assert f.accept(rec(postal_code="M1 1AA", legal_name="Fixture Lettings Ltd", sic_codes=["99999"]))  # keyword OR sic
    assert not f.accept(rec(postal_code="M1 1AA", sic_codes=["99999"]))
    assert not f.accept(rec(postal_code="M1 1AA", legal_name="Dormant Lettings Ltd", sic_codes=["68310"]))
    assert not f.accept(rec(postal_code="M1 1AA", sic_codes=["68310"], status="Dissolved"))
    assert not f.accept(rec(postal_code="M1 1AA", sic_codes=["68310"], country_code="IE"))
    assert RecordFilter(DiscoveryQuery(active_only=False)).accept(rec(status="Dissolved"))


def test_category_match_modes():
    r1 = rec(sic_codes=["68310"], legal_name="Nothing Relevant Ltd")
    r2 = rec(sic_codes=["11111"], legal_name="Fixture Lettings Ltd")
    both = rec(sic_codes=["68310"], legal_name="Fixture Lettings Ltd")
    mk = lambda mode: RecordFilter(DiscoveryQuery(sic_codes=["68310"], keywords=["lettings"], extra={"category_match": mode}))  # noqa: E731
    assert mk("any").accept(r1) and mk("any").accept(r2)
    assert not mk("all").accept(r1) and not mk("all").accept(r2) and mk("all").accept(both)
    assert mk("sic").accept(r1) and not mk("sic").accept(r2)
    assert mk("keywords").accept(r2) and not mk("keywords").accept(r1)


def test_radius_filter_requires_coordinates():
    q = DiscoveryQuery(centre_lat=53.48, centre_lon=-2.24, radius_km=5)
    f = RecordFilter(q)
    assert f.accept(rec(lat=53.481, lon=-2.241))
    assert not f.accept(rec(lat=51.5, lon=-0.12))
    assert not f.accept(rec())  # no coordinates -> rejected (strict by default)
    q.extra["radius_keep_unlocated"] = True
    assert RecordFilter(q).accept(rec())
