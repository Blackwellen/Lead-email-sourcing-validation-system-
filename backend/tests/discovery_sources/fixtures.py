"""SYNTHETIC fixtures in the real file formats.  Every organisation is fictional (Example ... Ltd, numbers 9999xxxx, example.com)."""
from __future__ import annotations

import csv
import gzip
import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

# ----------------------------------------------------------------------------------------------- Charity Commission
CHARITY_COLS = [
    "date_of_extract", "organisation_number", "registered_charity_number", "linked_charity_number", "charity_name", "charity_type",
    "charity_registration_status", "date_of_registration", "date_of_removal", "charity_reporting_status",
    "latest_acc_fin_period_start_date", "latest_acc_fin_period_end_date", "latest_income", "latest_expenditure",
    "charity_contact_address1", "charity_contact_address2", "charity_contact_address3", "charity_contact_address4",
    "charity_contact_address5", "charity_contact_postcode", "charity_contact_phone", "charity_contact_email", "charity_contact_web",
    "charity_company_registration_number", "charity_insolvent", "charity_in_administration", "charity_previously_excepted",
    "charity_is_cdf_or_cif", "charity_is_cio", "cio_is_dissolved", "date_cio_dissolution_notice", "charity_activities",
    "charity_gift_aid", "charity_has_land",
]


def charity_rows() -> list[dict]:
    def row(i, regno, linked, name, status="Registered", **kw):
        d = {c: None for c in CHARITY_COLS}
        d.update(date_of_extract="2026-10-13T00:00:00", organisation_number=i, registered_charity_number=regno, linked_charity_number=linked,
                 charity_name=name, charity_registration_status=status, date_of_registration="2001-02-03T00:00:00",
                 charity_insolvent=False, charity_in_administration=False)
        d.update(kw)
        return d

    return [
        row(1, 9999001, 0, "EXAMPLE COMMUNITY HALL TRUST", charity_contact_address1="1 Example Road", charity_contact_address2="Exampleton",
            charity_contact_address3="Exampleshire", charity_contact_postcode="M1 1AA", charity_contact_phone="0161 000 0001",
            charity_contact_email="office@example.com", charity_contact_web="www.example.com", charity_company_registration_number="99999991",
            latest_income=12345, charity_activities="Provides a village hall for hire."),
        row(2, 9999001, 1, "EXAMPLE COMMUNITY HALL TRUST SUB FUND"),
        row(3, 9999002, 0, "SAMPLE ALMSHOUSES CHARITY", status="Removed", date_of_removal="2015-01-01T00:00:00"),
        row(4, 9999003, 0, "PLACEHOLDER YOUTH CLUB", charity_contact_address1="2 Sample Street", charity_contact_postcode="LS1 4AP"),
        row(5, 9999004, 0, "TEST WILDLIFE SOCIETY", charity_contact_postcode="SW1A 1AA", charity_company_registration_number="123"),
    ]


def write_charity_json_zip(path: Path, rows=None) -> Path:
    rows = rows or charity_rows()
    body = "[" + "\n,".join(json.dumps(r) for r in rows) + "]"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("publicextract.charity.json", "﻿" + body)
    return path


def write_charity_txt_zip(path: Path, rows=None) -> Path:
    rows = rows or charity_rows()
    buf = io.StringIO()
    buf.write("\t".join(CHARITY_COLS) + "\n")
    for r in rows:
        buf.write("\t".join("" if r[c] is None else str(r[c]) for c in CHARITY_COLS) + "\n")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("publicextract.charity.txt", buf.getvalue())
    return path


# ----------------------------------------------------------------------------------------------- OCDS (Contracts Finder / FTS shape)
def _party(pid, name, roles, *, scheme=None, ident=None, street=None, locality=None, postcode=None, country="United Kingdom",
           email=None, url=None, region=None):
    p = {"id": pid, "name": name, "identifier": {"legalName": name}, "roles": roles}
    if scheme:
        p["identifier"].update(scheme=scheme, id=ident)
    addr = {k: v for k, v in dict(streetAddress=street, locality=locality, postalCode=postcode, countryName=country, region=region).items() if v}
    if addr:
        p["address"] = addr
    if email:
        p["contactPoint"] = {"email": email}
    if url:
        p["details"] = {"url": url}
    return p


