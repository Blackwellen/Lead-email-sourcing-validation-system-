"""DISC-09: business signals (opportunity indicators) with mandatory provenance.

Deterministic, page-based detection only (JSON-LD ``JobPosting`` / ``Event`` / articles, careers and news pages,
accreditation keyword lists). Every signal keeps source URL, observed time, parser version and a confidence that follows
explicit rules:

* structured (JSON-LD) item with a machine-readable date ........ 0.80-0.90
* ``<time datetime>`` item + keyword rule ........................ 0.70
* date found in text + keyword rule ............................... 0.60
* careers page with job-like listing (no structured data) ......... 0.55
* keyword only, no date ............................................ 0.35-0.45
"""
from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import and_, func, literal_column, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import BusinessSignal, Organisation, RunOrganisation
from .htmlutil import clean, jsonld_nodes, node_types, parse, text_of, visible_text

PARSER_VERSION = "signals-1"
SIGNAL_TYPES = (
    "new_location", "new_service", "expansion", "hiring", "contract_award", "supplier_partnership", "event",
    "accreditation", "new_registration", "exhibitor", "sponsor",
)

# --------------------------------------------------------------------------- dates
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], 1)}
_MONTHS.update({k[:3]: v for k, v in list(_MONTHS.items())})
_MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?)?")
_DMY_TXT = re.compile(rf"(?<!\d)(\d{{1,2}})(?:st|nd|rd|th)?[\s,.-]+(?:of\s+)?{_MON}\.?,?\s+(\d{{4}})", re.I)
_MDY_TXT = re.compile(rf"{_MON}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})", re.I)
_DMY_NUM = re.compile(r"(?<!\d)(\d{1,2})[/.](\d{1,2})[/.](\d{4})(?!\d)")


def _mk(y: int, m: int, d: int, hh: int = 0, mm: int = 0, ss: int = 0, tz: str | None = None) -> datetime | None:
    try:
        dt = datetime(y, m, d, hh, mm, ss, tzinfo=UTC)
    except ValueError:
        return None
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        digits = tz[1:].replace(":", "")
        dt = dt - sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:4]))
    return dt


def parse_date(value: str | None, *, now: datetime | None = None) -> datetime | None:
    """Best-effort date parse (ISO, '4 March 2025', 'March 4, 2025', UK day-first 04/03/2025). UTC; None if implausible."""
    if not value:
        return None
    now = now or datetime.now(UTC)
    s = str(value)
    dt: datetime | None = None
    if m := _ISO.search(s):
        dt = _mk(int(m[1]), int(m[2]), int(m[3]), int(m[4] or 0), int(m[5] or 0), int(m[6] or 0), m[7])
    elif m := _DMY_TXT.search(s):
        dt = _mk(int(m[3]), _MONTHS[m[2].lower()[:3]], int(m[1]))
    elif m := _MDY_TXT.search(s):
        dt = _mk(int(m[3]), _MONTHS[m[1].lower()[:3]], int(m[2]))
    elif m := _DMY_NUM.search(s):
        a, b = int(m[1]), int(m[2])
        day, mon = (a, b) if b <= 12 else (b, a)  # UK day-first; swap only when unambiguous
        dt = _mk(int(m[3]), mon, day)
    if dt is None or dt.year < 1995 or dt.year > now.year + 3:
        return None
    return dt


