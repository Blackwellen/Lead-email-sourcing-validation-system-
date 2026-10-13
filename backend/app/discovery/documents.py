"""DISC-11: deterministic document intelligence (PDF, CSV, XLSX, XML, JSON/JSONL, vCard, HTML).

``parse_document`` turns an untrusted file into :class:`ExtractedRecord` objects, each carrying its exact ``locator``
(``page 3, line 14`` / ``Sheet1!A7:F7`` / ``row 42`` / an XPath / a JSON pointer / ``vcard 2``), per-field locators and the file's
SHA-256. Hostile files are tolerated, never trusted:

* size, row, page, character, record and wall-clock limits everywhere
* XLSX zip containers are inspected before opening (member count, total uncompressed size, compression ratio = zip-bomb guard)
* spreadsheet formulas are NEVER evaluated and formula-like cell text (``=``, ``@`` ...) is dropped from mapped fields
* XML is parsed with entity resolution and network access disabled and any DOCTYPE / ENTITY declaration is rejected (XXE / billion laughs)
* malformed input yields ``[]`` plus a warning, never an exception
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..crawler.extract import clean_ws, looks_like_person_name, scan_text_for_emails
from ..crawler.types import EmailObservation
from ..crawler.urls import registrable_domain
from ..models import DocumentRecord, OrganisationDomain
from ..pipeline.ingest import normalise_email, persist_observations
from ..sources.base import BusinessRecord
from ..sources.urls import normalise_website
from .resolution import resolving_upsert_ex

PARSER_VERSION = "docs-1"
DOC_TYPES = ("pdf", "csv", "xlsx", "xml", "json", "vcard", "html")
METHOD_FOR_TYPE = {"pdf": "pdf", "csv": "csv", "xlsx": "xlsx", "xml": "xml", "json": "json", "vcard": "vcard", "html": "text"}


@dataclass
class DocLimits:
    max_bytes: int = 25_000_000
    max_rows: int = 200_000
    max_records: int = 50_000
    max_pages: int = 60
    max_chars: int = 2_000_000
    max_seconds: float = 20.0
    max_sheets: int = 20
    zip_max_members: int = 2000
    zip_max_uncompressed: int = 250_000_000
    zip_max_ratio: int = 200
    max_xml_nodes: int = 400_000


@dataclass
class ExtractedRecord:
    doc_type: str
    locator: str
    fields: dict[str, Any]
    file_sha256: str
    filename: str | None = None
    field_locators: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def emails(self) -> list[str]:
        return list(self.fields.get("emails") or [])

    def to_json(self) -> dict[str, Any]:
        return {"fields": self.fields, "field_locators": self.field_locators, "raw": self.raw, "warnings": self.warnings,
                "parser_version": PARSER_VERSION}


# --------------------------------------------------------------------------- header aliases
ALIASES: dict[str, set[str]] = {
    "company": {"company", "company name", "companyname", "organisation", "organization", "org", "organisation name", "organization name",
                "business", "business name", "firm", "firm name", "trading name", "trading as", "legal name", "registered name",
                "supplier", "supplier name", "vendor", "vendor name", "employer", "account name", "account", "member", "member name",
                "exhibitor", "exhibitor name", "contractor"},
    "name": {"name", "contact", "contact name", "full name", "fullname", "person", "contact person", "representative", "owner", "director",
             "key contact", "main contact", "primary contact"},
    "given_name": {"first name", "firstname", "given name", "forename", "first"},
    "family_name": {"last name", "lastname", "surname", "family name", "last"},
    "email": {"email", "e mail", "email address", "e mail address", "mail", "contact email", "emailaddress", "email 1", "email 2",
              "work email", "business email", "email1", "email2", "emails", "email addresses", "e mails"},
    "phone": {"phone", "telephone", "tel", "mobile", "phone number", "telephone number", "tel no", "contact number", "cell", "tel number"},
    "website": {"website", "web", "url", "site", "web address", "homepage", "web site", "domain", "company website", "www", "website url"},
    "address": {"address", "street", "street address", "address line 1", "registered address", "location", "address 1", "full address"},
    "postcode": {"postcode", "post code", "postal code", "zip", "zip code", "postcode zip"},
    "city": {"city", "town", "locality", "city town"},
    "country": {"country", "country code"},
    "role": {"role", "title", "job title", "position", "job role", "designation", "jobtitle", "job"},
    "company_number": {"company number", "company no", "registration number", "reg no", "companies house number", "crn", "company reg",
                       "registered number", "company registration number"},
}
_ALIAS_LOOKUP = {a: f for f, names in ALIASES.items() for a in names}
_CELL_SPLIT = re.compile(r"\s*\|\s*|\t+|\s{2,}|;\s*")
_FORMULA_START = ("=", "@")


def norm_header(h: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(h or "").casefold()).strip()


def map_headers(headers: list[Any]) -> dict[int, str]:
    """Column index -> canonical field (first occurrence wins, except e-mail columns which may repeat)."""
    out: dict[int, str] = {}
    used: set[str] = set()
    for i, h in enumerate(headers):
        f = _ALIAS_LOOKUP.get(norm_header(h))
        if f and (f == "email" or f not in used):
            out[i] = f
            used.add(f)
    return out


def _col_letter(n: int) -> str:
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


# --------------------------------------------------------------------------- normalisation
def _safe_text(v: Any, field_name: str, warns: list[str], where: str) -> str | None:
    """Cell/field text -> cleaned string; formula-like content is dropped (never evaluated)."""
    if v is None:
        return None
    s = clean_ws(str(v)) if not isinstance(v, str) else clean_ws(v)
    if not s:
        return None
    if s.startswith(_FORMULA_START) or (s[0] in "+-" and len(s) > 1 and not re.match(r"[+-][\d(\s]", s)):
        warns.append(f"formula_like_cell_ignored:{where}")
        return None
    return s


def _normalise_fields(raw: dict[str, list[str]]) -> dict[str, Any]:
    f: dict[str, Any] = {}
    emails: list[str] = []
    for chunk in raw.get("email", []):
        for part in re.split(r"[;,\s]+", chunk):
            ne = normalise_email(part.strip("<>()[]\"'").removeprefix("mailto:")) if part else None
            if ne and ne not in emails:
                emails.append(ne)
    if emails:
        f["emails"] = emails
        f["email"] = emails[0]
    given = (raw.get("given_name") or [None])[0]
    family = (raw.get("family_name") or [None])[0]
    name = (raw.get("name") or [None])[0] or (f"{given} {family}".strip() if given or family else None)
    if raw.get("company"):
        f["company"] = raw["company"][0][:400]
    if name:
        f["name"] = name[:300]
    for k in ("phone", "address", "postcode", "city", "country", "role", "company_number"):
        if raw.get(k):
            f[k] = raw[k][0][:300 if k == "address" else 80]
    if raw.get("website"):
        site = normalise_website(raw["website"][0])
        if site is not None:
            f["website"] = site.url
    return f


def _name_is_person(values: list[str]) -> bool:
    vals = [v for v in values if v][:25]
    return bool(vals) and sum(looks_like_person_name(v) for v in vals) * 2 > len(vals)


def _resolve_name_role(colmap: dict[int, str], sample: list[list[Any]]) -> dict[int, str]:
    """A bare ``Name`` column is a COMPANY unless a company / first-name column exists or the values look like people."""
    cols = [i for i, f in colmap.items() if f == "name"]
    if not cols or {"company", "given_name", "family_name"} & set(colmap.values()):
        return colmap
    vals = [str(r[cols[0]]) for r in sample if cols[0] < len(r) and r[cols[0]]]
    if "email" in colmap.values() and "website" not in colmap.values() and _name_is_person(vals):
        return colmap  # a people list with e-mail addresses
    out = dict(colmap)
    out[cols[0]] = "company"
    return out


def _row_record(doc_type: str, sha: str, fname: str | None, locator: str, cells: list[Any], colmap: dict[int, str],
                headers: list[Any], coords: list[str] | None = None, drop_warns: list[str] | None = None) -> ExtractedRecord | None:
    raw: dict[str, list[str]] = {}
    warns: list[str] = []
    flocs: dict[str, str] = {}
    for i, f in colmap.items():
        if i >= len(cells):
            continue
        where = coords[i] if coords and i < len(coords) else f"{locator}:{i + 1}"
        t = _safe_text(cells[i], f, warns, where)
        if t:
            raw.setdefault(f, []).append(t)
            flocs.setdefault(f, where)
    fields = _normalise_fields(raw)
    if not any(k in fields for k in ("emails", "company", "name", "website", "phone")):
        if drop_warns is not None:
            drop_warns.extend(warns)  # keep the reason a row produced nothing
        return None
    shown = {norm_header(headers[i]) or f"col{i + 1}": str(cells[i])[:120] for i in colmap if i < len(cells) and cells[i] not in (None, "")}
    return ExtractedRecord(doc_type, locator, fields, sha, fname, flocs, {"row": shown}, warns)


# --------------------------------------------------------------------------- dispatch
def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def detect_type(data: bytes, filename: str | None, mime: str | None) -> str | None:
    head = data[:2048].lstrip(b"\xef\xbb\xbf \t\r\n")
    ext = (filename or "").lower().rsplit(".", 1)[-1] if filename and "." in filename else ""
    m = (mime or "").lower()
    if data[:5] == b"%PDF-":
        return "pdf"
    if data[:4] == b"PK\x03\x04" or ext in ("xlsx", "xlsm"):
        return "xlsx" if data[:2] == b"PK" else None
    if head[:11].upper().startswith(b"BEGIN:VCARD") or ext in ("vcf", "vcard") or "vcard" in m:
        return "vcard"
    if ext == "jsonl" or ext == "ndjson":
        return "json"
    if ext == "json" or "json" in m or head[:1] in (b"{", b"["):
        return "json"
    if ext == "xml" or "xml" in m or head[:5] == b"<?xml":
        return "xml"
    if ext in ("html", "htm") or "html" in m or head[:15].lower().startswith((b"<!doctype html", b"<html")):
        return "html"
    if ext in ("csv", "tsv", "txt") or "csv" in m:
        return "csv"
    return None


def parse_document(
    path_or_bytes: str | Path | bytes, filename: str | None = None, mime: str | None = None, *,
    limits: DocLimits | None = None, warnings: list[str] | None = None,
) -> list[ExtractedRecord]:
    """Parse one document. Never raises for hostile/malformed input; problems go to ``warnings`` (if a list is given)."""
    lim = limits or DocLimits()
    warns = warnings if warnings is not None else []
    try:
        if isinstance(path_or_bytes, bytes | bytearray):
            data = bytes(path_or_bytes)
        else:
            p = Path(path_or_bytes)
            filename = filename or p.name
            if p.stat().st_size > lim.max_bytes:
                warns.append("file_too_large")
                return []
            with p.open("rb") as fh:
                data = fh.read(lim.max_bytes + 1)
        if len(data) > lim.max_bytes:
            warns.append("file_too_large")
            return []
        dt = detect_type(data, filename, mime)
        if dt is None:
            warns.append("unsupported_or_unrecognised_type")
            return []
        sha = sha256_of(data)
        deadline = time.monotonic() + lim.max_seconds
        parser = {"pdf": _parse_pdf, "csv": _parse_csv, "xlsx": _parse_xlsx, "xml": _parse_xml, "json": _parse_json,
                  "vcard": _parse_vcard, "html": _parse_html}[dt]
        out = parser(data, sha, filename, lim, deadline, warns, mime=mime)
        return out[:lim.max_records]
    except Exception as e:  # hostile input must never crash the pipeline
        warns.append(f"parse_error:{type(e).__name__}")
        return []


# --------------------------------------------------------------------------- CSV
def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16") if data[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeError:
            continue
    return data.decode("latin-1", "replace")


def _parse_csv(data, sha, fname, lim, deadline, warns, **_):
    text_ = _decode(data).replace("\x00", "")
    try:
        dialect = csv.Sniffer().sniff(text_[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    csv.field_size_limit(1_000_000)
    reader = csv.reader(io.StringIO(text_, newline=""), dialect)
    out: list[ExtractedRecord] = []
    headers: list[Any] | None = None
    colmap: dict[int, str] = {}
    sample: list[list[str]] = []
    rows: list[tuple[int, list[str]]] = []
    try:
        for row in reader:
            if time.monotonic() > deadline or reader.line_num > lim.max_rows:
                warns.append("row_or_time_limit")
                break
            if not any(c.strip() for c in row):
                continue
            if headers is None:
                m = map_headers(row)
                if m:
                    headers, colmap = row, m
                    continue
                if reader.line_num > 10:
                    warns.append("no_recognised_header")
                    return []
                continue
            rows.append((reader.line_num, row))
            if len(sample) < 25:
                sample.append(row)
    except csv.Error as e:
        warns.append(f"csv_error:{e}")
    if headers is None:
        warns.append("no_recognised_header")
        return []
    colmap = _resolve_name_role(colmap, sample)
    for line_no, row in rows:
        rec = _row_record("csv", sha, fname, f"row {line_no}", row, colmap, headers, None, warns)
        if rec:
            out.append(rec)
    return out


# --------------------------------------------------------------------------- XLSX
def _check_zip(data: bytes, lim: DocLimits, warns: list[str]) -> bool:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        warns.append("bad_zip")
        return False
    infos = zf.infolist()
    total = sum(i.file_size for i in infos)
    if len(infos) > lim.zip_max_members or total > lim.zip_max_uncompressed:
        warns.append("zip_bomb_suspected")
        return False
    for i in infos:
        if i.file_size > 1_000_000 and i.compress_size and i.file_size / max(1, i.compress_size) > lim.zip_max_ratio:
            warns.append("zip_bomb_suspected")
            return False
        if i.filename.startswith(("/", "..")) or ".." in i.filename.split("/"):
            warns.append("zip_path_traversal")
            return False
    return True


def _parse_xlsx(data, sha, fname, lim, deadline, warns, **_):
    if not _check_zip(data, lim, warns):
        return []
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=False, keep_links=False)
    out: list[ExtractedRecord] = []
    try:
        for ws in wb.worksheets[:lim.max_sheets]:
            headers: list[Any] | None = None
            colmap: dict[int, str] = {}
            rows: list[tuple[int, list[Any], list[str]]] = []
            sample: list[list[Any]] = []
            for r_idx, row in enumerate(ws.iter_rows(), start=1):
                if time.monotonic() > deadline or r_idx > lim.max_rows:
                    warns.append("row_or_time_limit")
                    break
                vals: list[Any] = []
                coords: list[str] = []
                for c_idx, cell in enumerate(row):
                    v = getattr(cell, "value", None)
                    if getattr(cell, "data_type", None) == "f" and not (isinstance(v, str) and v.startswith("=")):
                        v = "=" + str(v)
                    vals.append(v)
                    coords.append(f"{ws.title}!{_col_letter(c_idx)}{r_idx}")
                if not any(v not in (None, "") for v in vals):
                    continue
                if headers is None:
                    m = map_headers(vals)
                    if m:
                        headers, colmap = vals, m
                        continue
                    if r_idx > 10:
                        break
                    continue
                rows.append((r_idx, vals, coords))
                if len(sample) < 25:
                    sample.append(vals)
            if headers is None:
                continue
            colmap = _resolve_name_role(colmap, sample)
            last = _col_letter(max(len(headers) - 1, 0))
            for r_idx, vals, coords in rows:
                rec = _row_record("xlsx", sha, fname, f"{ws.title}!A{r_idx}:{last}{r_idx}", vals, colmap, headers, coords, warns)
                if rec:
                    out.append(rec)
    finally:
        wb.close()
    return out


# --------------------------------------------------------------------------- XML
_XML_DANGER = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.I)
_COMPANY_TAGS = re.compile(r"^(?:company|organi[sz]ation|business|firm|supplier|vendor|exhibitor|member|employer|account|org)$", re.I)
_PERSON_TAGS = re.compile(r"^(?:contact|person|employee|staff|director|representative|individual|owner|user)$", re.I)


def _xml_fields(el, tag_is_company: bool, tag_is_person: bool, warns: list[str], where: str) -> tuple[dict[str, list[str]], dict[str, str]]:
    raw: dict[str, list[str]] = {}
    locs: dict[str, str] = {}

    def put(key: str, val: str | None, loc: str) -> None:
        f = _ALIAS_LOOKUP.get(norm_header(key))
        if f is None:
            return
        if f == "name":
            f = "company" if tag_is_company and not tag_is_person else f
        t = _safe_text(val, f, warns, loc)
        if t:
            raw.setdefault(f, []).append(t)
            locs.setdefault(f, loc)

    for k, v in el.attrib.items():
        put(k.rsplit("}", 1)[-1], v, f"{where}/@{k}")
    for ch in el:
        if not isinstance(ch.tag, str):
            continue
        if len(ch) == 0:
            put(ch.tag.rsplit("}", 1)[-1], ch.text, f"{where}/{ch.tag.rsplit('}', 1)[-1]}")
    return raw, locs


def _parse_xml(data, sha, fname, lim, deadline, warns, **_):
    from lxml import etree

    if _XML_DANGER.search(data[:2_000_000]):
        warns.append("dtd_or_entity_forbidden")
        return []
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, dtd_validation=False, huge_tree=False,
                             remove_comments=True, remove_pis=True, recover=False)
    try:
        root = etree.fromstring(data, parser)
    except etree.XMLSyntaxError:
        warns.append("malformed_xml")
        return []
    tree = root.getroottree()
    out: list[ExtractedRecord] = []
    ancestors_company: dict[int, str] = {}
    sample_names: list[str] = []
    stack = [(root, 0)]
    n_nodes = 0
    order: list[Any] = []
    while stack:
        el, depth = stack.pop()
        n_nodes += 1
        if n_nodes > lim.max_xml_nodes or depth > 200 or time.monotonic() > deadline:
            warns.append("xml_node_or_time_limit")
            break
        order.append(el)
        stack.extend((c, depth + 1) for c in reversed(list(el)) if isinstance(c.tag, str))
    for el in order:
        tag = el.tag.rsplit("}", 1)[-1]
        raw, locs = _xml_fields(el, bool(_COMPANY_TAGS.match(tag)), bool(_PERSON_TAGS.match(tag)), warns, tree.getpath(el))
        if raw.get("name") and not _COMPANY_TAGS.match(tag) and not _PERSON_TAGS.match(tag):
            sample_names += raw["name"]
        if not any(k in raw for k in ("email", "company", "name", "website", "phone")):
            continue
        if raw.get("name") and not _PERSON_TAGS.match(tag) and not raw.get("company") and not raw.get("email") and not _name_is_person(raw["name"]):
            raw["company"] = raw.pop("name")
            locs["company"] = locs.pop("name")
        fields = _normalise_fields(raw)
        path = tree.getpath(el)
        parent_company = None
        p = el.getparent()
        while p is not None and parent_company is None:
            parent_company = ancestors_company.get(id(p))
            p = p.getparent()
        if "company" in fields:
            ancestors_company[id(el)] = fields["company"]
        elif parent_company and ("emails" in fields or "name" in fields):
            fields["company"] = parent_company
            locs["company"] = "inherited-from-ancestor"
        if any(k in fields for k in ("emails", "company", "name", "website", "phone")):
            out.append(ExtractedRecord("xml", path, fields, sha, fname, locs, {"element": tag}, []))
    return out


# --------------------------------------------------------------------------- JSON / JSONL
def _singular(k: str) -> str:
    k = k.lower()
    return k[:-3] + "y" if k.endswith("ies") else (k[:-1] if k.endswith("s") else k)


def _walk_json(node: Any, pointer: str, sha: str, fname: str | None, out: list[ExtractedRecord], warns: list[str], lim: DocLimits,
               depth: int = 0, parent_company: str | None = None, key_hint: str = "") -> None:
    if depth > 8 or len(out) >= lim.max_records:
        return
    if isinstance(node, list):
        for i, x in enumerate(node[:lim.max_rows]):
            _walk_json(x, f"{pointer}/{i}", sha, fname, out, warns, lim, depth + 1, parent_company, key_hint)
        return
    if not isinstance(node, dict):
        return
    raw: dict[str, list[str]] = {}
    locs: dict[str, str] = {}
    tag_company = bool(_COMPANY_TAGS.match(_singular(key_hint))) if key_hint else False
    tag_person = bool(_PERSON_TAGS.match(_singular(key_hint))) if key_hint else False
    for k, v in node.items():
        f = _ALIAS_LOOKUP.get(norm_header(k))
        if f is None or isinstance(v, dict | list) and f != "email":
            continue
        vals = v if isinstance(v, list) else [v]
        for j, one in enumerate(vals):
            if isinstance(one, dict | list):
                continue
            ff = "company" if f == "name" and tag_company and not tag_person else f
            t = _safe_text(one, ff, warns, f"{pointer}/{k}")
            if t:
                raw.setdefault(ff, []).append(t)
                locs.setdefault(ff, f"{pointer}/{k}" + (f"/{j}" if isinstance(v, list) else ""))
    if raw.get("name") and not raw.get("company") and not raw.get("email") and not tag_person and not _name_is_person(raw["name"]):
        raw["company"] = raw.pop("name")
        locs["company"] = locs.pop("name")
    fields = _normalise_fields(raw)
    company = fields.get("company") or parent_company
    if fields and not fields.get("company") and parent_company and ("emails" in fields or "name" in fields):
        fields["company"] = parent_company
        locs["company"] = "inherited-from-ancestor"
    if any(k in fields for k in ("emails", "company", "name", "website", "phone")):
        out.append(ExtractedRecord("json", pointer or "/", fields, sha, fname, locs, {}, []))
    for k, v in node.items():
        if isinstance(v, dict | list):
            _walk_json(v, f"{pointer}/{k}", sha, fname, out, warns, lim, depth + 1, company, str(k))


def _parse_json(data, sha, fname, lim, deadline, warns, **_):
    text_ = _decode(data)
    out: list[ExtractedRecord] = []
    is_lines = (fname or "").lower().endswith((".jsonl", ".ndjson"))
    if not is_lines:
        try:
            obj = json.loads(text_)
        except (ValueError, RecursionError):
            is_lines = True  # maybe JSONL without the extension
        else:
            _walk_json(obj, "", sha, fname, out, warns, lim)
            return out
    n_bad = 0
    for n, line in enumerate(text_.splitlines(), start=1):
        if time.monotonic() > deadline or n > lim.max_rows:
            warns.append("row_or_time_limit")
            break
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, RecursionError):
            n_bad += 1
            continue
        sub: list[ExtractedRecord] = []
        _walk_json(obj, "", sha, fname, sub, warns, lim)
        for r in sub:
            r.locator = f"line {n}" + ("" if r.locator in ("", "/") else r.locator)
            out.append(r)
    if n_bad:
        warns.append(f"malformed_json_lines:{n_bad}")
    return out


# --------------------------------------------------------------------------- vCard
def _parse_vcard(data, sha, fname, lim, deadline, warns, **_):
    text_ = re.sub(r"\r?\n[ \t]", "", _decode(data))
    out: list[ExtractedRecord] = []
    for n, block in enumerate(re.findall(r"BEGIN:VCARD(.*?)END:VCARD", text_, re.I | re.S), start=1):
        if time.monotonic() > deadline or n > lim.max_rows:
            warns.append("row_or_time_limit")
            break
        raw: dict[str, list[str]] = {}
        locs: dict[str, str] = {}
        fn = org = None
        for ln, line in enumerate(block.splitlines(), start=1):
            if ":" not in line:
                continue
            prop, _, val = line.partition(":")
            key = prop.split(";")[0].upper()
            val = re.sub(r"\\([,;nN])", lambda m: "\n" if m[1] in "nN" else m[1], val).strip()
            where = f"vcard {n}:{key}"
            f = {"FN": "name", "ORG": "company", "TITLE": "role", "EMAIL": "email", "TEL": "phone", "URL": "website", "ADR": "address"}.get(key)
            if f is None:
                continue
            if key == "ORG":
                val = val.split(";")[0]
                org = val
            if key == "FN":
                fn = val
            if key == "ADR":
                val = ", ".join(p for p in val.split(";") if p.strip())
            t = _safe_text(val, f, warns, where)
            if t:
                raw.setdefault(f, []).append(t)
                locs.setdefault(f, f"{where}#{ln}")
        if fn and org and norm_header(fn) == norm_header(org):
            raw.pop("name", None)  # a company card, not a person
        fields = _normalise_fields(raw)
        if any(k in fields for k in ("emails", "company", "name", "website", "phone")):
            out.append(ExtractedRecord("vcard", f"vcard {n}", fields, sha, fname, locs, {}, []))
    return out


# --------------------------------------------------------------------------- HTML
def _parse_html(data, sha, fname, lim, deadline, warns, **_):
    from lxml import html as lh

    from ..crawler.extract import extract_from_html

    text_ = _decode(data[:3_000_000])
    ex = extract_from_html(text_, f"file:{fname or sha[:12]}")
    company = next((o.name or o.legal_name for o in ex.org if o.name or o.legal_name), None)
    try:
        root = lh.fromstring(text_)
    except Exception:
        warns.append("malformed_html")
        return []
    tree = root.getroottree()
    by_email: dict[str, Any] = {}
    for o in ex.emails:
        ne = normalise_email(o.email)
        if ne and ne not in by_email:
            by_email[ne] = o
    out: list[ExtractedRecord] = []
    seen: set[str] = set()
    for el in root.iter():
        if not isinstance(el.tag, str) or time.monotonic() > deadline:
            continue
        blob = (el.get("href") or "") if el.tag == "a" else ""
        cands = []
        if blob.lower().startswith("mailto:"):
            cands = [blob[7:].split("?")[0]]
        elif el.text and "@" in el.text:
            cands = [e for e, _m, _p in scan_text_for_emails(el.text)]
        for c in cands:
            ne = normalise_email(c)
            if not ne or ne in seen:
                continue
            seen.add(ne)
            o = by_email.get(ne)
            path = tree.getpath(el)
            fields = {"emails": [ne], "email": ne}
            if o is not None:
                if o.name:
                    fields["name"] = o.name
                if o.title:
                    fields["role"] = o.title
                if o.phone:
                    fields["phone"] = o.phone
            if company:
                fields["company"] = company[:400]
            out.append(ExtractedRecord("html", path, fields, sha, fname, {"email": path}, {"context": (o.context if o else "")[:200]}, []))
            if len(out) >= lim.max_records:
                return out
    return out


# --------------------------------------------------------------------------- PDF
def _split_cells(line: str) -> list[str]:
    return [c.strip() for c in _CELL_SPLIT.split(line.strip()) if c.strip()]


def _parse_pdf(data, sha, fname, lim, deadline, warns, **_):
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted and not reader.decrypt(""):
            warns.append("encrypted_pdf")
            return []
        n_pages = len(reader.pages)
    except (PyPdfError, ValueError, OSError, KeyError, RecursionError) as e:
        warns.append(f"malformed_pdf:{type(e).__name__}")
        return []
    if n_pages > lim.max_pages:
        warns.append(f"page_limit:{n_pages}>{lim.max_pages}")
    out: list[ExtractedRecord] = []
    chars = 0
    for pno in range(min(n_pages, lim.max_pages)):
        if time.monotonic() > deadline or chars > lim.max_chars:
            warns.append("row_or_time_limit")
            break
        try:
            txt = reader.pages[pno].extract_text() or ""
        except Exception:
            warns.append(f"page_unreadable:{pno + 1}")
            continue
        chars += len(txt)
        lines = txt.replace("\x00", "").splitlines()
        headers: list[str] | None = None
        colmap: dict[int, str] = {}
        consumed: set[int] = set()
        for ln, line in enumerate(lines, start=1):
            cells = _split_cells(line)
            if len(cells) >= 2:
                m = map_headers(cells)
                if len(m) >= 2 and any(f in ("company", "email", "name", "website") for f in m.values()):
                    headers, colmap = cells, m
                    consumed.add(ln)
                    continue
            if headers is not None:
                if len(cells) >= max(2, len(headers) - 1) and len(cells) <= len(headers) + 1:
                    rec = _row_record("pdf", sha, fname, f"page {pno + 1}, line {ln}", cells, colmap, headers, None, warns)
                    if rec:
                        out.append(rec)
                        consumed.add(ln)
                        continue
                headers = None
        for ln, line in enumerate(lines, start=1):
            if ln in consumed:
                continue
            found = scan_text_for_emails(line)
            if not found:
                continue
            raw: dict[str, list[str]] = {"email": [e for e, _m, _p in found]}
            m = re.search(r"(?:tel|phone|t)[:.\s]*([+()\d][\d ()+\-.]{6,})", line, re.I)
            if m:
                raw["phone"] = [m.group(1).strip()]
            w = re.search(r"\b((?:https?://)?(?:www\.)[a-z0-9.-]+\.[a-z]{2,}[^\s,;]*)", line, re.I)
            if w:
                raw["website"] = [w.group(1)]
            fields = _normalise_fields(raw)
            out.append(ExtractedRecord("pdf", f"page {pno + 1}, line {ln}", fields, sha, fname, {"email": f"page {pno + 1}, line {ln}"},
                                       {"line": line[:200]}, []))
        out.sort(key=lambda r: (int(re.match(r"page (\d+)", r.locator).group(1)), int(r.locator.rsplit(" ", 1)[-1])))  # type: ignore[union-attr]
        if len(out) >= lim.max_records:
            break
    return out


# --------------------------------------------------------------------------- persistence
@dataclass
class PersistSummary:
    records: int = 0
    stored: int = 0
    linked: int = 0
    unlinked: int = 0
    emails: int = 0
    orgs_created: int = 0
    org_ids: list[int] = field(default_factory=list)


async def _org_for_record(session: AsyncSession, rec: ExtractedRecord, *, adapter: str, source_url: str | None, default_country: str | None,
                          create_orgs: bool, run_id: int | None, summary: PersistSummary) -> int | None:
    f = rec.fields
    company = f.get("company")
    if company and default_country:
        pc = f.get("postcode")
        br = BusinessRecord(
            source_ref=f"{rec.file_sha256[:16]}:{rec.locator}"[:300], legal_name=company, country_code=default_country.upper(),
            company_number=f.get("company_number"), website=f.get("website"), phone=f.get("phone"), address=f.get("address"),
            postal_code=pc, city=f.get("city"), adapter_id=adapter, source_url=source_url, raw={"doc_locator": rec.locator, "doc_sha256": rec.file_sha256})
        if create_orgs:
            oid, created, _res = await resolving_upsert_ex(session, br, adapter, run_id)
            summary.orgs_created += int(created)
            return oid
        from .resolution import resolve_record
        res = await resolve_record(session, br, adapter_id=adapter, source_ref=br.source_ref, persist=False)
        return res.org_id if res.decision == "matched" else None
    # e-mail domain of an already confirmed organisation
    for e in rec.emails:
        reg = registrable_domain(e.rpartition("@")[2])
        if not reg:
            continue
        row = (await session.execute(select(OrganisationDomain.organisation_id).where(
            OrganisationDomain.registrable_domain == reg, OrganisationDomain.verified_match_status.in_(("confirmed", "high")),
            OrganisationDomain.parked.is_(False)))).scalars().all()
        if len(set(row)) == 1:
            return row[0]
    return None


async def persist_document_records(
    session: AsyncSession, records: list[ExtractedRecord], *, source_url: str | None, adapter: str, org_id: int | None = None,
    run_id: int | None = None, default_country: str | None = None, create_orgs: bool = True,
) -> PersistSummary:
    """Store ``DocumentRecord`` rows (unique file_sha256+locator, idempotent) and link them to organisations/e-mails.

    Organisation: the explicit ``org_id``; else created/matched through the resolution engine when the record has a company
    name and ``default_country`` is given; else an already-confirmed organisation owning the e-mail's domain; else unlinked.
    E-mails are persisted through ``persist_observations`` with ``EvidenceSource.evidence_type`` = the document type, so they
    follow the same published/corroborated/needs-review rules as crawled addresses.
    """
    s = PersistSummary(records=len(records))
    by_org: dict[int, list[tuple[ExtractedRecord, EmailObservation]]] = {}
    rec_org: dict[int, int | None] = {}
    for i, rec in enumerate(records):
        oid = org_id if org_id is not None else await _org_for_record(
            session, rec, adapter=adapter, source_url=source_url, default_country=default_country, create_orgs=create_orgs,
            run_id=run_id, summary=s)
        rec_org[i] = oid
        if oid is None:
            s.unlinked += 1
            continue
        s.linked += 1
        if oid not in s.org_ids:
            s.org_ids.append(oid)
        for e in rec.emails:
            by_org.setdefault(oid, []).append((rec, EmailObservation(
                email=e, method=METHOD_FOR_TYPE.get(rec.doc_type, "text"), page_url=source_url or f"urn:sha256:{rec.file_sha256}",
                context=f"{rec.locator}: {str(rec.raw)[:200]}", name=rec.fields.get("name"), title=rec.fields.get("role"),
                phone=rec.fields.get("phone"))))
    email_ids: dict[str, int] = {}
    for oid, items in by_org.items():
        regs = {registrable_domain(o.email.rpartition("@")[2]) for _r, o in items}
        dom = (await session.execute(select(OrganisationDomain).where(
            OrganisationDomain.organisation_id == oid, OrganisationDomain.registrable_domain.in_([r for r in regs if r]))
            .order_by(OrganisationDomain.confidence.desc()).limit(1))).scalar_one_or_none()
        for (email_id, _created), (_r, ob) in zip(await persist_observations(
                session, oid, dom, [o for _r, o in items], run_id=run_id, source_adapter=adapter), items, strict=False):
            email_ids.setdefault(normalise_email(ob.email) or ob.email, email_id)
        s.emails += len({normalise_email(o.email) for _r, o in items})
    for i, rec in enumerate(records):
        first = email_ids.get(rec.emails[0]) if rec.emails else None
        stmt = pg_insert(DocumentRecord).values(
            source_adapter=adapter[:80], source_url=source_url, file_name=(rec.filename or "")[:300] or None, file_sha256=rec.file_sha256,
            doc_type=rec.doc_type, locator=rec.locator[:200], organisation_id=rec_org[i], email_id=first, extracted=rec.to_json(),
            parser_version=PARSER_VERSION)
        ex = stmt.excluded
        await session.execute(stmt.on_conflict_do_update(constraint="uq_docrec", set_={
            "extracted": ex.extracted, "parser_version": ex.parser_version, "observed_at": func.now(),
            "organisation_id": func.coalesce(ex.organisation_id, DocumentRecord.organisation_id),
            "email_id": func.coalesce(ex.email_id, DocumentRecord.email_id)}))
        s.stored += 1
    await session.flush()
    return s
