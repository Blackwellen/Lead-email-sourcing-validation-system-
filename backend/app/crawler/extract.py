"""Pure extraction functions (no I/O): HTML / vCard / PDF -> e-mail observations + identity facts.

Rules this module enforces:
* Everything returned was PUBLISHED on the page. A person's name is never derived from an
  e-mail local part; it must come from structured data or from a DOM neighbour of the address.
* Association of name/title/phone to an address is deliberately conservative: only inside a
  single card/row/list-item/article container that holds exactly one distinct address.
* Emails in elements that are hidden (``hidden``, ``display:none`` ...) are ignored - they are
  typically honeypots/spam traps.
* Page text is data. Nothing in it is ever interpreted as an instruction.
"""
from __future__ import annotations

import html as html_lib
import io
import json
import logging
import quopri
import re
import unicodedata
from collections.abc import Callable, Iterable
from urllib.parse import unquote, urljoin, urlsplit

from .types import (
    EmailObservation,
    OrgMetadata,
    PageExtraction,
    PageLink,
    RankedLink,
)
from .urls import host_of, known_tlds, normalise_url, strip_www, to_ascii_host

log = logging.getLogger(__name__)
logging.getLogger("pypdf").setLevel(logging.ERROR)

try:  # pragma: no cover - import guard
    from selectolax.lexbor import LexborHTMLParser as _HTMLParser
except Exception:  # pragma: no cover
    _HTMLParser = None  # type: ignore[assignment,misc]