# --------------------------------------------------------------------------- keyword rules
ACCREDITATIONS: list[tuple[str, re.Pattern[str]]] = [
    ("ISO 9001", re.compile(r"\bISO[\s-]?9001\b", re.I)), ("ISO 14001", re.compile(r"\bISO[\s-]?14001\b", re.I)),
    ("ISO 45001", re.compile(r"\bISO[\s-]?45001\b", re.I)), ("ISO 27001", re.compile(r"\bISO[\s-]?27001\b", re.I)),
    ("ISO 22000", re.compile(r"\bISO[\s-]?22000\b", re.I)), ("ISO 13485", re.compile(r"\bISO[\s-]?13485\b", re.I)),
    ("Gas Safe", re.compile(r"\bgas[\s-]?safe\b", re.I)), ("NICEIC", re.compile(r"\bniceic\b", re.I)),
    ("NAPIT", re.compile(r"\bnapit\b", re.I)), ("OFTEC", re.compile(r"\boftec\b", re.I)), ("HETAS", re.compile(r"\bhetas\b", re.I)),
    ("CHAS", re.compile(r"\bCHAS\b")), ("SafeContractor", re.compile(r"\bsafe[\s-]?contractor\b", re.I)),
    ("Constructionline", re.compile(r"\bconstructionline\b", re.I)), ("Cyber Essentials", re.compile(r"\bcyber essentials\b", re.I)),
    ("Investors in People", re.compile(r"\binvestors in people\b", re.I)), ("BAFE", re.compile(r"\bBAFE\b")),
    ("FENSA", re.compile(r"\bfensa\b", re.I)), ("TrustMark", re.compile(r"\btrustmark\b", re.I)),
    ("FCA authorised", re.compile(r"\b(?:fca|financial conduct authority)[\s-]+(?:authori[sz]ed|regulated)\b", re.I)),
    ("SRA regulated", re.compile(r"\b(?:sra|solicitors regulation authority)[\s-]+regulated\b", re.I)),
    ("RICS regulated", re.compile(r"\brics[\s-]+regulated\b", re.I)),
]
_ACCRED_CUE = re.compile(r"accredit|certif|registered|approved|member of|awarded|authori[sz]ed|regulated|holder", re.I)

KEYWORD_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("new_location", re.compile(
        r"\b(?:open(?:s|ed|ing)?|launch(?:es|ed)?|new)\b.{0,50}\b(?:branch|office|showroom|store|shop|depot|clinic|practice|studio|"
        r"premises|site|location|warehouse|hub)\b|\b(?:branch|office|showroom|store|depot|clinic|practice)\b.{0,40}\b(?:opening|opens|opened)\b|"
        r"\brelocat(?:e|es|ed|ion)\b|\bmoves? to new\b", re.I)),
    ("expansion", re.compile(
        r"\bexpan(?:d|ds|ded|ding|sion)\b|\bacquir(?:e|es|ed|ition)\b|\bmerger\b|\bnew headquarters\b|\binvest(?:s|ed|ment)\b.{0,30}\b(?:growth|expansion)\b|"
        r"\bgrowth\b.{0,30}\b(?:plans?|team|turnover)\b|\bdoubl(?:e|es|ed|ing) (?:its|our|the) (?:team|size|capacity)\b", re.I)),
    ("new_service", re.compile(
        r"\blaunch(?:es|ed|ing)?\b|\bintroduc(?:e|es|ed|ing)\b.{0,40}\b(?:service|range|product|offering)\b|\bnew (?:service|range|product|offering)\b|"
        r"\bnow (?:offer|offering|available)\b", re.I)),
    ("accreditation", re.compile(
        r"\baccredit|\bcertif(?:ied|ication)\b|\bawarded\b.{0,40}\b(?:iso|gas safe|niceic|napit|trustmark)\b|\bachiev(?:es|ed)\b.{0,30}\bISO\b", re.I)),
    ("supplier_partnership", re.compile(r"\bpartner(?:s|ed|ship)? with\b|\bnew partnership\b|\bappointed as\b.{0,30}\b(?:distributor|supplier|partner)\b", re.I)),
    ("contract_award", re.compile(r"\bwins?\b.{0,30}\bcontract\b|\bawarded\b.{0,30}\bcontract\b|\bcontract award", re.I)),
    ("sponsor", re.compile(r"\bsponsor(?:s|ed|ing|ship)?\b", re.I)),
]
_NEWS_URL = re.compile(r"/(?:news|press|blog|media|updates?|insights?|latest|announcements?|whats-new|articles?|stories)(?:/|$|\.|-)", re.I)
_JOBS_URL = re.compile(r"/(?:careers?|jobs?|vacanc(?:y|ies)|work-(?:with|for)-us|join-(?:us|our-team)|opportunities|recruitment)(?:/|$|\.|-)", re.I)
_EVENTS_URL = re.compile(r"/(?:events?|exhibitions?|webinars?|conferences?)(?:/|$|\.|-)", re.I)
_JOBLIKE = re.compile(r"\b(?:apply(?: now| online| here)?|vacanc(?:y|ies)|current (?:openings|vacancies)|we(?:'| a)re hiring|join our team|"
                      r"full[\s-]time|part[\s-]time|permanent|salary|£\s?\d)\b", re.I)


