"""Live network probes -- skipped unless LEAD_RUN_LIVE=1.  Small HEAD/GET requests only."""
import httpx
import pytest

pytestmark = pytest.mark.live
UA = {"User-Agent": "LeadIntelBot/0.1 (+contact: set LEAD_CONTACT_EMAIL)"}


def test_companies_house_product_page_and_zip_head():
    page = httpx.get("https://download.companieshouse.gov.uk/en_output.html", headers=UA, timeout=30).text
    import re

    name = re.search(r"BasicCompanyDataAsOneFile-\d{4}-\d{2}-\d{2}\.zip", page).group(0)
    h = httpx.head("https://download.companieshouse.gov.uk/" + name, headers=UA, timeout=30)
    assert h.status_code == 200 and "zip" in h.headers["content-type"]


def test_geofabrik_extract_head():
    h = httpx.head("https://download.geofabrik.de/europe/great-britain-latest.osm.pbf", headers=UA, timeout=30, follow_redirects=True)
    assert h.status_code == 200


def test_geofabrik_index_matches_table():
    import json

    from app.sources.geofabrik_regions import COUNTRY_EXTRACTS

    idx = json.loads(httpx.get("https://download.geofabrik.de/index-v1-nogeom.json", headers=UA, timeout=60).text)
    ids = {f["properties"]["id"] for f in idx["features"]}
    missing = {p for paths in COUNTRY_EXTRACTS.values() for p in paths if p.rsplit("/", 1)[-1] not in ids}
    assert not missing, sorted(missing)


def test_commoncrawl_collinfo():
    r = httpx.get("https://index.commoncrawl.org/collinfo.json", headers=UA, timeout=30)
    assert r.status_code == 200 and r.json()[0]["id"].startswith("CC-MAIN")


def test_wikidata_ask():
    r = httpx.get("https://query.wikidata.org/sparql", params={"query": "ASK{}", "format": "json"}, headers=UA, timeout=30)
    assert r.status_code == 200
