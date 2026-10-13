import pytest

from app.sources.base import list_adapters
from app.sources.catalogue_full import ACCESS_METHODS, SCORE_WEIGHTS, search_catalogue, source_catalogue
from app.sources.directory_catalogue import load_catalogue

REQUIRED = {"id", "name", "countries", "source_type", "access_method", "licence", "cost", "credentials", "credentials_required", "refresh_frequency",
            "automated_allowed", "industries", "configured", "health"}


@pytest.fixture(scope="module")
def rows():
    return source_catalogue()


def test_every_adapter_and_catalogue_entry_is_listed_once(rows):
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids))
    adapter_ids = {a.info.adapter_id for a in list_adapters() if not a.__class__.__module__.endswith(".pseudo") and not a.info.adapter_id.startswith("dir_")}
    assert adapter_ids <= set(ids), adapter_ids - set(ids)
    assert not any(i.startswith("dir_") for i in ids)  # info-only catalogue stubs are merged into their catalogue entry
    cat_ids = {r["catalogue_id"] for r in rows if r["catalogue_id"]}
    assert cat_ids == {e["id"] for e in load_catalogue()}


def test_rich_fields_are_complete_and_well_formed(rows):
    for r in rows:
        assert REQUIRED <= set(r), r["id"]
        assert r["access_method"] in ACCESS_METHODS and r["credentials"] in ("none", "optional", "required")
        assert r["credentials_required"] == (r["credentials"] == "required")
        assert r["cost"] in ("free", "free_registration", "paid") and r["countries"] and r["industries"]
        assert r["health"]["status"] in ("ok", "not_configured", "info_only", "restricted", "not_implemented") and isinstance(r["configured"], bool)
        if not r["automated_allowed"]:
            assert r["access_method"] == "restricted" and not r["configured"]


def test_honest_declarations_for_new_sources(rows):
    by = {r["id"]: r for r in rows}
    for aid, cc in [("charity_commission_ew", {"GB"}), ("contracts_finder_ocds", {"GB"}), ("find_a_tender_ocds", {"GB"}), ("usaspending", {"US"}),
                    ("abn_lookup_au", {"AU"}), ("fsa_fhrs", {"GB"}), ("companies_house_new_registrations", {"GB"})]:
        assert set(by[aid]["countries"]) == cc, aid
        assert by[aid]["cost"] == "free" and by[aid]["automated_allowed"] and by[aid]["refresh_frequency"], aid
    assert by["gleif_lei"]["countries"] == ["*"] and "CC0" in by["gleif_lei"]["licence"]
    assert by["abn_lookup_au"]["credentials"] == "optional"
    assert "DE" in by["eu_ted"]["countries"] and "US" not in by["eu_ted"]["countries"]
    assert by["yell"]["access_method"] == "restricted" and not by["yell"]["automated_allowed"]
    assert by["gas_safe"]["access_method"] == "restricted"
    assert by["companies_house_api"]["credentials_required"] is True and by["companies_house_api"]["access_method"] != "bulk_file"
    assert by["companies_house_bulk"]["access_method"] == "bulk_file" and by["companies_house_bulk"]["configured"]  # works without config
    assert by["fsa_fhrs"]["configured"] is False and by["charity_commission_ew"]["configured"] is False  # need a path/url first
    assert by["exhibitor_list_import"]["access_method"] == "manual_import"


def test_configured_flag_follows_stored_config():
    assert not {r["id"]: r for r in source_catalogue()}["fsa_fhrs"]["configured"]
    r = {r["id"]: r for r in source_catalogue({"fsa_fhrs": {"authority_codes": ["501"]}})}["fsa_fhrs"]
    assert r["configured"] and r["health"]["status"] == "ok"


def test_search_is_deterministic_and_ranked(rows):
    a = search_catalogue(country="GB", business_type="pubs")
    b = search_catalogue(country="GB", business_type="pubs")
    assert a == b and [r["id"] for r in a] == [r["id"] for r in sorted(a, key=lambda r: (-r["score"], r["id"]))]
    assert a[0]["id"] == "fsa_fhrs"  # the only GB source that is exactly about pubs and is usable
    assert a[0]["score"] == sum(a[0]["score_breakdown"].values())
    bd = a[0]["score_breakdown"]
    assert bd["country_exact"] == SCORE_WEIGHTS["country_exact"] and bd["industry_exact"] == SCORE_WEIGHTS["industry_exact"] and bd["automated_allowed"] == SCORE_WEIGHTS["automated_allowed"]
    assert search_catalogue(country="GB", business_type="Pub") [0]["id"] == "fsa_fhrs"
    shuffled = search_catalogue(country="GB", business_type="pubs", rows=list(reversed(rows)))
    assert shuffled == a  # input order is irrelevant


def test_country_filter_excludes_other_countries_but_keeps_worldwide():
    ids = {r["id"] for r in search_catalogue(country="AU")}
    assert "abn_lookup_au" in ids and "gleif_lei" in ids and "charity_commission_ew" not in ids and "yell" not in ids
    top = search_catalogue(country="AU")[0]
    assert top["id"] == "abn_lookup_au"  # country-specific beats worldwide
    assert {r["id"] for r in search_catalogue(country="DE")} >= {"eu_ted", "gleif_lei", "handelsregister_de"}


def test_hard_filters():
    assert {r["access_method"] for r in search_catalogue(access_method="bulk_file")} == {"bulk_file"}
    assert {r["id"] for r in search_catalogue(access_method="restricted", country="GB")} >= {"yell", "gas_safe"}
    assert {r["cost"] for r in search_catalogue(cost="paid")} <= {"paid"}
    assert all("cc0" in r["licence"].casefold() for r in search_catalogue(licence="CC0"))
    assert {r["id"] for r in search_catalogue(licence="open government licence", country="GB")} >= {"charity_commission_ew", "contracts_finder_ocds", "fsa_fhrs"}
    odd = search_catalogue(country="ZZ", business_type="zzzz-nothing")  # only worldwide, industry-agnostic sources can match
    assert odd and all("*" in r["countries"] and "*" in r["industries"] for r in odd)
    assert all("country_exact" not in r["score_breakdown"] and "industry_exact" not in r["score_breakdown"] for r in odd)


def test_free_text_query_ranking():
    res = search_catalogue(q="charity register")
    assert res[0]["id"] == "charity_commission_ew"
    assert res[0]["score_breakdown"]["q_name_token"] >= 2 * SCORE_WEIGHTS["q_name_token"]
    assert [r["id"] for r in search_catalogue(q="tender award")][:3] and search_catalogue(q="qqqqzzzz") == []
    assert {r["id"] for r in search_catalogue(q="contract awards", country="GB")} >= {"contracts_finder_ocds", "find_a_tender_ocds"}


def test_limit_and_restricted_penalty():
    assert len(search_catalogue(country="GB", limit=3)) == 3
    res = {r["id"]: r for r in search_catalogue(country="GB", business_type="estate agents")}
    assert res["propertymark"]["score_breakdown"]["restricted"] == SCORE_WEIGHTS["restricted"] < 0