@dataclass
class DetectedSignal:
    signal_type: str
    title: str
    confidence: float
    source_url: str
    occurred_at: datetime | None = None
    detail: dict[str, Any] = field(default_factory=dict)


def classify_keywords(text_: str) -> str | None:
    for stype, rx in KEYWORD_RULES:
        if rx.search(text_):
            return stype
    return None


def accreditations_in(text_: str) -> list[str]:
    out = []
    for name, rx in ACCREDITATIONS:
        if (m := rx.search(text_)) and _ACCRED_CUE.search(text_[max(0, m.start() - 120): m.end() + 120]):
            out.append(name)
    return out


def _detail(method: str, evidence: str, **kw: Any) -> dict[str, Any]:
    d = {"method": method, "evidence": clean(evidence)[:300], "parser_version": PARSER_VERSION}
    d.update(kw)
    return d


def is_fresh(occurred_at: datetime | None, freshness_days: int | None, now: datetime | None = None) -> bool:
    """Dated signals older than ``freshness_days`` are stale; undated signals are kept (they were observed now)."""
    if freshness_days is None or occurred_at is None:
        return True
    now = now or datetime.now(UTC)
    return occurred_at >= now - timedelta(days=freshness_days)


# --------------------------------------------------------------------------- detection
def detect_signals_from_html(
    html: str, page_url: str, org: Any = None, *, now: datetime | None = None, freshness_days: int | None = None,
    types: list[str] | None = None, max_per_page: int = 25,
) -> list[DetectedSignal]:
    """Deterministic signals from ONE page. ``org`` (optional) is only recorded in the detail for traceability."""
    now = now or datetime.now(UTC)
    out: list[DetectedSignal] = []
    tree = parse(html)
    if tree is None:
        return out
    path = urlsplit(page_url).path or "/"
    org_name = (getattr(org, "legal_name", None) or getattr(org, "trading_name", None)) if org is not None else None
    base = {"org": org_name} if org_name else {}

    # ---- JSON-LD
    jobs = 0
    for node in jsonld_nodes(html, tree):
        ts = node_types(node)
        if "jobposting" in ts and jobs < 10:
            title = clean(str(node.get("title") or node.get("name") or ""))
            if not title:
                continue
            posted = parse_date(str(node.get("datePosted") or ""), now=now)
            valid = parse_date(str(node.get("validThrough") or ""), now=now)
            if valid is not None and valid < now:
                continue  # expired posting
            jobs += 1
            out.append(DetectedSignal("hiring", f"Hiring: {title}", 0.9 if posted else 0.8, page_url, posted,
                                      _detail("jsonld_jobposting", title, **base)))
        elif any(t.endswith("event") for t in ts):
            title = clean(str(node.get("name") or ""))
            start = parse_date(str(node.get("startDate") or ""), now=now)
            if title:
                out.append(DetectedSignal("event", f"Event: {title}", 0.9 if start else 0.6, page_url, start,
                                          _detail("jsonld_event", title, **base)))
        elif any(t in ("newsarticle", "article", "blogposting", "pressrelease", "report") for t in ts):
            head = clean(str(node.get("headline") or node.get("name") or ""))
            body = clean(str(node.get("description") or ""))
            st = classify_keywords(f"{head}. {body}")
            if head and st:
                when = parse_date(str(node.get("datePublished") or node.get("dateCreated") or ""), now=now)
                out.append(DetectedSignal(st, head[:400], 0.8 if when else 0.4, page_url, when,
                                          _detail("jsonld_article", f"{head}. {body}", **base)))
        for cred in ([node.get("hasCredential")] if node.get("hasCredential") else []) + (
                [node.get("certification")] if node.get("certification") else []):
            for c in cred if isinstance(cred, list) else [cred]:
                nm = clean(str(c.get("name") or c.get("credentialCategory") or "")) if isinstance(c, dict) else clean(str(c))
                if nm:
                    out.append(DetectedSignal("accreditation", f"Accreditation: {nm}", 0.8, page_url, None,
                                              _detail("jsonld_credential", nm, accreditation=nm, **base)))

    # ---- careers page without structured data
    if _JOBS_URL.search(path + "/") and jobs == 0:
        body = visible_text(html)
        hits = len(_JOBLIKE.findall(body))
        if hits >= 2:
            out.append(DetectedSignal("hiring", "Careers page lists open vacancies", 0.55, page_url, None,
                                      _detail("careers_page", body[:300], job_cues=hits, **base)))

    # ---- events page (heading + date, no structured data)
    # (only structured events are trusted; an events URL alone is not evidence of a specific event)

    # ---- news / press items
    if _NEWS_URL.search(path + "/") or tree.css("article"):
        items = tree.css("article") or [n for n in tree.css("li") if n.css_first("time")]
        seen: set[str] = set()
        for it in items[:80]:
            head_n = it.css_first("h1, h2, h3, h4") or it.css_first("a")
            head = text_of(head_n, 240) if head_n is not None else ""
            body = text_of(it, 500)
            if not head or head in seen:
                continue
            seen.add(head)
            st = classify_keywords(f"{head}. {body}")
            if not st:
                continue
            t_n = it.css_first("time")
            when, conf = None, 0.35
            if t_n is not None and (when := parse_date(t_n.attributes.get("datetime") or text_of(t_n), now=now)) is not None:
                conf = 0.7
            elif (when := parse_date(body, now=now)) is not None:
                conf = 0.6
            out.append(DetectedSignal(st, head[:400], conf, page_url, when,
                                      _detail("news_item_time" if conf == 0.7 else ("news_item_text_date" if conf == 0.6 else "news_item_keyword"),
                                              body, **base)))

    # ---- accreditation mentions anywhere (keyword only unless on an accreditation-style page)
    page_text = visible_text(html)
    on_accred_page = bool(re.search(r"accredit|certif|about|why-us|quality|safety", path, re.I))
    for name in accreditations_in(page_text):
        if any(s.signal_type == "accreditation" and name in s.title for s in out):
            continue
        out.append(DetectedSignal("accreditation", f"Accreditation: {name}", 0.55 if on_accred_page else 0.45, page_url, None,
                                  _detail("accreditation_keyword", name, accreditation=name, **base)))

    # ---- filters
    res = []
    seen_keys: set[tuple[str, str]] = set()
    for s in out:
        if types and s.signal_type not in types:
            continue
        if not is_fresh(s.occurred_at, freshness_days, now):
            continue
        k = (s.signal_type, clean(s.title).lower())
        if k in seen_keys:
            continue
        seen_keys.add(k)
        res.append(s)
        if len(res) >= max_per_page:
            break
    return res


