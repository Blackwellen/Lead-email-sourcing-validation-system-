"""Live probes (LEAD_RUN_LIVE=1).  They only exercise small, polite requests against official endpoints."""
import pytest

pytestmark = pytest.mark.live


async def test_live_gleif_publish_index():
    from app.sources.gleif import resolve_latest_url

    url = await resolve_latest_url(fmt="csv", delta="IntraDay")
    assert url.startswith("https://goldencopy.gleif.org/") and url.endswith(".csv.zip")


async def test_live_ted_search_one_page():
    import httpx

    from app.sources.base import DiscoveryQuery
    from app.sources.eu_ted import TedAdapter

    a = TedAdapter({"days_back": 14, "page_size": 5, "http_client": httpx.AsyncClient(timeout=60)})
    n = 0
    async for rec, _cp in a.iter_businesses(DiscoveryQuery(active_only=False, limit=3)):
        assert rec.raw["signals"][0]["source_url"].startswith("https://ted.europa.eu/")
        n += 1
    assert n >= 1


async def test_live_usaspending_one_page():
    import httpx

    from app.sources.base import DiscoveryQuery
    from app.sources.usaspending import UsaSpendingAdapter

    a = UsaSpendingAdapter({"days_back": 30, "page_size": 5, "max_pages": 1, "http_client": httpx.AsyncClient(timeout=60)})
    got = [r async for r, _ in a.iter_businesses(DiscoveryQuery(active_only=False, limit=3))]
    assert got and got[0].raw["signals"][0]["source_url"].startswith("https://www.usaspending.gov/award/")


async def test_live_find_a_tender_one_page():
    import httpx

    from app.sources.base import DiscoveryQuery
    from app.sources.ocds import FindATenderOCDS

    a = FindATenderOCDS({"days_back": 3, "page_size": 3, "http_client": httpx.AsyncClient(timeout=60)})
    async for rec, _cp in a.iter_businesses(DiscoveryQuery(active_only=False, limit=1)):
        assert rec.raw["signals"][0]["source_url"].startswith("https://www.find-tender.service.gov.uk/")
        break


async def test_live_fhrs_file_and_robots():
    import httpx

    from app.sources.base import DiscoveryQuery
    from app.sources.industry_directories import FsaFhrsAdapter

    a = FsaFhrsAdapter({"authority_codes": ["501"], "http_client": httpx.AsyncClient(timeout=60, follow_redirects=True)})
    got = [r async for r, _ in a.iter_businesses(DiscoveryQuery(active_only=False, limit=5))]
    assert got and got[0].source_url.startswith("https://ratings.food.gov.uk/business/")


async def test_live_commoncrawl_collinfo():
    from app.sources.commoncrawl import CDXClient

    async with CDXClient() as c:
        crawls = await c.list_crawls()
    assert crawls and crawls[0]["id"].startswith("CC-MAIN-")


async def test_live_charity_zip_headers_only():
    import httpx

    r = httpx.head("https://ccewuksprdoneregsadata1.blob.core.windows.net/data/json/publicextract.charity.zip", follow_redirects=True, timeout=60)
    assert r.status_code == 200