def _release(rid, date, buyer, parties, awards, *, title="Example works", cpv="45000000", tag=("award",)):
    return {
        "ocid": f"ocds-b5fd17-{rid}", "id": rid, "language": "en", "date": date, "tag": list(tag), "initiationType": "tender",
        "tender": {"id": "T" + rid, "title": title, "classification": {"scheme": "CPV", "id": cpv, "description": "x"}},
        "parties": parties, "buyer": {"id": buyer[0], "name": buyer[1]}, "awards": awards,
    }


def ocds_releases() -> list[dict]:
    buyer = _party("GB-CFS-1", "Example District Council", ["buyer"], street="1 Council Road", postcode="M1 1AA")
    s1 = _party("GB-CFS-11", "Example Supplies Ltd", ["supplier"], scheme="GB-COH", ident="99999991", street="1 Supply Way, Exampleton, M2 2BB",
                email="sales@example-widgets.co.uk", url="https://www.example.com")
    s2 = _party("GB-CFS-12", "Sample Builders Ltd", ["supplier"], scheme="GB-COH", ident="99999992", postcode="LS1 4AP", locality="Leeds")
    s3 = _party("GB-CFS-13", "Placeholder Cleaning Services", ["supplier"], street="5 Test Lane, Bristol, BS1 5AH")
    s4 = _party("GB-CFS-14", "Never Awarded Ltd", ["supplier"])
    s5 = _party("GB-CFS-15", "Overseas Example GmbH", ["supplier"], country="Germany", locality="Berlin", postcode="10115")

    def award(aid, supplier, date, amount, url=None, status="active", extra=None):
        a = {"id": aid, "status": status, "date": date, "value": {"amount": amount, "currency": "GBP"},
             "suppliers": [{"id": supplier["id"], "name": supplier["name"]}]}
        if url:
            a["documents"] = [{"id": "1", "documentType": "awardNotice", "url": url}]
        if extra:
            a.update(extra)
        return a

    b = ("GB-CFS-1", "Example District Council")
    return [
        _release("r1", "2024-03-01T10:00:00+00:00", b, [buyer, s1], [award("a1", s1, "2024-03-01T00:00:00+00:00", 1000, "https://www.contractsfinder.service.gov.uk/Notice/aaaa1111")]),
        _release("r2", "2025-04-01T10:00:00+01:00", b, [buyer, s1, s2],
                 [award("a2", s1, "2025-04-01T00:00:00+01:00", 2000, "https://www.contractsfinder.service.gov.uk/Notice/aaaa2222"),
                  award("a3", s2, "2025-04-01T00:00:00+01:00", 3000, "https://www.contractsfinder.service.gov.uk/Notice/aaaa3333")], cpv="79950000"),
        _release("r3", "2025-09-01T10:00:00+01:00", b, [buyer, s1, s3],
                 [award("a4", s1, "2025-09-01T00:00:00+01:00", 4000, "https://www.contractsfinder.service.gov.uk/Notice/aaaa4444"),
                  award("a5", s3, "2025-09-01T00:00:00+01:00", 500, "https://www.contractsfinder.service.gov.uk/Notice/aaaa5555")], cpv="90910000"),
        _release("r4", "2025-09-02T10:00:00+01:00", b, [buyer, s4], [award("a6", s4, "2025-09-02T00:00:00+01:00", 1, "https://www.contractsfinder.service.gov.uk/Notice/aaaa6666", status="cancelled")]),
        _release("r5", "2026-01-05T10:00:00+00:00", b, [buyer, s5], [award("a7", s5, "2026-01-05T00:00:00+00:00", 7000, "https://www.contractsfinder.service.gov.uk/Notice/aaaa7777")]),
        # award without any document URL -> no signal possible (and no package uri) -> record has no source URL either
        _release("r6", "2026-02-01T10:00:00+00:00", b, [buyer, s2], [award("a8", s2, "2026-02-01T00:00:00+00:00", 8000)]),
    ]


