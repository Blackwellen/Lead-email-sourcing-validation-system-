import csv
import dataclasses
import json

import httpx
import pytest

from app.crawler.safe_http import SafeFetcher
from app.sources.base import (
    AdapterInfo,
    AdapterUnavailable,
    BusinessRecord,
    DiscoveryQuery,
    NotConfigured,
    NotPermitted,
    get_adapter,
    list_adapters,
)
from app.sources.directory_catalogue import get_entry, is_automation_permitted, load_catalogue
from app.sources.industry_directories import (
    UK_LICENSED_SPONSORS,
    DirectoryDiscovery,
    DirectoryFetcher,
    FsaFhrsAdapter,
    OpenDataMemberRegisterAdapter,
    PermissionGatedSiteAdapter,
    evaluate_permission,
    require_permission,
)

from .helpers import assert_resume_no_dup_no_gap, collect

FHRS_XML = """<?xml version="1.0"?><FHRSEstablishment xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><Header><ExtractDate>2026-10-01</ExtractDate><ItemCount>3</ItemCount><ReturnCode>Success</ReturnCode></Header><EstablishmentCollection>
<EstablishmentDetail><FHRSID>999001</FHRSID><LocalAuthorityBusinessID>1</LocalAuthorityBusinessID><BusinessName>The Example Arms</BusinessName><BusinessType>Pub/bar/nightclub</BusinessType><AddressLine1>1 Example Road</AddressLine1><AddressLine4>Exampleton</AddressLine4><PostCode>M1 1AA</PostCode><RatingValue>5</RatingValue><RatingDate>2026-02-25</RatingDate><LocalAuthorityName>Example Council</LocalAuthorityName><LocalAuthorityWebSite>http://council.example.org</LocalAuthorityWebSite><LocalAuthorityEmailAddress>foodsafety@council.example.org</LocalAuthorityEmailAddress><Geocode><Longitude>-2.24</Longitude><Latitude>53.48</Latitude></Geocode></EstablishmentDetail>
<EstablishmentDetail><FHRSID>999002</FHRSID><BusinessName>Sample Hotel &amp; Spa</BusinessName><BusinessType>Hotel/bed &amp; breakfast/guest house</BusinessType><AddressLine2>2 Sample Street</AddressLine2><AddressLine3>Sampleton</AddressLine3><PostCode>LS1 4AP</PostCode><RatingValue>4</RatingValue><LocalAuthorityName>Example Council</LocalAuthorityName></EstablishmentDetail>
<EstablishmentDetail><FHRSID>999003</FHRSID><BusinessName>Placeholder Cafe</BusinessName><BusinessType>Restaurant/Cafe/Canteen</BusinessType><PostCode>BS1 5AH</PostCode></EstablishmentDetail>
</EstablishmentCollection></FHRSEstablishment>"""


# ------------------------------------------------------------------------------------------------ permission gating
def test_default_deny_for_every_non_open_catalogue_entry():
    for e in load_catalogue():
        st = evaluate_permission(e, None)
        assert st.allowed == is_automation_permitted(e), e["id"]
        if not st.allowed:
            assert st.status in ("blocked", "permission_required", "review_terms") and "re-verified" in st.reason.lower() or "Terms must be" in st.reason


@pytest.mark.parametrize("entry_id,status", [("yell", "blocked"), ("gas_safe", "permission_required"), ("freeindex", "review_terms"),
                                              ("companies_house_api", "review_terms")])
def test_require_permission_raises_with_explanation(entry_id, status):
    with pytest.raises(NotPermitted) as ei:
        require_permission(entry_id)
    assert get_entry(entry_id)["name"] in str(ei.value)
    assert evaluate_permission(get_entry(entry_id), {}).status == status


def test_unknown_entry_denied_by_default():
    st = evaluate_permission(None, None, entry_id="made_up_directory")
    assert not st.allowed and st.status == "unknown"
    assert evaluate_permission(None, {"permission_basis": "terms_reviewed: read the ToS on 2026-09-01"}, entry_id="made_up_directory").allowed is False
    assert evaluate_permission(None, {"permission_basis": "written: email from owner 2026-09-01 ref 123"}, entry_id="made_up_directory").allowed is True


def test_written_permission_enables_any_status_but_vague_basis_does_not():
    assert require_permission("gas_safe", {"permission_basis": "written: licence email from Gas Safe Register, ref GS-123, 2026-09-01"}).allowed
    assert require_permission("yell", {"permission_basis": "written: signed data licence YELL-2026-01"}).status == "allowed_by_permission"
    for bad in ("yes", "written: ok", "I have permission", "terms_reviewed: ok", None, {"type": "written"}, {"type": "written", "reference": "abc"}):
        with pytest.raises(NotPermitted):
            require_permission("gas_safe", {"permission_basis": bad})
    assert require_permission("gas_safe", {"permission_basis": {"type": "written", "reference": "GS-123 email", "date": "2026-09-01", "recorded_by": "ops"}}).allowed


