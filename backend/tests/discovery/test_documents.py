from __future__ import annotations

import io
import json
import zipfile

import openpyxl
from sqlalchemy import select

from app.discovery.documents import (
    DocLimits,
    map_headers,
    parse_document,
    persist_document_records,
    sha256_of,
)
from app.models import ContactEvidence, DocumentRecord, EmailAddress, EvidenceSource, Organisation, OrganisationDomain
from tests.crawler.pdfgen import make_pdf

CSV = ("Company Name;Contact;E-mail;Telephone;Website\r\n"
       "Acme Plumbing Ltd;Jane Doe;jane@acme.co.uk;01632 960001;www.acme.co.uk\r\n"
       "\r\n"
       "Bolt & Nut Ltd;;info@boltnut.co.uk, sales@boltnut.co.uk;;\r\n"
       "=HYPERLINK(\"http://evil.test\",\"x\");;bad@evil.co.uk;;\r\n"
       "Empty Row Ltd;;;;\r\n")


def test_csv_locators_aliases_formula_safety():
    w: list[str] = []
    recs = parse_document(CSV.encode(), "suppliers.csv", warnings=w)
    assert [r.locator for r in recs] == ["row 2", "row 4", "row 5", "row 6"]
    a, b, c, d = recs
    assert a.fields["company"] == "Acme Plumbing Ltd" and a.fields["name"] == "Jane Doe" and a.fields["email"] == "jane@acme.co.uk"
    assert a.fields["website"] == "https://www.acme.co.uk" and a.fields["phone"] == "01632 960001"
    assert a.file_sha256 == sha256_of(CSV.encode()) and a.doc_type == "csv" and a.filename == "suppliers.csv"
    assert b.fields["emails"] == ["info@boltnut.co.uk", "sales@boltnut.co.uk"]
    assert "company" not in c.fields and c.fields["email"] == "bad@evil.co.uk"  # formula text dropped, never kept
    assert any(x.startswith("formula_like_cell_ignored") for x in c.warnings)
    assert d.fields == {"company": "Empty Row Ltd"}
    assert map_headers(["Organisation", "E-Mail Address", "Tel", "Job Title", "Surname"]) == {0: "company", 1: "email", 2: "phone", 3: "role", 4: "family_name"}


def test_csv_bare_name_column_and_malformed():
    recs = parse_document(b"Name,Email\nJane Smith,jane@x.co.uk\nBob Jones,bob@x.co.uk\n", "p.csv")
    assert recs[0].fields["name"] == "Jane Smith" and "company" not in recs[0].fields
    comp = parse_document(b"Name,Website\nAcme Supplies,acme.co.uk\nBest Parts,best.co.uk\n", "c.csv")
    assert comp[0].fields["company"] == "Acme Supplies"
    w: list[str] = []
    assert parse_document(b"\x00\x01\x02 not a table at all", "x.csv", warnings=w) == [] and w
    assert parse_document(b"", "e.csv") == []
    assert parse_document(b"random", "x.unknown") == []