def ocds_package(releases=None, *, uri="https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search?x=1", nxt=None) -> dict:
    pkg = {"uri": uri, "version": "1.1", "publishedDate": "2026-10-01T00:00:00Z", "publisher": {"name": "Example"},
           "license": "http://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/", "releases": releases or ocds_releases(),
           "links": {}}
    if nxt:
        pkg["links"]["next"] = nxt
    return pkg


def write_ocds_package(path: Path, releases=None) -> Path:
    path.write_text(json.dumps(ocds_package(releases), indent=2), encoding="utf-8")
    return path


def fts_release() -> dict:
    """Find a Tender shape: supplier id 'GB-COH-<n>' with identifier, release id '<6 digits>-<year>', no award documents."""
    buyer = _party("GB-FTS-1", "Example Housing Authority", ["buyer"], street="1 Authority Road", postcode="M1 1AA")
    sup = _party("GB-COH-99999993", "Example Banking Group", ["supplier"], scheme="GB-COH", ident="99999993", street="250 Example Street",
                 locality="London", postcode="EC2M 4AA", region="UKI4")
    return {
        "ocid": "ocds-h6vhtk-0aaaa1", "id": "012345-2026", "date": "2026-09-02T00:00:00Z", "tag": ["award", "contract"],
        "tender": {"id": "T.1", "title": "Example banking services", "classification": {"scheme": "CPV", "id": "66110000"}},
        "parties": [buyer, sup], "buyer": {"id": "GB-FTS-1", "name": "Example Housing Authority"},
        "awards": [{"id": "012345-2026-1", "status": "active", "suppliers": [{"id": "GB-COH-99999993", "name": "Example Banking Group"}]}],
    }


# ----------------------------------------------------------------------------------------------- GLEIF
LEI_COLS = (
    ["LEI", "Entity.LegalName", "Entity.LegalName.xmllang"]
    + [f"Entity.OtherEntityNames.OtherEntityName.{i}{s}" for i in range(1, 4) for s in ("", ".xmllang", ".type")]
    + ["Entity.LegalAddress.xmllang", "Entity.LegalAddress.FirstAddressLine", "Entity.LegalAddress.AddressNumber",
       "Entity.LegalAddress.AdditionalAddressLine.1", "Entity.LegalAddress.AdditionalAddressLine.2", "Entity.LegalAddress.AdditionalAddressLine.3",
       "Entity.LegalAddress.City", "Entity.LegalAddress.Region", "Entity.LegalAddress.Country", "Entity.LegalAddress.PostalCode",
       "Entity.HeadquartersAddress.FirstAddressLine", "Entity.HeadquartersAddress.City", "Entity.HeadquartersAddress.Country",
       "Entity.HeadquartersAddress.PostalCode",
       "Entity.RegistrationAuthority.RegistrationAuthorityID", "Entity.RegistrationAuthority.RegistrationAuthorityEntityID",
       "Entity.LegalJurisdiction", "Entity.EntityCategory", "Entity.LegalForm.EntityLegalFormCode", "Entity.EntityStatus",
       "Entity.EntityCreationDate", "Registration.InitialRegistrationDate", "Registration.LastUpdateDate", "Registration.RegistrationStatus"]
)