# --------------------------------------------------------------------------- persistence
def dedupe_hash(org_id: int, signal_type: str, title: str, occurred_at: datetime | None) -> str:
    """Stable hash of the SAME real-world signal: type + normalised title + calendar day (not URL, not observation time)."""
    norm = re.sub(r"[^a-z0-9]+", " ", title.casefold()).strip()
    day = occurred_at.astimezone(UTC).date().isoformat() if occurred_at else ""
    return hashlib.sha256(f"{org_id}|{signal_type}|{norm}|{day}".encode()).hexdigest()


async def record_signal(
    session: AsyncSession, org_id: int, type: str, title: str, source_adapter: str, source_url: str,
    occurred_at: datetime | None = None, confidence: float = 0.5, detail: dict[str, Any] | None = None,
) -> tuple[int, bool]:
    """Idempotently store a signal; returns ``(signal_id, created)``. ``source_url`` is mandatory (ValueError otherwise)."""
    if type not in SIGNAL_TYPES:
        raise ValueError(f"unknown signal type {type!r}")
    url = (source_url or "").strip()
    sp = urlsplit(url) if url else None
    if not url or sp is None or sp.scheme not in ("http", "https") or not sp.netloc:
        raise ValueError("source_url is mandatory (an http(s) URL): no source URL, no signal")
    title = clean(title)[:400]
    if not title:
        raise ValueError("title is required")
    conf = max(0.0, min(1.0, float(confidence)))
    det = dict(detail or {})
    det.setdefault("parser_version", PARSER_VERSION)
    stmt = pg_insert(BusinessSignal).values(
        organisation_id=org_id, signal_type=type, title=title, detail=det, source_adapter=source_adapter[:80], source_url=url,
        occurred_at=occurred_at, confidence=conf, dedupe_hash=dedupe_hash(org_id, type, title, occurred_at))
    stmt = stmt.on_conflict_do_update(
        constraint="uq_signal_dedupe",
        set_={"confidence": func.greatest(BusinessSignal.confidence, stmt.excluded.confidence), "observed_at": func.now(),
              "detail": BusinessSignal.detail.op("||")(stmt.excluded.detail)},
    ).returning(BusinessSignal.id, literal_column("(xmax = 0)").label("inserted"))
    row = (await session.execute(stmt)).one()
    return int(row.id), bool(row.inserted)


