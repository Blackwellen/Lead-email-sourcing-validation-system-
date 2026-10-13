import zipfile
from pathlib import Path

import openpyxl
import pytest

from app.sources.base import DiscoveryQuery
from app.sources.csv_import import (
    CsvImportAdapter,
    ImportLimits,
    ImportRejected,
    ImportReport,
    detect_encoding,
    inspect_xlsx,
    iter_import_rows,
    validate_import_file,
)


async def collect(adapter, q=None, cp=None, stop_after=None):
    out = []
    async for rec, c in adapter.iter_businesses(q or DiscoveryQuery(), cp):
        out.append((rec, c))
        if stop_after and len(out) >= stop_after:
            break
    return out


def write(tmp_path: Path, name: str, text: str, enc="utf-8") -> Path:
    p = tmp_path / name
    p.write_bytes(text.encode(enc))
    return p


async def test_domain_list_basic_and_dedupe(tmp_path):
    p = write(tmp_path, "d.csv", "Website\nfixture-one.example.com\nhttps://www.Fixture-Two.example.com/about?x=1\n"
                                 "fixture-one.example.com\nnot a domain\nmailto:a@b.com\nhttp://127.0.0.1/\nlocalhost\n"
                                 "info@fixture-three.example.com\n")
    a = CsvImportAdapter({"path": str(p), "country_code": "GB"})
    got = await collect(a)
    assert [r.website for r, _ in got] == ["https://fixture-one.example.com", "https://www.fixture-two.example.com/about?x=1"]
    assert all(r.country_code == "GB" and r.licence.startswith("operator-supplied") for r, _ in got)
    assert got[0][0].raw["name_derived_from_domain"] is True
    rep = a.last_report
    assert rep.accepted == 2 and rep.duplicates == 1 and rep.rejected == 5
    assert rep.reasons["invalid_domain"] == 5


async def test_business_rows_header_aliases_and_semicolons(tmp_path):
    p = write(tmp_path, "b.csv",
              "Business Name;Reg No;Web Address;Telephone;Post Code;Town;Country\n"
              "Fixture Widgets Ltd;TEST0001;fixture-widgets.example.com;0161 000 0000;m1 1aa;Manchester;United Kingdom\n"
              "Fixture Gadgets;;;;;Leeds;GB\n")
    got = await collect(CsvImportAdapter({"path": str(p)}))
    r1, r2 = got[0][0], got[1][0]
    assert r1.legal_name == "Fixture Widgets Ltd" and r1.company_number == "TEST0001" and r1.country_code == "GB"
    assert r1.website == "https://fixture-widgets.example.com" and r1.phone == "0161 000 0000" and r1.city == "Manchester"
    assert r2.trading_name == "Fixture Gadgets" and r2.website is None


async def test_missing_country_rejected_unless_default(tmp_path):
    p = write(tmp_path, "d.csv", "domain\nfixture-one.example.com\n")
    a = CsvImportAdapter({"path": str(p)})
    assert await collect(a) == []
    assert a.last_report.reasons == {"missing_country": 1}
    assert len(await collect(CsvImportAdapter({"path": str(p)}), DiscoveryQuery(countries=["IE"]))) == 1


async def test_formula_cells_are_dropped_not_evaluated(tmp_path):
    p = write(tmp_path, "f.csv", 'name,domain,phone\n"=HYPERLINK(""http://evil.example.com"")",fixture-one.example.com,"+44 1"\n'
                                 '@SUM(A1),fixture-two.example.com,\n')
    a = CsvImportAdapter({"path": str(p), "country_code": "GB"})
    got = await collect(a)
    assert a.last_report.formula_cells_dropped == 2
    assert got[0][0].trading_name == "fixture-one.example.com"  # name dropped -> derived from domain
    assert got[0][0].phone == "+44 1"
    assert not any("=" in (r.trading_name or "") for r, _ in got)