LEI_ROWS = [
    {"LEI": "EXAMPLE00000000000001", "Entity.LegalName": "EXAMPLE HOLDINGS PLC", "Entity.LegalAddress.FirstAddressLine": "1 EXAMPLE STREET",
     "Entity.LegalAddress.City": "LONDON", "Entity.LegalAddress.Region": "GB-LND", "Entity.LegalAddress.Country": "GB",
     "Entity.LegalAddress.PostalCode": "EC2M 4AA", "Entity.RegistrationAuthority.RegistrationAuthorityID": "RA000585",
     "Entity.RegistrationAuthority.RegistrationAuthorityEntityID": "99999994", "Entity.LegalJurisdiction": "GB", "Entity.EntityCategory": "GENERAL",
     "Entity.LegalForm.EntityLegalFormCode": "H0PO", "Entity.EntityStatus": "ACTIVE", "Registration.RegistrationStatus": "ISSUED",
     "Entity.OtherEntityNames.OtherEntityName.1": "EXAMPLE TRADING", "Entity.OtherEntityNames.OtherEntityName.1.type": "TRADING_OR_OPERATING_NAME"},
    {"LEI": "EXAMPLE00000000000002", "Entity.LegalName": "EXAMPLE FINANCE SUBSIDIARY LIMITED", "Entity.LegalAddress.FirstAddressLine": "2 EXAMPLE STREET",
     "Entity.LegalAddress.City": "LEEDS", "Entity.LegalAddress.Country": "GB", "Entity.LegalAddress.PostalCode": "LS1 4AP",
     "Entity.RegistrationAuthority.RegistrationAuthorityID": "RA000585", "Entity.RegistrationAuthority.RegistrationAuthorityEntityID": "99999995",
     "Entity.LegalJurisdiction": "GB", "Entity.EntityStatus": "ACTIVE", "Registration.RegistrationStatus": "ISSUED"},
    {"LEI": "EXAMPLE00000000000003", "Entity.LegalName": "BEISPIEL BETEILIGUNGS GMBH", "Entity.LegalAddress.FirstAddressLine": "BEISPIELWEG 1",
     "Entity.LegalAddress.City": "BERLIN", "Entity.LegalAddress.Country": "DE", "Entity.LegalAddress.PostalCode": "10115",
     "Entity.RegistrationAuthority.RegistrationAuthorityID": "RA000100", "Entity.RegistrationAuthority.RegistrationAuthorityEntityID": "HRB 000000",
     "Entity.LegalJurisdiction": "DE", "Entity.EntityStatus": "ACTIVE", "Registration.RegistrationStatus": "ISSUED"},
    {"LEI": "EXAMPLE00000000000004", "Entity.LegalName": "EXAMPLE DEFUNCT LIMITED", "Entity.LegalAddress.Country": "GB", "Entity.LegalAddress.City": "BRISTOL",
     "Entity.LegalAddress.PostalCode": "BS1 5AH", "Entity.LegalJurisdiction": "GB", "Entity.EntityStatus": "INACTIVE", "Registration.RegistrationStatus": "LAPSED"},
]


def write_lei_csv_zip(path: Path, rows=None) -> Path:
    rows = rows or LEI_ROWS
    buf = io.StringIO(newline="")
    w = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\n")
    w.writerow(LEI_COLS)
    for r in rows:
        w.writerow([r.get(c, "") for c in LEI_COLS])
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("20261013-0800-gleif-goldencopy-lei2-golden-copy.csv", buf.getvalue())
    return path


def _leaf(v):
    return {"$": v}


def lei_json_record(r: dict) -> dict:
    def addr(prefix):
        d = {"@xml:lang": "en"}
        for f in ("FirstAddressLine", "City", "Region", "Country", "PostalCode"):
            v = r.get(f"Entity.{prefix}.{f}")
            if v:
                d[f] = _leaf(v)
        return d

    ent = {
        "LegalName": {"@xml:lang": "en", "$": r["Entity.LegalName"]}, "LegalAddress": addr("LegalAddress"),
        "RegistrationAuthority": {}, "LegalJurisdiction": _leaf(r.get("Entity.LegalJurisdiction", "")), "EntityStatus": _leaf(r.get("Entity.EntityStatus", "")),
    }
    if r.get("Entity.RegistrationAuthority.RegistrationAuthorityID"):
        ent["RegistrationAuthority"] = {"RegistrationAuthorityID": _leaf(r["Entity.RegistrationAuthority.RegistrationAuthorityID"]),
                                        "RegistrationAuthorityEntityID": _leaf(r["Entity.RegistrationAuthority.RegistrationAuthorityEntityID"])}
    if r.get("Entity.OtherEntityNames.OtherEntityName.1"):
        ent["OtherEntityNames"] = {"OtherEntityName": [{"@xml:lang": "en", "@type": r["Entity.OtherEntityNames.OtherEntityName.1.type"],
                                                         "$": r["Entity.OtherEntityNames.OtherEntityName.1"]}]}
    return {"LEI": _leaf(r["LEI"]), "Entity": ent, "Registration": {"RegistrationStatus": _leaf(r.get("Registration.RegistrationStatus", ""))}}