def make_xlsx(rows, title="Suppliers"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = title
    for r in rows:
        ws.append(r)
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


def test_xlsx_cell_locators_and_formulas():
    data = make_xlsx([["Company", "Email", "Phone", "Notes"],
                      ["Acme Ltd", "info@acme.co.uk", "01632 960002", "ok"],
                      ["=1+1", "=\"a\"&\"@b.co.uk\"", None, None],
                      ["@SUM(A1)", "real@boltnut.co.uk", None, None],
                      [None, None, None, None]])
    w: list[str] = []
    recs = parse_document(data, "s.xlsx", warnings=w)
    assert [r.locator for r in recs] == ["Suppliers!A2:D2", "Suppliers!A4:D4"]
    assert recs[0].field_locators == {"company": "Suppliers!A2", "email": "Suppliers!B2", "phone": "Suppliers!C2"}
    assert recs[0].fields["email"] == "info@acme.co.uk"
    r = recs[1]
    assert r.fields == {"emails": ["real@boltnut.co.uk"], "email": "real@boltnut.co.uk"}  # '@SUM(A1)' dropped, row 3 (pure formulas) skipped
    assert sum(1 for x in w + [x for rec in recs for x in rec.warnings] if "formula_like" in x) >= 3


def test_xlsx_zip_bomb_rejected():
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        with z.open("xl/worksheets/sheet1.xml", "w", force_zip64=True) as f:
            chunk = b"0" * 1_000_000
            for _ in range(120):
                f.write(chunk)
    data = bio.getvalue()
    assert len(data) < 1_000_000
    w: list[str] = []
    assert parse_document(data, "bomb.xlsx", warnings=w) == [] and "zip_bomb_suspected" in w
    # too many members
    bio2 = io.BytesIO()
    with zipfile.ZipFile(bio2, "w") as z:
        for i in range(30):
            z.writestr(f"f{i}.txt", "x")
    w2: list[str] = []
    assert parse_document(bio2.getvalue(), "many.xlsx", limits=DocLimits(zip_max_members=10), warnings=w2) == [] and "zip_bomb_suspected" in w2
    # garbage with xlsx extension
    w3: list[str] = []
    assert parse_document(b"PK\x03\x04garbage", "g.xlsx", warnings=w3) == [] and w3


XML = b"""<?xml version="1.0"?>
<directory>
  <company><name>Acme Ltd</name><website>acme.co.uk</website>
    <contacts><contact><name>Jane Doe</name><email>jane@acme.co.uk</email><title>Director</title></contact>
              <contact><name>Bob Roe</name><email>bob@acme.co.uk</email></contact></contacts></company>
  <company name="Bolt Ltd" email="info@bolt.co.uk"/>
</directory>"""


def test_xml_records_xpath_and_inheritance():
    recs = parse_document(XML, "d.xml")
    by = {r.locator: r for r in recs}
    assert "/directory/company[1]" in by and "/directory/company[2]" in by
    assert by["/directory/company[1]"].fields["company"] == "Acme Ltd"
    j = by["/directory/company[1]/contacts/contact[1]"]
    assert j.fields["name"] == "Jane Doe" and j.fields["email"] == "jane@acme.co.uk" and j.fields["company"] == "Acme Ltd"
    assert j.fields["role"] == "Director"
    assert by["/directory/company[2]"].fields["company"] == "Bolt Ltd" and by["/directory/company[2]"].fields["email"] == "info@bolt.co.uk"


def test_xml_xxe_and_billion_laughs_rejected():
    xxe = b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file:///etc/passwd">]><d><company><name>&x;</name><email>a@b.co.uk</email></company></d>'
    w: list[str] = []
    assert parse_document(xxe, "x.xml", warnings=w) == [] and "dtd_or_entity_forbidden" in w
    laughs = (b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>'
              b'<lolz><company><name>&lol2;</name></company></lolz>')
    w2: list[str] = []
    assert parse_document(laughs, "l.xml", warnings=w2) == []
    w3: list[str] = []
    assert parse_document(b"<a><b></a>", "m.xml", warnings=w3) == [] and "malformed_xml" in w3
    # an entity-free numeric reference is fine
    ok = parse_document(b"<d><company><name>R&#38;D Ltd</name><email>a@rd.co.uk</email></company></d>", "ok.xml")
    assert ok[0].fields["company"] == "R&D Ltd"


def test_json_and_jsonl_pointers():
    doc = {"companies": [{"name": "Acme Ltd", "email": "a@acme.co.uk", "website": "acme.co.uk",
                          "contacts": [{"name": "Jane Doe", "email": "j@acme.co.uk", "job_title": "CEO"}]},
                         {"name": "Bolt Ltd", "emails": ["x@bolt.co.uk", "y@bolt.co.uk"]}]}
    recs = parse_document(json.dumps(doc).encode(), "d.json")
    by = {r.locator: r for r in recs}
    assert set(by) == {"/companies/0", "/companies/0/contacts/0", "/companies/1"}
    assert by["/companies/0"].fields["company"] == "Acme Ltd" and by["/companies/0"].field_locators["email"] == "/companies/0/email"
    j = by["/companies/0/contacts/0"]
    assert j.fields["company"] == "Acme Ltd" and j.fields["name"] == "Jane Doe" and j.fields["role"] == "CEO"
    assert by["/companies/1"].fields["emails"] == ["x@bolt.co.uk", "y@bolt.co.uk"]
    lines = b'{"company": "A Ltd", "email": "a@a.co.uk"}\nnot json\n\n{"company": "B Ltd", "email": "b@b.co.uk"}\n'
    w: list[str] = []
    recs = parse_document(lines, "d.jsonl", warnings=w)
    assert [r.locator for r in recs] == ["line 1", "line 4"] and any(x.startswith("malformed_json_lines") for x in w)
    deep = b"[" * 5000 + b"]" * 5000
    assert parse_document(deep, "deep.json") == []  # recursion bomb tolerated


def test_vcard():
    v = (b"BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Jane Doe\r\nORG:Acme Ltd;Sales\r\nTITLE:Sales Director\r\nEMAIL;TYPE=WORK:jane@acme.co.uk\r\n"
         b"TEL;TYPE=WORK:+44 1632 960003\r\nURL:https://www.acme.co.uk\r\nADR;TYPE=WORK:;;1 High St;Leeds;;LS1 4AB;UK\r\nEND:VCARD\r\n"
         b"BEGIN:VCARD\r\nFN:Acme Ltd\r\nORG:Acme Ltd\r\nEMAIL:info@acme.co.uk\r\nEND:VCARD\r\n")
    recs = parse_document(v, "c.vcf")
    assert [r.locator for r in recs] == ["vcard 1", "vcard 2"]
    a, b = recs
    assert a.fields["name"] == "Jane Doe" and a.fields["company"] == "Acme Ltd" and a.fields["role"] == "Sales Director"
    assert a.fields["phone"] == "+44 1632 960003" and "LS1 4AB" in a.fields["address"]
    assert "name" not in b.fields and b.fields["company"] == "Acme Ltd"


def test_html_xpath_locators():
    html = (b"<html><head><script type='application/ld+json'>{\"@type\":\"Organization\",\"name\":\"Acme Ltd\"}</script></head><body>"
            b"<div><a href='mailto:info@acme.co.uk'>Email us</a></div><p>Sales: sales@acme.co.uk</p></body></html>")
    recs = parse_document(html, "p.html")
    by = {r.fields["email"]: r for r in recs}
    assert by["info@acme.co.uk"].locator == "/html/body/div/a" and by["sales@acme.co.uk"].locator == "/html/body/p"
    assert by["info@acme.co.uk"].fields["company"] == "Acme Ltd"


def test_pdf_pages_tables_and_limits():
    pdf = make_pdf(["Supplier directory", "Company | Email | Phone", "Acme Ltd | info@acme.co.uk | 01632 960004",
                    "Bolt Ltd | sales@bolt.co.uk | 01632 960005", "", "Questions? write to help@acme.co.uk Tel: 01632 960006"], pages=2)
    recs = parse_document(pdf, "s.pdf")
    locs = [r.locator for r in recs]
    assert locs[:3] == ["page 1, line 3", "page 1, line 4", "page 1, line 6"]
    assert recs[0].fields["company"] == "Acme Ltd" and recs[0].fields["email"] == "info@acme.co.uk" and recs[0].fields["phone"] == "01632 960004"
    assert recs[2].fields["email"] == "help@acme.co.uk" and recs[2].fields["phone"].startswith("01632 960006")
    assert any(l.startswith("page 2") for l in locs)
    w: list[str] = []
    limited = parse_document(pdf, "s.pdf", limits=DocLimits(max_pages=1), warnings=w)
    assert all(r.locator.startswith("page 1") for r in limited) and any(x.startswith("page_limit") for x in w)
    w2: list[str] = []
    assert parse_document(pdf, "s.pdf", limits=DocLimits(max_bytes=100), warnings=w2) == [] and "file_too_large" in w2
    w3: list[str] = []
    assert parse_document(b"%PDF-1.4\nthis is not a pdf at all", "bad.pdf", warnings=w3) == [] and w3
    big = make_pdf(["a@x.co.uk"], pages=40)
    assert len(parse_document(big, "big.pdf", limits=DocLimits(max_pages=5, max_records=3))) == 3
    assert parse_document(pdf[:200], "trunc.pdf") == []


def test_parse_from_path(tmp_path):
    p = tmp_path / "x.csv"
    p.write_bytes(CSV.encode())
    assert len(parse_document(str(p))) == 4 and parse_document(p, limits=DocLimits(max_bytes=10)) == []


async def test_persist_document_records_idempotent_and_linked(session):
    recs = parse_document(CSV.encode(), "suppliers.csv")
    s = await persist_document_records(session, recs, source_url="https://example.test/files/suppliers.csv", adapter="document_import",
                                       default_country="GB")
    assert s.records == 4 and s.stored == 4 and s.linked == 4 and s.orgs_created == 4 and s.emails == 4
    rows = (await session.execute(select(DocumentRecord).order_by(DocumentRecord.locator))).scalars().all()
    assert [r.locator for r in rows] == ["row 2", "row 4", "row 5", "row 6"]
    assert all(r.file_sha256 == recs[0].file_sha256 and r.doc_type == "csv" and r.parser_version for r in rows)
    assert rows[0].email_id is not None and rows[0].organisation_id is not None and rows[0].extracted["fields"]["company"] == "Acme Plumbing Ltd"
    ev = (await session.execute(select(EvidenceSource).where(EvidenceSource.evidence_type == "csv"))).scalars().all()
    assert ev and all(e.source_adapter == "document_import" and e.source_url.endswith("suppliers.csv") for e in ev)
    ctx = (await session.execute(select(ContactEvidence.context))).scalars().all()
    assert any("row 2" in str(c) for c in ctx)
    # no domain is confirmed by a document
    assert not (await session.execute(select(OrganisationDomain).where(OrganisationDomain.verified_match_status.in_(("confirmed", "high"))))).scalars().all()
    n_orgs = len((await session.execute(select(Organisation))).scalars().all())
    n_em = len((await session.execute(select(EmailAddress))).scalars().all())
    s2 = await persist_document_records(session, recs, source_url="https://example.test/files/suppliers.csv", adapter="document_import",
                                        default_country="GB")
    assert s2.orgs_created == 0
    assert len((await session.execute(select(DocumentRecord))).scalars().all()) == 4
    assert len((await session.execute(select(Organisation))).scalars().all()) == n_orgs
    assert len((await session.execute(select(EmailAddress))).scalars().all()) == n_em
    # explicit organisation, no country needed; unlinked when nothing identifies the organisation
    o = Organisation(legal_name="Known Ltd", country_code="GB", canonical_identity="GB:nm:known")
    session.add(o)
    await session.flush()
    v = parse_document(b"BEGIN:VCARD\nFN:Pat Lee\nEMAIL:pat@known.co.uk\nEND:VCARD\n", "k.vcf")
    s3 = await persist_document_records(session, v, source_url=None, adapter="document_import", org_id=o.id)
    assert s3.linked == 1 and s3.emails == 1
    em = (await session.execute(select(EmailAddress).where(EmailAddress.email_normalised == "pat@known.co.uk"))).scalar_one()
    assert em.organisation_id == o.id
    v2 = parse_document(b"BEGIN:VCARD\nFN:Nobody Here\nEMAIL:nobody@nowhere.co.uk\nEND:VCARD\n", "n.vcf")
    s4 = await persist_document_records(session, v2, source_url=None, adapter="document_import")
    assert s4.unlinked == 1 and s4.linked == 0
    assert (await session.execute(select(DocumentRecord).where(DocumentRecord.doc_type == "vcard", DocumentRecord.organisation_id.is_(None)))).scalars().all()