# --------------------------------------------------------------------------- e-mail syntax / garbage
_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+\-/=])([A-Za-z0-9_][A-Za-z0-9._%+\-']{0,63})@((?:[^\W_]|-)+(?:\.(?:[^\W_]|-)+)+)",
    re.UNICODE,
)
_AT = (r"(?:\s*\[\s*at\s*\]\s*|\s*\(\s*at\s*\)\s*|\s*\{\s*at\s*\}\s*|\s*<\s*at\s*>\s*"
       r"|\s*\[\s*@\s*\]\s*|\s*\(\s*@\s*\)\s*|\s*\{\s*@\s*\}\s*)")
_DOTSEP = (r"(?:\s*\[\s*dot\s*\]\s*|\s*\(\s*dot\s*\)\s*|\s*\{\s*dot\s*\}\s*|\s*<\s*dot\s*>\s*|\s+dot\s+"
           r"|\s*\[\s*\.\s*\]\s*|\s*\(\s*\.\s*\)\s*|\.)")
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"
_LOCAL_OBF = r"[A-Za-z0-9_](?:[A-Za-z0-9._%+\-]|\[dot\]|\(dot\)){0,63}"
_OBF_BRACKET_RE = re.compile(
    rf"(?<![A-Za-z0-9._%+\-/=])({_LOCAL_OBF}){_AT}({_LABEL}(?:{_DOTSEP}{_LABEL})+)", re.I)
_OBF_WORDS_RE = re.compile(
    rf"(?<![A-Za-z0-9._%+\-/=@])({_LOCAL_OBF})\s+at\s+({_LABEL}(?:\s+dot\s+{_LABEL})+)", re.I)
_OBF_HINTS = ("[at]", "(at)", "{at}", "<at>", "[@]", "(@)", "{@}", " at ")
_EMAIL_LABEL_BEFORE = re.compile(r"(e-?mail|mail|contact|write|enquir|reach|send)[^.]{0,25}$", re.I)

_ROLE_LOCALS = {
    "info", "sales", "contact", "enquiries", "enquiry", "hello", "office", "admin", "support", "help",
    "mail", "team", "accounts", "billing", "bookings", "reception", "marketing", "press", "hr", "jobs",
    "careers", "service", "customerservice", "webmaster", "general", "post", "mailbox", "lettings",
}
_FILE_EXTS = {
    "png", "jpg", "jpeg", "gif", "svg", "webp", "avif", "bmp", "ico", "tif", "tiff", "css", "js", "mjs", "map",
    "json", "woff", "woff2", "ttf", "otf", "eot", "mp4", "webm", "mp3", "wav", "mov", "zip", "gz", "tar", "rar",
    "exe", "dll", "html", "htm", "php", "asp", "aspx", "pdf", "doc", "docx", "xls", "xlsx", "txt", "xml",
}
_PLACEHOLDER_DOMAINS = {
    "example.com", "example.org", "example.net", "example.co.uk", "example.edu", "test.com", "domain.com",
    "yourdomain.com", "yourdomain.co.uk", "yourcompany.com", "yoursite.com", "yourwebsite.com", "mydomain.com",
    "mysite.com", "sentry.io", "sentry-next.wixpress.com", "sentry.wixpress.com", "wixpress.com", "localhost",
    "email.com", "emailaddress.com", "website.com", "company.com", "domain.co.uk", "domainname.com",
    "yourdomainname.com", "sampledomain.com", "placeholder.com",
}
_GENERIC_PLACEHOLDER_DOMAINS = {"email.com", "domain.com", "company.com", "website.com", "emailaddress.com",
                                "domain.co.uk", "domainname.com", "yourdomain.com", "yourcompany.com"}
_PLACEHOLDER_LOCALS = {
    "yourname", "your.name", "your_name", "youremail", "your-email", "your.email", "yourmail", "firstname.lastname",
    "first.last", "firstname", "lastname", "first.lastname", "firstname_lastname", "nom", "votre.nom",
    "name.surname", "username", "user.name", "someone", "somebody", "email", "name", "user", "example", "test",
    "john.doe", "jane.doe", "johndoe", "janedoe", "xxx", "xxxx", "abc", "foo", "bar",
}
_HEXISH = re.compile(r"^[0-9a-f]{24,}$")
_JSON_ESCAPE_PREFIXES = ("u003e", "u003c", "u0026", "u0022", "u0027", "u002f", "u003d", "u0040")  # lost-backslash artifacts


def valid_email_parts(local: str, domain: str) -> str | None:
    """Return the canonical-case address ``local@domain`` or None if it is syntactically junk."""
    local = local.strip(".-_%+'")
    if "'" in local and not re.search(r"(?:^|[._-])[od]'[a-z]", local, re.I):
        local = local.rsplit("'", 1)[1]  # French elision such as l'info@ -> info@
    domain = domain.strip(".-").lower()
    if not local or len(local) > 64 or ".." in local:
        return None
    if local.lower().startswith(_JSON_ESCAPE_PREFIXES):
        return None
    ascii_domain = to_ascii_host(domain)
    if not ascii_domain or "." not in ascii_domain:
        return None
    labels = ascii_domain.split(".")
    if any(not lab or lab.startswith("-") or lab.endswith("-") for lab in labels):
        return None
    tld = labels[-1]
    if tld in _FILE_EXTS or tld not in known_tlds():
        return None
    if re.fullmatch(r"\d+x", labels[0]) and tld in _FILE_EXTS:
        return None
    low = local.lower()
    if ascii_domain in _PLACEHOLDER_DOMAINS:
        return None
    if low in _PLACEHOLDER_LOCALS and (ascii_domain in _GENERIC_PLACEHOLDER_DOMAINS or low in {
        "yourname", "your.name", "your_name", "youremail", "your-email", "your.email", "yourmail",
        "firstname.lastname", "first.last", "firstname", "lastname", "first.lastname", "username", "user.name",
        "john.doe", "jane.doe", "johndoe", "janedoe", "xxx", "xxxx", "name.surname", "votre.nom",
    }):
        return None
    if _HEXISH.match(low):  # sentry/analytics keys such as 0123abcd...@o123.ingest.sentry.io
        return None
    if re.search(r"(?:^|\.)(sentry|ingest\.sentry|wixpress)\.", ascii_domain + "."):
        return None
    return f"{local}@{domain}"


def clean_email(value: str) -> str | None:
    m = _EMAIL_RE.fullmatch(value.strip())
    return valid_email_parts(m.group(1), m.group(2)) if m else None


def decode_cfemail(hexstr: str) -> str | None:
    """Decode Cloudflare e-mail protection (public, page-embedded XOR obfuscation)."""
    try:
        raw = bytes.fromhex(hexstr.strip())
    except ValueError:
        return None
    if len(raw) < 4:
        return None
    key = raw[0]
    try:
        return bytes(b ^ key for b in raw[1:]).decode("utf-8")
    except UnicodeDecodeError:
        return None


def scan_text_for_emails(text: str) -> list[tuple[str, str, int]]:
    """Find plain and obfuscated addresses in free text -> [(email, method, start)]."""
    found: list[tuple[str, str, int]] = []
    low = text.lower()
    if "@" in text:
        for m in _EMAIL_RE.finditer(text):
            e = valid_email_parts(m.group(1), m.group(2))
            if e:
                found.append((e, "text", m.start()))
    if any(h in low for h in _OBF_HINTS):
        for m in _OBF_BRACKET_RE.finditer(text):
            local = re.sub(r"\s*[\[(]dot[\])]\s*", ".", m.group(1), flags=re.I)
            dom = re.sub(_DOTSEP, ".", m.group(2), flags=re.I)
            e = valid_email_parts(local, dom)
            if e:
                found.append((e, "obfuscated", m.start()))
        if " dot " in low and " at " in low:
            for m in _OBF_WORDS_RE.finditer(text):
                local_raw = m.group(1)
                local = re.sub(r"\s*[\[(]dot[\])]\s*", ".", local_raw, flags=re.I)
                prefix = text[max(0, m.start() - 40): m.start()]
                plausible = (
                    local.lower() in _ROLE_LOCALS
                    or any(c in local for c in "._-") or any(c.isdigit() for c in local)
                    or bool(_EMAIL_LABEL_BEFORE.search(prefix))
                )
                if not plausible or local.lower() in _NOT_LOCALS:
                    continue
                dom = re.sub(r"\s+dot\s+", ".", m.group(2), flags=re.I)
                e = valid_email_parts(local, dom)
                if e:
                    found.append((e, "obfuscated", m.start()))
    return found


_NOT_LOCALS = {"look", "meet", "arrive", "stay", "call", "aim", "here", "available", "sold", "located", "open",
               "based", "starting", "only", "it", "us", "me", "you", "we", "them", "found", "seen", "live", "work",
               "smile", "stop", "start", "great", "good", "best"}

# --------------------------------------------------------------------------- names / titles / phones
_HONORIFICS = {"mr", "mrs", "ms", "miss", "mx", "dr", "prof", "professor", "sir", "dame", "rev", "lord", "lady",
               "herr", "frau", "mme", "monsieur", "madame", "mlle", "m", "sra", "sr", "hr", "fr", "eng", "cllr"}
_NAME_STOP = {
    "contact", "us", "our", "team", "office", "offices", "enquiries", "enquiry", "email", "e-mail", "mail", "phone",
    "tel", "telephone", "fax", "address", "details", "information", "info", "sales", "support", "service",
    "services", "customer", "customers", "careers", "jobs", "opening", "hours", "head", "general", "follow",
    "find", "get", "touch", "visit", "call", "write", "send", "message", "login", "register", "subscribe",
    "newsletter", "privacy", "cookie", "cookies", "terms", "copyright", "ltd", "limited", "llp", "plc", "inc",
    "group", "solicitors", "estate", "agents", "department", "reception", "accounts", "welcome", "about",
    "meet", "the", "and", "of", "for", "to", "in", "at", "with", "your", "my", "we", "online", "free", "quote",
    "book", "now", "more", "read", "view", "click", "here", "home", "news", "blog", "products", "directions",
    "location", "locations", "branch", "branches", "staff", "people", "management", "board", "directors",
    "partners", "company", "business", "professional", "registered", "number", "vat", "social", "media", "enquire",
    "request", "appointment", "appointments", "emergency", "help", "desk", "dept", "centre", "center",
    "clinic", "practice", "surgery", "studio", "shop", "store", "limited.", "solutions", "consulting", "associates",
}
_TITLE_WORDS = re.compile(
    r"\b(director|manager|partner|associate|solicitor|consultant|executive|officer|ceo|cfo|coo|cto|cmo|founder|"
    r"co-founder|owner|principal|head|lead|chair(?:man|woman|person)?|president|vice[- ]president|vp|secretary|"
    r"administrator|assistant|advis[eo]r|surveyor|accountant|agent|negotiator|engineer|architect|developer|"
    r"designer|specialist|coordinator|co-ordinator|supervisor|technician|receptionist|apprentice|dentist|doctor|"
    r"nurse|therapist|practitioner|paralegal|barrister|clerk|broker|analyst|controller|trustee|proprietor|"
    r"managing|senior|junior|chief|lecturer|teacher|headteacher|bursar|registrar|valuer|planner|stylist|"
    r"mortgage|conveyancer|legal|sales|marketing|finance|operations|hr|recruiter|editor|producer|"
    r"geschäftsführer|directeur|directrice|gérant|responsable|inhaber)\b", re.I)
_DEPT_LABEL = re.compile(
    r"(sales|accounts?|support|enquiries|enquiry|lettings|marketing|hr|human resources|careers|recruitment|press|media|"
    r"billing|customer (?:service|care)|reception|bookings?|admissions|general enquiries|technical support|"
    r"accounts payable|finance|legal|compliance|orders|helpdesk|help desk)"
    r"(?:\s+(?:department|team|enquiries|enquiry))?\s*(?:e-?mail)?\s*[:\-–—]\s*$", re.I)
_NAME_TOKEN = re.compile(r"^[^\W\d_](?:[^\W\d_]|['’\-.])*$", re.UNICODE)
_PARTICLES = {"van", "von", "de", "del", "della", "di", "da", "dos", "das", "du", "le", "la", "el", "al", "bin", "ibn",
              "ter", "ten", "der", "den", "st", "st.", "mac", "op", "zu", "af", "ap", "ben", "bint", "y", "e"}


def clean_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip()


def looks_like_job_title(s: str | None) -> bool:
    if not s:
        return False
    s = clean_ws(s)
    return 2 <= len(s) <= 90 and "@" not in s and sum(c.isdigit() for c in s) <= 2 and bool(_TITLE_WORDS.search(s))


def looks_like_person_name(s: str | None) -> bool:
    """Conservative: 2-5 alphabetic tokens, capitalised, no UI/role/company words, no digits."""
    if not s:
        return False
    s = clean_ws(s).strip(",;:|-–—")
    if not (3 <= len(s) <= 60) or "@" in s or any(c.isdigit() for c in s):
        return False
    toks = s.replace(",", " ").split()
    if toks and toks[0].lower().rstrip(".") in _HONORIFICS:
        toks = toks[1:]
    if not (2 <= len(toks) <= 5):
        return False
    caps = 0
    for i, t in enumerate(toks):
        tl = t.lower().strip(".,")
        if tl in _NAME_STOP and tl not in _PARTICLES:
            return False
        if _TITLE_WORDS.fullmatch(tl):
            return False
        if not _NAME_TOKEN.match(t):
            return False
        if not t[0].islower():  # capitalised, or caseless script (CJK, Arabic ...)
            caps += 1
        elif not (i > 0 and tl in _PARTICLES):
            return False
    return caps >= 2


_PHONE_LABELLED = re.compile(
    r"(?<![A-Za-z])(?:tel(?:ephone)?|phone|call(?:\s+us)?|mobile|mob|direct(?:\s+dial)?|dd|t|m|p)\s*[:.\-]\s*"
    r"((?:\+|00)?\(?\d[\d\s().\-]{6,18}\d)", re.I)
_PHONE_LABEL_WORD = re.compile(
    r"(?<![A-Za-z])(?:tel(?:ephone)?|phone|call(?:\s+us)?|mobile|direct\s+dial)\s+((?:\+|00)?\(?\d[\d\s().\-]{6,18}\d)", re.I)
_PHONE_INTL = re.compile(r"(?<![\w+])(\+\d{1,3}[\s.\-]?\(?\d{1,4}\)?(?:[\s.\-]?\d{2,4}){2,4})(?!\d)")
_FAX_BEFORE = re.compile(r"fax\s*[:.\-]?\s*$", re.I)


def normalise_phone_display(p: str) -> str:
    return clean_ws(p).strip(" .-")


def phone_key(p: str | None) -> str | None:
    """Comparable key: last 10 digits (handles +44 vs 0 trunk prefix). None if too short."""
    if not p:
        return None
    digits = re.sub(r"\D", "", p)
    if digits.startswith("00"):
        digits = digits[2:]
    return digits[-10:] if len(digits) >= 9 else None


def find_phones(text: str, *, limit: int = 8) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for rx in (_PHONE_LABELLED, _PHONE_LABEL_WORD, _PHONE_INTL):
        for m in rx.finditer(text):
            if _FAX_BEFORE.search(text[max(0, m.start() - 8): m.start(1)]):
                continue
            raw = normalise_phone_display(m.group(1))
            digits = re.sub(r"\D", "", raw)
            if not (8 <= len(digits) <= 15):
                continue
            if re.fullmatch(r"(?:19|20)\d{2}[\s.\-]+(?:19|20)\d{2}", raw):
                continue
            k = phone_key(raw)
            if k and k not in seen:
                seen.add(k)
                out.append(raw)
                if len(out) >= limit:
                    return out
    return out


_CN_PREFIX = r"(?:SC|NI|OC|SO|NC|RC|IP|LP|SL|FC|GE|OE|CE|ZC|SE|NF|R0|IC|SI|NP|SA)?"
_CN_NUM = rf"({_CN_PREFIX}\s?\d{{5,8}})"
_COMPANY_NO_RES = [
    re.compile(r"(?:company|co\.?|registered|registration|reg\.?)\s*(?:company\s*)?(?:registration\s*)?(?:reg\.?\s*)?"
               r"(?:no\.?|number|num\.?|#)\s*[:.\-]?\s*" + _CN_NUM + r"\b", re.I),
    re.compile(r"registered\s+in\s+(?:england|scotland|wales|northern\s+ireland|ireland)(?:\s*(?:and|&)\s*wales)?"
               r"[\s,.:;\-]*(?:under\s+|with\s+|as\s+)?(?:company\s+)?(?:(?:no\.?|number|num\.?|#)\s*)?[:.]?\s*" + _CN_NUM + r"\b", re.I),
    re.compile(r"\b(HRB|HRA)\s?(\d{3,7})\b"),
    re.compile(r"\b(?:SIREN|siren)\s*[:.]?\s*(\d{3}\s?\d{3}\s?\d{3})\b"),
]


def normalise_company_number(num: str) -> str:
    return re.sub(r"[\s\-.]", "", num).upper()


def find_company_numbers(text: str, limit: int = 5) -> list[str]:
    out: list[str] = []
    for i, rx in enumerate(_COMPANY_NO_RES):
        for m in rx.finditer(text):
            val = normalise_company_number(m.group(1) if i < 2 or i == 3 else m.group(1) + m.group(2))
            if i in (0, 1) and not re.search(r"\d", val):
                continue
            if val not in out:
                out.append(val)
            if len(out) >= limit:
                return out
    return out


_UK_POSTCODE = re.compile(r"\b([A-Z]{1,2}[0-9][A-Z0-9]?)\s?([0-9][A-Z]{2})\b")


def find_postcodes(text: str, limit: int = 8) -> list[str]:
    out: list[str] = []
    for m in _UK_POSTCODE.finditer(text):
        pc = f"{m.group(1)} {m.group(2)}"
        if pc not in out:
            out.append(pc)
            if len(out) >= limit:
                break
    return out


# --------------------------------------------------------------------------- JSON-LD
_ORG_SUFFIXES = ("organization", "organisation", "business", "store", "shop", "restaurant", "hotel", "agent", "service",
                 "office", "practice", "clinic", "dentist", "physician", "lawfirm", "attorney", "school", "university",
                 "club", "center", "centre", "company", "corporation", "ngo", "bank", "pharmacy", "salon", "garage")
_JSONLD_CLEAN_COMMA = re.compile(r",\s*([}\]])")


def _loads_lenient(s: str):
    s = s.strip()
    s = re.sub(r"^\s*(?://)?\s*<!\[CDATA\[", "", s)
    s = re.sub(r"(?://)?\s*\]\]>\s*$", "", s).strip()
    s = re.sub(r"^<!--|-->$", "", s).strip()
    for candidate in (s, _JSONLD_CLEAN_COMMA.sub(r"\1", s)):
        try:
            return json.loads(candidate, strict=False)
        except (ValueError, RecursionError):
            continue
    return None


def _as_list(v) -> list:
    return v if isinstance(v, list) else ([] if v is None else [v])


def _s(v) -> str | None:
    if isinstance(v, str):
        v = clean_ws(html_lib.unescape(v))
        return v or None
    if isinstance(v, int | float) and not isinstance(v, bool):
        return str(v)
    return None


def _types(d: dict) -> list[str]:
    return [str(t).lower().rsplit("/", 1)[-1] for t in _as_list(d.get("@type")) if isinstance(t, str)]


def _is_org_type(types: list[str]) -> bool:
    return any(t.endswith(_ORG_SUFFIXES) or t in {"professionalservice", "medicalbusiness", "foodestablishment"}
               for t in types)


def _parse_address(addr, meta: OrgMetadata) -> None:
    for a in _as_list(addr):
        if isinstance(a, str):
            if not meta.street:
                meta.street = _s(a)
        elif isinstance(a, dict):
            meta.street = meta.street or _s(a.get("streetAddress"))
            meta.locality = meta.locality or _s(a.get("addressLocality"))
            meta.region = meta.region or _s(a.get("addressRegion"))
            meta.postcode = meta.postcode or _s(a.get("postalCode"))
            c = a.get("addressCountry")
            meta.country = meta.country or (_s(c) if not isinstance(c, dict) else _s(c.get("name")))


def _strip_mailto(v: str) -> str:
    v = clean_ws(unquote(v))
    if v.lower().startswith("mailto:"):
        v = v[7:]
    return v.split("?", 1)[0].strip()


def _walk_jsonld(node, page_url: str, orgs: list[OrgMetadata], obs: list[EmailObservation], depth: int = 0,
                 budget: list[int] | None = None, parent_org: str | None = None) -> None:
    budget = budget if budget is not None else [3000]
    if depth > 8 or budget[0] <= 0:
        return
    budget[0] -= 1
    if isinstance(node, list):
        for x in node:
            _walk_jsonld(x, page_url, orgs, obs, depth + 1, budget, parent_org)
        return
    if not isinstance(node, dict):
        return
    types = _types(node)
    name = _s(node.get("name"))
    is_org = _is_org_type(types)
    is_person = "person" in types
    is_cp = "contactpoint" in types
    this_org = parent_org
    if is_org:
        meta = OrgMetadata(source="jsonld", name=name, legal_name=_s(node.get("legalName")), schema_type=types[0],
                           url=_s(node.get("url")))
        meta.telephone = [t for t in (_s(x) for x in _as_list(node.get("telephone"))) if t]
        meta.email = [e for e in (_strip_mailto(x) for x in _as_list(node.get("email")) if isinstance(x, str)) if e]
        _parse_address(node.get("address"), meta)
        loc = node.get("location")
        for lo in _as_list(loc):
            if isinstance(lo, dict):
                _parse_address(lo.get("address"), meta)
        meta.same_as = [u for u in (_s(x) for x in _as_list(node.get("sameAs"))) if u][:20]
        meta.vat_id = _s(node.get("vatID")) or _s(node.get("taxID"))
        for ident in _as_list(node.get("identifier")) + _as_list(node.get("iso6523Code")):
            if isinstance(ident, dict):
                pid = (_s(ident.get("propertyID")) or _s(ident.get("name")) or "").lower()
                if any(k in pid for k in ("company", "registration", "companies house")):
                    meta.company_number = normalise_company_number(_s(ident.get("value")) or "") or None
        orgs.append(meta)
        this_org = name or parent_org
    # e-mail attached to this node (organisation, person, contact point or anything else)
    for raw in _as_list(node.get("email")):
        if not isinstance(raw, str):
            continue
        email = clean_email(_strip_mailto(raw))
        if not email:
            continue
        phone = None
        tels = [t for t in (_s(x) for x in _as_list(node.get("telephone"))) if t]
        if tels and (is_person or is_cp or is_org):
            phone = tels[0]
        ctx_bits = ["JSON-LD", types[0] if types else "node"] + ([name] if name else [])
        obs.append(EmailObservation(
            email=email, method="jsonld", page_url=page_url, context=": ".join(ctx_bits[:2]) + (f" ({name})" if name else ""),
            name=name if is_person and looks_like_person_name(name) else None,
            title=_s(node.get("jobTitle")) if is_person else None,
            department=_s(node.get("contactType")) if is_cp else None,
            phone=phone))
    for key, val in node.items():
        if key == "@graph" or isinstance(val, dict | list):
            _walk_jsonld(val, page_url, orgs, obs, depth + 1, budget, this_org)


# --------------------------------------------------------------------------- vCard
def _unfold_vcard(text: str) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"=\n(?=[ \t]?\S)", "", text) if "QUOTED-PRINTABLE" in text.upper() else text
    return re.sub(r"\n[ \t]", "", text).split("\n")


