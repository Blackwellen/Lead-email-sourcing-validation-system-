import csv
import json

import httpx
import pytest

from app.sources.base import DiscoveryQuery, NotConfigured, NotPermitted, get_adapter
from app.sources.exhibitors import ExhibitorListImport, normalise_role, parse_event_date

from .test_directories import robots_fetcher
from .helpers import assert_resume_no_dup_no_gap, collect

LIST_URL = "https://events.example.org/example-expo-2027/exhibitors"
ROWS = [
    ["Company", "Website", "Stand", "Event Name", "Event Date", "Event URL", "Role", "Country"],
    ["Example Fixings Ltd", "www.example-fixings.example.com", "A12", "Example Expo 2027", "2027-03-10", "https://events.example.org/example-expo-2027", "Exhibitor", "GB"],
    ["Sample Software Inc", "", "", "Example Expo 2027", "10/03/2027", "", "Gold Sponsor", "US"],
    ["Test Speaker Consulting", "", "", "Example Expo 2027", "10 March 2027", "", "Speaker", "GB"],
    ["", "x.example.com", "B1", "Example Expo 2027", "2027-03-10", "", "Exhibitor", "GB"],
    ["No Country Co", "", "", "Example Expo 2027", "2027-03-10", "", "Exhibitor", ""],
]


@pytest.fixture
def csv_file(tmp_path):
    p = tmp_path / "exhibitors.csv"
    with open(p, "w", newline="") as fh:
        csv.writer(fh).writerows(ROWS)
    return p


def imp(path, **cfg):
    return ExhibitorListImport({"path": str(path), "list_url": LIST_URL, **cfg})


def test_metadata():
    i = get_adapter("exhibitor_list_import").info
    assert i.source_type == "manual_import" and i.cost == "free" and i.access_method == "manual_import" and i.requires_config


def test_dates_and_roles():
    for s in ("2027-03-10", "10/03/2027", "10 March 2027", "2027-03-10 to 2027-03-12", "March 10, 2027"):
        assert parse_event_date(s).date().isoformat() == "2027-03-10", s
    assert parse_event_date("soon") is None and parse_event_date("") is None
    assert [normalise_role(x) for x in ("Exhibitor", "Gold Sponsor", "Headline Sponsor", "Speaker", "", "stand holder")] == \
        ["exhibitor", "sponsor", "sponsor", "speaker", "exhibitor", "exhibitor"]


async def test_csv_records_and_signals(csv_file):
    a = imp(csv_file)
    got = await collect(a)
    recs = {r.legal_name: r for r, _ in got}
    assert set(recs) == {"Example Fixings Ltd", "Sample Software Inc", "Test Speaker Consulting"}  # blank company / no country skipped
    ex = recs["Example Fixings Ltd"]
    assert ex.website == "www.example-fixings.example.com" and ex.category_hint == "exhibitor"
    s = ex.raw["signals"][0]
    assert s["signal_type"] == "exhibitor" and s["source_url"] == LIST_URL and s["occurred_at"].startswith("2027-03-10")
    assert s["detail"] == {"role": "exhibitor", "event_name": "Example Expo 2027", "event_date": "2027-03-10", "stand": "A12",
                           "event_url": "https://events.example.org/example-expo-2027", "list_url": LIST_URL}
    assert recs["Sample Software Inc"].raw["signals"][0]["signal_type"] == "sponsor" and recs["Sample Software Inc"].country_code == "US"
    assert recs["Test Speaker Consulting"].raw["signals"][0]["signal_type"] == "event"
    assert ex.raw["participation"]["stand"] == "A12" and ex.raw["event_key"] == "example expo 2027"
    assert ex.raw["relationships"][0]["relation"] == "exhibits_with" and not recs["Sample Software Inc"].raw["relationships"]
    assert a.last_stats["no_company_or_country"] == 2 and a.last_stats["rows"] == 5


