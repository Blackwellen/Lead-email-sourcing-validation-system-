"""Common Crawl access: CDX index API client, offline columnar-index (Parquet) reader, contact-page finder, WARC fetch.

Everything here is *URL discovery and archived-content access for domains you already know*; it never produces
businesses.  Anything extracted from archived pages is HISTORICAL and must be persisted through
:mod:`app.sources.cc_persist` (flagged ``historical`` + ``unconfirmed``, low reliability, needs review).

(a) :class:`CDXClient` -- ``https://index.commoncrawl.org/``: ``collinfo.json`` crawl list, per-domain / prefix queries with
    ``showNumPages`` page iteration, polite pacing (default 1 request/s), exponential back-off on 429/503 honouring
    ``Retry-After``, descriptive User-Agent (the app's configured contact UA).
(b) :class:`ColumnarIndexReader` -- reads the ``cc-index`` table Parquet files from LOCAL paths with ``pyarrow.dataset``:
    column projection + predicate pushdown on registered domain / host / TLD / status / mime, row-batch streaming
    (``Scanner.to_batches`` with read-ahead 1) so a full index is never loaded.  Requires the optional ``pyarrow``
    dependency (``pip install lead-intel[commoncrawl]``).
(c) :func:`fetch_warc_record` -- a single record from ``https://data.commoncrawl.org/<warc>`` with an HTTP ``Range``
    request, gzip-member + WARC + HTTP parsing, size-bounded.

LIVE STATUS: not verified from the build sandbox (index.commoncrawl.org dropped the proxied connection; data.commoncrawl.org
answered a HEAD).  Everything is tested with generated fixtures + ``httpx.MockTransport``.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import re
import zlib
from collections.abc import AsyncIterator, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from ._http import HttpFailure, PoliteClient
from .base import (
    AdapterInfo,
    AdapterUnavailable,
    BusinessRecord,
    Checkpoint,
    DiscoveryQuery,
    RatePolicy,
    SourceAdapter,
    register,
)

INDEX_BASE = "https://index.commoncrawl.org/"
DATA_BASE = "https://data.commoncrawl.org/"
_TS = re.compile(r"^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})$")


@dataclass(frozen=True)
class UrlHit:
    url: str
    domain: str  # registered domain (columnar) or host (CDX)
    crawl_id: str
    fetch_time: datetime | None
    mime: str | None
    status: int | None
    warc_filename: str | None = None
    warc_offset: int | None = None
    warc_length: int | None = None
    digest: str | None = None
    source: str = "cdx"  # cdx | parquet

    @property
    def warc_url(self) -> str | None:
        return DATA_BASE + self.warc_filename if self.warc_filename else None

    @property
    def has_warc(self) -> bool:
        return bool(self.warc_filename and self.warc_offset is not None and self.warc_length)


def parse_cc_timestamp(ts: str | None) -> datetime | None:
    if not ts:
        return None
    m = _TS.match(str(ts).strip())
    if not m:
        return None
    try:
        return datetime(*(int(g) for g in m.groups()), tzinfo=UTC)
    except ValueError:
        return None


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def hit_from_cdx(d: dict[str, Any], crawl_id: str) -> UrlHit | None:
    url = d.get("url")
    if not url:
        return None
    host = (urlsplit(url).hostname or "").lower()
    return UrlHit(
        url=url, domain=host, crawl_id=crawl_id, fetch_time=parse_cc_timestamp(d.get("timestamp")),
        mime=d.get("mime") or d.get("mime-detected"), status=_int(d.get("status")), warc_filename=d.get("filename"),
        warc_offset=_int(d.get("offset")), warc_length=_int(d.get("length")), digest=d.get("digest"), source="cdx",
    )


# --------------------------------------------------------------------------- (a) CDX client
class CDXClient:
    """Polite client for the Common Crawl CDX server."""

    def __init__(self, *, http: PoliteClient | None = None, client: httpx.AsyncClient | None = None, base: str = INDEX_BASE,
                 min_interval_s: float = 1.0, max_attempts: int = 5, max_wait_s: float = 60.0, sleep=asyncio.sleep):
        self.base = base if base.endswith("/") else base + "/"
        self._http = http or PoliteClient(client=client, min_interval_s=min_interval_s, max_attempts=max_attempts,
                                          max_wait_s=max_wait_s, sleep=sleep, retry_statuses=(500, 502, 503, 504))

    async def __aenter__(self) -> CDXClient:
        await self._http.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._http.__aexit__(*exc)

    @property
    def stats(self) -> dict[str, int]:
        return self._http.stats

    async def list_crawls(self) -> list[dict[str, Any]]:
        """``collinfo.json`` (newest first)."""
        data = await self._http.get_json(self.base + "collinfo.json")
        if not isinstance(data, list):
            raise HttpFailure(200, "collinfo.json: unexpected shape")
        return data

    async def latest_crawl_id(self) -> str:
        crawls = await self.list_crawls()
        if not crawls:
            raise HttpFailure(200, "collinfo.json is empty")
        return str(crawls[0]["id"])

    def _params(self, url: str, match_type: str | None, filters: Iterable[str], extra: dict[str, Any]) -> dict[str, Any]:
        p: dict[str, Any] = {"url": url, "output": "json"}
        if match_type:
            p["matchType"] = match_type
        f = list(filters)
        if f:
            p["filter"] = f
        p.update(extra)
        return p

    async def num_pages(self, crawl_id: str, url: str, *, match_type: str | None = "domain", filters: Iterable[str] = ()) -> int:
        resp = await self._http.request("GET", f"{self.base}{crawl_id}-index",
                                        params=self._params(url, match_type, filters, {"showNumPages": "true"}))
        if resp.status_code == 404:
            return 0
        if resp.status_code >= 400:
            raise HttpFailure(resp.status_code, f"CDX HTTP {resp.status_code}")
        try:
            return int(json.loads(resp.text.strip().splitlines()[0]).get("pages", 0))
        except (ValueError, IndexError, AttributeError):
            return 0

    async def query(
        self, url: str, *, crawl_id: str | None = None, match_type: str | None = "domain",
        filters: Iterable[str] = ("=status:200",), limit: int | None = None, start_page: int = 0, max_pages: int | None = None,
    ) -> AsyncIterator[tuple[UrlHit, int]]:
        """Yield ``(hit, page)``; ``start_page`` supports resume.  ``url`` is a domain (``example.com``) or prefix."""
        crawl = crawl_id or await self.latest_crawl_id()
        pages = await self.num_pages(crawl, url, match_type=match_type, filters=filters)
        if max_pages is not None:
            pages = min(pages, start_page + max_pages)
        n = 0
        for page in range(start_page, pages):
            resp = await self._http.request("GET", f"{self.base}{crawl}-index",
                                            params=self._params(url, match_type, filters, {"page": page}))
            if resp.status_code == 404:
                continue  # "No Captures found"
            if resp.status_code >= 400:
                raise HttpFailure(resp.status_code, f"CDX HTTP {resp.status_code}")
            for line in resp.text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                hit = hit_from_cdx(d, crawl) if isinstance(d, dict) else None
                if hit is None:
                    continue
                yield hit, page
                n += 1
                if limit and n >= limit:
                    return


# --------------------------------------------------------------------------- (b) columnar index
_WANTED_COLS = (
    "url_surt", "url", "url_host_name", "url_host_registered_domain", "url_host_tld", "fetch_status", "content_mime_type",
    "content_mime_detected", "warc_filename", "warc_record_offset", "warc_record_length", "warc_offset", "warc_length",
    "crawl", "fetch_time",
)


def _require_pyarrow():
    try:
        import pyarrow  # noqa: F401
        import pyarrow.compute  # noqa: F401
        import pyarrow.dataset as ds
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise AdapterUnavailable("pyarrow is required for the Common Crawl columnar index: pip install 'lead-intel[commoncrawl]'") from exc
    return ds


class ColumnarIndexReader:
    """Stream :class:`UrlHit` from local ``cc-index`` Parquet files / directories.

    Predicates are pushed into the Parquet scan (row-group statistics pruning + page filtering), only the needed
    columns are read, and batches are consumed lazily -- peak memory is O(batch), never O(index).
    """

    def __init__(
        self, paths: str | Path | Iterable[str | Path], *, registered_domains: Iterable[str] | None = None,
        hosts: Iterable[str] | None = None, tlds: Iterable[str] | None = None, status: int | None = 200,
        mime_prefixes: Iterable[str] | None = None, batch_size: int = 8192, crawl_id: str | None = None,
    ):
        self.paths = [str(p) for p in ([paths] if isinstance(paths, str | Path) else list(paths))]
        self.registered_domains = sorted({d.lower().strip().lstrip(".") for d in (registered_domains or []) if d})
        self.hosts = sorted({h.lower().strip() for h in (hosts or []) if h})
        self.tlds = sorted({t.lower().strip().lstrip(".") for t in (tlds or []) if t})
        self.status = status
        self.mime_prefixes = tuple(m.lower() for m in (mime_prefixes or ()))
        self.batch_size = batch_size
        self.crawl_id = crawl_id
        self.stats = {"batches": 0, "rows_scanned": 0, "rows_emitted": 0}

    def _filter(self, ds, schema_names: set[str]):
        import pyarrow.compute as pc

        expr = None

        def andd(a, b):
            return b if a is None else a & b

        field = ds.field
        if self.registered_domains and "url_host_registered_domain" in schema_names:
            expr = andd(expr, field("url_host_registered_domain").isin(self.registered_domains))
        if self.hosts and "url_host_name" in schema_names:
            expr = andd(expr, field("url_host_name").isin(self.hosts))
        if self.tlds and "url_host_tld" in schema_names:
            expr = andd(expr, field("url_host_tld").isin(self.tlds))
        if self.status is not None and "fetch_status" in schema_names:
            expr = andd(expr, field("fetch_status") == self.status)
        _ = pc
        return expr

    def iter_hits(self) -> Iterator[UrlHit]:
        ds = _require_pyarrow()
        for path in self.paths:
            dataset = ds.dataset(path, format="parquet", partitioning="hive" if Path(path).is_dir() else None)
            names = set(dataset.schema.names)
            cols = [c for c in _WANTED_COLS if c in names]
            if "url" not in cols:
                raise AdapterUnavailable(f"{path}: not a cc-index table (no 'url' column)")
            scanner = dataset.scanner(
                columns=cols, filter=self._filter(ds, names), batch_size=self.batch_size, batch_readahead=1, fragment_readahead=1,
            )
            for batch in scanner.to_batches():
                self.stats["batches"] += 1
                self.stats["rows_scanned"] += batch.num_rows
                data = batch.to_pydict()
                for i in range(batch.num_rows):
                    row = {c: data[c][i] for c in cols}
                    mime = row.get("content_mime_detected") or row.get("content_mime_type")
                    if self.mime_prefixes and not (mime or "").lower().startswith(self.mime_prefixes):
                        continue
                    ft = row.get("fetch_time")
                    if isinstance(ft, datetime) and ft.tzinfo is None:
                        ft = ft.replace(tzinfo=UTC)
                    host = row.get("url_host_registered_domain") or row.get("url_host_name") or (urlsplit(row["url"]).hostname or "")
                    self.stats["rows_emitted"] += 1
                    yield UrlHit(
                        url=row["url"], domain=str(host).lower(), crawl_id=str(row.get("crawl") or self.crawl_id or ""),
                        fetch_time=ft if isinstance(ft, datetime) else None, mime=mime, status=_int(row.get("fetch_status")),
                        warc_filename=row.get("warc_filename"), warc_offset=_int(row.get("warc_record_offset", row.get("warc_offset"))),
                        warc_length=_int(row.get("warc_record_length", row.get("warc_length"))), source="parquet",
                    )


# --------------------------------------------------------------------------- contact page finder
_PAGE_PATTERNS: tuple[tuple[str, re.Pattern[str], int], ...] = (
    ("contact", re.compile(r"(?:^|/)(?:contact(?:[-_]?us)?|contacts|get[-_]in[-_]touch|enquir(?:y|ies)|kontakt|contacto|contatti|contactez[-_]nous|nous[-_]contacter)(?:[-_.][a-z0-9]+)?(?:/|\.html?|\.php|\.aspx)?$", re.I), 100),
    ("legal", re.compile(r"(?:^|/)(?:impressum|imprint|legal[-_]notice|mentions[-_]legales|aviso[-_]legal|company[-_]information|company[-_]details)(?:/|\.html?|\.php)?$", re.I), 85),
    ("team", re.compile(r"(?:^|/)(?:(?:our[-_]|the[-_]|meet[-_](?:the[-_])?)?(?:team|people|staff|leadership|management|directors|partners|board)|who[-_]we[-_]are|our[-_]people)(?:/|\.html?|\.php)?$", re.I), 80),
    ("locations", re.compile(r"(?:^|/)(?:locations?|find[-_]us|offices?|branches|branch[-_]finder|stores?|store[-_]locator|visit[-_]us|where[-_]to[-_]find[-_]us|our[-_]offices)(?:/|\.html?|\.php)?$", re.I), 70),
    ("about", re.compile(r"(?:^|/)(?:about(?:[-_]us)?|company|who[-_]we[-_]are|our[-_]story|ueber[-_]uns|a[-_]propos|quienes[-_]somos)(?:/|\.html?|\.php)?$", re.I), 60),
)
_DOC_EXT = re.compile(r"\.(pdf|docx?|xlsx?|csv|txt|odt|ods)$", re.I)
_DOC_HINT = re.compile(r"(directory|members?|exhibitors?|delegate|attendee|contact|staff|team|brochure|catalogue|catalog|price[-_]?list|annual[-_]report|accounts|handbook|register)", re.I)
_ASSET_EXT = re.compile(r"\.(js|css|png|jpe?g|gif|svg|webp|ico|woff2?|ttf|eot|mp4|mp3|zip|gz|xml|json|rss)$", re.I)
_NOISE = re.compile(r"(?:^|/)(?:wp-json|wp-admin|wp-content|feed|tag|category|author|search|cart|checkout|login|account|cdn-cgi)(?:/|$)", re.I)


@dataclass(frozen=True)
class ContactPageCandidate:
    hit: UrlHit
    kind: str  # contact | legal | team | locations | about | document
    score: int
    reason: str = ""


class CommonCrawlContactPageFinder:
    """Pick likely contact/about/team/location pages and public documents out of URL hits (pure URL pattern matching)."""

    HTML_MIMES = ("text/html", "application/xhtml")
    DOC_MIMES = ("application/pdf", "application/msword", "application/vnd.openxmlformats", "application/vnd.ms-excel", "text/plain", "text/csv")

    def __init__(self, *, kinds: Iterable[str] | None = None, include_documents: bool = True, max_depth: int = 4,
                 allow_status: Iterable[int] = (200,)):
        self.kinds = set(kinds) if kinds else None
        self.include_documents = include_documents
        self.max_depth = max_depth
        self.allow_status = set(allow_status)

    def classify(self, hit: UrlHit) -> ContactPageCandidate | None:
        if hit.status is not None and hit.status not in self.allow_status:
            return None
        sp = urlsplit(hit.url)
        path = sp.path or "/"
        if _NOISE.search(path):
            return None
        mime = (hit.mime or "").lower()
        if _DOC_EXT.search(path) or mime.startswith(self.DOC_MIMES):
            if not self.include_documents or (self.kinds and "document" not in self.kinds):
                return None
            score = 40 + (25 if _DOC_HINT.search(path) else 0)
            return ContactPageCandidate(hit, "document", score, "public document")
        if _ASSET_EXT.search(path):
            return None
        if mime and not mime.startswith(self.HTML_MIMES):
            return None
        if sp.query and len(sp.query) > 60:
            return None
        segs = [s for s in path.split("/") if s]
        if len(segs) > self.max_depth:
            return None
        for kind, pat, base in _PAGE_PATTERNS:
            if self.kinds and kind not in self.kinds:
                continue
            if pat.search(path):
                score = base - 5 * max(0, len(segs) - 1) - (10 if sp.query else 0)
                return ContactPageCandidate(hit, kind, score, f"url matches {kind} pattern")
        return None

    def filter(self, hits: Iterable[UrlHit]) -> Iterator[ContactPageCandidate]:
        for h in hits:
            c = self.classify(h)
            if c is not None:
                yield c

    def best_per_domain(self, hits: Iterable[UrlHit], per_domain: int = 5) -> list[ContactPageCandidate]:
        """De-duplicate by normalised URL (newest capture wins) and keep the top ``per_domain`` per domain; deterministic order."""
        newest: dict[str, ContactPageCandidate] = {}
        for c in self.filter(hits):
            key = re.sub(r"^https?://(www\.)?", "", c.hit.url.lower()).rstrip("/").split("#")[0]
            cur = newest.get(key)
            if cur is None or (c.hit.fetch_time or datetime.min.replace(tzinfo=UTC)) > (cur.hit.fetch_time or datetime.min.replace(tzinfo=UTC)):
                newest[key] = c
        by_dom: dict[str, list[ContactPageCandidate]] = {}
        for c in newest.values():
            by_dom.setdefault(c.hit.domain, []).append(c)
        out: list[ContactPageCandidate] = []
        for dom in sorted(by_dom):
            ranked = sorted(by_dom[dom], key=lambda c: (-c.score, c.hit.url))
            out.extend(ranked[:per_domain])
        return out


# --------------------------------------------------------------------------- (c) WARC record fetch
@dataclass
class ArchivedPage:
    url: str
    status: int | None
    content_type: str | None
    body: bytes
    fetch_time: datetime | None
    warc_headers: dict[str, str] = field(default_factory=dict)
    http_headers: dict[str, str] = field(default_factory=dict)
    truncated: bool = False

    def text(self) -> str:
        from ..crawler.safe_http import decode_body

        return decode_body(self.body, self.content_type)

    @property
    def is_pdf(self) -> bool:
        return (self.content_type or "").lower().startswith("application/pdf") or self.body[:5] == b"%PDF-"


def _parse_header_block(raw: bytes) -> tuple[str, dict[str, str]]:
    lines = raw.decode("utf-8", "replace").split("\r\n")
    first = lines[0] if lines else ""
    headers: dict[str, str] = {}
    for ln in lines[1:]:
        if ":" in ln:
            k, _, v = ln.partition(":")
            headers.setdefault(k.strip().lower(), v.strip())
    return first, headers


def _dechunk(body: bytes) -> bytes:
    out, pos = bytearray(), 0
    while True:
        eol = body.find(b"\r\n", pos)
        if eol < 0:
            return bytes(out) or body
        try:
            size = int(body[pos:eol].split(b";")[0].strip() or b"0", 16)
        except ValueError:
            return bytes(out) or body
        if size == 0:
            return bytes(out)
        out += body[eol + 2: eol + 2 + size]
        pos = eol + 2 + size + 2


def parse_warc_record(gz_member: bytes, *, max_body: int = 5 * 1024 * 1024) -> ArchivedPage:
    """Parse ONE gzip-compressed WARC ``response`` record (as returned by a CC range request)."""
    d = zlib.decompressobj(wbits=31)
    raw = d.decompress(gz_member, max_body + 65536)
    truncated = bool(d.unconsumed_tail)
    if not raw.startswith(b"WARC/"):
        raise ValueError("not a WARC record")
    head_end = raw.find(b"\r\n\r\n")
    if head_end < 0:
        raise ValueError("truncated WARC header")
    _, warc_h = _parse_header_block(raw[:head_end])
    payload = raw[head_end + 4:]
    rtype = warc_h.get("warc-type", "")
    url = warc_h.get("warc-target-uri", "")
    ft = None
    if warc_h.get("warc-date"):
        try:
            ft = datetime.fromisoformat(warc_h["warc-date"].replace("Z", "+00:00"))
        except ValueError:
            ft = None
    if rtype != "response":
        return ArchivedPage(url, None, None, payload[:max_body], ft, warc_h, {}, truncated)
    h_end = payload.find(b"\r\n\r\n")
    if h_end < 0:
        return ArchivedPage(url, None, None, b"", ft, warc_h, {}, truncated)
    status_line, hh = _parse_header_block(payload[:h_end])
    m = re.match(r"HTTP/\S+\s+(\d{3})", status_line)
    body = payload[h_end + 4:]
    if "chunked" in hh.get("transfer-encoding", "").lower():
        body = _dechunk(body)
    enc = hh.get("content-encoding", "").lower()
    if enc in ("gzip", "x-gzip"):
        try:
            body = gzip.decompress(body)
        except (OSError, EOFError, zlib.error):
            pass
    return ArchivedPage(url, int(m.group(1)) if m else None, hh.get("content-type"), body[:max_body], ft, warc_h, hh, truncated)


async def fetch_warc_record(
    hit: UrlHit, *, client: httpx.AsyncClient | None = None, http: PoliteClient | None = None, max_body: int = 5 * 1024 * 1024,
    max_range_bytes: int = 2 * 1024 * 1024,
) -> ArchivedPage:
    """Range-fetch and parse the single WARC record of ``hit`` from data.commoncrawl.org (HTTPS only)."""
    if not hit.has_warc:
        raise ValueError("hit has no WARC coordinates")
    assert hit.warc_offset is not None and hit.warc_length is not None
    if hit.warc_length > max_range_bytes:
        raise ValueError(f"record too large ({hit.warc_length} bytes > {max_range_bytes})")
    rng = f"bytes={hit.warc_offset}-{hit.warc_offset + hit.warc_length - 1}"
    own = http is None
    cli = http or PoliteClient(client=client, min_interval_s=1.0)
    if own:
        await cli.__aenter__()
    try:
        resp = await cli.request("GET", hit.warc_url or "", headers={"Range": rng, "Accept-Encoding": "identity"})
    finally:
        if own:
            await cli.__aexit__(None, None, None)
    if resp.status_code not in (200, 206):
        raise HttpFailure(resp.status_code, f"WARC range fetch HTTP {resp.status_code}")
    content = resp.content
    if resp.status_code == 200 and len(content) > hit.warc_length:  # server ignored Range -> slice it ourselves
        content = content[hit.warc_offset: hit.warc_offset + hit.warc_length]
    page = parse_warc_record(content, max_body=max_body)
    if not page.url:
        page = ArchivedPage(hit.url, page.status, page.content_type, page.body, page.fetch_time or hit.fetch_time, page.warc_headers, page.http_headers, page.truncated)
    return page


def extract_archived_observations(page: ArchivedPage, *, company_hint: str | None = None):
    """Run the standard extractor over an archived page (HTML or PDF); returns a ``PageExtraction``."""
    from ..crawler.extract import extract_from_html, extract_from_pdf_bytes

    if page.is_pdf:
        return extract_from_pdf_bytes(page.body, page.url)
    return extract_from_html(page.text(), page.url, company_hint=company_hint)


# --------------------------------------------------------------------------- registry entry (info only)
@register
class CommonCrawlArchiveInfo(SourceAdapter):
    info = AdapterInfo(
        adapter_id="commoncrawl_archive",
        name="Common Crawl archive (CDX / columnar index / WARC) -- historical evidence",
        country_coverage=("*",),
        source_type="web_archive",
        licence="Common Crawl Terms of Use; archived page content remains the property of its owners",
        source_url="https://commoncrawl.org/terms-of-use",
        access_requirements="CDX API (public, polite use) or locally downloaded cc-index Parquet files + optional pyarrow; WARC range fetches from data.commoncrawl.org.",
        automated_collection_permitted=True,
        rate_policy=RatePolicy(requests_per_minute=60, concurrency=1),
        fields_available=("url", "fetch_time", "warc_coordinates"),
        info_only=True,
        requires_config=True,
        cost="free", credentials="none", refresh_frequency="monthly-ish (new crawl roughly every 1-2 months)",
        access_method="api", industries=("*",),
        allowed_usage="Index metadata is open; use archived contacts only as HISTORICAL, UNCONFIRMED evidence until re-observed on the live site.",
        notes="Library-style source (UrlHit / ArchivedPage), not a business-producing adapter. Live access UNVERIFIED from the build sandbox.",
    )

    async def iter_businesses(
        self, query: DiscoveryQuery, checkpoint: Checkpoint | None = None
    ) -> AsyncIterator[tuple[BusinessRecord, Checkpoint]]:
        raise AdapterUnavailable("Common Crawl yields URL hits / archived pages for known domains, not businesses; use CDXClient / ColumnarIndexReader")
        yield  # pragma: no cover


__all__ = [
    "UrlHit", "CDXClient", "ColumnarIndexReader", "CommonCrawlContactPageFinder", "ContactPageCandidate", "ArchivedPage",
    "fetch_warc_record", "parse_warc_record", "extract_archived_observations", "parse_cc_timestamp", "hit_from_cdx",
]