def _vc_unescape(v: str) -> str:
    return clean_ws(v.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\"))


def extract_from_vcard(text: str, page_url: str) -> list[EmailObservation]:
    """Parse vCard 2.1/3.0/4.0 text (``.vcf`` or an inline block). Never raises."""
    out: list[EmailObservation] = []
    try:
        lines = _unfold_vcard(text[:2_000_000])
    except Exception:
        return out
    card: dict | None = None
    for line in lines:
        s = line.strip()
        if not s:
            continue
        up = s.upper()
        if up == "BEGIN:VCARD":
            card = {"emails": [], "tel": []}
            continue
        if up == "END:VCARD":
            if card:
                name = card.get("fn") or card.get("n")
                if name and not looks_like_person_name(name):
                    name = None if card.get("org") and clean_ws(name).lower() == clean_ws(card["org"]).lower() else name
                    if name and not looks_like_person_name(name):
                        name = None
                for e in card["emails"]:
                    out.append(EmailObservation(
                        email=e, method="vcard", page_url=page_url,
                        context=f"vCard{(': ' + (card.get('fn') or '')) if card.get('fn') else ''}"[:200],
                        name=name, title=card.get("title") or card.get("role"),
                        department=card.get("dept"), phone=card["tel"][0] if card["tel"] else None))
            card = None
            continue
        if card is None or ":" not in s:
            continue
        head, _, val = s.partition(":")
        prop_full = head.split(";")[0]
        prop = prop_full.split(".")[-1].upper()
        params = head.upper()
        if "QUOTED-PRINTABLE" in params:
            try:
                val = quopri.decodestring(val.encode("latin-1", "ignore")).decode("utf-8", "replace")
            except Exception:
                pass
        if prop == "EMAIL":
            e = clean_email(_vc_unescape(val).replace("mailto:", ""))
            if e and e not in card["emails"]:
                card["emails"].append(e)
        elif prop == "FN":
            card["fn"] = _vc_unescape(val)
        elif prop == "N":
            parts = (val.split(";") + [""] * 5)[:5]
            fam, giv = _vc_unescape(parts[0]), _vc_unescape(parts[1])
            card["n"] = clean_ws(f"{giv} {fam}") if giv or fam else None
        elif prop == "TITLE":
            card["title"] = _vc_unescape(val)[:200]
        elif prop == "ROLE":
            card["role"] = _vc_unescape(val)[:200]
        elif prop == "ORG":
            segs = val.split(";")
            card["org"] = _vc_unescape(segs[0])
            if len(segs) > 1 and segs[1].strip():
                card["dept"] = _vc_unescape(segs[1])
        elif prop == "TEL" and "FAX" not in params:
            t = normalise_phone_display(_vc_unescape(val).replace("tel:", ""))
            if t:
                card["tel"].append(t)
    return out


# --------------------------------------------------------------------------- link ranking
_BINARY_EXT = re.compile(
    r"\.(?:jpe?g|png|gif|svg|webp|avif|ico|bmp|css|js|mjs|json|woff2?|ttf|eot|mp[34]|webm|mov|avi|zip|gz|rar|7z|tar|"
    r"docx?|xlsx?|pptx?|csv|exe|dmg|apk|iso|rss|atom)(?:$|\?)", re.I)
_RANK_RULES: list[tuple[int, str, re.Pattern[str]]] = [
    (100, "contact", re.compile(r"contact|get[-_ ]?in[-_ ]?touch|enquir|reach[-_ ]?us|write[-_ ]?to[-_ ]?us|kontakt|contacto|contatti|contactez|nous[-_ ]?contacter|talk[-_ ]?to[-_ ]?us|email[-_ ]?us", re.I)),
    (92, "imprint", re.compile(r"impressum|imprint|legal[-_ ]?notice|mentions[-_ ]?l[eé]gales|aviso[-_ ]?legal|colophon", re.I)),
    (85, "team", re.compile(r"\bteam\b|/team|our[-_ ]?people|\bpeople\b|\bstaff\b|meet[-_ ]?(?:the|our|us)|who[-_ ]?we[-_ ]?are|leadership|management|directors|partners|attorneys|lawyers|solicitors|doctors|professionals|practitioners|consultants|advisers|advisors|our[-_ ]?experts|colleagues|mitarbeiter|equipe|[ée]quipe", re.I)),
    (72, "locations", re.compile(r"location|branch|office|find[-_ ]?us|stores?\b|where[-_ ]?we[-_ ]?are|directions|visit[-_ ]?us|our[-_ ]?sites|standorte|agences?", re.I)),
    (62, "about", re.compile(r"about|company|who[-_ ]?we|our[-_ ]?story|[uü]ber[-_ ]?uns|qui[-_ ]?sommes|chi[-_ ]?siamo|quienes", re.I)),
    (36, "privacy", re.compile(r"privacy|data[-_ ]?protection|gdpr|datenschutz|confidentialit", re.I)),
    (30, "complaints", re.compile(r"complaint|accessib", re.I)),
    (16, "terms", re.compile(r"terms|conditions|legal", re.I)),
]
_NEGATIVE = re.compile(r"login|log-in|signin|sign-in|register|cart|basket|checkout|account|wp-admin|wp-login|/tag/|/category/|/feed|/search|logout|password|/cdn-cgi/|replytocom|\?s=|/author/|/page/\d+|/20\d\d/\d\d/", re.I)
_KEYWORD_SCORE = 52


def rank_internal_links(
    links: Iterable[PageLink | str],
    base_url: str,
    *,
    keywords: Iterable[str] = (),
    is_same_site: Callable[[str], bool] | None = None,
    limit: int | None = 40,
    include_documents: bool = True,
) -> list[RankedLink]:
    """Score same-site links by how likely they are to carry contact details (best first)."""
    kws = [k.lower() for k in keywords if k and k.strip()]
    base_host = strip_www(host_of(base_url) or "")
    if is_same_site is None:
        def is_same_site(u: str) -> bool:
            return strip_www(host_of(u) or "") == base_host
    best: dict[str, RankedLink] = {}
    for item in links:
        url, text = (item.url, item.text) if isinstance(item, PageLink) else (item, "")
        norm = normalise_url(urljoin(base_url, url))
        if not norm or not is_same_site(norm):
            continue
        parts = urlsplit(norm)
        path_l = parts.path.lower()
        is_doc = path_l.endswith((".pdf", ".vcf"))
        if _BINARY_EXT.search(norm) and not is_doc:
            continue
        if is_doc and not include_documents:
            continue
        hay = f"{path_l} {text.lower()}"
        if _NEGATIVE.search(norm.lower()):
            continue
        score, reason = 0, ""
        for sc, why, rx in _RANK_RULES:
            if rx.search(hay):
                score, reason = sc, why
                break
        if kws and score < _KEYWORD_SCORE and any(k in hay for k in kws):
            score, reason = _KEYWORD_SCORE, "keyword"
        if is_doc:
            score = max(score, 40) if score else 40
            reason = reason or "document"
            if path_l.endswith(".vcf"):
                score, reason = max(score, 80), "vcard"
        if score <= 0:
            continue
        depth = len([p for p in parts.path.split("/") if p])
        score -= max(0, depth - 2) * 4
        if parts.query:
            score -= 6
        score = max(score, 1)
        cur = best.get(norm)
        if cur is None or score > cur.score:
            best[norm] = RankedLink(norm, score, reason)
    ranked = sorted(best.values(), key=lambda r: (-r.score, len(r.url), r.url))
    return ranked[:limit] if limit else ranked


# --------------------------------------------------------------------------- HTML extraction
_WS = re.compile(r"\s+")
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?:px)?\s*(?:;|$)", re.I)
_INLINE_TAGS = {"a", "span", "strong", "em", "b", "i", "small", "label", "font", "u", "abbr", "br", "sup", "sub"}
_CARD_TAGS = {"li", "article", "tr", "dd", "dt", "address", "figure", "section", "div", "p", "td", "th", "aside", "blockquote", "footer", "header"}
_CARD_CLASS = re.compile(r"card|member|team|staff|person|profile|employee|bio|people|vcard|contact|author|agent|advisor|adviser|partner|doctor|lawyer|attorney|solicitor|entry|item|col|box|tile|grid", re.I)
_NAME_CLASS = re.compile(r"(?:^|[\s_-])(?:name|fn|person-name|member-name|staff-name|employee-name|full-?name|team-name|agent-name)(?:$|[\s_-])", re.I)
_TITLE_CLASS = re.compile(r"(?:^|[\s_-])(?:title|job|jobtitle|job-title|position|role|designation|member-role|staff-title|team-role|occupation)(?:$|[\s_-])", re.I)
_DEPT_CLASS = re.compile(r"depart|division", re.I)
_PHONE_CLASS = re.compile(r"(?:^|[\s_-])(?:tel|phone|telephone|mobile)(?:$|[\s_-])", re.I)
_HEADER_KIND = [
    ("name", re.compile(r"^(?:full\s*)?name|^person|^contact$|^who", re.I)),
    ("title", re.compile(r"title|position|role|job|designation", re.I)),
    ("dept", re.compile(r"depart|division|team$|area", re.I)),
    ("email", re.compile(r"e-?mail", re.I)),
    ("phone", re.compile(r"phone|tel|mobile|direct", re.I)),
]
_METHOD_PRIORITY = ["jsonld", "vcard", "table", "mailto", "text", "obfuscated", "pdf"]