def write_lei_json_zip(path: Path, rows=None) -> Path:
    rows = rows or LEI_ROWS
    body = '{"records":[\n' + ",\n".join(json.dumps(lei_json_record(r), indent=2) for r in rows) + "\n]}"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("20261013-0800-gleif-goldencopy-lei2-golden-copy.json", body)
    return path


RR_COLS = ["Relationship.StartNode.NodeID", "Relationship.StartNode.NodeIDType", "Relationship.EndNode.NodeID", "Relationship.EndNode.NodeIDType",
           "Relationship.RelationshipType", "Relationship.RelationshipStatus", "Relationship.Period.1.startDate", "Relationship.Period.1.endDate",
           "Relationship.Period.1.periodType", "Registration.RegistrationStatus"]
RR_ROWS = [
    ["EXAMPLE00000000000002", "LEI", "EXAMPLE00000000000001", "LEI", "IS_DIRECTLY_CONSOLIDATED_BY", "ACTIVE", "2025-01-01T00:00:00Z", "", "RELATIONSHIP_PERIOD", "PUBLISHED"],
    ["EXAMPLE00000000000002", "LEI", "EXAMPLE00000000000009", "LEI", "IS_ULTIMATELY_CONSOLIDATED_BY", "ACTIVE", "", "", "", "PUBLISHED"],
    ["EXAMPLE00000000000003", "LEI", "EXAMPLE00000000000001", "LEI", "IS_DIRECTLY_CONSOLIDATED_BY", "INACTIVE", "", "", "", "PUBLISHED"],
]


def write_rr_csv_zip(path: Path) -> Path:
    buf = io.StringIO(newline="")
    w = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\n")
    w.writerow(RR_COLS)
    w.writerows(RR_ROWS)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("20261013-0800-gleif-goldencopy-rr-golden-copy.csv", buf.getvalue())
    return path


def write_rr_json_zip(path: Path) -> Path:
    recs = []
    for r in RR_ROWS:
        recs.append({"RelationshipRecord": {"Relationship": {
            "StartNode": {"NodeID": _leaf(r[0]), "NodeIDType": _leaf("LEI")}, "EndNode": {"NodeID": _leaf(r[2]), "NodeIDType": _leaf("LEI")},
            "RelationshipType": _leaf(r[4]),
            "RelationshipPeriods": {"RelationshipPeriod": [{"StartDate": _leaf(r[6] or "2020-01-01T00:00:00Z"), "PeriodType": _leaf("RELATIONSHIP_PERIOD")}]},
            "RelationshipStatus": _leaf(r[5])}}})
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("rr.json", '{"relations":[\n' + ",\n".join(json.dumps(x, indent=2) for x in recs) + "\n]}")
    return path


