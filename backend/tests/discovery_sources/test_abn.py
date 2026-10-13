import json

import httpx
import pytest
from lxml import etree

from app.sources.abn_lookup import AbnLookupAdapter, abr_to_record, lookup_to_record, parse_lookup_response
from app.sources.base import DiscoveryQuery, NotConfigured, get_adapter

from .fixtures import abn_xml, abr_xml, write_abn_zip
from .helpers import assert_resume_no_dup_no_gap, collect


def test_metadata():
    i = get_adapter("abn_lookup_au").info
    assert i.country_coverage == ("AU",) and "Attribution 3.0 Australia" in i.licence and i.cost == "free"
    assert i.credentials == "optional" and "GUID" in i.credentials_note and i.refresh_frequency


async def test_bulk_parse(tmp_path):
    p = write_abn_zip(tmp_path / "abn.zip")
    got = await collect(AbnLookupAdapter({"path": str(p)}))
    by = {r.source_ref: r for r, _ in got}
    assert set(by) == {"11000000001", "11000000002", "11000000004", "11000000005"}  # sole trader skipped
    a = by["11000000001"]
    assert a.legal_name == "EXAMPLE PTY LTD" and a.trading_name == "Example Trading Co" and a.company_number == "11000000001"
    assert a.region == "NSW" and a.postal_code == "2000" and a.status == "Active" and a.legal_type == "Australian Private Company"
    assert a.raw["acn"] == "000000019" and {"type": "acn", "value": "000000019"} in a.raw["identifiers"]
    assert a.source_url == "https://abr.business.gov.au/ABN/View?abn=11000000001" and "CC BY 3.0" in a.attribution
    assert a.website is None and a.email_published is None
    assert by["11000000004"].status == "Cancelled"
    active = await collect(AbnLookupAdapter({"path": str(p)}), DiscoveryQuery())
    assert "11000000004" not in {r.source_ref for r, _ in active}


async def test_individuals_only_when_opted_in_and_never_by_person_name(tmp_path):
    xml = abn_xml([abr_xml("11000000009", "", individual=True, etype=("IND", "Individual/Sole Trader"), trading="Example Plumbing")])
    p = tmp_path / "x.xml"
    p.write_text(xml)
    assert await collect(AbnLookupAdapter({"path": str(p)})) == []
    got = await collect(AbnLookupAdapter({"path": str(p), "include_individuals": True}))
    assert got[0][0].legal_name == "Example Plumbing" and "Person" not in got[0][0].legal_name


async def test_filters_and_entity_types(tmp_path):
    p = write_abn_zip(tmp_path / "abn.zip")
    got = await collect(AbnLookupAdapter({"path": str(p)}), DiscoveryQuery(active_only=False, postcodes=["3000"]))
    assert [r.source_ref for r, _ in got] == ["11000000002"]
    got = await collect(AbnLookupAdapter({"path": str(p), "entity_types": ["DIT"]}))
    assert [r.source_ref for r, _ in got] == ["11000000005"]
    assert await collect(AbnLookupAdapter({"path": str(p)}), DiscoveryQuery(countries=["GB"])) == []


async def test_resume_across_zip_members(tmp_path):
    p = write_abn_zip(tmp_path / "abn.zip")
    full = await assert_resume_no_dup_no_gap(lambda: AbnLookupAdapter({"path": str(p)}))
    assert {cp["key"].split("::")[1] for _, cp in full} == {"20261006_Public01.xml", "20261006_Public02.xml"}


async def test_directory_of_parts(tmp_path):
    d = tmp_path / "parts"
    d.mkdir()
    (d / "a.xml").write_text(abn_xml([abr_xml("11000000010", "A PTY LTD")]))
    (d / "b.xml").write_text(abn_xml([abr_xml("11000000011", "B PTY LTD")]))
    got = await collect(AbnLookupAdapter({"path": str(d)}))
    assert [r.source_ref for r, _ in got] == ["11000000010", "11000000011"]


def test_malformed_abn_is_skipped():
    el = etree.fromstring(abr_xml("123", "BAD"))
    assert abr_to_record(el) is None


async def test_not_configured():
    with pytest.raises(NotConfigured):
        await collect(AbnLookupAdapter({}))


# -- optional web-service enrichment (GUID); format UNVERIFIED live, shape per ABR docs -------------------------------
def test_lookup_response_parsing():
    body = {"Abn": "11000000001", "AbnStatus": "Active", "AddressPostcode": "2000", "AddressState": "NSW", "EntityName": "EXAMPLE PTY LTD",
            "EntityTypeCode": "PRV", "EntityTypeName": "Australian Private Company", "Gst": "2000-07-01", "BusinessName": ["Example Trading"], "Message": ""}
    parsed = parse_lookup_response("cb(" + json.dumps(body) + ")")
    r = lookup_to_record(parsed)
    assert r.legal_name == "EXAMPLE PTY LTD" and r.trading_name == "Example Trading" and r.raw["via"] == "abn_lookup_web_services"
    assert lookup_to_record({**body, "EntityTypeCode": "IND"}) is None and lookup_to_record({**body, "Message": "No records found"}) is None


async def test_guid_lookup_mode(nosleep):
    seen = []

    def handler(req: httpx.Request):
        seen.append(dict(req.url.params))
        abn = req.url.params["abn"]
        return httpx.Response(200, text="cb(" + json.dumps({"Abn": abn, "AbnStatus": "Active", "EntityName": f"EXAMPLE {abn} PTY LTD", "EntityTypeCode": "PRV",
                                                            "AddressState": "NSW", "AddressPostcode": "2000", "Message": ""}) + ")")

    a = lambda: AbnLookupAdapter({"guid": "00000000-0000-0000-0000-000000000000", "abns": ["11 000 000 001", "11000000002"],  # noqa: E731
                                  "http_client": httpx.AsyncClient(transport=httpx.MockTransport(handler)), "sleep": nosleep, "min_interval_s": 0})
    full = await assert_resume_no_dup_no_gap(a)
    assert [r.source_ref for r, _ in full] == ["11000000001", "11000000002"] and seen[0]["guid"] == "00000000-0000-0000-0000-000000000000"