class _Found:
    __slots__ = ("email", "method", "node", "ctx", "name", "title", "dept", "phone", "chain", "label_dept")

    def __init__(self, email: str, method: str, node, ctx: str = "", label_dept: str | None = None) -> None:
        self.email, self.method, self.node, self.ctx = email, method, node, ctx
        self.name = self.title = self.dept = self.phone = None
        self.chain: list = []
        self.label_dept = label_dept


def _is_hidden(node) -> bool:
    n = node
    for _ in range(40):
        if n is None:
            return False
        try:
            a = n.attributes
        except Exception:
            return False
        if "hidden" in a or a.get("aria-hidden") == "true" or (a.get("style") and _HIDDEN_STYLE.search(a["style"])):
            return True
        n = n.parent
    return False


def _text_of(node, limit: int = 400) -> str:
    try:
        return clean_ws(node.text(separator=" ", strip=True))[:limit]
    except Exception:
        return ""


def _snippet(node, email: str) -> str:
    n = node
    for _ in range(4):
        if n is None or n.tag in ("body", "html"):
            break
        t = _text_of(n, 600)
        if len(t) >= len(email) and len(t) <= 300:
            return t[:220]
        n = n.parent
    return _text_of(node, 220)


def _first_text(root, selector: str, check: Callable[[str], bool] | None = None, skip_in_nested_emails: bool = True):
    try:
        nodes = root.css(selector)
    except Exception:
        return None
    for nd in nodes:
        t = _text_of(nd, 200)
        if t and (check is None or check(t)):
            return t
    return None