# ----------------------------------------------------------------------------------------------- TED (API v3 search response)
def ted_notice(pn, *, names, ids=None, countries=None, cities=None, emails=None, winner_ids=None, winner_countries=None,
               cpv=("45000000",), date="2026-09-01+02:00", buyer="Beispiel Behoerde", lang="eng", site=None, value="1000.00"):
    n = {
        "publication-number": pn, "publication-date": date, "notice-title": {"eng": f"Example notice {pn}"},
        "buyer-name": {lang: [buyer]}, "classification-cpv": list(cpv), "organisation-name-tenderer": {lang: list(names)},
        "tender-value": [value], "tender-value-cur": ["EUR"], "winner-decision-date": ["2026-08-18+02:00"],
        "links": {"html": {"ENG": f"https://ted.europa.eu/en/notice/-/detail/{pn}"}},
    }
    if ids is not None:
        n["organisation-identifier-tenderer"] = ids
    if countries is not None:
        n["organisation-country-tenderer"] = countries
    if cities is not None:
        n["organisation-city-tenderer"] = cities
    if emails is not None:
        n["organisation-email-tenderer"] = emails
    if winner_ids is not None:
        n["winner-identifier"] = winner_ids
    if winner_countries is not None:
        n["winner-country"] = winner_countries
    if site:
        n["winner-internet-address"] = [site]
    return n


def ted_notices() -> list[dict]:
    return [
        # single tenderer, full attributes, published e-mail + site
        ted_notice("100001-2026", names=["Beispiel Bau GmbH"], ids=["DE000000001"], countries=["DEU"], cities=["Berlin"],
                   emails=["kontakt@example.com"], winner_ids=["DE000000001"], winner_countries=["DEU"], site="http://www.example.com", cpv=("71332000",)),
        # three tenderers, one winner identified by id
        ted_notice("100002-2026", names=["Exemple SARL", "Muster AG", "Ejemplo SA"], ids=["FR000000002", "CH000000003", "ES000000004"],
                   countries=["FRA", "CHE", "ESP"], cities=["Paris", "Zurich", "Madrid"], winner_ids=["CH000000003"], winner_countries=["CHE"], cpv=("79950000",)),
        # two tenderers, no ids, winner-country count == names -> all winners (inferred)
        ted_notice("100003-2026", names=["Voorbeeld BV", "Esempio SRL"], countries=["NLD", "ITA"], winner_countries=["NLD", "ITA"], cpv=("90910000",)),
        # ambiguous: 2 tenderers, 1 winner country, no ids
        ted_notice("100004-2026", names=["Ambiguous One Ltd", "Ambiguous Two Ltd"], countries=["IRL", "IRL"], winner_countries=["IRL"]),
        # no tenderers at all
        {"publication-number": "100005-2026", "links": {}},
    ]


def ted_pages(size=2):
    """Pages of an ITERATION-mode search response over :func:`ted_notices`."""
    ns = ted_notices()
    pages = []
    for i in range(0, len(ns), size):
        chunk = ns[i:i + size]
        nxt = f"tok{i // size + 1}" if i + size < len(ns) else None
        pages.append({"notices": chunk, "totalNoticeCount": len(ns), "iterationNextToken": nxt, "timedOut": False})
    return pages


# ----------------------------------------------------------------------------------------------- USAspending
def usa_row(award_id, name, uei, *, amount=1000.0, start="2026-01-15", city="EXAMPLEVILLE", state="TX", zip5="75001", naics="541512",
            agency="Department of Examples", internal=None):
    return {
        "internal_id": abs(hash(award_id)) % 10**8, "Award ID": award_id, "Recipient Name": name, "Recipient UEI": uei,
        "Recipient Location": {"location_country_code": "USA", "country_name": "UNITED STATES", "state_code": state, "city_name": city,
                                 "address_line1": "1 EXAMPLE WAY", "zip5": zip5},
        "Award Amount": amount, "Start Date": start, "End Date": "2027-01-15", "Awarding Agency": agency,
        "NAICS": {"code": naics, "description": "Computer Systems Design Services"}, "PSC": {"code": "D302", "description": "IT"},
        "Contract Award Type": "DEFINITIVE CONTRACT", "Description": "Example services", "recipient_id": f"rid-{uei}-C",
        "generated_internal_id": internal or f"CONT_AWD_{award_id}_9700_-NONE-_-NONE-",
    }


