"""Streaming sanity: bounded memory over large synthetic inputs (all rows are fictional)."""
import json
import time
import tracemalloc

import psutil

from app.sources.base import DiscoveryQuery
from app.sources.companies_house_bulk import CompaniesHouseBulkAdapter
from app.sources.csv_import import CsvImportAdapter
from app.sources.osm_geofabrik import OsmGeofabrikAdapter
from tests.sources.fixtures import write_ch_zip_streaming

N = 200_000
MB = 1024 * 1024


async def drain(adapter, query, checkpoint=None):
    n = 0
    last = None
    async for _rec, cp in adapter.iter_businesses(query, checkpoint):
        n += 1
        last = cp
    return n, last


async def test_companies_house_200k_rows_bounded_memory(tmp_path):
    p = write_ch_zip_streaming(tmp_path / "BasicCompanyDataAsOneFile-2026-10-01.zip", N)
    a = CompaniesHouseBulkAdapter({"path": str(p)})
    tracemalloc.start()
    n, _ = await drain(a, DiscoveryQuery(sic_codes=["68310"]))
    peak_filtered = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert n == N // 1000
    assert peak_filtered < 8 * MB, peak_filtered / MB
    # emit every row (consumer discards them); tracemalloc is too slow here, so watch process RSS instead
    proc = psutil.Process()
    rss0 = proc.memory_info().rss
    t0 = time.perf_counter()
    n_all, last_all = await drain(a, DiscoveryQuery())
    grown = proc.memory_info().rss - rss0
    assert n_all == N and last_all["row"] == N - 1
    assert grown < 60 * MB, grown / MB
    assert time.perf_counter() - t0 < 120
    # resume near the end must not re-emit earlier rows
    n_tail, _ = await drain(a, DiscoveryQuery(), {"row": N - 11, "file": p.name})
    assert n_tail == 10


async def test_osm_json_stream_bounded_memory(tmp_path):
    p = tmp_path / "big.json"
    with open(p, "w") as fh:
        fh.write('{"version":0.6,"elements":[')
        for i in range(30_000):
            el = {"type": "node", "id": i + 1, "lat": 51.0 + (i % 1000) / 10000, "lon": -1.0,
                  "tags": {"name": f"Fixture Shop {i}", "shop": "estate_agent" if i % 500 == 0 else "bakery", "addr:postcode": "M1 1AA"}}
            fh.write(("," if i else "") + json.dumps(el))
        fh.write("]}")
    a = OsmGeofabrikAdapter({"path": str(p), "country_code": "GB"})
    tracemalloc.start()
    n, _ = await drain(a, DiscoveryQuery(osm_tags=["shop=estate_agent"]))
    peak = tracemalloc.get_traced_memory()[1]
    n_all, _ = await drain(a, DiscoveryQuery(osm_tags=["shop=*"]))
    peak_all = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert n == 60 and n_all == 30_000
    assert p.stat().st_size > 3 * MB
    assert peak < 6 * MB and peak_all < 6 * MB, (peak / MB, peak_all / MB)


async def test_osm_xml_stream_bounded_memory(tmp_path):
    p = tmp_path / "big.osm"
    with open(p, "w") as fh:
        fh.write('<?xml version="1.0"?><osm version="0.6">')
        for i in range(40_000):
            fh.write(f'<node id="{i + 1}" lat="51.5" lon="-0.1"><tag k="name" v="Fixture {i}"/>'
                     f'<tag k="{"office" if i % 400 == 0 else "highway"}" v="{"estate_agent" if i % 400 == 0 else "bus_stop"}"/></node>')
        fh.write("</osm>")
    a = OsmGeofabrikAdapter({"path": str(p), "country_code": "GB"})
    tracemalloc.start()
    n, _ = await drain(a, DiscoveryQuery(osm_tags=["office=estate_agent"]))
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert n == 100 and peak < 8 * MB, peak / MB


async def test_csv_import_100k_rows_bounded_memory(tmp_path):
    p = tmp_path / "domains.csv"
    with open(p, "w") as fh:
        fh.write("domain,company name\n")
        for i in range(100_000):
            fh.write(f"fixture-{i}.example.com,Fixture {i}\n")
    a = CsvImportAdapter({"path": str(p), "country_code": "GB", "max_rows": 150_000})
    tracemalloc.start()
    n, _ = await drain(a, DiscoveryQuery())
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert n == 100_000
    assert peak < 40 * MB, peak / MB  # dedupe set of keys is the only O(n) state