def _cell_text(cell) -> str:
    return _text_of(cell, 200)


def _header_map(table) -> dict[int, str]:
    """Map column index -> kind from the first header row."""
    if table is None:
        return {}
    try:
        row = table.css_first("thead tr") or next((r for r in table.css("tr") if r.css_first("th")), None)
    except Exception:
        row = None
    if row is None:
        return {}
    out: dict[int, str] = {}
    for i, c in enumerate(c for c in row.iter() if c.tag in ("th", "td")):
        t = _cell_text(c)
        for kind, rx in _HEADER_KIND:
            if rx.search(t):
                out[i] = kind
                break
    return out


def _card_info(A, item: _Found) -> bool:
    """Try to find name/title/dept/phone evidence inside container A. True when accepted."""
    tag = A.tag
    attrs = A.attributes
    cls = " ".join(filter(None, [attrs.get("class") or "", attrs.get("itemprop") or "", attrs.get("id") or ""]))
    card_like = tag in ("article", "li", "tr", "dl", "address") or bool(_CARD_CLASS.search(cls)) or "itemscope" in attrs
    name = title = dept = phone = None
    method_override = None
    if tag == "tr":
        method_override = "table"
        cells = [c for c in A.iter() if c.tag in ("td", "th")]
        table = A.parent
        while table is not None and table.tag != "table":
            table = table.parent
        hm = _header_map(table)
        if hm:
            for i, c in enumerate(cells):
                kind = hm.get(i)
                t = _cell_text(c)
                if kind == "name" and looks_like_person_name(t):
                    name = t
                elif kind == "title" and t:
                    title = t[:120]
                elif kind == "dept" and t:
                    dept = t[:120]
                elif kind == "phone" and t:
                    phone = (find_phones("Tel: " + t) or [None])[0]
        else:
            for c in cells:
                t = _cell_text(c)
                if name is None and looks_like_person_name(t):
                    name = t
                elif title is None and looks_like_job_title(t):
                    title = t
            if name and not title:
                name = None  # header-less rows need name + title evidence
    else:
        explicit = False
        name_nodes = []
        try:
            name_nodes = A.css('[itemprop="name"]') + [n for n in A.css("[class]") if _NAME_CLASS.search(n.attributes.get("class") or "")]
        except Exception:
            pass
        for n in name_nodes:
            t = _text_of(n, 120)
            if looks_like_person_name(t):
                name, explicit, anchor = t, True, n
                break
        heading_based = False
        if name is None:
            for n in A.css("h1, h2, h3, h4, h5, h6, strong, b, dt, th"):
                t = _text_of(n, 120)
                if looks_like_person_name(t):
                    name, anchor, heading_based = t, n, True
                    break
        if name:
            for n in A.css('[itemprop="jobTitle"], [class]'):
                a = n.attributes
                if n.mem_id == anchor.mem_id:
                    continue
                if (a.get("itemprop") == "jobTitle") or _TITLE_CLASS.search(a.get("class") or ""):
                    t = _text_of(n, 120)
                    if t and not looks_like_person_name(t) and "@" not in t:
                        title = t
                        break
            if title is None:
                sib = anchor.next
                hops = 0
                while sib is not None and hops < 3:
                    if sib.tag in ("p", "span", "em", "div", "small", "h4", "h5", "h6", "dd", "i", "li"):
                        t = _text_of(sib, 120)
                        if t and "@" not in t and looks_like_job_title(t):
                            title = t
                            break
                    hops += 1
                    sib = sib.next
            if tag == "dl":  # definition list: only trust when dt/dd adjacency is clear
                method_override = "table"
            if not (explicit or title or (heading_based and card_like)):
                name = title = None
        if tag == "dd" and name is None:
            # <dt>Name</dt><dd>title</dd><dd><a mailto></dd>
            sib = A.prev
            hops = 0
            while sib is not None and hops < 4:
                if sib.tag == "dt":
                    t = _text_of(sib, 120)
                    if looks_like_person_name(t):
                        name = t
                        method_override = "table"
                    break
                if sib.tag == "dd":
                    t = _text_of(sib, 120)
                    if title is None and looks_like_job_title(t):
                        title = t
                sib = sib.prev
                hops += 1
    if not name:
        return False
    # department / phone (only reached when container qualifies)
    if dept is None:
        try:
            dn = A.css_first('[itemprop="department"]') or next(
                (n for n in A.css("[class]") if _DEPT_CLASS.search(n.attributes.get("class") or "")), None)
            if dn is not None:
                dept = _text_of(dn, 100) or None
        except Exception:
            pass
    if phone is None:
        try:
            tel = A.css_first('a[href^="tel:"]')
            if tel is not None:
                phone = normalise_phone_display(unquote(tel.attributes.get("href", "")[4:]))
            else:
                pn = next((n for n in A.css("[class]") if _PHONE_CLASS.search(n.attributes.get("class") or "")), None)
                phone = (find_phones("Tel: " + _text_of(pn, 80)) or [None])[0] if pn is not None else None
                if phone is None:
                    phone = (find_phones(_text_of(A, 500)) or [None])[0]
        except Exception:
            phone = None
    item.name, item.title, item.dept, item.phone = name, title, dept, phone
    if method_override:
        item.method = method_override
    return True


