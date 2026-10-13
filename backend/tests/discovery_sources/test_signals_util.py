from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.models import BusinessSignal, OrganisationRecordLink, OrganisationSource
from app.sources.base import BusinessRecord
from app.sources.signals_util import MissingSourceUrl, attach_signal, make_signal, persist_record, record_signal, signal_dedupe_hash


def rec(**kw):
    r = BusinessRecord(source_ref="99999991", legal_name="Example Supplies Ltd", company_number="99999991", country_code="GB", adapter_id="contracts_finder_ocds",
                       postal_code="M1 1AA", source_url="https://x.example/n")
    for k, v in kw.items():
        setattr(r, k, v)
    return r


def test_dedupe_hash_is_deterministic_and_sensitive():
    a = signal_dedupe_hash("contract_award", "https://u/1", "Title", datetime(2026, 1, 1, 5, tzinfo=UTC))
    assert a == signal_dedupe_hash("contract_award", "https://u/1", "Title", "2026-01-01") and len(a) == 64
    assert a != signal_dedupe_hash("contract_award", "https://u/2", "Title", "2026-01-01")
    assert a != signal_dedupe_hash("contract_award", "https://u/1", "Title", "2026-01-02")
    assert a != signal_dedupe_hash("sponsor", "https://u/1", "Title", "2026-01-01")


def test_make_signal_validation():
    with pytest.raises(MissingSourceUrl):
        make_signal("contract_award", "t", "")
    with pytest.raises(ValueError):
        make_signal("nonsense", "t", "https://u")
    with pytest.raises(ValueError):
        make_signal("contract_award", "  ", "https://u")
    assert make_signal("contract_award", "t" * 500, "https://u")["title"] == "t" * 400


async def test_persist_record_writes_org_signals_and_link(session):
    r = rec()
    attach_signal(r, "contract_award", "Awarded: X", "https://x.example/n", occurred_at="2026-01-01", detail={"v": 1})
    r.raw["identifiers"] = [{"type": "company_number", "value": "99999991"}]
    r.raw["relationships"] = [{"relation": "supplier_of", "counterparty": {"name": "Buyer"}}]
    res = await persist_record(session, r)
    assert res.created and res.signals_inserted == 1 and res.link_written and res.relationships[0]["relation"] == "supplier_of"
    sig = (await session.execute(select(BusinessSignal))).scalar_one()
    assert sig.organisation_id == res.organisation_id and sig.source_url == "https://x.example/n" and sig.source_adapter == "contracts_finder_ocds"
    assert sig.occurred_at.date().isoformat() == "2026-01-01" and sig.detail == {"v": 1} and len(sig.dedupe_hash) == 64
    src = (await session.execute(select(OrganisationSource))).scalar_one()
    assert "signals" not in src.raw and src.raw["relationships"]  # signals stripped (size-capped column), graph hints kept
    link = (await session.execute(select(OrganisationRecordLink))).scalar_one()
    assert (link.identifier_type, link.identifier_value, link.match_basis) == ("company_number", "99999991", "registration_number")


async def test_persist_record_is_idempotent(session):
    for _ in range(3):
        r = rec()
        attach_signal(r, "contract_award", "Awarded: X", "https://x.example/n", occurred_at="2026-01-01")
        res = await persist_record(session, r)
    assert not res.created and res.signals_inserted == 0 and res.signals_total == 1
    assert (await session.execute(select(func.count()).select_from(BusinessSignal))).scalar_one() == 1


async def test_record_signal_rejects_missing_url(session):
    r = rec()
    res = await persist_record(session, r)
    with pytest.raises(MissingSourceUrl):
        await record_signal(session, res.organisation_id, "x", {"signal_type": "exhibitor", "title": "t", "source_url": None})
    assert (await session.execute(select(func.count()).select_from(BusinessSignal))).scalar_one() == 0


async def test_distinct_awards_make_distinct_signals(session):
    r = rec()
    attach_signal(r, "contract_award", "Awarded: A", "https://x.example/1", occurred_at="2026-01-01")
    attach_signal(r, "contract_award", "Awarded: B", "https://x.example/2", occurred_at="2026-01-01")
    res = await persist_record(session, r)
    assert res.signals_inserted == 2
