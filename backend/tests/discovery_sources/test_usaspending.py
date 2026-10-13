import json

import httpx

from app.sources.base import DiscoveryQuery, get_adapter
from app.sources.usaspending import UsaSpendingAdapter, row_to_record

from .fixtures import usa_pages, usa_row, usa_rows
from .helpers import assert_resume_no_dup_no_gap, collect


def adapter(pages=None, seen=None, **cfg):
    pages = pages or usa_pages()

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        if seen is not None:
            seen.append(body)
        return httpx.Response(200, json=pages[body["page"] - 1])

    async def nosleep(_s):
        return None

    return UsaSpendingAdapter({"http_client": httpx.AsyncClient(transport=httpx.MockTransport(handler)), "min_interval_s": 0, "sleep": nosleep, **cfg})


def test_metadata():
    i = get_adapter("usaspending").info
    assert i.country_coverage == ("US",) and i.cost == "free" and i.credentials == "none" and "ublic domain" in i.licence


def test_row_mapping():
    r = row_to_record(usa_rows()[0])
    assert r.legal_name == "EXAMPLE FEDERAL SERVICES INC" and r.country_code == "US" and r.region == "TX" and r.postal_code == "75001"
    assert r.raw["uei"] == "EXAMPLEUEI001" and r.raw["industries"] == ["professional_services"] and r.company_number is None
    assert r.raw["identifiers"] == [{"type": "uei", "value": "EXAMPLEUEI001"}]
    s = r.raw["signals"][0]
    assert s["signal_type"] == "contract_award" and s["source_url"].startswith("https://www.usaspending.gov/award/CONT_AWD_EX0001")
    assert s["detail"]["awarding_agency"] == "Department of Examples" and s["detail"]["currency"] == "USD"
    assert r.website is None and r.email_published is None
    assert row_to_record(usa_row("X", "MULTIPLE RECIPIENTS", "")) is None


def test_row_without_internal_id_has_no_signal():
    row = usa_row("EX9", "NO ID INC", "U9")
    row["generated_internal_id"] = None
    r = row_to_record(row)
    assert r.source_url is None and "signals" not in r.raw


async def test_paging_body_and_resume():
    seen = []
    full = await assert_resume_no_dup_no_gap(lambda: adapter(seen=seen))
    assert len(full) == 4  # MULTIPLE RECIPIENTS skipped
    b = seen[0]
    assert b["filters"]["award_type_codes"] == ["A", "B", "C", "D"] and b["limit"] == 100 and b["page"] == 1
    assert "Recipient UEI" in b["fields"]


async def test_established_filter():
    got = await collect(adapter(), DiscoveryQuery(active_only=False, extra={"min_awards": 2}))
    assert [r.legal_name for r, _ in got] == ["EXAMPLE FEDERAL SERVICES INC"]
    assert got[0][0].raw["award_count"] == 2 and got[0][0].raw["award_years"] == 2
    got = await collect(adapter(min_award_years=2))
    assert len(got) == 1


async def test_saved_response_file(tmp_path):
    p = tmp_path / "resp.json"
    p.write_text(json.dumps({"results": usa_rows()}))
    got = await collect(UsaSpendingAdapter({"path": str(p)}))
    assert len(got) == 4
    await assert_resume_no_dup_no_gap(lambda: UsaSpendingAdapter({"path": str(p)}))