def _associate(items: list[_Found]) -> None:
    anc: dict[int, set[str]] = {}
    for it in items:
        n = it.node
        chain = []
        for _ in range(8):
            if n is None or n.tag in ("body", "html", "-document", "#document"):
                break
            chain.append(n)
            anc.setdefault(n.mem_id, set()).add(it.email.lower())
            n = n.parent
        it.chain = chain
    for it in items:
        for A in it.chain:
            if len(anc[A.mem_id]) > 1:
                break
            if A.tag in _INLINE_TAGS:
                continue
            if A.tag not in _CARD_TAGS and A.tag not in ("dl", "ul", "ol", "table", "tbody"):
                continue
            if len(_text_of(A, 900)) > 800:
                break
            if _card_info(A, it):
                break


def _parse_html(html: str):
    if _HTMLParser is None:
        return None
    try:
        return _HTMLParser(html)
    except Exception:
        try:  # lxml can repair markup selectolax chokes on
            import lxml.html as lh

            root = lh.fromstring(html)
            return _HTMLParser(lh.tostring(root, encoding="unicode"))
        except Exception:
            return None


def _meta_content(tree, selector: str) -> str | None:
    n = tree.css_first(selector)
    if n is None:
        return None
    v = n.attributes.get("content")
    return clean_ws(v)[:300] if v else None