def usa_rows() -> list[dict]:
    return [
        usa_row("EX0001", "EXAMPLE FEDERAL SERVICES INC", "EXAMPLEUEI001", start="2024-02-01"),
        usa_row("EX0002", "EXAMPLE FEDERAL SERVICES INC", "EXAMPLEUEI001", start="2025-06-01", amount=2500.0),
        usa_row("EX0003", "SAMPLE ENGINEERING LLC", "EXAMPLEUEI002", start="2026-03-01", naics="237310", city="SAMPLETON", state="CA", zip5="90001"),
        usa_row("EX0004", "MULTIPLE RECIPIENTS", "", start="2026-03-01"),
        usa_row("EX0005", "ONE OFF TEST CO", "EXAMPLEUEI003", start="2026-04-01", naics="722511"),
    ]


def usa_pages(size=2):
    rows = usa_rows()
    pages = []
    for i in range(0, len(rows), size):
        pages.append({"results": rows[i:i + size], "page_metadata": {"page": i // size + 1, "hasNext": i + size < len(rows)}})
    return pages


# ----------------------------------------------------------------------------------------------- ABN bulk extract
def abr_xml(abn, name, *, status="ACT", etype=("PRV", "Australian Private Company"), state="NSW", postcode="2000", acn=None, trading=None,
            individual=None, updated="20260930"):
    parts = [f'<ABR recordLastUpdatedDate="{updated}" replaced="N">',
             f'<ABN status="{status}" ABNStatusFromDate="20010101">{abn}</ABN>',
             f"<EntityType><EntityTypeInd>{etype[0]}</EntityTypeInd><EntityTypeText>{etype[1]}</EntityTypeText></EntityType>"]
    if individual:
        parts.append('<LegalEntity><IndividualName type="LGL"><GivenName>Test</GivenName><FamilyName>Person</FamilyName></IndividualName>'
                     f"<BusinessAddress><AddressDetails><State>{state}</State><Postcode>{postcode}</Postcode></AddressDetails></BusinessAddress></LegalEntity>")
    else:
        parts.append(f'<MainEntity><NonIndividualName type="MN"><NonIndividualNameText>{name}</NonIndividualNameText></NonIndividualName>'
                     f"<BusinessAddress><AddressDetails><State>{state}</State><Postcode>{postcode}</Postcode></AddressDetails></BusinessAddress></MainEntity>")
    if acn:
        parts.append(f'<ASICNumber ASICNumberType="undetermined">{acn}</ASICNumber>')
    parts.append('<GST status="ACT" GSTStatusFromDate="20010101"/>')
    if trading:
        parts.append(f'<OtherEntity><NonIndividualName type="TRD"><NonIndividualNameText>{trading}</NonIndividualNameText></NonIndividualName></OtherEntity>')
    parts.append("</ABR>")
    return "".join(parts)


def abn_xml(records: list[str], seq=1) -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<Transfer error="">'
            f"<TransferInfo><FileSequenceNumber>{seq}</FileSequenceNumber><RecordCount>{len(records)}</RecordCount>"
            "<ExtractTime>2026-10-06T00:00:00</ExtractTime></TransferInfo>" + "".join(records) + "</Transfer>")


def write_abn_zip(path: Path) -> Path:
    part1 = [
        abr_xml("11000000001", "EXAMPLE PTY LTD", acn="000000019", trading="Example Trading Co"),
        abr_xml("11000000002", "SAMPLE HOLDINGS PTY LTD", state="VIC", postcode="3000"),
        abr_xml("11000000003", "", individual=True, etype=("IND", "Individual/Sole Trader")),
    ]
    part2 = [
        abr_xml("11000000004", "CANCELLED EXAMPLE PTY LTD", status="CAN", state="QLD", postcode="4000"),
        abr_xml("11000000005", "TEST TRUST", etype=("DIT", "Discretionary Investment Trust"), state="WA", postcode="6000"),
    ]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("20261006_Public01.xml", abn_xml(part1, 1))
        zf.writestr("20261006_Public02.xml", abn_xml(part2, 2))
    return path


# ----------------------------------------------------------------------------------------------- Common Crawl
CC_ROWS = [
    # (url, host, registered_domain, tld, status, mime, crawl, fetch_time)
    ("https://www.example.com/", "www.example.com", "example.com", "com", 200, "text/html"),
    ("https://www.example.com/contact-us", "www.example.com", "example.com", "com", 200, "text/html"),
    ("https://www.example.com/about", "www.example.com", "example.com", "com", 200, "text/html"),
    ("https://www.example.com/team/", "www.example.com", "example.com", "com", 200, "text/html"),
    ("https://www.example.com/missing", "www.example.com", "example.com", "com", 404, "text/html"),
    ("https://www.example.com/brochure.pdf", "www.example.com", "example.com", "com", 200, "application/pdf"),
    ("https://www.example.com/static/app.js", "www.example.com", "example.com", "com", 200, "application/javascript"),
    ("https://www.example.org/contact", "www.example.org", "example.org", "org", 200, "text/html"),
    ("https://shop.sample.co.uk/find-us", "shop.sample.co.uk", "sample.co.uk", "uk", 200, "text/html"),
    ("https://www.other.de/impressum", "www.other.de", "other.de", "de", 200, "text/html"),
    ("https://www.other.de/", "www.other.de", "other.de", "de", 200, "text/html"),
]


def write_cc_parquet(path: Path, rows=None, *, row_group_size=3, crawl="CC-MAIN-2026-33") -> Path:
    import pyarrow as pa
    import pyarrow.parquet as pq

    rows = rows if rows is not None else CC_ROWS
    rows = sorted(rows, key=lambda r: (r[2], r[0]))
    t0 = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)
    cols = {
        "url_surt": [".".join(reversed(r[1].split("."))) + ")" + r[0].split(r[1])[-1] for r in rows],
        "url": [r[0] for r in rows], "url_host_name": [r[1] for r in rows], "url_host_registered_domain": [r[2] for r in rows],
        "url_host_tld": [r[3] for r in rows], "fetch_status": pa.array([r[4] for r in rows], pa.int16()),
        "content_mime_type": [r[5] for r in rows], "content_mime_detected": [r[5] for r in rows],
        "warc_filename": [f"crawl-data/{crawl}/segments/1/warc/example-{i:05d}.warc.gz" for i in range(len(rows))],
        "warc_record_offset": pa.array([1000 * (i + 1) for i in range(len(rows))], pa.int64()),
        "warc_record_length": pa.array([500 + i for i in range(len(rows))], pa.int32()),
        "crawl": [crawl] * len(rows),
        "fetch_time": pa.array([t0] * len(rows), pa.timestamp("ms", tz="UTC")),
    }
    pq.write_table(pa.table(cols), path, row_group_size=row_group_size)
    return path


def make_warc_member(url: str, html: str, *, warc_date="2026-08-10T12:00:00Z", status=200, content_type="text/html; charset=utf-8",
                     chunked=False) -> bytes:
    body = html.encode("utf-8")
    if chunked:
        body = f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n"
    http = (f"HTTP/1.1 {status} OK\r\nContent-Type: {content_type}\r\n"
            + ("Transfer-Encoding: chunked\r\n" if chunked else f"Content-Length: {len(body)}\r\n") + "\r\n").encode() + body
    warc = (f"WARC/1.0\r\nWARC-Type: response\r\nWARC-Date: {warc_date}\r\nWARC-Target-URI: {url}\r\n"
            f"Content-Type: application/http; msgtype=response\r\nContent-Length: {len(http)}\r\n\r\n").encode() + http + b"\r\n\r\n"
    return gzip.compress(warc)


CC_CONTACT_HTML = """<html><head><title>Contact Example Ltd</title></head><body>
<h1>Contact us</h1><p>Sales: <a href="mailto:sales@example-widgets.co.uk">sales@example.com</a></p>
<p>Old contact: <a href="mailto:former.staff@example-widgets.co.uk">Former Staff</a></p></body></html>"""
