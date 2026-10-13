"""Generators for SYNTHETIC fixture files in the real formats.  Every business here is fictional test data."""
from __future__ import annotations

import bz2
import csv
import io
import json
import zipfile
from pathlib import Path

# Real BasicCompanyData header layout (note the leading space on ' CompanyNumber', as in the published file).
CH_HEADER = [
    "CompanyName", " CompanyNumber", "RegAddress.CareOf", "RegAddress.POBox", "RegAddress.AddressLine1",
    " RegAddress.AddressLine2", "RegAddress.PostTown", "RegAddress.County", "RegAddress.Country", "RegAddress.PostCode",
    "CompanyCategory", "CompanyStatus", "CountryOfOrigin", "DissolutionDate", "IncorporationDate",
    "Accounts.AccountRefDay", "Accounts.AccountRefMonth", "Accounts.NextDueDate", "Accounts.LastMadeUpDate",
    "Accounts.AccountCategory", "Returns.NextDueDate", "Returns.LastMadeUpDate", "Mortgages.NumMortCharges",
    "Mortgages.NumMortOutstanding", "Mortgages.NumMortPartSatisfied", "Mortgages.NumMortSatisfied",
    "SICCode.SicText_1", "SICCode.SicText_2", "SICCode.SicText_3", "SICCode.SicText_4",
    "LimitedPartnerships.NumGenPartners", "LimitedPartnerships.NumLimPartners", "URI",
    "PreviousName_1.CONDATE", "PreviousName_1.CompanyName", "ConfStmtNextDueDate", "ConfStmtLastMadeUpDate",
]


def ch_row(name, number, *, town="", county="", postcode="", status="Active", sics=(), category="Private Limited Company",
           line1="1 Fixture Street", extra_sics=None):
    r = {h: "" for h in CH_HEADER}
    r.update({
        "CompanyName": name, " CompanyNumber": number, "RegAddress.AddressLine1": line1, "RegAddress.PostTown": town,
        "RegAddress.County": county, "RegAddress.Country": "United Kingdom", "RegAddress.PostCode": postcode,
        "CompanyCategory": category, "CompanyStatus": status, "CountryOfOrigin": "United Kingdom",
        "IncorporationDate": "01/01/2010", "URI": f"http://business.data.gov.uk/id/company/{number}",
    })
    for i, s in enumerate(sics, 1):
        r[f"SICCode.SicText_{i}"] = s
    return [r[h] for h in CH_HEADER]


def small_ch_rows():
    """Eight tiny fictional rows covering filters.  Names/numbers are synthetic (FIXTURE prefix, TEST numbers)."""
    return [
        ch_row("FIXTURE ESTATE AGENTS LTD", "TEST0001", town="MANCHESTER", county="GREATER MANCHESTER", postcode="M1 1AA",
               sics=["68310 - Real estate agencies"]),
        ch_row("FIXTURE LETTINGS & SONS LIMITED", "TEST0002", town="LEEDS", county="WEST YORKSHIRE", postcode="LS1 4AP",
               sics=["68310 - Real estate agencies", "68320 - Management of real estate on a fee or contract basis"]),
        ch_row("FIXTURE ELECTRICAL SERVICES LTD", "TEST0003", town="LONDON", postcode="SW1A 1AA",
               sics=["43210 - Electrical installation"]),
        ch_row("FIXTURE PLUMBING AND HEATING LTD", "TEST0004", town="BRISTOL", postcode="BS1 5AH",
               sics=["43220 - Plumbing, heat and air-conditioning installation"]),
        ch_row("FIXTURE DORMANT HOLDINGS LTD", "TEST0005", town="MANCHESTER", postcode="M2 3AB", sics=["99999 - Dormant Company"]),
        ch_row("FIXTURE OLD ESTATE AGENTS LTD", "TEST0006", town="MANCHESTER", postcode="M3 4CD", status="Dissolved",
               sics=["68310 - Real estate agencies"]),
        ch_row("FIXTURE PROPERTY CLEANERS LTD", "TEST0007", town="EDINBURGH", postcode="EH1 1YZ",
               sics=["None Supplied"]),
        ch_row("SC FIXTURE PUB COMPANY LTD", "SC000008", town="GLASGOW", postcode="G1 1AA",
               sics=["56302 - Public houses and bars"], status="Active - Proposal to Strike off"),
    ]


def write_ch_zip(path: Path, rows, *, member="BasicCompanyDataAsOneFile-2026-10-01.csv") -> Path:
    buf = io.StringIO(newline="")
    w = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    w.writerow(CH_HEADER)
    for r in rows:
        w.writerow(r)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(member, buf.getvalue())
    return path