def _merge_observations(found: list[EmailObservation]) -> list[EmailObservation]:
    by: dict[str, list[EmailObservation]] = {}
    order: list[str] = []
    for o in found:
        k = o.email.lower()
        if k not in by:
            by[k] = []
            order.append(k)
        by[k].append(o)
    out: list[EmailObservation] = []
    for k in order:
        group = sorted(by[k], key=lambda o: _METHOD_PRIORITY.index(o.method) if o.method in _METHOD_PRIORITY else 99)
        prim = group[0]
        merged = EmailObservation(email=prim.email, method=prim.method, page_url=prim.page_url, context=prim.context)
        for o in group:
            merged.name = merged.name or o.name
            merged.title = merged.title or o.title
            merged.department = merged.department or o.department
            merged.phone = merged.phone or o.phone
            merged.context = merged.context or o.context
        merged.also_methods = tuple(sorted({o.method for o in group} - {prim.method}))
        out.append(merged)
    return out


def extract_from_html(html: str, page_url: str, *, company_hint: str | None = None) -> PageExtraction:
    """Extract e-mail observations and identity signals from one HTML document. Never raises."""
    ex = PageExtraction(page_url=page_url)
    if not html:
        return ex
    try:
        _extract_html_into(ex, html, page_url, company_hint)
    except Exception as e:  # defensive: a broken page must never break the crawl
        log.debug("extract_from_html failed for %s: %s", page_url, e)
    return ex