async def detect_and_record(
    session: AsyncSession, org: Organisation, page_url: str, html: str, *, source_adapter: str = "website_signals",
    freshness_days: int | None = None, types: list[str] | None = None, now: datetime | None = None,
) -> list[tuple[int, bool]]:
    found = detect_signals_from_html(html, page_url, org, now=now, freshness_days=freshness_days, types=types)
    return [await record_signal(session, org.id, s.signal_type, s.title, source_adapter, s.source_url, s.occurred_at, s.confidence, s.detail)
            for s in found]


# --------------------------------------------------------------------------- query
def _enc(ts: datetime, i: int) -> str:
    return base64.urlsafe_b64encode(f"{ts.isoformat()}|{i}".encode()).decode()


def _dec(cur: str) -> tuple[datetime, int]:
    ts, _, i = base64.urlsafe_b64decode(cur.encode()).decode().rpartition("|")
    return datetime.fromisoformat(ts), int(i)


async def query_signals(session: AsyncSession, filters: dict[str, Any] | None = None, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
    """Keyset-paginated (newest first) signals for the Opportunities UI.

    filters: org_id, types[], min_confidence, since, freshness_days, country_code, run_id, source_adapter, q
    """
    f = filters or {}
    limit = max(1, min(int(limit), 200))
    sort_ts = func.coalesce(BusinessSignal.occurred_at, BusinessSignal.observed_at)
    q = (select(BusinessSignal, Organisation.legal_name, Organisation.trading_name, Organisation.country_code, sort_ts.label("sort_ts"))
         .join(Organisation, Organisation.id == BusinessSignal.organisation_id).where(Organisation.merged_into_id.is_(None)))
    if f.get("org_id"):
        q = q.where(BusinessSignal.organisation_id == f["org_id"])
    if f.get("types"):
        q = q.where(BusinessSignal.signal_type.in_(f["types"]))
    if f.get("min_confidence") is not None:
        q = q.where(BusinessSignal.confidence >= f["min_confidence"])
    if f.get("since"):
        q = q.where(sort_ts >= f["since"])
    if f.get("freshness_days"):
        q = q.where(sort_ts >= datetime.now(UTC) - timedelta(days=int(f["freshness_days"])))
    if f.get("country_code"):
        q = q.where(Organisation.country_code == f["country_code"].upper())
    if f.get("source_adapter"):
        q = q.where(BusinessSignal.source_adapter == f["source_adapter"])
    if f.get("run_id"):
        q = q.where(BusinessSignal.organisation_id.in_(select(RunOrganisation.organisation_id).where(RunOrganisation.run_id == f["run_id"])))
    if f.get("q"):
        q = q.where(BusinessSignal.title.ilike("%" + re.sub(r"[%_]", "", str(f["q"])) + "%"))
    if cursor:
        ts, i = _dec(cursor)
        q = q.where(or_(sort_ts < ts, and_(sort_ts == ts, BusinessSignal.id < i)))
    rows = (await session.execute(q.order_by(sort_ts.desc(), BusinessSignal.id.desc()).limit(limit + 1))).all()
    more = len(rows) > limit
    rows = rows[:limit]
    items = [{
        "id": s.id, "organisation_id": s.organisation_id, "organisation": legal or trading, "country_code": cc,
        "signal_type": s.signal_type, "title": s.title, "detail": s.detail, "source_adapter": s.source_adapter,
        "source_url": s.source_url, "occurred_at": s.occurred_at, "observed_at": s.observed_at, "confidence": s.confidence,
    } for s, legal, trading, cc, _ts in rows]
    nxt = _enc(rows[-1][4], rows[-1][0].id) if more and rows else None
    return {"items": items, "next_cursor": nxt}


# --------------------------------------------------------------------------- monitor
async def run_monitor(session: AsyncSession, fetcher: Any, monitor: Any, *, limiter: Any = None, robots: Any = None,
                      max_pages_per_org: int = 4) -> dict[str, Any]:
    """MonitorSchedule kind='signals': scan up to ``config.limit`` confirmed-domain organisations (round-robin by id) for
    news / careers / events pages and record the signals found. Bounded, robots- and rate-limit-aware."""
    from ..crawler.extract import extract_from_html
    from ..crawler.ratelimit import RateLimiter
    from ..crawler.robots import RobotsCache
    from ..models import OrganisationDomain

    cfg = dict(getattr(monitor, "config", None) or {})
    limit = int(cfg.get("limit", 20))
    fresh = int(cfg.get("freshness_days", 180))
    types = cfg.get("types") or None
    last_id = int((getattr(monitor, "last_result", None) or {}).get("last_org_id", 0))
    limiter = limiter or RateLimiter(min_interval=1.0)
    robots = robots or RobotsCache(fetcher)
    rows = (await session.execute(
        select(Organisation, OrganisationDomain).join(OrganisationDomain, OrganisationDomain.organisation_id == Organisation.id)
        .where(Organisation.merged_into_id.is_(None), OrganisationDomain.verified_match_status.in_(("confirmed", "high")),
               OrganisationDomain.parked.is_(False), Organisation.id > last_id)
        .order_by(Organisation.id).limit(limit))).all()
    seen_orgs: set[int] = set()
    stats = {"orgs": 0, "pages": 0, "signals_new": 0, "signals_seen": 0, "last_org_id": last_id}
    for org, dom in rows:
        if org.id in seen_orgs:
            continue
        seen_orgs.add(org.id)
        base = dom.website_url or f"https://{dom.domain}/"
        pages = [base]
        stats["orgs"] += 1
        i = 0
        while i < len(pages) and i < max_pages_per_org:
            url = pages[i]
            i += 1
            if not (await robots.check(url)).allowed:
                continue
            host = urlsplit(url).netloc
            async with limiter.acquire(host) as slot:
                res = await fetcher.fetch(url)
                slot.report(res.status, res.retry_after() if res.status in (429, 503) else None)
            stats["pages"] += 1
            if not res.usable_body:
                continue
            html = res.text()
            if i == 1:  # homepage: pick the news / careers / events links
                ex = extract_from_html(html, url)
                for ln in ex.links:
                    p = urlsplit(ln.url)
                    if p.netloc == urlsplit(url).netloc and (_NEWS_URL.search(p.path + "/") or _JOBS_URL.search(p.path + "/")
                                                            or _EVENTS_URL.search(p.path + "/")) and ln.url not in pages:
                        pages.append(ln.url)
            for _id, created in await detect_and_record(session, org, res.final_url or url, html, freshness_days=fresh, types=types):
                stats["signals_new" if created else "signals_seen"] += 1
        stats["last_org_id"] = org.id
    if len(rows) < limit:
        stats["last_org_id"] = 0  # wrapped around
    return stats