def test_terms_reviewed_only_valid_where_terms_are_unknown():
    cfg = {"permission_basis": "terms_reviewed: read ToS 2026-09-01 section 4 allows automated access"}
    assert require_permission("freeindex", cfg).allowed
    for eid in ("yell", "gas_safe"):  # prohibited / permission_required need the owner's written permission
        with pytest.raises(NotPermitted):
            require_permission(eid, cfg)


def test_open_data_entries_allowed_without_config():
    assert require_permission("fsa_fhrs").status == "allowed_open_data"
    assert require_permission("charity_commission_ew").allowed


class SiteAdapter(PermissionGatedSiteAdapter):
    catalogue_id = "freeindex"
    info = AdapterInfo(adapter_id="test_site_freeindex", name="t", country_coverage=("GB",), source_type="directory", licence="n/a", source_url="https://x",
                       access_requirements="", automated_collection_permitted=True)

    def parse(self, url, html):
        for line in html.splitlines():
            if line.strip():
                yield BusinessRecord(source_ref=line.strip(), legal_name=line.strip(), country_code="GB")


def robots_fetcher(robots_txt, pages=None, robots_status=200):
    seen = []

    def handler(req: httpx.Request):
        seen.append(str(req.url))
        if req.url.path == "/robots.txt":
            return httpx.Response(robots_status, text=robots_txt, headers={"content-type": "text/plain"})
        return httpx.Response(200, text=(pages or {}).get(req.url.path, "Acme Plumbing\nBeta Heating"), headers={"content-type": "text/html"})

    async def pub(host, port):
        return ["93.184.216.34"]

    return SafeFetcher(resolver=pub, transport=httpx.MockTransport(handler), allow_private_networks=False), seen


async def test_site_adapter_default_deny_makes_no_request():
    f, seen = robots_fetcher("User-agent: *\nAllow: /")
    a = SiteAdapter({"urls": ["https://directory.example.org/list"], "fetcher": f})
    with pytest.raises(NotPermitted):
        await collect(a, DiscoveryQuery())
    assert seen == []


async def test_site_adapter_enabled_by_recorded_permission_and_robots_checked():
    f, seen = robots_fetcher("User-agent: *\nAllow: /")
    cfg = {"urls": ["https://directory.example.org/list"], "fetcher": f, "permission_basis": "terms_reviewed: ToS reviewed 2026-09-01, automation allowed s.4"}
    got = await collect(SiteAdapter(cfg), DiscoveryQuery())
    assert [r.legal_name for r, _ in got] == ["Acme Plumbing", "Beta Heating"]
    assert seen[0].endswith("/robots.txt") and seen[1].endswith("/list")  # robots first, then the page


async def test_site_adapter_robots_disallow_blocks_fetch():
    f, seen = robots_fetcher("User-agent: *\nDisallow: /")
    cfg = {"urls": ["https://directory.example.org/list"], "fetcher": f, "permission_basis": "terms_reviewed: ToS reviewed 2026-09-01, automation allowed s.4"}
    with pytest.raises(NotPermitted, match="robots.txt"):
        await collect(SiteAdapter(cfg), DiscoveryQuery())
    assert all(u.endswith("/robots.txt") for u in seen)


async def test_robots_unreachable_fails_closed():
    f, seen = robots_fetcher("", robots_status=503)
    df = DirectoryFetcher(f)
    with pytest.raises(AdapterUnavailable):
        await df.check("https://directory.example.org/list")


# ------------------------------------------------------------------------------------------------ generic open data register
@pytest.fixture
def sponsors_csv(tmp_path):
    p = tmp_path / "SP_Register_2026-10-13.csv"
    with open(p, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, lineterminator="\r\n")
        w.writerow(["Organisation Name", "Town/City", "County", "Type & Rating", "Route"])
        w.writerows([[" Example Care Ltd", "Leeds", "", "Worker (A rating)", "Skilled Worker"], ["Sample Foods Limited ", " London ", "Greater London", "Worker (A rating)", "Skilled Worker"],
                     ["", "Nowhere", "", "Worker (A rating)", "x"], ["Test Engineering Ltd", "Bristol", "", "Worker (A rating)", "Skilled Worker"]])
    return p


def reg(path, **cfg):
    return OpenDataMemberRegisterAdapter({**UK_LICENSED_SPONSORS, "path": str(path), **cfg})