def _extract_html_into(ex: PageExtraction, html: str, page_url: str, company_hint: str | None) -> None:
    tree = _parse_html(html)
    if tree is None:
        return _extract_html_lxml_fallback(ex, html, page_url)
    raw: list[EmailObservation] = []
    items: list[_Found] = []

    # --- document metadata
    t = tree.css_first("title")
    ex.title = clean_ws(t.text())[:300] if t is not None and t.text() else None
    ex.meta_description = _meta_content(tree, 'meta[name="description"]')
    ex.site_name = _meta_content(tree, 'meta[property="og:site_name"]') or _meta_content(tree, 'meta[name="application-name"]')
    ex.robots_meta = _meta_content(tree, 'meta[name="robots"]')
    can = tree.css_first('link[rel="canonical"]')
    ex.canonical = can.attributes.get("href") if can is not None else None
    root = tree.css_first("html")
    ex.lang = (root.attributes.get("lang") if root is not None else None) or None
    ex.h1 = [x for x in (clean_ws(n.text(separator=" ", strip=True))[:200] for n in tree.css("h1")[:5]) if x]
    base_node = tree.css_first("base[href]")
    base_url = urljoin(page_url, base_node.attributes["href"]) if base_node is not None else page_url

    # --- JSON-LD (before stripping scripts)
    orgs: list[OrgMetadata] = []
    ld_obs: list[EmailObservation] = []
    scripts = tree.css("script")
    ex.script_count = len(scripts)
    for s in scripts:
        if (s.attributes.get("type") or "").lower().startswith("application/ld+json"):
            data = _loads_lenient(s.text() or "")
            if data is not None:
                _walk_jsonld(data, page_url, orgs, ld_obs)
    raw.extend(ld_obs)

    # --- vCards embedded in the page source
    if "BEGIN:VCARD" in html.upper():
        plain = html_lib.unescape(re.sub(r"<[^>]+>", "", re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", html)))
        raw.extend(extract_from_vcard(plain, page_url))

    tree.strip_tags(["script", "style", "noscript", "template", "svg", "iframe", "canvas"])
    body = tree.body or tree.root
    if body is None:
        return
    try:
        body.merge_text_nodes()
    except Exception:
        pass

    # --- anchors: links, mailto, tel, Cloudflare
    links: dict[str, PageLink] = {}
    tel_links: list[str] = []
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href:
            continue
        low = href[:12].lower()
        if low.startswith("mailto:"):
            if _is_hidden(a):
                continue
            addr_part = unquote(href[7:]).split("?", 1)[0]
            for piece in re.split(r"[,;]", addr_part):
                e = clean_email(html_lib.unescape(piece))
                if e:
                    items.append(_Found(e, "mailto", a))
        elif low.startswith("tel:"):
            p = normalise_phone_display(unquote(href[4:]).split(";")[0])
            if p and p not in tel_links and len(re.sub(r"\D", "", p)) >= 7:
                tel_links.append(p)
        elif "/cdn-cgi/l/email-protection" in href:
            h = href.split("#", 1)[1] if "#" in href else ""
            e = decode_cfemail(h) if h else None
            e = clean_email(e) if e else None
            if e and not _is_hidden(a):
                items.append(_Found(e, "obfuscated", a))
        elif low.startswith(("javascript:", "data:", "#", "sms:", "whatsapp:")):
            continue
        else:
            if len(links) < 400:
                try:
                    absu = normalise_url(urljoin(base_url, href))
                except Exception:
                    absu = None
                if absu and absu not in links:
                    txt = clean_ws(a.text(separator=" ", strip=True))[:120]
                    if not txt:
                        txt = clean_ws(a.attributes.get("title") or a.attributes.get("aria-label") or "")[:120]
                    rel = (a.attributes.get("rel") or "").lower()
                    links[absu] = PageLink(absu, txt, "nofollow" in rel)
    for n in tree.css("[data-cfemail]"):
        e = decode_cfemail(n.attributes.get("data-cfemail") or "")
        e = clean_email(e) if e else None
        if e and not _is_hidden(n):
            items.append(_Found(e, "obfuscated", n))
    ex.links = list(links.values())

    # --- microdata
    for n in tree.css('[itemprop="email"]'):
        a = n.attributes
        v = a.get("content") or a.get("href") or n.text()
        e = clean_email(_strip_mailto(v or ""))
        if e and not _is_hidden(n):
            scope = n.parent
            while scope is not None and "itemscope" not in scope.attributes:
                scope = scope.parent
            nm = title = ph = None
            if scope is not None:
                nn = scope.css_first('[itemprop="name"]')
                nm = _text_of(nn, 120) if nn is not None else None
                is_person = "person" in (scope.attributes.get("itemtype") or "").lower()
                nm = nm if is_person and looks_like_person_name(nm) else None
                jt = scope.css_first('[itemprop="jobTitle"]')
                title = _text_of(jt, 120) if jt is not None and is_person else None
                tl = scope.css_first('[itemprop="telephone"]')
                ph = _text_of(tl, 40) if tl is not None else None
            raw.append(EmailObservation(email=e, method="jsonld", page_url=page_url, context="microdata",
                                        name=nm, title=title, phone=ph))
    for scope in tree.css('[itemtype*="Organization" i], [itemtype*="LocalBusiness" i]')[:3]:
        meta = OrgMetadata(source="microdata", schema_type="Organization")
        for prop, attr in (("name", "name"), ("legalName", "legal_name"), ("postalCode", "postcode"),
                           ("streetAddress", "street"), ("addressLocality", "locality")):
            nn = scope.css_first(f'[itemprop="{prop}"]')
            if nn is not None:
                setattr(meta, attr, _text_of(nn, 150) or None)
        for nn in scope.css('[itemprop="telephone"]')[:3]:
            tt = _text_of(nn, 40)
            if tt:
                meta.telephone.append(tt)
        orgs.append(meta)

    # --- visible text + node-level text scan
    text = _WS.sub(" ", body.text(separator=" ", strip=True) or "")
    ex.text_length = len(text)
    ex.text_sample = text[:3000]
    low_text = text.lower()
    if "@" in text or any(h in low_text for h in _OBF_HINTS):
        for node in body.traverse(include_text=True):
            if node.tag != "-text":
                continue
            tv = node.text_content or ""
            if "@" not in tv and not any(h in tv.lower() for h in _OBF_HINTS):
                continue
            hits = scan_text_for_emails(tv)
            if not hits:
                continue
            parent = node.parent
            if parent is None or _is_hidden(parent):
                continue
            for e, method, pos in hits:
                dept = None
                if pos > 0:
                    mm = _DEPT_LABEL.search(tv[max(0, pos - 40): pos])
                    if mm:
                        dept = mm.group(1).strip().title()
                ctx = clean_ws(tv[max(0, pos - 70): pos + len(e) + 70])
                items.append(_Found(e, method, parent, ctx, dept))

    # --- footer / identity facts
    footer = None
    for sel in ("footer", '[role="contentinfo"]', "#footer", ".footer", "#site-footer", ".site-footer"):
        try:
            cand = tree.css(sel)
        except Exception:
            continue
        if cand:
            footer = cand[-1]
            break
    ex.footer_text = (_text_of(footer, 2500) if footer is not None else text[-1500:])[:2500]
    address_text = " ".join(_text_of(n, 300) for n in tree.css("address")[:5])
    id_text = f"{text[:60000]} {ex.footer_text}"
    ex.company_numbers = find_company_numbers(id_text)
    ex.postcodes = find_postcodes(f"{ex.footer_text} {address_text} {text[:30000]}")
    phones = list(tel_links)
    for p in find_phones(f"{address_text} {ex.footer_text}"):
        if phone_key(p) not in {phone_key(x) for x in phones}:
            phones.append(p)
    for o in orgs:
        for p in o.telephone:
            if phone_key(p) not in {phone_key(x) for x in phones}:
                phones.append(p)
    ex.phones = phones[:12]

    # --- associate names/titles/phones with addresses (conservative DOM proximity)
    if items:
        try:
            _associate(items)
        except Exception as e:
            log.debug("association failed on %s: %s", page_url, e)
    for it in items:
        if _is_hidden(it.node):
            continue
        ctx = it.ctx or _snippet(it.node, it.email)
        raw.append(EmailObservation(
            email=it.email, method=it.method, page_url=page_url, context=ctx[:220], name=it.name, title=it.title,
            department=it.dept or it.label_dept, phone=it.phone))

    ex.emails = _merge_observations(raw)
    if company_hint:
        hint = re.sub(r"\W+", "", company_hint.lower())
        orgs.sort(key=lambda o: 0 if hint and hint in re.sub(r"\W+", "", (o.name or o.legal_name or "").lower()) else 1)
        for o in ex.emails:  # a "person" that is really the company name is not a person
            if o.name and re.sub(r"\W+", "", o.name.lower()) == hint:
                o.name = None
    ex.org = orgs[:6]
    if ex.company_numbers and ex.org and not any(o.company_number for o in ex.org):
        ex.org[0].company_number = ex.org[0].company_number or None


def _extract_html_lxml_fallback(ex: PageExtraction, html: str, page_url: str) -> None:  # pragma: no cover - rarely used
    """Degraded path when selectolax is unavailable: mailto, text and JSON-LD only."""
    import lxml.html as lh

    root = lh.fromstring(html)
    raw: list[EmailObservation] = []
    t = root.find(".//title")
    ex.title = clean_ws(t.text_content())[:300] if t is not None else None
    orgs: list[OrgMetadata] = []
    for s in root.xpath('//script[@type="application/ld+json"]'):
        data = _loads_lenient(s.text_content())
        if data is not None:
            _walk_jsonld(data, page_url, orgs, raw)
    for a in root.xpath("//a[@href]"):
        href = a.get("href", "")
        if href.lower().startswith("mailto:"):
            e = clean_email(unquote(href[7:]).split("?")[0])
            if e:
                raw.append(EmailObservation(e, "mailto", page_url))
    for bad in root.xpath("//script|//style|//noscript"):
        bad.drop_tree()
    text = _WS.sub(" ", root.text_content())
    ex.text_sample, ex.text_length = text[:3000], len(text)
    for e, method, pos in scan_text_for_emails(text):
        raw.append(EmailObservation(e, method, page_url, context=text[max(0, pos - 60): pos + 100]))
    ex.emails = _merge_observations(raw)
    ex.org = orgs[:6]
    ex.company_numbers = find_company_numbers(text)
    ex.postcodes = find_postcodes(text)


# --------------------------------------------------------------------------- PDF
def extract_from_pdf_bytes(data: bytes, page_url: str, *, max_pages: int = 12, max_chars: int = 300_000) -> PageExtraction:
    """Extract addresses from a PDF using pypdf (bounded; broken/encrypted PDFs tolerated)."""
    ex = PageExtraction(page_url=page_url)
    if not data or not data.lstrip()[:8].startswith(b"%PDF"):
        return ex
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            try:
                if not reader.decrypt(""):  # only the empty user password; never guess/bypass
                    return ex
            except Exception:
                return ex
        raw: list[EmailObservation] = []
        chars = 0
        pages = reader.pages
        n_pages = min(len(pages), max_pages)
        texts: list[str] = []
        for i in range(n_pages):
            try:
                page = pages[i]
                tx = page.extract_text() or ""
                try:
                    for annot in page.get("/Annots") or []:
                        obj = annot.get_object()
                        action = obj.get("/A")
                        uri = action.get_object().get("/URI") if action is not None else None
                        if uri and str(uri).lower().startswith("mailto:"):
                            e = clean_email(unquote(str(uri)[7:]).split("?")[0])
                            if e:
                                raw.append(EmailObservation(e, "pdf", page_url, context=f"PDF link p.{i + 1}"))
                except Exception:
                    pass
            except Exception:
                continue
            tx = tx[: max(0, max_chars - chars)]
            chars += len(tx)
            texts.append(tx)
            for e, _m, pos in scan_text_for_emails(tx.replace("\n", " ")):
                ctx = clean_ws(tx[max(0, pos - 70): pos + len(e) + 70])
                raw.append(EmailObservation(e, "pdf", page_url, context=ctx[:220]))
            if chars >= max_chars:
                break
        full = " ".join(texts)
        ex.text_sample = clean_ws(full[:3000])
        ex.text_length = len(full)
        ex.company_numbers = find_company_numbers(full)
        ex.postcodes = find_postcodes(full)
        ex.phones = find_phones(full)
        ex.emails = _merge_observations(raw)
        meta = getattr(reader, "metadata", None)
        ex.title = clean_ws(str(meta.title))[:300] if meta is not None and getattr(meta, "title", None) else None
    except Exception as e:
        log.debug("pdf extraction failed for %s: %s", page_url, e)
    return ex


def unicode_fold(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