def write_ch_zip_streaming(path: Path, n_rows: int, *, member="BasicCompanyDataAsOneFile-2026-10-01.csv") -> Path:
    """Write ``n_rows`` synthetic rows without holding them in memory."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf, zf.open(member, "w") as raw:
        txt = io.TextIOWrapper(raw, encoding="utf-8", newline="")
        w = csv.writer(txt, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
        w.writerow(CH_HEADER)
        for i in range(n_rows):
            sic = "68310 - Real estate agencies" if i % 1000 == 0 else "47190 - Other retail sale in non-specialised stores"
            w.writerow(ch_row(f"FIXTURE COMPANY {i} LTD", f"T{i:07d}", town="TESTTOWN", postcode="M1 1AA", sics=[sic]))
        txt.flush()
        txt.detach()
    return path


# ---------------------------------------------------------------------------- OSM
OSM_NODES = [
    # id, lat, lon, tags
    (1001, 53.4808, -2.2426, {"name": "Fixture Estate Agents", "office": "estate_agent", "website": "fixture-agents.example.com",
                              "email": "info@fixture-agents.example.com", "phone": "+44 161 000 0000", "addr:housenumber": "1",
                              "addr:street": "Test Street", "addr:city": "Manchester", "addr:postcode": "M1 1AA",
                              "addr:country": "GB"}),
    (1002, 53.4800, -2.2400, {"name": "Fixture Plumbers", "craft": "plumber", "contact:website": "https://plumbers.example.com/",
                              "addr:city": "Manchester", "addr:postcode": "M2 2BB"}),
    (1003, 51.5074, -0.1278, {"name": "The Fixture Arms", "amenity": "pub", "addr:city": "London", "addr:postcode": "SW1A 2AA",
                              "contact:email": "bar@fixture-arms.example.com"}),
    (1004, 51.5080, -0.1280, {"name": "Fixture Hotel", "tourism": "hotel", "addr:city": "London", "addr:postcode": "SW1A 2AB"}),
    (1005, 53.4810, -2.2430, {"name": "Fixture Bench", "amenity": "bench"}),
    (1006, 53.4820, -2.2440, {"shop": "estate_agent"}),  # unnamed -> ignored
    (1007, 54.5973, -5.9301, {"name": "Fixture Belfast Agents", "office": "estate_agent", "addr:city": "Belfast", "addr:postcode": "BT1 5GS"}),
]
OSM_WAYS = [
    (2001, [1001, 1002, 1003], {"name": "Fixture Hall", "amenity": "community_centre", "addr:city": "Manchester", "addr:postcode": "M3 3CC",
                                "website": "hall.example.com"}),
]


def write_osm_xml(path: Path, *, bz: bool = False) -> Path:
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', '<osm version="0.6" generator="fixture">']
    for nid, lat, lon, tags in OSM_NODES:
        parts.append(f'<node id="{nid}" lat="{lat}" lon="{lon}" version="1">')
        parts += [f'<tag k="{k}" v="{v}"/>' for k, v in tags.items()]
        parts.append("</node>")
    for wid, refs, tags in OSM_WAYS:
        parts.append(f'<way id="{wid}" version="1">')
        parts += [f'<nd ref="{r}"/>' for r in refs]
        parts += [f'<tag k="{k}" v="{v}"/>' for k, v in tags.items()]
        parts.append("</way>")
    parts.append("</osm>")
    data = "\n".join(parts).encode()
    if bz:
        path.write_bytes(bz2.compress(data))
    else:
        path.write_bytes(data)
    return path


def write_overpass_json(path: Path) -> Path:
    els = []
    for nid, lat, lon, tags in OSM_NODES:
        els.append({"type": "node", "id": nid, "lat": lat, "lon": lon, "tags": tags})
    for wid, _refs, tags in OSM_WAYS:
        els.append({"type": "way", "id": wid, "center": {"lat": 53.48, "lon": -2.24}, "tags": tags})
    path.write_text(json.dumps({"version": 0.6, "generator": "fixture", "elements": els}, indent=1))
    return path


def write_geojson(path: Path, *, lines: bool = False) -> Path:
    feats = []
    for nid, lat, lon, tags in OSM_NODES:
        feats.append({"type": "Feature", "id": f"node/{nid}", "properties": dict(tags),
                      "geometry": {"type": "Point", "coordinates": [lon, lat]}})
    for wid, _r, tags in OSM_WAYS:
        feats.append({"type": "Feature", "id": f"way/{wid}", "properties": dict(tags),
                      "geometry": {"type": "Polygon", "coordinates": [[[-2.25, 53.47], [-2.23, 53.47], [-2.23, 53.49], [-2.25, 53.49], [-2.25, 53.47]]]}})
    if lines:
        path.write_text("\n".join(json.dumps(f) for f in feats))
    else:
        path.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    return path


def write_pbf(path: Path) -> Path:
    import osmium

    if path.exists():
        path.unlink()
    w = osmium.SimpleWriter(str(path))
    for nid, lat, lon, tags in OSM_NODES:
        w.add_node(osmium.osm.mutable.Node(id=nid, location=(lon, lat), tags=tags))
    for wid, refs, tags in OSM_WAYS:
        w.add_way(osmium.osm.mutable.Way(id=wid, nodes=refs, tags=tags))
    w.close()
    return path