async def test_generic_csv_register(sponsors_csv):
    got = await collect(reg(sponsors_csv))
    recs = [r for r, _ in got]
    assert [r.legal_name for r in recs] == ["Example Care Ltd", "Sample Foods Limited", "Test Engineering Ltd"]
    assert recs[1].city == "London" and recs[1].region == "Greater London" and recs[0].country_code == "GB"
    assert recs[0].category_hint == "licensed_sponsor" and "Open Government Licence" in recs[0].licence and recs[0].website is None
    await assert_resume_no_dup_no_gap(lambda: reg(sponsors_csv))
    got = await collect(reg(sponsors_csv), DiscoveryQuery(localities=["Leeds"]))
    assert [r.legal_name for r, _ in got] == ["Example Care Ltd"]


async def test_generic_register_requires_licence_and_mapping(sponsors_csv):
    with pytest.raises(NotConfigured):
        await collect(OpenDataMemberRegisterAdapter({"path": str(sponsors_csv), "country_code": "GB", "mapping": {"legal_name": "Organisation Name"}}))
    with pytest.raises(NotConfigured):
        await collect(OpenDataMemberRegisterAdapter({"path": str(sponsors_csv), "licence": "OGL", "country_code": "GB"}))


async def test_generic_register_entry_gate(sponsors_csv):
    with pytest.raises(NotPermitted):
        await collect(reg(sponsors_csv, entry_id="gas_safe"))
    got = await collect(reg(sponsors_csv, entry_id="gas_safe", permission_basis="written: data licence email GS-2026-77 dated 2026-09-01"))
    assert len(got) == 3


async def test_generic_register_json_xml_xlsx(tmp_path):
    base = {"licence": "OGL", "country_code": "GB"}
    j = tmp_path / "m.json"
    j.write_text(json.dumps({"members": [{"org": {"name": "Json One Ltd"}, "pc": "M1 1AA", "web": "json-one.example.com", "tel": "0161 1"}, {"org": {"name": "Json Two Ltd"}}]}))
    got = await collect(OpenDataMemberRegisterAdapter({**base, "path": str(j), "record_path": "members",
                                                         "mapping": {"legal_name": "org.name", "postal_code": "pc", "website": "web", "phone": "tel"}}))
    assert [r.legal_name for r, _ in got] == ["Json One Ltd", "Json Two Ltd"] and got[0][0].postal_code == "M1 1AA" and got[0][0].website == "json-one.example.com"
    x = tmp_path / "m.xml"
    x.write_text("<root><m id='1'><name>Xml One Ltd</name><addr><town>Leeds</town></addr></m><m id='2'><name>Xml Two Ltd</name></m></root>")
    got = await collect(OpenDataMemberRegisterAdapter({**base, "path": str(x), "record_tag": "m",
                                                         "mapping": {"legal_name": "name", "city": "addr/town", "source_ref": "@id"}}))
    assert [(r.source_ref, r.legal_name, r.city) for r, _ in got] == [("1", "Xml One Ltd", "Leeds"), ("2", "Xml Two Ltd", None)]
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Member", "Postcode", "Number"])
    ws.append(["Xlsx One Ltd", "LS1 4AP", 12345])
    p = tmp_path / "m.xlsx"
    wb.save(p)
    got = await collect(OpenDataMemberRegisterAdapter({**base, "path": str(p), "mapping": {"legal_name": "Member", "postal_code": "Postcode", "company_number": "Number"}}))
    assert got[0][0].legal_name == "Xlsx One Ltd" and got[0][0].company_number == "12345" and got[0][0].postal_code == "LS1 4AP"


async def test_generic_register_email_only_when_dataset_publishes_it(tmp_path):
    p = tmp_path / "m.csv"
    p.write_text("Name,Email\nEx One Ltd,hello@ex-one.example.com\n")
    cfg = {"licence": "OGL", "country_code": "GB", "path": str(p), "mapping": {"legal_name": "Name", "email": "Email"}}
    assert (await collect(OpenDataMemberRegisterAdapter(cfg)))[0][0].email_published is None
    assert (await collect(OpenDataMemberRegisterAdapter({**cfg, "dataset_publishes_email": True})))[0][0].email_published == "hello@ex-one.example.com"


async def test_generic_register_url_download_after_robots(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAD_DATA_DIR", str(tmp_path))
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
    f, seen = robots_fetcher("User-agent: *\nAllow: /")
    body = "Organisation Name,Town/City,County\nUrl One Ltd,Leeds,\n"

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, text=body, headers={"content-type": "text/csv", "content-length": str(len(body))})

    cfg = {**UK_LICENSED_SPONSORS, "url": "https://assets.example.org/files/reg.csv", "fetcher": f, "http_client": httpx.AsyncClient(transport=httpx.MockTransport(handler))}
    got = await collect(OpenDataMemberRegisterAdapter(cfg))
    assert [r.legal_name for r, _ in got] == ["Url One Ltd"] and seen[0].endswith("/robots.txt")
    f2, seen2 = robots_fetcher("User-agent: *\nDisallow: /files/")
    with pytest.raises(NotPermitted):
        await collect(OpenDataMemberRegisterAdapter({**cfg, "fetcher": f2}))
    assert all("reg.csv" not in u for u in seen2)