async def test_signal_source_url_falls_back_to_event_url_else_no_signal(csv_file):
    got = await collect(ExhibitorListImport({"path": str(csv_file), "country_code": "GB"}))
    by = {r.legal_name: r for r, _ in got}
    assert by["Example Fixings Ltd"].raw["signals"][0]["source_url"] == "https://events.example.org/example-expo-2027"
    assert "signals" not in by["Sample Software Inc"].raw and by["Sample Software Inc"].source_url is None  # nothing to cite -> no signal


async def test_column_aliases_overrides_and_defaults(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("Exhibitor Name,Booth,Show\nAlias Widgets Ltd,C3,Alias Show\n")
    got = await collect(imp(p, country_code="GB", event_date="2027-05-01"))
    r = got[0][0]
    assert r.legal_name == "Alias Widgets Ltd" and r.raw["participation"]["stand"] == "C3" and r.raw["signals"][0]["occurred_at"].startswith("2027-05-01")
    p2 = tmp_path / "y.csv"
    p2.write_text("Firma,Land\nBeispiel AG,DE\n")
    got = await collect(imp(p2, columns={"company": "Firma", "country": "Land"}, event_name="Messe"))
    assert got[0][0].country_code == "DE" and "Messe" in got[0][0].raw["signals"][0]["title"]
    p3 = tmp_path / "z.csv"
    p3.write_text("foo,bar\n1,2\n")
    with pytest.raises(NotConfigured):
        await collect(imp(p3, country_code="GB"))


async def test_json_and_xlsx(tmp_path):
    p = tmp_path / "e.json"
    p.write_text(json.dumps([{"company": "Json Stand Ltd", "stand": "D4", "event": "Json Expo", "date": "2027-06-01", "role": "sponsor"}]))
    got = await collect(imp(p, country_code="GB"))
    assert got[0][0].raw["signals"][0]["signal_type"] == "sponsor"
    import openpyxl

    wb = openpyxl.Workbook()
    wb.active.append(["Company", "Stand", "Event"])
    wb.active.append(["Xlsx Stand Ltd", 12, "Xlsx Expo"])
    x = tmp_path / "e.xlsx"
    wb.save(x)
    got = await collect(imp(x, country_code="GB"))
    assert got[0][0].legal_name == "Xlsx Stand Ltd" and got[0][0].raw["participation"]["stand"] == "12"


async def test_resume(csv_file):
    await assert_resume_no_dup_no_gap(lambda: imp(csv_file))


async def test_feed_url_requires_permission_basis_and_robots(tmp_path, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
    body = "Company,Stand,Event,Role\nFeed Exhibitor Ltd,E5,Feed Expo,Exhibitor\n"

    def handler(req):
        return httpx.Response(200, text=body, headers={"content-type": "text/csv", "content-length": str(len(body))})

    f, seen = robots_fetcher("User-agent: *\nAllow: /")
    base = {"url": "https://feeds.example.org/expo/exhibitors.csv", "country_code": "GB", "fetcher": f,
            "http_client": httpx.AsyncClient(transport=httpx.MockTransport(handler))}
    with pytest.raises(NotPermitted):  # default deny
        await collect(ExhibitorListImport(base))
    assert seen == []
    with pytest.raises(NotPermitted):
        await collect(ExhibitorListImport({**base, "permission_basis": "yes"}))
    got = await collect(ExhibitorListImport({**base, "permission_basis": "open_licence: organiser publishes the list under CC BY (page footer)"}))
    assert got[0][0].raw["signals"][0]["source_url"] == "https://feeds.example.org/expo/exhibitors.csv" and seen[0].endswith("/robots.txt")
    f2, _ = robots_fetcher("User-agent: *\nDisallow: /")
    with pytest.raises(NotPermitted, match="robots"):
        await collect(ExhibitorListImport({**base, "fetcher": f2, "permission_basis": "own_feed: operator's own export"}))


async def test_filters_apply(csv_file):
    got = await collect(imp(csv_file), DiscoveryQuery(active_only=False, countries=["US"]))
    assert [r.legal_name for r, _ in got] == ["Sample Software Inc"]