async def test_email_column_is_ignored(tmp_path):
    p = write(tmp_path, "e.csv", "name,domain,email\nFixture Co,fixture-one.example.com,sales@fixture-one.example.com\n")
    a = CsvImportAdapter({"path": str(p), "country_code": "GB"})
    got = await collect(a)
    assert got[0][0].email_published is None
    assert "email (ignored by design)" in a.last_report.columns_ignored


@pytest.mark.parametrize("enc", ["utf-8", "utf-8-sig", "utf-16", "cp1252"])
async def test_encodings(tmp_path, enc):
    p = write(tmp_path, "enc.csv", "name,domain\nCafé Fixture Ltd,fixture-cafe.example.com\n", enc)
    got = await collect(CsvImportAdapter({"path": str(p), "country_code": "GB"}))
    assert got[0][0].trading_name == "Café Fixture Ltd"


def test_detect_encoding_rejects_binary():
    assert detect_encoding("é".encode()) == "utf-8"
    assert detect_encoding("é".encode("cp1252")) == "cp1252"
    with pytest.raises(ImportRejected):
        detect_encoding(b"PK\x03\x04\x00\x00\x00\x00binary")


async def test_binary_file_rejected(tmp_path):
    p = tmp_path / "x.csv"
    p.write_bytes(b"\x00\x01\x02" * 100)
    with pytest.raises(ImportRejected):
        await collect(CsvImportAdapter({"path": str(p), "country_code": "GB"}))


async def test_size_row_and_column_limits(tmp_path):
    p = write(tmp_path, "big.csv", "domain\n" + "\n".join(f"fixture-{i}.example.com" for i in range(50)))
    with pytest.raises(ImportRejected, match="too large"):
        await collect(CsvImportAdapter({"path": str(p), "country_code": "GB", "max_bytes": 100}))
    with pytest.raises(ImportRejected, match="row limit"):
        await collect(CsvImportAdapter({"path": str(p), "country_code": "GB", "max_rows": 10}))
    wide = write(tmp_path, "wide.csv", ",".join(f"c{i}" for i in range(500)) + "\n" + ",".join("x" for _ in range(500)))
    with pytest.raises(ImportRejected, match="columns"):
        await collect(CsvImportAdapter({"path": str(wide), "country_code": "GB", "max_cols": 20}))
    empty = tmp_path / "empty.csv"
    empty.write_bytes(b"")
    with pytest.raises(ImportRejected, match="empty"):
        await collect(CsvImportAdapter({"path": str(empty), "country_code": "GB"}))


async def test_unrecognised_header_rejected(tmp_path):
    p = write(tmp_path, "h.csv", "foo,bar\n1,2\n")
    with pytest.raises(ImportRejected, match="header"):
        await collect(CsvImportAdapter({"path": str(p), "country_code": "GB"}))


async def test_huge_field_is_truncated_not_crashing(tmp_path):
    p = write(tmp_path, "h.csv", "name,domain\n" + "A" * 5000 + ",fixture-one.example.com\n")
    got = await collect(CsvImportAdapter({"path": str(p), "country_code": "GB"}))
    assert len(got[0][0].trading_name) <= 2000


async def test_checkpoint_resume(tmp_path):
    p = write(tmp_path, "d.csv", "domain\n" + "\n".join(f"fixture-{i}.example.com" for i in range(20)) + "\nfixture-3.example.com\n")
    a = CsvImportAdapter({"path": str(p), "country_code": "GB"})
    full = [r.website for r, _ in await collect(a)]
    assert len(full) == 20
    for k in (1, 7, 19):
        first = await collect(a, stop_after=k)
        rest = await collect(a, cp=first[-1][1])
        assert [r.website for r, _ in first] + [r.website for r, _ in rest] == full


async def test_limit(tmp_path):
    p = write(tmp_path, "d.csv", "domain\n" + "\n".join(f"fixture-{i}.example.com" for i in range(20)))
    assert len(await collect(CsvImportAdapter({"path": str(p), "country_code": "GB"}), DiscoveryQuery(limit=5))) == 5


