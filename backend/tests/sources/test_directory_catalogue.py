import pytest

from app.sources.base import AdapterUnavailable, DiscoveryQuery, NotPermitted, get_adapter, list_adapters
from app.sources.directory_catalogue import (
    ACCESS_PATHS,
    STATUSES,
    catalogue_disclaimer,
    entries_for_country,
    get_entry,
    is_automation_permitted,
    load_catalogue,
)


def test_catalogue_integrity():
    entries = load_catalogue()
    assert len(entries) >= 50
    ids = [e["id"] for e in entries]
    assert len(ids) == len(set(ids))
    for e in entries:
        assert e["automated_extraction"] in STATUSES and e["recommended_access"] in ACCESS_PATHS
        assert e["name"] and e["url"].startswith("https://") and e["countries"] and e["licence_notes"]
    assert "re-verify" in catalogue_disclaimer().lower()


def test_expected_uk_entries_present_and_default_deny():
    uk = {e["id"]: e for e in entries_for_country("GB")}
    for want in ("yell", "thomson_local", "freeindex", "trustmark", "rics", "propertymark", "naea", "arla", "checkatrade",
                 "gas_safe", "niceic", "napit", "fensa", "bcc_chambers", "fsb"):
        assert want in uk, want
    for bad in ("yell", "thomson_local", "checkatrade", "google_maps", "linkedin"):
        assert not is_automation_permitted(get_entry(bad))
    # only clearly open data is "permitted"; anything unknown/permission-based is denied
    for e in uk.values():
        assert is_automation_permitted(e) == (e["automated_extraction"] == "allowed_open_data")
    assert get_entry("yell")["automated_extraction"] == "prohibited"
    assert get_entry("gas_safe")["automated_extraction"] == "permission_required"
    assert get_entry("google_maps")["recommended_access"] == "skip"
    open_ids = {e["id"] for e in uk.values() if e["automated_extraction"] == "allowed_open_data"}
    assert {"companies_house_bulk", "osm_geofabrik", "wikidata"} <= open_ids


def test_non_uk_countries_have_leads():
    assert any(e["id"] == "sirene_fr" for e in entries_for_country("FR"))
    assert any(e["id"] == "brreg_no" for e in entries_for_country("NO"))
    assert all(e["id"] != "yell" for e in entries_for_country("FR"))


@pytest.mark.parametrize("entry_id", ["yell", "checkatrade", "gas_safe", "freeindex", "google_maps"])
async def test_catalogue_adapters_raise_not_permitted(entry_id):
    a = get_adapter(f"dir_{entry_id}")
    assert a.info.info_only and not a.info.automated_collection_permitted
    with pytest.raises(NotPermitted) as ei:
        async for _ in a.iter_businesses(DiscoveryQuery()):
            pass
    assert "re-verified" in str(ei.value) and get_entry(entry_id)["recommended_access"] in str(ei.value)
    h = await a.health()
    assert not h.ok and h.status == "info_only"


async def test_open_dataset_without_adapter_is_unavailable_not_permitted():
    a = get_adapter("dir_oscr")
    with pytest.raises(AdapterUnavailable):
        async for _ in a.iter_businesses(DiscoveryQuery()):
            pass


def test_no_catalogue_adapter_is_automatically_permitted():
    for a in list_adapters():
        if a.info.adapter_id.startswith("dir_"):
            assert a.info.automated_collection_permitted is False and a.info.info_only


def test_implemented_adapters_not_duplicated_as_catalogue_adapters():
    ids = {a.info.adapter_id for a in list_adapters()}
    assert "dir_companies_house_bulk" not in ids and "dir_osm_geofabrik" not in ids and "dir_wikidata" not in ids