# ------------------------------------------------------------------------------------------------ FHRS
@pytest.fixture
def fhrs_file(tmp_path):
    p = tmp_path / "FHRS999en-GB.xml"
    p.write_text(FHRS_XML, encoding="utf-8")
    return p


async def test_fhrs_records(fhrs_file):
    got = await collect(FsaFhrsAdapter({"paths": [str(fhrs_file)]}))
    by = {r.source_ref: r for r, _ in got}
    arms = by["999001"]
    assert arms.trading_name == "The Example Arms" and arms.postal_code == "M1 1AA" and arms.city == "Exampleton" and arms.lat == 53.48 and arms.lon == -2.24
    assert arms.raw["industries"] == ["hospitality", "pubs"] and arms.raw["rating"] == "5" and arms.source_url == "https://ratings.food.gov.uk/business/999001"
    assert arms.website is None and arms.email_published is None and arms.phone is None  # the council's contact data is NEVER copied
    dump = json.dumps(dataclasses.asdict(arms), default=str).lower()
    assert "foodsafety@" not in dump and "council.example.org" not in dump
    assert by["999002"].raw["industries"] == ["hospitality", "hotels"] and by["999002"].legal_name == "Sample Hotel & Spa"
    assert "Open Government Licence" in arms.licence
    await assert_resume_no_dup_no_gap(lambda: FsaFhrsAdapter({"paths": [str(fhrs_file)]}))


async def test_fhrs_multi_file_resume_and_download(tmp_path, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
    f, seen = robots_fetcher("User-agent: *\nAllow: /", robots_status=404)

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, content=FHRS_XML.encode(), headers={"content-type": "text/xml", "content-length": str(len(FHRS_XML.encode()))})

    mk = lambda: FsaFhrsAdapter({"authority_codes": ["501", "502"], "fetcher": f, "http_client": httpx.AsyncClient(transport=httpx.MockTransport(handler))})  # noqa: E731
    full = await assert_resume_no_dup_no_gap(mk)
    assert len(full) == 6 and {cp["file"] for _, cp in full} == {"FHRS501", "FHRS502"}
    assert any(u.endswith("/OpenDataFiles/FHRS501en-GB.xml") for u in seen)


def test_fhrs_metadata_and_registry_replaces_info_only_entry():
    i = get_adapter("fsa_fhrs").info
    assert i.country_coverage == ("GB",) and "Open Government Licence" in i.licence and i.cost == "free" and i.credentials == "none" and i.live_verified
    ids = {a.info.adapter_id for a in list_adapters()}
    assert "dir_fsa_fhrs" not in ids and "dir_charity_commission_ew" not in ids and "dir_abn_au" not in ids


# ------------------------------------------------------------------------------------------------ discovery helper
def test_directory_discovery_ranking_and_status():
    d = DirectoryDiscovery()
    res = d.find("estate agents", "GB")
    ids = [m.entry["id"] for m in res]
    assert {"propertymark", "naea"} <= set(ids)
    top = res[0]
    assert top.score >= res[-1].score and all(m.permission.allowed is False for m in res if m.entry["automated_extraction"] != "allowed_open_data")
    assert res == d.find("estate agents", "GB") and [m.entry["id"] for m in res] == [m.entry["id"] for m in d.find("Estate Agent", "gb")]
    assert all("GB" in m.entry["countries"] or "*" in m.entry["countries"] for m in res)


def test_directory_discovery_permission_overrides_and_filters():
    d = DirectoryDiscovery(configs={"naea": {"permission_basis": "written: NAEA data licence NAEA-77 signed 2026-09-01"}})
    m = {x.entry["id"]: x for x in d.find("estate agents", "GB")}
    assert m["naea"].permission.allowed and m["naea"].permission.status == "allowed_by_permission" and not m["propertymark"].permission.allowed
    only_ok = d.find("pubs", "GB", include_blocked=False)
    assert only_ok and all(x.permission.allowed for x in only_ok) and "fsa_fhrs" in [x.entry["id"] for x in only_ok]
    assert d.find("estate agents", "FR") == [] or all("FR" in x.entry["countries"] or "*" in x.entry["countries"] for x in d.find("estate agents", "FR"))
    payload = m["naea"].to_dict()
    assert payload["permission"]["allowed"] is True and payload["relevance_reasons"] and payload["recommended_access"]
    assert len(d.find("pubs", "GB", limit=2)) == 2
