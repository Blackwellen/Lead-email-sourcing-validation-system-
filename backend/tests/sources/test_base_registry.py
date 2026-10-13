import pytest

from app.models import SourceAdapterState
from app.sources import base
from app.sources.base import (
    SOURCE_TYPE_PRIORITY,
    AdapterInfo,
    DiscoveryQuery,
    adapter_status,
    effective_priority,
    get_adapter,
    list_adapters,
    ordered_adapters,
    record_adapter_run,
    set_adapter_enabled,
    set_adapter_priority,
    sync_adapter_states,
)


def test_priority_hierarchy_values():
    assert SOURCE_TYPE_PRIORITY["bulk_dataset"] == 10
    assert SOURCE_TYPE_PRIORITY["open_data"] == 20
    assert SOURCE_TYPE_PRIORITY["registry"] == 30
    assert SOURCE_TYPE_PRIORITY["website"] == 40
    assert SOURCE_TYPE_PRIORITY["search_api"] == 50
    assert SOURCE_TYPE_PRIORITY["manual_import"] == 60


def test_core_adapters_registered():
    ids = {a.info.adapter_id for a in list_adapters()}
    for want in ("companies_house_bulk", "osm_geofabrik", "csv_import", "wikidata", "manual_search_results", "searxng",
                 "brave_search", "commoncrawl_index", "company_websites", "public_pdfs"):
        assert want in ids
    assert get_adapter("companies_house_bulk").info.default_priority == 10
    with pytest.raises(KeyError):
        get_adapter("nope")


def test_adapter_info_rejects_unknown_source_type():
    with pytest.raises(ValueError):
        AdapterInfo(adapter_id="x", name="x", country_coverage=("*",), source_type="nonsense", licence="", source_url="",
                    access_requirements="", automated_collection_permitted=True)


def test_every_adapter_has_complete_info():
    for a in list_adapters():
        i = a.info
        assert i.adapter_id and i.name and i.source_type in base.SOURCE_TYPES
        assert i.country_coverage
        assert i.licence is not None
        if not i.automated_collection_permitted:
            assert i.info_only


def test_query_with_terms_merges_without_duplicates():
    from app.taxonomy import DiscoveryQueryTerms

    q = DiscoveryQuery(keywords=["a"], sic_codes=["68310"])
    t = DiscoveryQueryTerms(category_ids=[1], keywords=["a", "b"], sic_codes=["68310", "68320"], excluded_keywords=["x"])
    q2 = q.with_terms(t)
    assert q2.keywords == ["a", "b"] and q2.sic_codes == ["68310", "68320"] and q2.exclusions == ["x"]
    assert q.keywords == ["a"]  # original untouched


async def test_sync_state_and_overrides(session):
    n = await sync_adapter_states(session)
    assert n >= 10
    assert await sync_adapter_states(session) == 0  # idempotent
    ch = await session.get(SourceAdapterState, "companies_house_bulk")
    assert ch.priority == 10 and ch.enabled is True
    assert (await session.get(SourceAdapterState, "brave_search")).enabled is False  # needs config -> disabled
    assert (await session.get(SourceAdapterState, "dir_yell")).enabled is False

    await set_adapter_priority(session, "osm_geofabrik", 5)
    assert await effective_priority(session, "osm_geofabrik") == 5
    await set_adapter_enabled(session, "csv_import", True)

    ordered = await ordered_adapters(session, country_code="GB")
    ids = [a.info.adapter_id for a, _ in ordered]
    assert ids[0] == "osm_geofabrik"  # priority override wins over the type default
    assert "companies_house_bulk" in ids and "csv_import" in ids
    assert not any(i.startswith("dir_") for i in ids)  # info-only never scheduled
    assert "brave_search" not in ids  # disabled until configured
    assert [p for _, p in ordered] == sorted(p for _, p in ordered)

    await set_adapter_enabled(session, "companies_house_bulk", False)
    ids = [a.info.adapter_id for a, _ in await ordered_adapters(session, country_code="GB")]
    assert "companies_house_bulk" not in ids
    other = [a.info.adapter_id for a, _ in await ordered_adapters(session, country_code="FR")]
    assert "companies_house_bulk" not in other


async def test_record_run_counters(session):
    await record_adapter_run(session, "companies_house_bulk", ok=True, recovered=10)
    await record_adapter_run(session, "companies_house_bulk", ok=False, error="boom")
    await record_adapter_run(session, "companies_house_bulk", ok=True, recovered=5)
    st = await session.get(SourceAdapterState, "companies_house_bulk")
    assert (st.runs_ok, st.runs_failed, st.businesses_recovered) == (2, 1, 15)
    assert st.last_success_at is not None and st.last_error is None
    rows = await adapter_status(session)
    row = next(r for r in rows if r["adapter_id"] == "companies_house_bulk")
    assert row["runs_ok"] == 2 and row["default_priority"] == 10