def test_validate_dry_run(tmp_path):
    p = write(tmp_path, "d.csv", "domain,company name\nfixture-one.example.com,Fixture One\nbad domain,Fixture Two\n")
    rep = validate_import_file(p, default_country="GB")
    assert (rep.accepted, rep.rejected) == (1, 1)
    assert rep.columns_mapped == {"domain": "domain", "company name": "name"}


# ------------------------------------------------------------------ XLSX
def make_xlsx(path: Path, rows, extra=None):
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    wb.save(path)
    return path


async def test_xlsx_import_and_formulas(tmp_path):
    p = make_xlsx(tmp_path / "a.xlsx", [
        ["Company Name", "Website", "Country"],
        ["Fixture Xlsx Ltd", "fixture-xlsx.example.com", "GB"],
        ["=1+1", "fixture-two.example.com", "GB"],
        ["Fixture Three", "=HYPERLINK(\"http://x\")", "GB"],
    ])
    a = CsvImportAdapter({"path": str(p)})
    got = await collect(a)
    assert [r.website for r, _ in got][:2] == ["https://fixture-xlsx.example.com", "https://fixture-two.example.com"]
    # data_only=True: a never-calculated formula has no cached value, so the cell simply arrives empty (never evaluated)
    assert got[1][0].trading_name == "fixture-two.example.com"
    assert got[2][0].website is None and got[2][0].trading_name == "Fixture Three"


def test_xlsx_zip_bomb_rejected(tmp_path):
    p = tmp_path / "bomb.xlsx"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("xl/workbook.xml", "<workbook/>")
        zf.writestr("xl/worksheets/sheet1.xml", b"0" * (50 * 1024 * 1024))  # compresses ~1000:1
    with pytest.raises(ImportRejected, match="zip bomb|compression ratio"):
        inspect_xlsx(p, ImportLimits())
    with pytest.raises(ImportRejected):
        list(iter_import_rows(p, limits=ImportLimits(), report=ImportReport(), default_country="GB"))


def test_xlsx_total_uncompressed_limit(tmp_path):
    p = tmp_path / "big.xlsx"
    import os

    with zipfile.ZipFile(p, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("xl/workbook.xml", "<workbook/>")
        zf.writestr("xl/sheet.bin", os.urandom(3 * 1024 * 1024))
    with pytest.raises(ImportRejected, match="size exceeds"):
        inspect_xlsx(p, ImportLimits(xlsx_max_uncompressed=1024 * 1024))


def test_xlsx_macros_traversal_and_garbage(tmp_path):
    def zipped(name, entries):
        p = tmp_path / name
        with zipfile.ZipFile(p, "w") as zf:
            for n in entries:
                zf.writestr(n, "x")
        return p

    base = ["[Content_Types].xml", "xl/workbook.xml"]
    with pytest.raises(ImportRejected, match="macro"):
        inspect_xlsx(zipped("m.xlsx", [*base, "xl/vbaProject.bin"]), ImportLimits())
    with pytest.raises(ImportRejected, match="unsafe"):
        inspect_xlsx(zipped("t.xlsx", [*base, "../evil.txt"]), ImportLimits())
    with pytest.raises(ImportRejected, match="too many"):
        inspect_xlsx(zipped("n.xlsx", [*base, *[f"xl/f{i}.xml" for i in range(300)]]), ImportLimits())
    g = tmp_path / "g.xlsx"
    g.write_bytes(b"this is not a zip")
    # not a zip -> treated as CSV; binary sniffing or header check must reject it, never crash
    with pytest.raises(ImportRejected):
        list(iter_import_rows(g, limits=ImportLimits(), report=ImportReport(), default_country="GB"))
    xls = tmp_path / "old.xls"
    xls.write_bytes(b"\xd0\xcf\x11\xe0" + b"\x00" * 100)
    with pytest.raises(ImportRejected, match="xls"):
        list(iter_import_rows(xls, limits=ImportLimits(), report=ImportReport(), default_country="GB"))


async def test_health(tmp_path):
    assert (await CsvImportAdapter({}).health()).status == "not_configured"
    p = write(tmp_path, "d.csv", "domain\nfixture-one.example.com\n")
    assert (await CsvImportAdapter({"path": str(p)}).health()).ok
